"""Interactive Onboarding for Marinara Engine (`extensions.onboarding`).

When a fan starts a Roleplay chat with the card, Marinara asks them a few questions and builds their persona from
the answers (docs/characters/interactive-onboarding.md in Marinara). The AI writes a playful, flirty set of
questions in the character's own voice: what to call the fan, how they know each other, the pace and dynamic they
want, and which of the persona's themes they're into. Kink options come only from the persona's own content
themes and kinks (never invented), and excluded themes are always removed.

Everything is validated against Marinara's rules before it goes in the card:
* variable names match ^[A-Za-z_]\\w{0,63}$ and avoid Marinara's built-in macro names;
* every {{variable}} used in the persona fields is a question (or user/char), and every question is used;
* {{#if}} … {{/if}} blocks are balanced;
* at most 100 questions (we keep it to 12).
"""

from __future__ import annotations

import json
import re

from .util import log

VAR_RE = re.compile(r"^[A-Za-z_]\w{0,63}$")
# Marinara's built-in macro names (lower-case); a variable with one of these names would never resolve.
RESERVED = {
    "addnumvar", "addvar", "agent", "appearance", "backstory", "char", "char_about", "characters", "charname",
    "charnamephonetic", "charposthistory", "charsysinfo", "chatid", "commands", "context", "convo_behavior",
    "convo_display", "date", "datetime", "description", "example", "gamestoryboardkeyframecount", "getvar", "group",
    "idle_duration", "include", "incvar", "input", "lastgenerationtype", "lorebook", "lorebooksize", "lowercase",
    "memories", "model", "name", "newline", "noop", "outlet", "persona", "persona_about", "personaappearance",
    "personabackstory", "personadescription", "personality", "personapersonality", "personascenario", "random",
    "reactrules", "reasoning", "replyrules", "roll", "scenario", "setvar", "time", "timezone", "trim", "trimstart",
    "uppercase", "user", "username", "usernamephonetic", "weekday", "original", "isotime", "trimend", "decvar",
    "banned", "prompt", "if", "else", "n", "status", "commandlist", "emojireact", "memoryrecall", "lore", "character",
    "__proto__", "constructor", "prototype",
}
FIELDS = ("description", "personality", "backstory", "appearance", "scenario")
ADULT_LINE = "{{player}} is an adult (18+)."
MAX_QUESTIONS = 12


def _system(name: str, nsfw: bool) -> str:
    adult = (
        "This is an 18+ card. Make it flirty and a little kinky, in {{char}}'s teasing voice, but suggestive rather "
        "than graphic. Kink/theme options may ONLY come from the persona's 'persona_kinks' and 'content_themes' in "
        "the profile – never invent others. Everyone is an adult; never offer anything minor-coded (teen, school, "
        "age-play), non-consensual, incapacitated, or bestiality. Step-family themes are adult step-relation roleplay."
        if nsfw else "Keep it SFW, warm and playful."
    )
    return f"""You design Marinara Engine "Interactive Onboarding" for the character card of {name}: a few questions the
player answers when they start a chat; the answers fill a short persona for the player.

{adult}

Write 6-10 questions, most of them multiple choice, phrased the way {{{{char}}}} would ask (use the profile's voice):
1. "player": what {{{{char}}}} should call them (free text, no options).
2. how they know each other (4-6 situations that fit {name}'s real world, e.g. longtime subscriber, met at a meet-up…).
3. the pace they want (slow-burn tease … straight to it).
4. who leads ({{{{char}}}} takes charge / they take charge / switch).
5. a multi-select of what they're into, built from the persona's themes (4-8 options){' ' if nsfw else ' – keep it to hobbies/romance'}.
6-10. optional extras that fit the persona: their look, a pet name they'd like, a fantasy scenario to start in…
Use {{{{#if var == value}}}}…{{{{/if}}}} only for an optional follow-up.

Then write the player's persona fields using {{{{variable}}}} where answers go (each 1-4 sentences, second person
is NOT used – describe the player in third person by {{{{player}}}}):
- description (must contain "{ADULT_LINE}"), personality, backstory (how they know {{{{char}}}}), appearance, scenario
  (where tonight's chat starts, using the answers).

Variable names: lowercase_with_underscores, never these: name, persona, personality, appearance, scenario,
description, user, char, time, date, random, input, model.

Output JSON:
{{"questions": [{{"var": "player", "question": "", "help": "", "options": [{{"label": "", "value": "", "help": ""}}],
  "allow_custom": false, "multi": false}}],
 "description": "", "personality": "", "backstory": "", "appearance": "", "scenario": ""}}"""


