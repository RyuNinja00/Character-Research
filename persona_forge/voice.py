"""Voice profile: how the person actually writes, measured from their own public messages.

Two layers:
1. Measured habits (no AI): message length, lower-case starts, punctuation, emoji, laughs, pet names, swearing,
   stretched words, signature phrases.
2. An AI-written style guide from those numbers plus a sample of real messages: rules to follow, vocabulary,
   humour, how they reply to people, things to avoid, and example lines.

The card writer treats this as the authority on speech; refine mode uses it to rewrite dialogue.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter

from .llm import LLMClient
from .util import log

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤♥]")
LAUGHS = ["lol", "lmao", "lmfao", "haha", "hahaha", "hehe", "rofl", "lolol", "ahaha", "kek", "💀", "😂", "🤣"]
PET_NAMES = ["babe", "baby", "bb", "hun", "hon", "honey", "love", "darling", "sweetie", "sweetheart", "cutie",
             "handsome", "pretty", "daddy", "sir", "boo", "bestie", "king", "queen", "angel", "sugar"]
SWEARS = ["fuck", "fucking", "shit", "damn", "bitch", "ass", "hell", "crap", "dick", "piss"]
STOP = set("""a an the and or but if so to of in on at for with from by as is are was were be been being it its it's this that
these those i me my we our you your he she they them his her their what which who whom when where why how not no yes do does did
have has had will would can could should just about up out over into than then there here all any some more most very really
im i'm dont don't like get got go going one also too only even""".split())


def voice_stats(messages: list[str]) -> dict:
    msgs = [m.strip() for m in messages if m and m.strip()]
    if not msgs:
        return {}
    words = [re.findall(r"[\w'’]+", m.lower()) for m in msgs]
    n = len(msgs)
    lengths = sorted(len(w) for w in words)
    starts = [m.lstrip("\"'*(“ ") for m in msgs]
    alpha_starts = [s for s in starts if s[:1].isalpha()]
    all_words = [w for ws in words for w in ws]
    emojis = Counter(e for m in msgs for e in EMOJI_RE.findall(m))
    low_text = " ".join(m.lower() for m in msgs)

    def per_msg(rx: str) -> float:
        return round(sum(len(re.findall(rx, m)) for m in msgs) / n, 2)

    laughs = {l: len(re.findall(rf"(?<!\w){re.escape(l)}(?!\w)", low_text)) for l in LAUGHS}
    pets = {p: sum(1 for w in all_words if w == p) for p in PET_NAMES}
    grams: Counter = Counter()
    for ws in words:
        for k in (2, 3):
            for i in range(len(ws) - k + 1):
                g = ws[i:i + k]
                if not all(x in STOP for x in g) and not any(x.isdigit() for x in g):
                    grams[" ".join(g)] += 1
    return {
        "messages_analysed": n,
        "median_words": lengths[n // 2],
        "short_messages_pct": round(100 * sum(1 for l in lengths if l <= 12) / n),
        "long_messages_pct": round(100 * sum(1 for l in lengths if l >= 60) / n),
        "starts_lowercase_pct": round(100 * sum(1 for s in alpha_starts if s[0].islower()) / max(1, len(alpha_starts))),
        "ends_without_punctuation_pct": round(100 * sum(1 for m in msgs if m[-1:].isalnum()) / n),
        "exclamations_per_msg": per_msg(r"!"),
        "questions_per_msg": per_msg(r"\?"),
        "ellipses_per_msg": per_msg(r"\.\.\.|…"),
        "emoji_per_msg": round(sum(emojis.values()) / n, 2),
        "top_emojis": [e for e, _ in emojis.most_common(8)],
        "laughs": {k: v for k, v in sorted(laughs.items(), key=lambda kv: -kv[1]) if v},
        "pet_names": {k: v for k, v in sorted(pets.items(), key=lambda kv: -kv[1]) if v},
        "swears_per_100_words": round(100 * sum(1 for w in all_words if w in SWEARS) / max(1, len(all_words)), 1),
        "all_caps_words_per_msg": per_msg(r"\b[A-Z]{3,}\b"),
        "stretched_words": [w for w, _ in Counter(w for w in all_words if re.search(r"(\w)\1\1", w)).most_common(6)],
        "signature_phrases": [g for g, c in grams.most_common(40) if c >= 3][:15],
    }


def sample_messages(messages: list[str], k: int = 60, seed: int = 7) -> list[str]:
    """A varied sample of real messages: no links, not too long, mixed lengths."""
    pool = [m.strip() for m in messages if m and "http" not in m and 2 <= len(m.split()) <= 70]
    rnd = random.Random(seed)
    rnd.shuffle(pool)
    short = [m for m in pool if len(m.split()) <= 15]
    longer = [m for m in pool if len(m.split()) > 15]
    picked = short[: k * 2 // 3] + longer[: k - min(len(short), k * 2 // 3)]
    return picked[:k]


def build_voice_profile(llm: LLMClient, person: str, messages: list[str], nsfw: bool = False,
                        min_messages: int = 15) -> dict | None:
    if len(messages) < min_messages:
        if messages:
            log(f"  i only {len(messages)} public messages – not enough for a voice profile (needs {min_messages})")
        return None
    log(f"→ Measuring their voice from {len(messages)} public messages…")
    stats = voice_stats(messages)
    samples = sample_messages(messages)
    system = f"""You are a dialogue coach. From measurements and real samples of how {person} writes online, produce a
