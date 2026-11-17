"""Card token budget: keep the raw card, and make a condensed "refined" copy that fits a token budget.

The raw card (`<Name>.png`) is never touched. The refined copy (`<Name>_refined.png`) is the same card with its
permanent fields condensed until they fit the budget:

* "Permanent" = what Marinara sends with every reply: description, personality, scenario, appearance, backstory,
  system prompt, post-history instructions, depth prompt and example dialogue. Greetings are counted separately
  (only one is in the chat, once), and the lorebook has its own budget.
* The creator's hard boundaries are copied word for word and never condensed (they count toward the budget).
* Details that had to be cut are moved into lorebook entries when they still matter, so they come back when the
  chat mentions them instead of being lost.
* The card's `name` stays the same (Marinara's {{char}} is the name); the copy is marked "refined" in its file name,
  library summary, version and tags.

Token counts use tiktoken when it's installed, otherwise a close estimate (about 4 characters per token).
"""

from __future__ import annotations

import copy
import json
import re
import time

from .util import log

BOUNDARY_MARK = "[Hard boundaries set by the creator"
TOP_FIELDS = ["description", "personality", "scenario", "system_prompt", "post_history_instructions", "mes_example"]
EXT_FIELDS = ["appearance", "backstory"]
# share of the (budget minus boundaries) each field gets when condensing
WEIGHTS = {"description": 0.36, "mes_example": 0.20, "appearance": 0.08, "backstory": 0.08, "scenario": 0.07,
           "system_prompt": 0.08, "personality": 0.05, "depth_prompt": 0.04, "post_history_instructions": 0.04}
MIN_BUDGET = 300

_enc = None


def count_tokens(text: str) -> int:
    global _enc
    if not text:
        return 0
    if _enc is None:
        try:
            import tiktoken  # type: ignore

            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 – not installed / no data files
            _enc = False
    if _enc:
        return len(_enc.encode(text))
    words = len(re.findall(r"\S+", text))
    return max(1, round(max(len(text) / 4.0, words * 1.3)))


def split_boundaries(system_prompt: str) -> tuple[str, str]:
    """(model-written part, creator's verbatim boundaries block)."""
    sp = system_prompt or ""
    i = sp.find(BOUNDARY_MARK)
    return (sp, "") if i < 0 else (sp[:i].rstrip(), sp[i:].strip())


def fields_of(data: dict) -> dict[str, str]:
    ext = data.get("extensions") or {}
    sp, _ = split_boundaries(str(data.get("system_prompt", "")))
    out = {k: str(data.get(k) or "") for k in TOP_FIELDS}
    out["system_prompt"] = sp
    for k in EXT_FIELDS:
        out[k] = str(ext.get(k) or "")
    out["depth_prompt"] = str((ext.get("depth_prompt") or {}).get("prompt", "")) if isinstance(ext.get("depth_prompt"), dict) else ""
    return out


def card_tokens(data: dict) -> dict:
    """Per-field token counts plus totals."""
    f = fields_of(data)
    _, bounds = split_boundaries(str(data.get("system_prompt", "")))
    per = {k: count_tokens(v) for k, v in f.items()}
    per["boundaries (never condensed)"] = count_tokens(bounds)
    greetings = [str(data.get("first_mes") or "")] + [str(g) for g in data.get("alternate_greetings") or []]
    entries = ((data.get("character_book") or {}).get("entries") or []) if isinstance(data.get("character_book"), dict) else []
    return {"fields": per, "permanent": sum(per.values()),
            "greeting_longest": max((count_tokens(g) for g in greetings), default=0),
            "greetings_total": sum(count_tokens(g) for g in greetings),
            "lorebook_entries": len(entries),
            "lorebook_total": sum(count_tokens(str(e.get("content", ""))) for e in entries)}


def _targets(data: dict, budget: int) -> dict[str, int]:
    f = fields_of(data)
    bounds = count_tokens(split_boundaries(str(data.get("system_prompt", "")))[1])
    room = max(150, budget - bounds)
    present = {k: w for k, w in WEIGHTS.items() if f.get(k, "").strip()}
    total_w = sum(present.values()) or 1
    cur = {k: count_tokens(f[k]) for k in present}
    # never grow a field: give spare room from short fields to the rest
    t = {k: min(cur[k], int(room * w / total_w)) for k, w in present.items()}
    spare = room - sum(t.values())
    for k in sorted(present, key=lambda k: -present[k]):
        if spare <= 0:
            break
        add = min(spare, cur[k] - t[k])
        t[k] += add
        spare -= add
    return t