def _themes(dossier: dict) -> list[str]:
    it = dossier.get("intimacy") if isinstance(dossier.get("intimacy"), dict) else {}
    out = []
    for th in it.get("content_themes") or []:
        if isinstance(th, dict) and th.get("theme") and not str(th.get("prominence", "")).startswith("drop"):
            out.append(str(th["theme"]))
    for k in it.get("persona_kinks") or []:
        out.append(re.sub(r"\s*\((from posted content|stated)\)\s*$", "", str(k)))
    return list(dict.fromkeys(t.strip() for t in out if t.strip()))


def generate(llm, dossier: dict, card: dict, name: str, nsfw: bool) -> dict:
    """The onboarding block for extensions.onboarding (AI-written, validated; a template if the AI fails)."""
    profile = {k: dossier.get(k) for k in ("one_line", "personality_traits", "speech_style", "voice_profile", "likes")
               if dossier.get(k)}
    if isinstance(profile.get("voice_profile"), dict):
        profile["voice_profile"] = {k: profile["voice_profile"].get(k) for k in ("summary", "rules", "example_lines")}
    if nsfw:
        it = dossier.get("intimacy") if isinstance(dossier.get("intimacy"), dict) else {}
        profile["intimacy"] = {k: it.get(k) for k in ("flirting_style", "dynamics_and_themes", "fan_interaction_style",
                                                      "content_themes", "persona_kinks", "stated_limits") if it.get(k)}
    profile["card_scenario"] = card.get("scenario", "")
    raw = None
    if llm is not None:
        try:
            raw = llm.chat_json(_system(name, nsfw), "PROFILE:\n" + json.dumps(profile, ensure_ascii=False)[:16000],
                                temperature=0.8, max_tokens=4000)
        except Exception as e:  # noqa: BLE001
            log(f"  ! onboarding: the AI step failed ({e}); using the built-in template")
    ob = validate(raw if isinstance(raw, dict) else template(dossier, name, nsfw), dossier, nsfw)
    if len(ob["variables"]) < 3:  # the AI's answer was unusable
        ob = validate(template(dossier, name, nsfw), dossier, nsfw)
    return ob


def template(dossier: dict, name: str, nsfw: bool) -> dict:
    themes = _themes(dossier)[:8] if nsfw else []
    qs = [
        {"var": "player", "question": "First things first… what should I call you? 😏", "options": []},
        {"var": "how_we_met", "question": "Remind me how we know each other?",
         "options": [{"label": "Longtime subscriber who finally got a DM back"}, {"label": "We met at a fan meet-up"},
                     {"label": "New fan who just found {{char}}"}, {"label": "Friend of a friend"}],
         "allow_custom": True},
        {"var": "pace", "question": "How do you want this to go?",
         "options": [{"label": "Slow burn — make me wait", "value": "slow-burn teasing"},
                     {"label": "Flirty, then see where it goes", "value": "playful flirting that builds"},
                     {"label": "Straight to the point", "value": "direct and bold"}]},
        {"var": "who_leads", "question": "Who's in charge tonight?",
         "options": [{"label": "{{char}} takes the lead", "value": "lets {{char}} take the lead"},
                     {"label": "I do", "value": "likes to take charge"},
                     {"label": "We switch", "value": "likes to switch who leads"}]},
    ]
    if themes:
        qs.append({"var": "into", "question": "Okay, tell me what you're into… pick as many as you like 👀",
                   "options": [{"label": t} for t in themes], "multi": True, "allow_custom": False})
    return {
        "questions": qs,
        "description": ADULT_LINE + " {{player}} knows {{char}} as: {{how_we_met}}." + (" Into: {{into}}." if themes else ""),
        "personality": "Wants {{pace}}; {{who_leads}}.",
        "backstory": "{{how_we_met}}.",
        "appearance": "",
        "scenario": "{{player}} and {{char}} pick things up from here: {{how_we_met}}, and {{player}} wants {{pace}}.",
    }


def _clean_var(v: str, used: set[str]) -> str:
    v = re.sub(r"[^A-Za-z0-9_]", "_", str(v or "").strip()).strip("_").lower()[:60] or "answer"
    if v[0].isdigit():
        v = "q_" + v
    if v in RESERVED and v != "player":
        v = "my_" + v
    base, i = v, 2
    while v in used:
        v = f"{base}_{i}"
        i += 1
    return v


