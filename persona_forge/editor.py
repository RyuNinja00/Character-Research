"""Editing assistant: a plain-language chat that walks a worker through improving a finished card.

* "Suggest improvements" – the AI reviews the card (voice, personality, flirting, greetings, onboarding, token size,
  flagged passages, unanswered confirm-sheet questions) and explains 4-7 concrete suggestions in plain words, each
  with a one-click "do this" request.
* Any request in plain words ("make her more teasing", "shorter greetings", "add a gym-crush scenario") – the AI
  answers in plain language and proposes changes, shown as before/after for each field.
* Apply saves a NEW version (`<Name>_v1.1.png`, `_v1.2`…); earlier versions stay on disk, so any edit can be undone
  by switching back to an older version. Creator boundaries and the consent line are always kept, and changes that
  bring in an excluded theme are refused.

State lives in talent/<slug>/editor/chat.json.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import time
import uuid
from pathlib import Path

from .budget import BOUNDARY_MARK, card_tokens, split_boundaries
from .util import log

TEXT_FIELDS = ["description", "personality", "scenario", "first_mes", "mes_example", "system_prompt",
               "post_history_instructions", "creator_notes", "summary"]
EXT_TEXT = ["appearance", "backstory", "imageAppearance"]
LABELS = {
    "description": "Description", "personality": "Personality summary", "scenario": "Scenario", "first_mes": "First message",
    "alternate_greetings": "Alternate greetings", "mes_example": "Example chats", "system_prompt": "Rules (system prompt)",
    "post_history_instructions": "Reminder after chat history", "creator_notes": "Creator notes", "summary": "Library blurb",
    "appearance": "Appearance", "backstory": "Backstory", "imageAppearance": "Image description",
    "depth_prompt": "Voice reminder (depth prompt)", "tags": "Tags", "onboarding": "Fan onboarding questions",
}


# ═══════════════════════════════════════════════════════════ card <-> editable view
def editable(data: dict) -> dict:
    ext = data.get("extensions") or {}
    sp, _ = split_boundaries(str(data.get("system_prompt", "")))
    view = {k: str(data.get(k) or "") for k in TEXT_FIELDS}
    view["system_prompt"] = sp
    view["alternate_greetings"] = [str(g) for g in data.get("alternate_greetings") or []]
    view["tags"] = [str(t) for t in data.get("tags") or []]
    for k in EXT_TEXT:
        view[k] = str(ext.get(k) or "")
    dp = ext.get("depth_prompt")
    view["depth_prompt"] = str(dp.get("prompt", "")) if isinstance(dp, dict) else ""
    if isinstance(ext.get("onboarding"), dict):
        view["onboarding"] = onboarding_to_raw(ext["onboarding"])
    return view


def onboarding_to_raw(ob: dict) -> dict:
    return {
        "questions": [{"var": v.get("variableName"), "question": v.get("question"), "multi": v.get("multiSelect"),
                       "allow_custom": v.get("allowCustom"),
                       "options": [{"label": o.get("label"), "value": o.get("value", ""), "help": o.get("description", "")}
                                   for o in v.get("options") or []]} for v in ob.get("variables") or []],
        **{f: ob.get(f, "") for f in ("description", "personality", "backstory", "appearance", "scenario")},
    }


def apply_changes(data: dict, changes: dict, *, nsfw: bool) -> tuple[dict, list[str], list[str]]:
    """New card data with `changes` applied. Returns (data, changed field names, warnings)."""
    from .onboarding import validate as validate_onboarding
    from .synthesize import EXCLUDED_THEMES_RE, _fix_mes_example

    new = copy.deepcopy(data)
    ext = new.setdefault("extensions", {})
    changed, warnings = [], []
    before = editable(data)
    for k, v in changes.items():
        if k not in LABELS:
            continue
        # refuse a change that brings in MORE excluded-theme words than the field already had
        new_hits = EXCLUDED_THEMES_RE.findall(json.dumps(v, ensure_ascii=False))
        old_hits = EXCLUDED_THEMES_RE.findall(json.dumps(before.get(k, ""), ensure_ascii=False))
        if len(new_hits) > len(old_hits):
            m = EXCLUDED_THEMES_RE.search(json.dumps(v, ensure_ascii=False))
            warnings.append(f"{LABELS[k]}: not changed – it would add an excluded theme (“{m.group(0) if m else ''}”).")
            continue
        if k == "alternate_greetings" and isinstance(v, list):
            new[k] = [str(x).strip() for x in v if str(x).strip()]
        elif k == "tags" and isinstance(v, list):
            new[k] = list(dict.fromkeys(str(x).strip().lower() for x in v if str(x).strip()))
            if nsfw and "nsfw" not in new[k]:
                new[k].append("nsfw")
        elif k == "onboarding" and isinstance(v, dict):
            ob = validate_onboarding(v, {}, nsfw)
            if ob["variables"]:
                ext["onboarding"] = ob
            else:
                warnings.append("Onboarding: not changed – the new version had no usable questions.")
                continue
        elif k == "system_prompt" and isinstance(v, str):
            _, bounds = split_boundaries(str(new.get("system_prompt", "")))
            body = split_boundaries(v)[0]
            new[k] = (body.strip() + ("\n\n" + bounds if bounds else "")).strip()
        elif k == "mes_example" and isinstance(v, str):
            new[k] = _fix_mes_example(v)
        elif k == "creator_notes" and isinstance(v, str):
            old = str(new.get("creator_notes") or "")
            auth = old.split("\n\n", 1)[0] if old.startswith("Authorization:") else ""
            v = v.strip()
            new[k] = v if (not auth or v.startswith("Authorization:")) else f"{auth}\n\n{v}"
        elif k in EXT_TEXT and isinstance(v, str):
            ext[k] = v.strip()
            if k == "imageAppearance":
                ext["imageAppearanceEnabled"] = bool(v.strip())
        elif k == "depth_prompt" and isinstance(v, str):
            dp = ext.get("depth_prompt") if isinstance(ext.get("depth_prompt"), dict) else {"depth": 4, "role": "system"}
            dp["prompt"] = v.strip()
            ext["depth_prompt"] = dp
        elif isinstance(v, str):
            new[k] = v.strip()
        else:
            continue
        changed.append(k)
    # safety net: the creator's boundaries never go missing
    if BOUNDARY_MARK in str(data.get("system_prompt", "")) and BOUNDARY_MARK not in str(new.get("system_prompt", "")):
        new["system_prompt"] = str(new.get("system_prompt", "")).rstrip() + "\n\n" + split_boundaries(
            str(data["system_prompt"]))[1]
    return new, changed, warnings


# ═══════════════════════════════════════════════════════════ diffs
def word_diff(a: str, b: str) -> list[list[str]]:
    ta = re.findall(r"\s+|[\w'’]+|[^\w\s]", a or "")
    tb = re.findall(r"\s+|[\w'’]+|[^\w\s]", b or "")
    out: list[list[str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(["=", "".join(ta[i1:i2])])
        else:
            if i2 > i1:
                out.append(["-", "".join(ta[i1:i2])])
            if j2 > j1:
                out.append(["+", "".join(tb[j1:j2])])
    return out


def diffs(old: dict, new: dict, fields: list[str]) -> list[dict]:
    ov, nv = editable(old), editable(new)
    out = []
    for k in fields:
        a, b = ov.get(k, ""), nv.get(k, "")
        if not isinstance(a, str) or not isinstance(b, str):
            a = json.dumps(a, ensure_ascii=False, indent=1) if not isinstance(a, str) else a
            b = json.dumps(b, ensure_ascii=False, indent=1) if not isinstance(b, str) else b
            if k == "alternate_greetings":
                a = "\n\n— ".join(ov.get(k) or [])
                b = "\n\n— ".join(nv.get(k) or [])
            elif k == "onboarding":
                from .onboarding import render_md

                a = render_md((old.get("extensions") or {}).get("onboarding") or {}, "")
                b = render_md((new.get("extensions") or {}).get("onboarding") or {}, "")
        out.append({"field": k, "label": LABELS.get(k, k), "ops": word_diff(a, b)})
    return out


# ═══════════════════════════════════════════════════════════ AI
def _context(data: dict, extra: dict) -> str:
    view = editable(data)
    tok = card_tokens(data)
    _, bounds = split_boundaries(str(data.get("system_prompt", "")))
    ctx = {"card": view, "creator_boundaries (fixed, never change)": bounds,
           "token_size": {"permanent_tokens": tok["permanent"], "per_field": tok["fields"]}, **extra}
    return json.dumps(ctx, ensure_ascii=False)[:60000]


def _rules(name: str, nsfw: bool) -> str:
    from .synthesize import Options, _content_rules

    return _content_rules(Options(name=name, nsfw=nsfw, talent=True))


def _system_suggest(name: str, nsfw: bool) -> str:
    return f"""You are the editing assistant in Persona Forge, helping a (non-technical) team member polish the
