"""LLM pipeline: per-source fact extraction (map) → dossier (reduce) → card fields + lorebook entries."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMClient
from .research import Source
from .util import chunk_text, debug, log

CATEGORIES = [
    "identity", "appearance", "personality", "inner_life", "speech", "belief", "skill", "preference",
    "intimacy", "content_theme", "backstory", "event", "work", "relationship", "place", "organization", "quote", "other",
]

PRIVACY_RULES = """Privacy rules (always apply):
- Never record home addresses, phone numbers, email addresses, license plates, ID or account numbers.
- Never record medical, financial or legal details about private (non-public) relatives, friends or minors.
  For private relatives, at most record their first name and relation, and only if widely published.
- Ignore rumours, gossip sites and unverified allegations unless covered by reputable sources; mark them low confidence."""

TALENT_PRIVACY_RULES = """Creator-safety rules (this card portrays an online creator's PUBLIC PERSONA; fans will chat with it):
- Use only their stage/creator name. Never record a legal or birth name unless it appears in a CREATOR BRIEF source.
- Never record their city, neighbourhood, address, gym, workplace, school, vehicle, travel plans or daily schedule,
  nor names/identities of partners, family or friends who are not public creators themselves. Country or a broad
  region is fine if they share it publicly.
- Never record phone numbers, emails, payment handles or account numbers.
- Record age only as publicly stated. Everyone involved must be an adult (18+); never record or invent anything that
  sexualises minors or describes the creator as underage.
- Ignore leak/repost sites, gossip and speculation. Sources titled CREATOR BRIEF were supplied by the creator's team
  and are authoritative; prefer them over anything else."""


def privacy_rules(opts: "Options") -> str:
    return TALENT_PRIVACY_RULES if opts.talent else PRIVACY_RULES


@dataclass
class Options:
    name: str
    descriptor: str = ""               # e.g. Wikipedia short description
    era: str = ""                      # portray as of this period ("1969", "during WWII", "present day")
    scenario_hint: str = ""            # user-supplied premise for the scenario/first message
    style: str = "roleplay"            # roleplay | chat
    pov: str = "third"                 # third | first (narration person in greetings/examples)
    greetings: int = 3                 # number of alternate greetings
    lore_max_entries: int = 40
    creator: str = "Persona Forge"
    language: str = "English"
    nsfw: bool = False                 # allow adult themes (consenting adults only)
    talent: bool = False               # managed-creator mode: public persona only, creator-safety privacy rules
    boundaries: str = ""               # creator's hard limits / rules, embedded verbatim in the card
    workers: int = 4
    chunk_chars: int = 12_000
    max_facts_chars: int = 45_000      # per reduce call
    dropped_third_party_intimacy: int = 0  # intimacy facts discarded because they came from third-party sites
    dropped_excluded_themes: int = 0       # minor-coded / excluded themes discarded


# Source kinds that count as the creator's own channels (link hub, pages it links to, their own sites,
# pages you chose with --url, creator briefs). Intimacy facts are accepted only from these.
OWN_CHANNEL_KINDS = {"hub", "user", "brief", "platform"}
ADULT_CATEGORIES = {"intimacy", "content_theme"}

# Themes that are never put in a card, even when they appear in posted content: anything minor-coded,
# plus bestiality and incapacitation. Step-family roleplay between adults is allowed and framed as such.
EXCLUDED_THEMES_RE = re.compile(
    r"\b(teen(s|age|ager)?|school ?(girl|boy|uniform)s?|high ?school|barely[- ]legal|jail ?bait|lol(i|ita)|shota|"
    r"under ?age(d)?|minors?|child(ren)?|kids?|little (girl|boy)|young (girl|boy)|pre-?teen|age[- ]?play|"
    r"bestiality|zoo(philia)?|sleeping|unconscious|passed out|drugged)\b",
    re.I,
)


def excluded_theme(text: str) -> bool:
    return bool(EXCLUDED_THEMES_RE.search(text or ""))


@dataclass
class Fact:
    category: str
    subject: str
    fact: str
    when: str = ""
    confidence: str = "medium"
    source: str = ""
    confidence_override: bool = False  # from a creator brief

    def __post_init__(self):
        if self.confidence_override:
            self.confidence = "high"

    def line(self) -> str:
        bits = [f"[{self.category}]"]
        if self.subject:
            bits.append(f"{self.subject}:")
        bits.append(self.fact)
        if self.when:
            bits.append(f"({self.when})")
        if self.confidence == "low":
            bits.append("[low confidence]")
        return " ".join(bits)


@dataclass
class Synthesis:
    facts: list[Fact] = field(default_factory=list)
    dossier: dict = field(default_factory=dict)
    card: dict = field(default_factory=dict)
    lore: list[dict] = field(default_factory=list)


# ════════════════════════════════════════════════════════════ MAP
def _extract_system(opts: Options) -> str:
    who = f"{opts.name}" + (f" ({opts.descriptor})" if opts.descriptor else "")
    return f"""You are a meticulous research assistant compiling notes for an AI roleplay character based on {who}.

Read the source text and extract facts ONLY about {opts.name} — ignore other people who share the name.
Facts about people, places, organizations and events in {opts.name}'s life are relevant (record them from
{opts.name}'s perspective, e.g. subject "Jane Doe", category "relationship", fact "Her doctoral advisor at MIT, 1990-94").

Categories: {", ".join(CATEGORIES)}
- identity: full name, nicknames/aliases, birth/death, nationality, occupation, titles
- appearance: physical look, height, hair, eyes, typical clothing, distinguishing features, voice
- personality: temperament, habits, quirks, strengths, flaws, how others describe them
- inner_life: motivations, insecurities, contradictions, emotional patterns, what they say drives or hurts them,
  how they handle praise/criticism/conflict, attachment and relationship style, persona vs. "real me" as THEY describe it
- speech: tone, vocabulary, accent, verbal tics, catchphrases, how they argue/joke
- belief: values, opinions, philosophy, politics, religion as publicly expressed
- skill / preference: abilities; likes, dislikes, hobbies, favourite things
- backstory / event: life events with dates; work: books, films, inventions, records, companies founded
- relationship: named people and how they relate; place: meaningful locations; organization: groups/teams/employers
- quote: short verbatim quotes (under 25 words) that show their voice
- intimacy: (adult creators only) stated turn-ons, kinks and preferences, flirting style, dynamics they promote,
  fan-interaction style, stated limits and boundaries. Describe, don't transcribe explicit content.
- content_theme: (adult creators only, from their own profile/store/post listings) each recurring theme, niche, tag,
  series, roleplay scenario or act their posted content focuses on. One fact per theme; put the theme in "subject"
  and in "fact" say how prominent it is with evidence, e.g. "12 of ~40 listed clips; also in profile tags".

Keep every fact atomic, concrete and specific (names, dates, numbers). Do not invent anything.
{privacy_rules(opts)}

JSON schema:
{{"relevant": true|false,
  "aliases": ["other names used for {opts.name} in this text"],
  "facts": [{{"category": "...", "subject": "who/what the fact is about (short)", "fact": "...",
             "when": "date or period if known", "confidence": "high|medium|low"}}]}}
If the text is not about {opts.name}, return {{"relevant": false, "aliases": [], "facts": []}}."""


def extract_facts(llm: LLMClient, sources: list[Source], opts: Options) -> tuple[list[Fact], list[str], list[Source]]:
    jobs: list[tuple[Source, str, int, int]] = []
    for src in sources:
        chunks = chunk_text(src.text, opts.chunk_chars)
        for i, ch in enumerate(chunks):
            jobs.append((src, ch, i + 1, len(chunks)))
    log(f"→ Extracting facts from {len(sources)} sources ({len(jobs)} chunks)…")
    system = _extract_system(opts)

    import threading

    lock = threading.Lock()
    done = [0]

    def run(job: tuple[Source, str, int, int]) -> tuple[Source, dict]:
        src, ch, i, n = job
        label = "CREATOR BRIEF (authoritative, supplied by the creator's team)" if src.kind == "brief" else src.title
        user = f"SOURCE: {label}\nURL: {src.url}\nPART {i}/{n}\n\n{ch}"
        try:
            result = src, llm.chat_json(system, user, temperature=0.1, max_tokens=4096)
        except Exception as e:  # noqa: BLE001
            log(f"  ! extraction failed for {src.url} part {i}: {e}")
            result = src, {}
        with lock:
            done[0] += 1
            log(f"  … {done[0]}/{len(jobs)} chunks read")
        return result

    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        results = list(pool.map(run, jobs))

    facts: list[Fact] = []
    aliases: set[str] = set()
    used: dict[str, Source] = {}
    for src, data in results:
        if not isinstance(data, dict) or not data.get("relevant", bool(data.get("facts"))):
            continue
        for a in data.get("aliases") or []:
            if isinstance(a, str) and a.strip() and a.strip().lower() != opts.name.lower():
                aliases.add(a.strip())
        for f in data.get("facts") or []:
            if not isinstance(f, dict) or not str(f.get("fact", "")).strip():
                continue
            cat = str(f.get("category", "other")).lower().strip()
            if cat in ADULT_CATEGORIES:
                if not opts.nsfw:
                    continue
                if src.kind not in OWN_CHANNEL_KINDS:
                    # Sexual preferences come only from the creator's own channels, never gossip/fan sites.
                    opts.dropped_third_party_intimacy += 1
                    continue
                if excluded_theme(f"{f.get('subject', '')} {f.get('fact', '')}"):
                    opts.dropped_excluded_themes += 1
                    continue
            facts.append(
                Fact(
                    category=cat if cat in CATEGORIES else "other",
                    subject=str(f.get("subject", "")).strip(),
                    fact=str(f["fact"]).strip(),
                    when=str(f.get("when", "") or "").strip(),
                    confidence=str(f.get("confidence", "medium")).lower(),
                    source=src.url,
                    confidence_override=src.kind == "brief",
                )
            )
            used[src.url] = src
    facts = _dedupe_facts(facts)
    log(f"  {len(facts)} unique facts from {len(used)} relevant sources")
    return facts, sorted(aliases), list(used.values())


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _dedupe_facts(facts: list[Fact]) -> list[Fact]:
    seen: dict[str, Fact] = {}
    rank = {"high": 2, "medium": 1, "low": 0}
    for f in facts:
        key = f.category + "|" + _norm(f.fact)[:160]
        if key not in seen or rank.get(f.confidence, 1) > rank.get(seen[key].confidence, 1):
            seen[key] = f
    return list(seen.values())


# ════════════════════════════════════════════════════════════ REDUCE → DOSSIER
DOSSIER_GROUPS: dict[str, dict[str, Any]] = {
    "persona": {
        "cats": ["identity", "appearance", "personality", "inner_life", "speech", "belief", "skill", "preference",
                 "intimacy", "content_theme", "quote", "other"],
        "schema": """{
 "full_name": "", "aliases": [], "one_line": "who they are in one sentence",
 "born": "", "died": "", "nationality": "", "occupations": [],
 "appearance": "detailed physical description (build, face, hair, eyes, clothing, voice) — only what sources support",
 "personality_traits": ["trait — short evidence"], "flaws": [], "strengths": [],
 "habits_and_quirks": [],
 "inner_life": {"core_motivations": [], "insecurities_and_fears": [], "contradictions": ["e.g. confident on camera, shy offline"],
                "emotional_patterns": [], "how_they_handle": {"praise": "", "criticism": "", "conflict": "", "boredom": "", "affection": ""},
                "attachment_and_relationship_style": "", "public_persona_vs_private_self": "", "recurring_themes": []},
 "intimacy": {"flirting_style": "", "dynamics_and_themes": [], "fan_interaction_style": "", "stated_limits": [],
              "stated_preferences": ["turn-ons/kinks they state themselves"],
              "content_themes": [{"theme": "", "prominence": "signature|frequent|occasional", "evidence": ""}],
              "persona_kinks": ["kink/preference the persona leans into (from posted content)"]},
 "speech_style": {"tone": "", "vocabulary": "", "accent_or_dialect": "", "verbal_tics": [], "humor": ""},
 "signature_quotes": ["short verbatim quotes from the facts only"],
 "core_values_and_beliefs": [], "skills": [], "likes": [], "dislikes": [],
 "tags": ["8-15 lowercase genre/category tags e.g. 'historical', 'scientist', 'real person'"]
}""",
    },
    "history": {
        "cats": ["identity", "backstory", "event", "work"],
        "schema": """{
 "timeline": [{"period": "year or range", "event": "what happened, specific"}],
 "key_events": [{"name": "short event name", "when": "", "details": "2-4 sentences: what, who, where, consequences, how they felt about it if known", "keywords": ["names/terms people would use to mention it"]}],
 "works_and_achievements": [{"name": "", "when": "", "details": "2-3 sentences", "keywords": []}],
 "controversies": [{"name": "", "when": "", "details": "neutral, sourced summary", "keywords": []}]
}""",
    },
    "world": {
        "cats": ["relationship", "place", "organization"],
        "schema": """{
 "relationships": [{"name": "person's name", "relation": "e.g. wife, rival, mentor", "details": "2-4 sentences on their history together and how {name} feels about them", "aliases": []}],
 "places": [{"name": "", "significance": "2-3 sentences", "aliases": []}],
 "organizations": [{"name": "", "role": "their role/affiliation", "details": "2-3 sentences", "aliases": []}]
}""",
    },
}


def build_dossier(llm: LLMClient, facts: list[Fact], aliases: list[str], opts: Options) -> dict:
    log("→ Conglomerating facts into a dossier…")
    dossier: dict[str, Any] = {"name": opts.name, "descriptor": opts.descriptor}

    def run(group: str) -> dict:
        spec = DOSSIER_GROUPS[group]
        lines = [f.line() for f in facts if f.category in spec["cats"]]
        merged: dict = {}
        # Split very large fact sets into several reduce passes, then merge.
        batches, cur, size = [], [], 0
        for ln in lines:
            if size + len(ln) > opts.max_facts_chars and cur:
                batches.append(cur)
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            batches.append(cur)
        if not batches:
            return {}
        schema = spec["schema"].replace("{name}", opts.name)
        system = (
            f"You consolidate research notes about {opts.name}"
            + (f" ({opts.descriptor})" if opts.descriptor else "")
            + " into a structured dossier for writing a roleplay character. Merge duplicates, resolve conflicts in "
            "favour of high-confidence facts, keep specifics (names, dates, numbers). Never invent facts; leave a "
            "field empty if unknown. "
            + privacy_rules(opts)
            + (" For 'intimacy': 'stated_preferences' holds only what they state themselves. 'content_themes' "
               "summarises the [content_theme] facts, ranked by prominence. 'persona_kinks' extrapolates the kinks and "
               "preferences the persona leans into from SIGNATURE and FREQUENT content themes (and stated preferences), "
               "each ending with '(from posted content)' or '(stated)'. Never infer sexual preferences from "
               "personality or appearance. Step-family themes are adult roleplay: phrase them as 'step-' roleplay "
               "between adults. Omit anything minor-coded." if opts.nsfw
               else " Leave 'intimacy' empty.")
            + " For 'inner_life', synthesise a coherent psychological picture from the evidence (what they say about "
            "themselves, how they behave, how others describe them); mark inferences as such, e.g. '(inferred)'."
            + f"\n\nJSON schema:\n{schema}"
        )
        for i, batch in enumerate(batches):
            prior = f"\n\nDOSSIER SO FAR (extend and refine it, keep everything still valid):\n{json.dumps(merged, ensure_ascii=False)}" if merged else ""
            user = f"Known aliases: {', '.join(aliases) or 'none'}\n\nFACTS ({i + 1}/{len(batches)}):\n" + "\n".join(batch) + prior
            try:
                merged = llm.chat_json(system, user, temperature=0.2, max_tokens=8192) or merged
            except Exception as e:  # noqa: BLE001
                log(f"  ! dossier step '{group}' batch {i + 1} failed: {e}")
        return merged if isinstance(merged, dict) else {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        for part in pool.map(run, list(DOSSIER_GROUPS)):
            dossier.update(part)
    al = set(dossier.get("aliases") or []) | set(aliases)
    dossier["aliases"] = sorted(a for a in al if a and a.lower() != opts.name.lower())
    if isinstance(dossier.get("intimacy"), dict):
        dossier["intimacy"] = _sanitize(dossier["intimacy"], opts)
    return dossier


def _sanitize(node: Any, opts: Options) -> Any:
    """Drop excluded (minor-coded etc.) themes from the dossier's intimacy section."""
    if isinstance(node, dict):
        if excluded_theme(json.dumps(node, ensure_ascii=False)) and "theme" in node:
            opts.dropped_excluded_themes += 1
            return None
        return {k: _sanitize(v, opts) for k, v in node.items()}
    if isinstance(node, list):
        out = []
        for v in node:
            if isinstance(v, str) and excluded_theme(v):
                opts.dropped_excluded_themes += 1
                continue
            v = _sanitize(v, opts)
            if v is not None:
                out.append(v)
        return out
    if isinstance(node, str) and excluded_theme(node):
        opts.dropped_excluded_themes += 1
        return ""
    return node


def flag_excluded(card: dict, lore: list[dict]) -> list[str]:
    """Fields of the finished card/lorebook that still mention an excluded theme, for human review."""
    hits = []
    for k, v in card.items():
        for text in (v if isinstance(v, list) else [v]):
            if isinstance(text, str) and (m := EXCLUDED_THEMES_RE.search(text)):
                hits.append(f"card.{k}: \"…{text[max(0, m.start() - 40): m.end() + 40]}…\"")
    for e in lore:
        if m := EXCLUDED_THEMES_RE.search(e.get("content", "")):
            hits.append(f"lorebook '{e['name']}': \"…{e['content'][max(0, m.start() - 40): m.end() + 40]}…\"")
    return hits


# ════════════════════════════════════════════════════════════ CARD
def _style_rules(opts: Options) -> str:
    if opts.style == "chat":
        return (
            "STYLE: Conversation/texting mode (Marinara 'Conversation' chats). Greetings and example messages are "
            "written as {{char}}'s own chat messages: casual, first person, no narration or asterisk actions, "
            "1-4 short lines, with their real voice, slang and emoji habits if any."
        )
    person = "third person (she/he/they + the name)" if opts.pov == "third" else "first person"
    return (
        "STYLE: Roleplay mode (Marinara 'Roleplay' chats). Greetings and example messages are immersive prose: "
        f"narration in {person}, past or present tense consistently, actions and sensory detail woven in, dialogue in "
        '"double quotes". 2-5 paragraphs for greetings. Never write {{user}}\'s actions, words or feelings.'
    )


def _card_system(opts: Options) -> str:
    flirt_section = '"Flirting & intimacy:", ' if opts.nsfw else ""
    era = opts.era or "the most defining period of their life (state which in the scenario)"
    return f"""You are an expert character-card writer for Marinara Engine, an LLM roleplay frontend that uses the
Character Card V2/V3 format. You turn a research dossier into a card that lets a model portray {opts.name}
convincingly and accurately. Write in {opts.language}.

Macros: use {{{{char}}}} for the character's name and {{{{user}}}} for the user inside description, personality,
scenario, first_mes, alternate_greetings, mes_example and system_prompt. (The 'name' field is plain text.)

Portray {opts.name} as of: {era}. Facts after that point must not appear as knowledge in the card.
{_style_rules(opts)}

Fields to produce (all strings unless noted):
- "summary": one-sentence library blurb (not sent to the model).
- "description": 350-700 words. The core definition, written as clear plain prose with short labelled sections
  ("Overview:", "Personality:", "Inner life:", "Speech:", "Mannerisms:", {flirt_section}"Current situation:").
  Concrete, specific, accurate. "Inner life" should make them psychologically real: what drives them, what they hide,
  their contradictions, how their mood shifts and what triggers it, how they open up over time.
  Do NOT include long physical description or the full biography here – those go in "appearance" and "backstory".
  Do NOT repeat details that belong in the lorebook (individual relationships, places, events) beyond a passing mention.
- "personality": 1-3 sentence personality summary (comma-separated traits plus one line of nuance).
- "appearance": 80-200 words of physical description at the chosen era (Marinara's Appearance field; also used for images).
- "image_appearance": a compact image-model prompt (40-90 words, comma-separated descriptors) of their look, used by
  Marinara's image generator for avatars, sprites, selfies and illustrations: begin with "adult" plus gender and
  approximate adult age, then hair, eyes, skin tone, face, build, height impression, tattoos/piercings/marks,
  signature outfits and accessories, overall vibe. Visual traits only – no scene, pose, nudity or sexual descriptors
  (each image adds its own scene). Never use youthful descriptors.
- "backstory": 200-450 words: concise biography up to the chosen era, chronological.
- "scenario": 2-5 sentences setting up where/when the chat starts and how {{{{user}}}} fits in.
  {f'Base it on this premise from the user: {opts.scenario_hint}' if opts.scenario_hint else 'Pick a plausible, inviting premise grounded in their real life.'}
- "first_mes": the opening message (see STYLE). It must reflect their real voice and situation and give {{{{user}}}} something to respond to.
- "alternate_greetings": array of {opts.greetings} distinct openings in different situations/eras-consistent settings.
- "mes_example": 3-4 example exchanges, each starting with a line "<START>", then lines beginning "{{{{user}}}}:" and
  "{{{{char}}}}:". Showcase their speech patterns, humour, values and quirks. Paraphrase real quotes into natural dialogue.
- "system_prompt": 2-5 sentences of portrayal rules specific to this character (accuracy to the record, era-appropriate
  knowledge, voice). Do not add generic jailbreak text.
- "post_history_instructions": one short reminder line about voice/era, or "".
- "depth_prompt": one short line (under 40 words) reinforcing the most important voice/behaviour rule.
- "creator_notes": notes for the human user: who this is, era portrayed, that the card is generated from public sources
  and is a fictionalised portrayal, and suggested uses.
- "tags": array of 6-15 lowercase tags (include "real person" for real people, "historical" if applicable).
- "name_color": a CSS hex colour that suits them (e.g. "#c0392b").
{_content_rules(opts)}
Never fabricate events, relationships or quotes that contradict the dossier. Where the dossier is silent, keep it vague
rather than inventing specifics.

Output JSON with exactly these keys: summary, description, personality, appearance, image_appearance, backstory, scenario, first_mes,
alternate_greetings, mes_example, system_prompt, post_history_instructions, depth_prompt, creator_notes, tags, name_color."""


def _content_rules(opts: Options) -> str:
    rules = []
    if opts.nsfw:
        rules.append(
            "ADULT CONTENT: this is an 18+ card for adult chat. Flirtation, sexual tension and adult themes are allowed "
            "and should match their real public persona and stated dynamics. {{char}} and {{user}} are both adults; never "
            "reference minors, ages under 18, school settings or childlike framing in any sexual context. Keep the card "
            "itself suggestive rather than graphic; intensity is left to the chat. Respect the boundaries below. "
            "Build {{char}}'s turn-ons, kinks and favourite scenarios from the dossier's 'intimacy' section: "
            "'persona_kinks' and signature/frequent 'content_themes' are what the persona leans into and brings up; "
            "occasional themes are things they're open to. Name nothing beyond that section; if it is empty, write "
            "flirting from their general personality and voice only. Step-family themes are always roleplay between "
            "consenting adults (step-relations, no blood relation, never a minor). Never include minor-coded "
            "themes (teen, school, age-play), bestiality, or sex with someone unconscious or incapacitated."
        )
    else:
        rules.append("Keep all content SFW.")
    if opts.talent:
        rules.append(
            "This is an official AI persona of a real creator, made with their consent. Portray their public persona only, "
            "and follow the creator-safety rules: no legal name, location, schedule or private people. In system_prompt, "
            "include: {{char}} never shares real-world personal details (location, legal name, contacts), never arranges "
            "real-life meetings, and does not deny being an AI persona if {{user}} sincerely asks."
        )
    if opts.boundaries:
        rules.append(
            "CREATOR BOUNDARIES (hard rules – reflect them in system_prompt and never contradict them anywhere):\n"
            + opts.boundaries.strip()
        )
    return "\n".join(rules)


def write_card(llm: LLMClient, dossier: dict, opts: Options) -> dict:
    log("→ Writing character card…")
    user = "DOSSIER:\n" + json.dumps(dossier, ensure_ascii=False, indent=1)
    card = llm.chat_json(_card_system(opts), user, temperature=0.75, max_tokens=8192)
    if not isinstance(card, dict) or not card.get("description"):
        raise RuntimeError("The model did not return a usable card. Try a stronger model or rerun.")
    # Normalise types
    for k in ("alternate_greetings", "tags"):
        v = card.get(k)
        if isinstance(v, str):
            card[k] = [v] if v.strip() else []
        elif not isinstance(v, list):
            card[k] = []
        card[k] = [str(x).strip() for x in card[k] if str(x).strip()]
    for k, v in list(card.items()):
        if isinstance(v, (dict, list)) and k not in ("alternate_greetings", "tags"):
            card[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else "\n".join(map(str, v))
    card["mes_example"] = _fix_mes_example(str(card.get("mes_example", "")))
    return card


def _fix_mes_example(text: str) -> str:
    text = text.strip()
    if text and not text.lstrip().upper().startswith("<START>"):
        text = "<START>\n" + text
    text = re.sub(r"\n*<START>\n*", "\n<START>\n", text).strip()
    return text


# ════════════════════════════════════════════════════════════ LOREBOOK
def _lore_items(dossier: dict) -> list[tuple[str, dict]]:
    """Flatten dossier sections into (kind, item) pairs that deserve their own lorebook entry."""
    items: list[tuple[str, dict]] = []
    for kind, key in (
        ("relationship", "relationships"),
        ("place", "places"),
        ("organization", "organizations"),
        ("event", "key_events"),
        ("work", "works_and_achievements"),
        ("controversy", "controversies"),
    ):
        for it in dossier.get(key) or []:
            if isinstance(it, dict) and it.get("name"):
                items.append((kind, it))
    return items


def _lore_system(opts: Options) -> str:
    return f"""You write lorebook (World Info) entries for Marinara Engine about {opts.name}'s world. A lorebook entry is
injected into the prompt only when one of its keys appears in recent chat, so each entry must stand alone.
Portray things as of: {opts.era or "the era used by the card"}. Write in {opts.language}.

For each input item produce one entry:
- "name": short title (e.g. "Pierre Curie", "Nobel Prize 1903").
- "keys": 3-8 specific trigger words/phrases people would actually type: full name, surname, nicknames, short forms,
  related distinctive terms. NO generic words ("he", "the city", "work", "love"). Lowercase is fine.
- "secondary_keys": [] unless a key is ambiguous; then give context words and the entry becomes selective.
- "content": 50-160 words, factual third person, starting with the subject's name, explaining what it is AND why it
  matters to {{{{char}}}} (their feelings, history together, current status as of the era). Use {{{{char}}}} for {opts.name}.
- "description": one line (under 25 words) summarising the entry for a retrieval router.
- "importance": 1 (core to who they are), 2 (notable), or 3 (minor detail).
Merge duplicates. Skip items with too little information to be useful. Never invent facts. {privacy_rules(opts)}

Output JSON: {{"entries": [{{"name": "", "keys": [], "secondary_keys": [], "content": "", "description": "", "importance": 2}}]}}"""


def write_lorebook(llm: LLMClient, dossier: dict, opts: Options) -> list[dict]:
    items = _lore_items(dossier)
    log(f"→ Writing lorebook ({len(items)} candidate topics)…")
    batches = [items[i : i + 10] for i in range(0, len(items), 10)]
    system = _lore_system(opts)

    def run(batch: list[tuple[str, dict]]) -> list[dict]:
        payload = [{"type": k, **v} for k, v in batch]
        user = "ITEMS:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
        try:
            data = llm.chat_json(system, user, temperature=0.4, max_tokens=8192)
        except Exception as e:  # noqa: BLE001
            log(f"  ! lorebook batch failed: {e}")
            return []
        return [e for e in (data.get("entries") if isinstance(data, dict) else data) or [] if isinstance(e, dict)]

    entries: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        for part in pool.map(run, batches):
            entries.extend(part)

    entries.extend(_biographical_entries(llm, dossier, opts))
    entries = _clean_lore(entries, opts)
    entries.sort(key=lambda e: (int(e.get("importance", 2)), e["name"].lower()))
    if len(entries) > opts.lore_max_entries:
        entries = entries[: opts.lore_max_entries]
    log(f"  {len(entries)} lorebook entries")
    return entries


def _biographical_entries(llm: LLMClient, dossier: dict, opts: Options) -> list[dict]:
    """A few cross-cutting entries: life eras, beliefs, signature sayings."""
    subset = {
        k: dossier.get(k)
        for k in ("full_name", "timeline", "core_values_and_beliefs", "signature_quotes", "likes", "dislikes", "skills",
                  "habits_and_quirks", "inner_life", "intimacy")
        if dossier.get(k)
    }
    if not subset:
        return []
    system = _lore_system(opts) + """

Instead of one entry per item, create 3-8 thematic entries from this material:
- one entry per major life period from the timeline (keys: years, place names, job titles of that period),
- one entry on beliefs/opinions (keys: topics they hold strong views on),
- one entry on hobbies/tastes if there is material (keys: the specific hobbies/things),
- one entry of signature sayings (keys: topics that would prompt them),
- if "inner_life" is present: 2-3 entries on emotional triggers and vulnerabilities, keyed on the topics that touch
  them (e.g. "lonely", "haters", "family", "burnout") so the deeper layer surfaces when relevant,
- if "intimacy" is present (adult card): one entry on their flirting style/dynamics keyed on flirtatious topics,
  suggestive not graphic, and never contradicting the creator's boundaries."""
    try:
        data = llm.chat_json(system, json.dumps(subset, ensure_ascii=False, indent=1), temperature=0.4, max_tokens=6000)
    except Exception as e:  # noqa: BLE001
        log(f"  ! biographical lore failed: {e}")
        return []
    return [e for e in (data.get("entries") if isinstance(data, dict) else []) or [] if isinstance(e, dict)]


_GENERIC_KEYS = {
    "he", "she", "they", "him", "her", "his", "the", "a", "an", "it", "city", "the city", "home", "work", "love",
    "family", "friend", "life", "time", "war", "money", "school", "job", "book", "film", "song", "team",
}


def _clean_lore(entries: list[dict], opts: Options) -> list[dict]:
    out: dict[str, dict] = {}
    char_name_l = opts.name.lower()
    for e in entries:
        name = str(e.get("name", "")).strip()
        content = str(e.get("content", "")).strip()
        if not name or len(content) < 40:
            continue
        keys = []
        for k in e.get("keys") or []:
            k = str(k).strip().strip('"').strip()
            kl = k.lower()
            if len(k) < 2 or kl in _GENERIC_KEYS or kl == char_name_l:
                continue  # the character's own name would fire every message
            if k not in keys:
                keys.append(k)
        if not keys:
            keys = [name]
        sec = [str(k).strip() for k in e.get("secondary_keys") or [] if str(k).strip()]
        try:
            imp = max(1, min(3, int(e.get("importance", 2))))
        except (TypeError, ValueError):
            imp = 2
        rec = {
            "name": name[:120],
            "keys": keys[:10],
            "secondary_keys": sec[:8],
            "content": content,
            "description": str(e.get("description", "")).strip()[:300],
            "importance": imp,
        }
        key = _norm(name)
        if key in out:  # merge duplicates
            prev = out[key]
            prev["keys"] = list(dict.fromkeys(prev["keys"] + rec["keys"]))[:10]
            if len(rec["content"]) > len(prev["content"]):
                prev["content"] = rec["content"]
            prev["importance"] = min(prev["importance"], rec["importance"])
        else:
            out[key] = rec
    return list(out.values())


# ════════════════════════════════════════════════════════════ ORCHESTRATION
def synthesize(llm: LLMClient, sources: list[Source], opts: Options) -> tuple[Synthesis, list[Source]]:
    syn = Synthesis()
    syn.facts, aliases, used = extract_facts(llm, sources, opts)
    if len(syn.facts) < 8:
        raise RuntimeError(
            f"Only {len(syn.facts)} facts found about {opts.name}. Add sources with --url, or check the name/article."
        )
    syn.dossier = build_dossier(llm, syn.facts, aliases, opts)
    debug(f"dossier keys: {sorted(syn.dossier)}")
    syn.card = write_card(llm, syn.dossier, opts)
    syn.lore = write_lorebook(llm, syn.dossier, opts)
    return syn, used