def _balance_ifs(text: str) -> str:
    opens = len(re.findall(r"\{\{#if\b", text))
    closes = len(re.findall(r"\{\{/if\}\}", text))
    if opens == closes and not re.search(r"\{\{#if\s*\}\}", text):
        return text
    return re.sub(r"\{\{#if[^}]*\}\}|\{\{/if\}\}|\{\{else\}\}", "", text)


def validate(raw: dict, dossier: dict, nsfw: bool) -> dict:
    from .synthesize import excluded_theme

    used: set[str] = set()
    rename: dict[str, str] = {}
    variables = []
    for q in (raw.get("questions") or [])[:MAX_QUESTIONS]:
        if not isinstance(q, dict):
            continue
        orig = str(q.get("var") or q.get("variableName") or "").strip()
        var = "player" if orig.lower() == "player" and "player" not in used else _clean_var(orig, used)
        if not VAR_RE.match(var):
            continue
        question = str(q.get("question") or "").strip()
        if not question or excluded_theme(question):
            continue
        opts = []
        for j, o in enumerate(q.get("options") or []):
            if isinstance(o, str):
                o = {"label": o}
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "").strip()
            value = str(o.get("value") or "").strip()
            if not label or excluded_theme(f"{label} {value} {o.get('help', '')}"):
                continue
            opt = {"id": f"{var}_o{j + 1}", "label": label[:120], "value": value[:300]}
            if str(o.get("help") or "").strip():
                opt["description"] = str(o["help"]).strip()[:200]
            opts.append(opt)
        if q.get("options") and not opts:
            continue  # every option was removed: drop the question
        used.add(var)
        if orig and orig != var:
            rename[orig] = var
        variables.append({
            "id": f"ob_{var}", "variableName": var, "question": question[:300],
            "options": opts, "allowCustom": bool(q.get("allow_custom") or q.get("allowCustom")) and bool(opts),
            "multiSelect": bool(q.get("multi") or q.get("multiSelect")) and len(opts) > 1,
            "separator": ", ", "displayMode": "auto", "optionSort": "manual",
        })
    names = {v["variableName"] for v in variables} | {"user", "char"}
    fields = {}
    for f in FIELDS:
        text = str(raw.get(f) or "").strip()
        for old, new in rename.items():
            text = re.sub(r"\{\{(#if\s+)?" + re.escape(old) + r"\b", lambda m: "{{" + (m.group(1) or "") + new, text)
        # sentences in an excluded theme go
        text = " ".join(s for s in re.split(r"(?<=[.!?])\s+", text) if not excluded_theme(s))
        # unknown {{placeholders}} would print raw: drop them
        text = re.sub(r"\{\{(\w+)\}\}", lambda m: m.group(0) if m.group(1) in names or m.group(1).lower() in RESERVED
                      else "", text)
        fields[f] = _balance_ifs(text)[:2000]
    if "18+" not in fields["description"] and "adult" not in fields["description"].lower():
        fields["description"] = (ADULT_LINE + " " + fields["description"]).strip()
    all_text = " ".join(fields.values())
    for v in variables:  # a question nobody uses is noise for the player
        if v["variableName"] != "player" and "{{" + v["variableName"] not in all_text and v["variableName"] not in all_text:
            fields["description"] += f" {v['question'].rstrip('?').strip()}: {{{{{v['variableName']}}}}}."
    return {"enabled": bool(variables), **fields, "variables": variables, "cardVariables": []}


def render_md(ob: dict, name: str) -> str:
    lines = [f"# Onboarding for {name}", "", "Questions a fan answers when starting a Roleplay chat in Marinara. Edit "
             "them in Marinara's Character Editor → Onboarding tab, or ask the editing assistant.", ""]
    for v in ob.get("variables", []):
        lines.append(f"- **{v['question']}** (`{v['variableName']}`" + (", pick several" if v["multiSelect"] else "") + ")")
        for o in v["options"]:
            lines.append(f"  - {o['label']}" + (f" → {o['value']}" if o.get("value") else ""))
    lines += ["", "## Persona the answers fill in", ""]
    for f in FIELDS:
        if ob.get(f):
            lines += [f"**{f.title()}:** {ob[f]}", ""]
    return "\n".join(lines) + "\n"