Marinara Engine character card of {name}, an official AI persona the creator authorised. Speak in plain, friendly
language – no jargon, no JSON talk. Say what you'd change and why it will make chats feel more alive.

Review the card and give 4-7 concrete suggestions, most valuable first. Look at: does the voice sound like a real
person (rhythm, slang, emoji, pet names) in first_mes and example chats; is the personality specific (quirks,
contradictions, moods) or generic; {"is the flirting/dynamic distinct and true to the persona's themes; " if nsfw else ""}are
greetings varied; is the onboarding fun; is the card bloated (token size); anything in 'flagged' or 'unanswered_questions'.

{_rules(name, nsfw)}

Output JSON: {{"reply": "2-5 sentence plain-language overview", "suggestions": [{{"title": "short title",
"why": "1-2 plain sentences", "instruction": "the exact request to send if the user says yes, written as the user would
say it"}}]}}"""


def _system_edit(name: str, nsfw: bool) -> str:
    fields = ", ".join(LABELS)
    return f"""You are the editing assistant in Persona Forge, helping a (non-technical) team member edit the Marinara
Engine character card of {name}, an official AI persona the creator authorised. They ask in plain words; you reply in
plain, friendly language (what you changed and why, or a question back if the request is unclear) and propose the
changes.

Editable fields: {fields}. Change only what the request needs; return the FULL new text of each field you change
(not a diff). Keep {{{{char}}}}/{{{{user}}}} macros, the labelled sections in description, and "<START>" separators
in mes_example. Keep everything else that's still right. If they ask about size, say roughly how many tokens it
saves. 'onboarding' uses this shape: {{"questions": [{{"var": "", "question": "", "options": [{{"label": "", "value":
""}}], "multi": false, "allow_custom": false}}], "description": "", "personality": "", "backstory": "",
"appearance": "", "scenario": ""}}. The creator boundaries are fixed: never change or weaken them, and never
contradict them. If a request would break them or the rules below, say so kindly and don't make that change.

{_rules(name, nsfw)}

Output JSON: {{"reply": "plain-language answer", "changes": {{"<field>": "<new full value>"}},
"summary": ["one short line per change"]}}   (use "changes": {{}} when you are only answering or asking)"""


def suggest(llm, data: dict, *, nsfw: bool, extra: dict | None = None) -> dict:
    name = str(data.get("name") or "the character")
    out = llm.chat_json(_system_suggest(name, nsfw), "CARD AND CONTEXT:\n" + _context(data, extra or {}),
                        temperature=0.5, max_tokens=3000)
    if not isinstance(out, dict):
        out = {}
    sugg = []
    for s in out.get("suggestions") or []:
        if isinstance(s, dict) and s.get("title") and s.get("instruction"):
            sugg.append({"id": uuid.uuid4().hex[:8], "title": str(s["title"])[:120], "why": str(s.get("why", ""))[:400],
                         "instruction": str(s["instruction"])[:600]})
    return {"reply": str(out.get("reply") or "Here's what I'd look at first:").strip(), "suggestions": sugg[:7]}


def propose(llm, data: dict, request: str, history: list[dict], *, nsfw: bool, extra: dict | None = None) -> dict:
    name = str(data.get("name") or "the character")
    convo = "\n".join(f"{m['role'].upper()}: {m['text'][:800]}" for m in history[-8:] if m.get("text"))
    user = ("CARD AND CONTEXT:\n" + _context(data, extra or {}) + ("\n\nEARLIER IN THIS CHAT:\n" + convo if convo else "")
            + "\n\nREQUEST: " + request)
    out = llm.chat_json(_system_edit(name, nsfw), user, temperature=0.6, max_tokens=8192)
    if not isinstance(out, dict):
        out = {}
    changes = out.get("changes") if isinstance(out.get("changes"), dict) else {}
    view = editable(data)
    changes = {k: v for k, v in changes.items() if k in LABELS and v not in (None, "") and v != view.get(k)}
    new, changed, warnings = apply_changes(data, changes, nsfw=nsfw)
    return {"reply": str(out.get("reply") or "").strip() or ("Here's my proposal." if changed else "Nothing to change."),
            "summary": [str(s) for s in out.get("summary") or []][:12], "changes": {k: changes[k] for k in changed},
            "diffs": diffs(data, new, changed), "warnings": warnings}


# ═══════════════════════════════════════════════════════════ versions on disk
def card_files(out_dir: Path) -> list[Path]:
    """Raw cards in an output folder (no refined copies, no generated avatars), newest first."""
    if not out_dir.exists():
        return []
    files = [p for p in out_dir.glob("*.png") if not p.stem.endswith("_refined") and p.name != "avatar_generated.png"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def bump(version: str) -> str:
    v = re.sub(r"-refined$", "", str(version or "1.0"))
    m = re.match(r"^(\d+)\.(\d+)$", v)
    return f"{m.group(1)}.{int(m.group(2)) + 1}" if m else f"{v}.1"


def save_version(out_dir: Path, data: dict, image: bytes | None, *, note: str, fields: list[str], spec: str = "v2") -> Path:
    from .marinara import save_card_files
    from .util import slugify

    data = copy.deepcopy(data)
    data["character_version"] = bump(data.get("character_version", "1.0"))
    ext = data.setdefault("extensions", {})
    pf = ext.get("persona_forge") if isinstance(ext.get("persona_forge"), dict) else {}
    pf.setdefault("edits", []).append({"date": time.strftime("%Y-%m-%d %H:%M"), "request": note[:300], "fields": fields})
    ext["persona_forge"] = pf
    stem = f"{slugify(str(data.get('name') or 'card'))}_v{data['character_version']}"
    png = save_card_files(out_dir, stem, data, image, spec=spec)
    log(f"✓ Saved {png.name}")
    return png