def _system(name: str, nsfw: bool) -> str:
    return f"""You condense a Marinara Engine character card for {name} so it fits a token budget, without losing what
makes the character feel alive. Each field has a target length in tokens (about 0.75 words per token).

Keep, in this order of importance:
1. the voice: how they talk (casing, slang, emoji, pet names, rhythm) – example dialogue stays in that voice;
2. distinctive personality, quirks, contradictions and emotional triggers – cut generic adjectives first;
3. {"flirting style, dynamics and the persona's themes/kinks – " if nsfw else ""}concrete specifics over summaries.
Cut repetition between fields, filler, hedging and anything the lorebook can hold. Keep {{{{char}}}}/{{{{user}}}}
macros, the labelled sections in description ("Personality:", "Speech:" …), and the "<START>" separators in
mes_example (fewer, shorter exchanges are fine). Never add new facts. Never weaken safety rules in system_prompt.

Details worth keeping but too long for the card go to "to_lorebook": entries with 3-6 specific trigger keys and
40-120 words of content, so they return when the chat mentions them.

Output JSON with every field you were given (condensed) plus "to_lorebook":
{{"description": "", "personality": "", "scenario": "", "appearance": "", "backstory": "", "system_prompt": "",
  "post_history_instructions": "", "depth_prompt": "", "mes_example": "",
  "to_lorebook": [{{"name": "", "keys": [], "content": ""}}]}}"""


def _apply_fields(data: dict, new: dict) -> None:
    ext = data.setdefault("extensions", {})
    _, bounds = split_boundaries(str(data.get("system_prompt", "")))
    for k in TOP_FIELDS:
        if isinstance(new.get(k), str):
            v = new[k].strip()
            if k == "system_prompt":
                v = split_boundaries(v)[0]  # the model must not touch the boundaries; re-added below
                data[k] = (v + ("\n\n" + bounds if bounds else "")).strip()
            elif k == "mes_example":
                from .synthesize import _fix_mes_example

                data[k] = _fix_mes_example(v) if v else ""
            else:
                data[k] = v
    for k in EXT_FIELDS:
        if isinstance(new.get(k), str):
            ext[k] = new[k].strip()
    if isinstance(new.get("depth_prompt"), str) and isinstance(ext.get("depth_prompt"), dict):
        ext["depth_prompt"]["prompt"] = new["depth_prompt"].strip()


def _hard_trim(data: dict, budget: int) -> None:
    """Last resort when the model can't get under budget: drop trailing example exchanges, then trailing sentences
    of the longest fields (never the boundaries)."""
    for _ in range(60):
        if card_tokens(data)["permanent"] <= budget:
            return
        ex = str(data.get("mes_example") or "")
        blocks = [b for b in re.split(r"\n?<START>\n?", ex) if b.strip()]
        if len(blocks) > 1:
            data["mes_example"] = "\n".join("<START>\n" + b.strip() for b in blocks[:-1])
            continue
        f = fields_of(data)
        k = max((k for k in ("description", "backstory", "appearance", "scenario", "personality") if f.get(k)),
                key=lambda k: count_tokens(f[k]), default=None)
        if not k:
            return
        sents = re.split(r"(?<=[.!?])\s+", f[k])
        if len(sents) <= 1:
            words = f[k].split()
            newv = " ".join(words[: max(5, int(len(words) * 0.85))])
        else:
            newv = " ".join(sents[:-1])
        _apply_fields(data, {k: newv})