precise style guide so an AI character sounds exactly like them in chat. Base everything on the evidence; don't
generalise beyond it. Write in English, but keep their own words, slang and spelling as they are.

JSON:
{{"summary": "2-3 sentences: the overall feel of how they write",
  "rules": ["8-14 concrete, checkable rules, e.g. 'mostly lowercase, rarely ends with a full stop', 'uses 😭 for both laughing and being flustered', 'answers questions with a question back'"],
  "vocabulary": ["their characteristic words, slang and phrases"],
  "emoji_and_punctuation": "how and how often",
  "humour": "",
  "warmth_and_attitude": "how they treat people: teasing, sweet, blunt, sarcastic…",
  {'"flirting_and_teasing": "how they flirt in writing, if the samples show it",' if nsfw else ''}
  "replying_to_people": "how they answer compliments, questions, criticism",
  "avoid": ["things that would sound wrong for them, e.g. 'long formal paragraphs', 'perfect grammar'"],
  "example_lines": ["8-12 short lines they could plausibly send in a chat, closely modelled on the samples"]}}"""
    user = ("MEASUREMENTS:\n" + json.dumps(stats, ensure_ascii=False, indent=1)
            + "\n\nREAL MESSAGES (sample):\n" + "\n".join(f"- {m}" for m in samples))
    try:
        profile = llm.chat_json(system, user, temperature=0.3, max_tokens=3000)
    except Exception as e:  # noqa: BLE001
        log(f"  ! voice profile failed: {e}")
        return {"measured": stats, "real_samples": samples[:20]}
    if not isinstance(profile, dict):
        profile = {}
    profile["measured"] = stats
    profile["real_samples"] = samples[:20]
    return profile


def merge_voice_into_dossier(dossier: dict, profile: dict) -> None:
    """Make the measured voice the authority on speech in the dossier."""
    dossier["voice_profile"] = profile
    st = dossier.setdefault("speech_style", {})
    if isinstance(st, dict):
        st.setdefault("tone", profile.get("summary", ""))
        if profile.get("vocabulary"):
            st["vocabulary"] = ", ".join(map(str, profile["vocabulary"][:20]))
        if profile.get("rules"):
            st["verbal_tics"] = list(profile["rules"][:10])
        if profile.get("humour"):
            st["humor"] = profile["humour"]
