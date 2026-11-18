"""Confirm-or-correct sheet: the stand-in for a creator questionnaire.

After research, every guess the app made about the persona becomes a short question the worker (or the creator)
answers with one click: Keep · Drop · Change, or a pick from a few options. Gaps the research couldn't fill become
"best guess" questions with options; nothing from a gap reaches the card until someone picks an answer.

    confirm_sheet.json   the questions + answers (the web UI's "Confirm or correct" panel edits it)
    confirm_sheet.md     the same as a printable checklist to send to the creator

Applying the answers edits dossier.json (a backup is kept as dossier.before_confirm.json), records new hard limits
under dossier["creator_confirmed"]["limits"] (added to the card's boundaries on the next build), and stamps what
was confirmed so the card can say so.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .util import log

CHOICE_PROMINENCE = ["Signature (her main thing)", "Frequent", "Occasional", "Drop it"]


def _get(d: dict, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set(d: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _text(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("theme") or v.get("name") or json.dumps(v, ensure_ascii=False)
    return str(v or "").strip()


class _Sheet:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, section: str, prompt: str, guess: str, *, path: str, kind: str = "keep", options=None,
            evidence: str = "", match: str = "") -> None:
        if not guess and kind == "keep":
            return
        self.items.append({
            "id": f"q{len(self.items) + 1}", "section": section, "prompt": prompt, "guess": guess,
            "evidence": evidence, "type": kind, "options": options or [], "path": path, "match": match or guess,
            "answer": None,
        })


# ═══════════════════════════════════════════════════════════ build
def build_sheet(dossier: dict, *, nsfw: bool = False, catalogs: list | None = None, gaps: list[str] | None = None,
                llm=None, name: str = "") -> dict:
    name = name or dossier.get("name") or "the creator"
    sh = _Sheet()

    # Who they are
    sh.add("Who they are", "One-line summary of the persona", _text(dossier.get("one_line")), path="one_line")
    for t in (dossier.get("personality_traits") or [])[:8]:
        sh.add("Who they are", "Personality trait", _text(t), path="personality_traits")
    for t in (dossier.get("habits_and_quirks") or [])[:4]:
        sh.add("Who they are", "Habit or quirk", _text(t), path="habits_and_quirks")
    for t in (dossier.get("likes") or [])[:5]:
        sh.add("Who they are", "Something they like", _text(t), path="likes")

    # How they talk
    st = dossier.get("speech_style") if isinstance(dossier.get("speech_style"), dict) else {}
    for key, label in (("tone", "Overall tone"), ("vocabulary", "Words and slang they use"), ("humor", "Sense of humour")):
        sh.add("How they talk", label, _text(st.get(key)), path=f"speech_style.{key}")
    for t in (st.get("verbal_tics") or [])[:6]:
        sh.add("How they talk", "Speech habit", _text(t), path="speech_style.verbal_tics")
    for q in (dossier.get("signature_quotes") or [])[:4]:
        sh.add("How they talk", "Something they'd say", _text(q), path="signature_quotes")

    if nsfw:
        it = dossier.get("intimacy") if isinstance(dossier.get("intimacy"), dict) else {}
        sh.add("Flirting & dynamics", "How they flirt", _text(it.get("flirting_style")), path="intimacy.flirting_style")
        sh.add("Flirting & dynamics", "How they treat fans", _text(it.get("fan_interaction_style")),
               path="intimacy.fan_interaction_style")
        for t in (it.get("dynamics_and_themes") or [])[:6]:
            sh.add("Flirting & dynamics", "A dynamic they play up", _text(t), path="intimacy.dynamics_and_themes")
        tag_counts = _tag_counts(catalogs)
        known = set()
        for th in (it.get("content_themes") or [])[:14]:
            if not isinstance(th, dict) or not th.get("theme"):
                continue
            known.add(th["theme"].lower())
            ev = th.get("evidence") or _tag_evidence(th["theme"], tag_counts)
            sh.add("Content themes", f"How big a part of the persona is “{th['theme']}”?",
                   f"{th['theme']} — {th.get('prominence') or 'frequent'}", path="intimacy.content_themes",
                   kind="choice", options=CHOICE_PROMINENCE, evidence=ev, match=th["theme"])
        for k in (it.get("persona_kinks") or [])[:12]:
            sh.add("Kinks the persona leans into", "Keep this kink in the persona?", _text(k),
                   path="intimacy.persona_kinks")
        # frequent clip tags the persona doesn't mention yet
        total = tag_counts.get("__total__", 0)
        for tag, n in sorted(((t, n) for t, n in tag_counts.items() if t != "__total__"), key=lambda x: -x[1])[:25]:
            if n < max(2, total * 0.08) or any(tag in k or k in tag for k in known):
                continue
            sh.add("Content themes", f"“{tag}” is on {n} of {total} clips but isn't in the persona yet. Add it?",
                   tag, path="intimacy.content_themes", kind="choice",
                   options=["Signature (her main thing)", "Frequent", "Occasional", "Leave it out"],
                   evidence=f"{n} of {total} clips", match=f"__new__:{tag}")
        for lim in (it.get("stated_limits") or [])[:8]:
            sh.add("Limits", "A limit the persona keeps", _text(lim), path="intimacy.stated_limits")

    sh.items.append({"id": f"q{len(sh.items) + 1}", "section": "Limits",
                     "prompt": "Anything the character must never do, say or talk about? One per line. These become "
                               "hard rules in the card.", "guess": "", "evidence": "", "type": "text", "options": [],
                     "path": "creator_confirmed.limits", "match": "", "answer": None})

    # Gaps: best guesses, only applied when someone picks one
    for g in _gap_guesses(llm, dossier, gaps or [], nsfw, name):
        sh.items.append({"id": f"q{len(sh.items) + 1}", "section": "Gaps (best guesses)", "prompt": g["question"],
                         "guess": g["options"][0] if g["options"] else "",
                         "evidence": "AI guess – nothing was found" if g["options"] else "nothing was found – write your own",
                         "type": "choice", "options": g["options"] + ["None of these"], "path": g["path"],
                         "match": "__gap__", "answer": None})
    return {"name": name, "created": time.strftime("%Y-%m-%d %H:%M"), "applied": None, "items": sh.items}


def _tag_counts(catalogs) -> dict:
    counts: dict = {"__total__": 0}
    for c in catalogs or []:
        data = c.to_json() if hasattr(c, "to_json") else c
        counts["__total__"] += len(data.get("clips", []))
        for t, n in data.get("tag_counts", []):
            counts[t] = counts.get(t, 0) + n
    return counts


def _tag_evidence(theme: str, counts: dict) -> str:
    total = counts.get("__total__", 0)
    words = set(re.findall(r"[a-z]{4,}", theme.lower()))
    best = max(((t, n) for t, n in counts.items() if t != "__total__" and words & set(re.findall(r"[a-z]{4,}", t))),
               key=lambda x: x[1], default=None)
    return f"tag “{best[0]}” on {best[1]} of {total} clips" if best and total else ""


# dossier path for each coverage label (gapfill.COVERAGE_SPEC / OWN_CHANNEL_ONLY_SPEC)
GAP_PATHS = {
    "Appearance": "appearance", "Personality traits": "personality_traits", "Habits & quirks": "habits_and_quirks",
    "Speech style": "speech_style.tone", "Signature quotes": "signature_quotes",
    "Motivations": "inner_life.core_motivations", "Insecurities & fears": "inner_life.insecurities_and_fears",
    "Contradictions": "inner_life.contradictions", "Emotional patterns": "inner_life.emotional_patterns",
    "Relationship style": "inner_life.attachment_and_relationship_style",
    "Persona vs. private self": "inner_life.public_persona_vs_private_self",
    "Values & beliefs": "core_values_and_beliefs", "Likes": "likes", "Dislikes": "dislikes",
    "Flirting style": "intimacy.flirting_style", "Dynamics & themes": "intimacy.dynamics_and_themes",
    "Fan-interaction style": "intimacy.fan_interaction_style", "Stated limits": "intimacy.stated_limits",
}


def _gap_guesses(llm, dossier: dict, gaps: list[str], nsfw: bool, name: str) -> list[dict]:
    gaps = [g for g in gaps if g in GAP_PATHS and (nsfw or not GAP_PATHS[g].startswith("intimacy"))][:10]
    if not gaps:
        return []
    fallback = [{"question": f"We found nothing on: {g}. Which fits {name} best?", "options": [], "path": GAP_PATHS[g]}
                for g in gaps]
    if llm is None:
        return fallback
    system = (
        f"You help a team finish an AI persona of {name}, a real creator who authorised it. Research left some parts of "
        "the profile empty. For each gap, write ONE short, friendly question and 3 best-guess answers that fit "
        "everything else in the profile (personality, voice, content themes). They are guesses a person will confirm "
        "or reject, so make them distinct and concrete, each under 20 words. For flirting/dynamics gaps, base guesses "
        "only on the profile's own content themes and stated preferences, never on appearance. Everyone is an adult; "
        "never suggest anything minor-coded, non-consensual or involving incapacitation.\n"
        'Output JSON: {"questions": [{"gap": "<gap label>", "question": "", "options": ["", "", ""]}]}'
    )
    summary = {k: dossier.get(k) for k in ("one_line", "personality_traits", "speech_style", "likes", "inner_life",
                                           "intimacy", "voice_profile") if dossier.get(k)}
    if isinstance(summary.get("voice_profile"), dict):
        summary["voice_profile"] = {"summary": summary["voice_profile"].get("summary", "")}
    try:
        data = llm.chat_json(system, "GAPS: " + ", ".join(gaps) + "\n\nPROFILE:\n"
                             + json.dumps(summary, ensure_ascii=False)[:14000], temperature=0.6, max_tokens=3000)
    except Exception as e:  # noqa: BLE001 – the sheet still works without guesses
        log(f"  ! best-guess questions failed ({e}); gaps are listed as open questions")
        return fallback
    from .synthesize import excluded_theme

    out = []
    for q in (data.get("questions") if isinstance(data, dict) else []) or []:
        g = str(q.get("gap", "")).strip()
        if g not in GAP_PATHS or g not in gaps:
            continue
        opts = [str(o).strip() for o in q.get("options") or [] if str(o).strip() and not excluded_theme(str(o))][:4]
        if opts:
            out.append({"question": str(q.get("question") or f"Which fits {name}: {g}?").strip(), "options": opts,
                        "path": GAP_PATHS[g]})
    return out or fallback


# ═══════════════════════════════════════════════════════════ apply
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _list_replace(lst: list, match: str, new: Any | None) -> bool:
    """Replace (or remove, when new is None) the list item whose text matches."""
    m = _norm(match)
    for i, v in enumerate(lst):
        if _norm(_text(v)) == m or (m and _norm(_text(v)).startswith(m[:60])):
            if new is None:
                lst.pop(i)
            elif isinstance(v, dict) and isinstance(new, dict):
                v.update(new)
            else:
                lst[i] = new
            return True
    return False


def apply_sheet(dossier: dict, sheet: dict) -> dict:
    """Apply answered items to the dossier (in place). Returns counts per action."""
    from .synthesize import excluded_theme

    stats = {"kept": 0, "dropped": 0, "changed": 0, "added": 0, "limits": 0, "skipped": 0}
    conf = dossier.setdefault("creator_confirmed", {})
    for it in sheet.get("items", []):
        ans = it.get("answer") or {}
        action = ans.get("action")
        if not action:
            continue
        path, match = it["path"], it.get("match") or it.get("guess", "")
        value = str(ans.get("value") or "").strip()
        if value and excluded_theme(value):
            stats["skipped"] += 1
            continue
        cur = _get(dossier, path)
        if it["type"] == "text":
            lines = [l.strip(" -•\t") for l in value.splitlines() if l.strip(" -•\t")]
            if lines and path == "creator_confirmed.limits":
                have = conf.setdefault("limits", [])
                for l in lines:
                    if l not in have:
                        have.append(l)
                        stats["limits"] += 1
            continue
        if it["type"] == "choice" and path == "intimacy.content_themes":
            themes = _get(dossier, path)
            if not isinstance(themes, list):
                themes = []
                _set(dossier, path, themes)
            pick = value.lower()
            theme = match.split(":", 1)[1] if match.startswith("__new__:") else match
            if pick.startswith(("drop", "leave")):
                if not match.startswith("__new__:"):
                    _list_replace(themes, theme, None)
                    stats["dropped"] += 1
                continue
            prom = "signature" if pick.startswith("signature") else "occasional" if pick.startswith("occasional") else "frequent"
            if match.startswith("__new__:"):
                themes.append({"theme": theme, "prominence": prom, "evidence": it.get("evidence", "") + " (confirmed)"})
                stats["added"] += 1
            elif _list_replace(themes, theme, {"prominence": prom, "confirmed": True}):
                stats["changed"] += 1
            continue
        if it["type"] == "choice" and match == "__gap__":
            if not value or value.lower().startswith("none of these") or value.startswith("(type"):
                continue
            if isinstance(cur, list):
                cur.append(value)
            elif isinstance(cur, dict):
                cur.setdefault("confirmed", value)
            else:
                _set(dossier, path, value)
            stats["added"] += 1
            continue
        # keep / drop / change on a single value or a list item
        if action == "keep":
            stats["kept"] += 1
        elif action == "drop":
            if isinstance(cur, list):
                _list_replace(cur, match, None)
            else:
                _set(dossier, path, "")
            stats["dropped"] += 1
        elif action == "change" and value:
            if isinstance(cur, list):
                if not _list_replace(cur, match, value):
                    cur.append(value)
            else:
                _set(dossier, path, value)
            stats["changed"] += 1
    conf["answered_on"] = time.strftime("%Y-%m-%d")
    conf["answers"] = sum(1 for it in sheet.get("items", []) if (it.get("answer") or {}).get("action"))
    sheet["applied"] = time.strftime("%Y-%m-%d %H:%M")
    return stats


def confirmed_limits(dossier: dict) -> list[str]:
    conf = dossier.get("creator_confirmed") if isinstance(dossier.get("creator_confirmed"), dict) else {}
    return [str(l) for l in conf.get("limits") or [] if str(l).strip()]


def merge_limits(boundaries: str, dossier: dict) -> str:
    """The card's boundaries plus limits confirmed on the sheet (each once)."""
    extra = [l for l in confirmed_limits(dossier) if l.lower() not in (boundaries or "").lower()]
    if not extra:
        return boundaries
    return ((boundaries or "").rstrip() + "\n" if boundaries.strip() else "") + "\n".join(f"- {l}" for l in extra)


# ═══════════════════════════════════════════════════════════ printable
def render_md(sheet: dict) -> str:
    lines = [f"# Confirm or correct: {sheet.get('name', '')}", "",
             "We built this persona from public sources. Please tick **Keep**, **Drop** or write a correction for each "
             "line. Only answered lines change anything. Gap questions are our best guesses; pick one or write your own.",
             ""]
    section = None
    for it in sheet.get("items", []):
        if it["section"] != section:
            section = it["section"]
            lines += ["", f"## {section}", ""]
        ev = f"  _(evidence: {it['evidence']})_" if it.get("evidence") else ""
        if it["type"] == "keep":
            lines.append(f"- [ ] Keep  [ ] Drop  — {it['prompt']}: **{it['guess']}**{ev}  \n  Correction: ________")
        elif it["type"] == "choice":
            lines.append(f"- {it['prompt']}{ev}  \n  " + "  ".join(f"[ ] {o}" for o in it["options"]))
        else:
            lines.append(f"- {it['prompt']}  \n  ______________________________")
    return "\n".join(lines) + "\n"