def fit_to_budget(llm, card: dict, budget: int, *, nsfw: bool = False, source_name: str = "") -> tuple[dict, dict]:
    """A refined copy of `card` (V2/V3 envelope or bare data) whose permanent fields fit `budget` tokens."""
    budget = max(MIN_BUDGET, int(budget))
    src = card.get("data", card)
    data = copy.deepcopy(src)
    name = str(data.get("name") or "the character")
    before = card_tokens(data)
    moved: list[dict] = []
    passes = 0
    if before["permanent"] > budget and llm is not None:
        log(f"→ Refining the card to fit {budget} tokens (now ~{before['permanent']})…")
        target_budget = budget
        for passes in range(1, 4):
            targets = _targets(data, int(target_budget * 0.95))
            current = fields_of(data)
            payload = {k: current[k] for k in targets}
            user = ("TARGETS (tokens per field):\n" + json.dumps(targets) + "\n\nCARD FIELDS:\n"
                    + json.dumps(payload, ensure_ascii=False, indent=1))
            try:
                new = llm.chat_json(_system(name, nsfw), user, temperature=0.3, max_tokens=8192)
            except Exception as e:  # noqa: BLE001
                log(f"  ! condensing pass {passes} failed ({e})")
                break
            if not isinstance(new, dict):
                break
            for e in new.get("to_lorebook") or []:
                if isinstance(e, dict) and str(e.get("content", "")).strip() and e.get("name"):
                    moved.append({"name": str(e["name"])[:120], "keys": [str(k) for k in e.get("keys") or []][:8]
                                  or [str(e["name"])], "content": str(e["content"]).strip(), "importance": 2,
                                  "description": f"Moved from the card when it was refined to {budget} tokens"})
            prev = card_tokens(data)["permanent"]
            _apply_fields(data, new)
            now = card_tokens(data)["permanent"]
            log(f"  pass {passes}: ~{now} tokens")
            if now <= budget:
                break
            if now >= prev * 0.97:  # the model isn't shortening: stop paying for passes, trim instead
                log("  the model didn't shorten it further – trimming the rest by hand")
                break
            target_budget = int(target_budget * budget / max(now, 1))  # aim lower next time
    if card_tokens(data)["permanent"] > budget:
        _hard_trim(data, budget)
    # moved details → lorebook (embedded character_book)
    from .marinara import _book_entry, build_character_book
    from .synthesize import _clean_lore, Options

    if moved:
        moved = _clean_lore(moved, Options(name=name))
        book = data.get("character_book") or build_character_book(name, [], scan_depth=3, token_budget=2048,
                                                                    description=f"Facts about {name}.")
        have = {re.sub(r"\W+", " ", str(e.get("name") or e.get("comment") or "").lower()).strip() for e in book.get("entries", [])}
        next_id = max([int(e.get("id", 0) or 0) for e in book.get("entries", [])] + [-1]) + 1
        for e in moved:
            if re.sub(r"\W+", " ", e["name"].lower()).strip() in have:
                continue
            book.setdefault("entries", []).append(_book_entry(next_id, e))
            next_id += 1
        data["character_book"] = book
    after = card_tokens(data)
    # mark the copy (the name stays: {{char}} is the name in Marinara)
    data["character_version"] = re.sub(r"(-refined)+$", "", str(data.get("character_version") or "1.0")) + "-refined"
    tags = [t for t in data.get("tags") or [] if t != "refined"] + ["refined"]
    data["tags"] = tags
    summ = re.sub(r"^\[Refined[^\]]*\]\s*", "", str(data.get("summary") or ""))
    data["summary"] = f"[Refined · ~{after['permanent']} tokens] {summ}".strip()
    ext = data.setdefault("extensions", {})
    pf = ext.get("persona_forge") if isinstance(ext.get("persona_forge"), dict) else {}
    pf["refined"] = {"budget": budget, "tokens_before": before["permanent"], "tokens_after": after["permanent"],
                     "from": source_name, "date": time.strftime("%Y-%m-%d"), "moved_to_lorebook": [m["name"] for m in moved]}
    ext["persona_forge"] = pf
    report = {"budget": budget, "before": before, "after": after, "passes": passes, "moved": [m["name"] for m in moved],
              "within": after["permanent"] <= budget}
    return data, report


def render_report(name: str, report: dict, raw_file: str, refined_file: str) -> str:
    b, a = report["before"], report["after"]
    lines = [f"# Refined copy of {name}", "",
             f"Budget: **{report['budget']} tokens** for the permanent card text (sent with every reply). "
             f"Raw card: `{raw_file}` (unchanged) · refined copy: `{refined_file}`.", "",
             f"Result: **~{a['permanent']} tokens** (was ~{b['permanent']})" + ("" if report["within"] else
                                                                               " – still over budget; raise the budget or "
                                                                               "trim by hand"), "",
             "| Field | Raw | Refined |", "| --- | ---: | ---: |"]
    for k in b["fields"]:
        lines.append(f"| {k} | {b['fields'][k]} | {a['fields'].get(k, 0)} |")
    lines += [f"| **Permanent total** | **{b['permanent']}** | **{a['permanent']}** |", "",
              f"Greetings (one is used per chat, once): longest ~{a['greeting_longest']} tokens.",
              f"Lorebook: {a['lorebook_entries']} entries (only the ones a message triggers are sent, within the "
              "lorebook's own budget).", ""]
    if report["moved"]:
        lines += ["## Moved into the lorebook", "", "Cut from the card but kept as lorebook entries:", ""]
        lines += [f"- {m}" for m in report["moved"]]
    return "\n".join(lines) + "\n"
