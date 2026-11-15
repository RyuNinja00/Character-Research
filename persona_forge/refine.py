"""Refine an existing character card with new material (public feeds, audio, files, pages).

    python -m persona_forge --refine output/Ivy_Wren/Ivy_Wren.png --feed "reddit: u/ivywren" --nsfw --talent ...

Works on cards from this app or from anywhere (Character Card V2/V3, PNG or JSON). It:
1. reads only the NEW sources you give (no Wikipedia, no web search, no deep research);
2. extracts their facts and measures the person's voice from their own posts;
3. asks the AI to update the card: integrate new facts and rewrite the dialogue (greetings, example chats, speech
   section, depth prompt) in the measured voice – keeping everything that's still right;
4. adds new lorebook entries (never removes existing ones);
5. writes a new version next to the original plus refine_report.md listing every change.
Hard boundaries, the consent note, sprites, image settings and other extensions are always kept.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .llm import LLMClient, LLMConfig
from .marinara import (
    _book_entry,
    build_character_book,
    embed_card_in_png,
    placeholder_avatar,
    push_to_marinara,
    read_card_from_png,
    to_v3,
)
from .research import Researcher
from .synthesize import (
    Options,
    _content_rules,
    _fix_mes_example,
    _norm,
    build_dossier,
    extract_facts,
    privacy_rules,
    write_lorebook,
)
from .util import DiskCache, log, slugify

BOUNDARY_MARK = "[Hard boundaries set by the creator"
CARD_FIELDS = ["description", "personality", "scenario", "first_mes", "alternate_greetings", "mes_example",
               "system_prompt", "post_history_instructions"]


def load_card(path: Path) -> tuple[dict, bytes | None]:
    raw = path.read_bytes()
    if raw.startswith(b"\x89PNG"):
        card = read_card_from_png(raw)
        if not card:
            raise SystemExit(f"{path.name} is a PNG without character data in it.")
        from .marinara import PNG_SIG, _chunk, _iter_chunks

        # keep the picture, drop the old card chunks (they're re-added on save)
        image = PNG_SIG + b"".join(_chunk(t, d) for t, d in _iter_chunks(raw)
                                   if not (t == b"tEXt" and d.split(b"\x00", 1)[0] in (b"chara", b"ccv3")))
        return card, image
    try:
        return json.loads(raw.decode("utf-8")), None
    except ValueError as e:
        raise SystemExit(f"{path.name} isn't a character card (PNG or JSON): {e}") from None


def _bump(version: str) -> str:
    m = re.match(r"^(\d+)\.(\d+)$", str(version or "").strip())
    return f"{m.group(1)}.{int(m.group(2)) + 1}" if m else (f"{version}.1" if version else "1.1")


def _refine_system(opts: Options, scope: str) -> str:
    what = {
        "voice": ("Only change HOW they talk: the 'Speech:' part of description, first_mes, alternate_greetings, "
                  "mes_example, depth_prompt and post_history_instructions. Don't add or remove facts."),
        "facts": ("Only integrate the NEW FACTS: update description, personality, backstory and appearance where the "
                  "facts add or correct something. Leave dialogue fields as they are unless a fact contradicts them."),
        "both": ("Integrate the NEW FACTS where they add or correct something AND rewrite the dialogue (first_mes, "
                 "alternate_greetings, mes_example, the 'Speech:' part of description, depth_prompt, "
                 "post_history_instructions) so it follows the VOICE PROFILE."),
    }[scope]
    return f"""You refine an existing character card for Marinara Engine (Character Card V2) about {opts.name}.
{what}

Rules:
- Keep everything in the current card that is still accurate. Keep its format: if its greetings are texting-style
  chat messages, stay in that style; if they are prose with narration, stay in prose. Keep {{{{char}}}}/{{{{user}}}} macros.
- The VOICE PROFILE was measured from the person's real public messages: it is the authority on message length,
  casing, punctuation, emoji, laughs, pet names and vocabulary. Dialogue should sound like its real_samples.
- Never remove or weaken the creator's hard boundaries or the consent/authorisation text.
- Don't invent facts beyond the current card and the new research.
{privacy_rules(opts)}
{_content_rules(opts)}

Return JSON with ONLY the fields you changed (full new text for each, not a diff), plus "changes":
{{"description": "", "personality": "", "scenario": "", "first_mes": "", "alternate_greetings": [],
  "mes_example": "", "system_prompt": "", "post_history_instructions": "", "depth_prompt": "", "appearance": "",
  "backstory": "", "changes": ["one short line per change, e.g. 'Rewrote greetings in her lowercase, emoji-heavy style'"]}}"""


def run_refine(args, parser) -> int:
    from . import cli  # late import: cli imports this module

    path = Path(args.refine)
    if not path.exists():
        raise SystemExit(f"Card not found: {path}")
    card, image = load_card(path)
    data = card.get("data", card)
    name = args.name or data.get("name") or "Unknown"
    ext = data.setdefault("extensions", {})
    pf = ext.get("persona_forge", {}) if isinstance(ext.get("persona_forge"), dict) else {}
    tags = [str(t).lower() for t in data.get("tags") or []]
    opts = Options(name=name, nsfw=args.nsfw or "nsfw" in tags, talent=args.talent or bool(pf.get("consent_note")),
                   descriptor=args.descriptor, language=args.language, workers=args.workers, chunk_chars=args.chunk_chars,
                   lore_max_entries=args.lore_entries)
    args.name = name
    args.nsfw = opts.nsfw
    log(f"→ Refining “{name}” from {path.name} (scope: {args.refine_scope})")

    # sources: only the new material
    args.no_wikipedia, args.search_web, args.deep, args._refine = True, False, False, True
    args.handles = cli.creator_handles(name, args.seed, args.handle)
    if args.urls_file:
        args.url += [ln.strip() for ln in Path(args.urls_file).read_text("utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
    if not (args.feed or args.feeds_file or args.audio or args.audio_url or args.file or args.url or args.seed):
        raise SystemExit("Refining needs new material: public feeds, audio, text files or pages.")
    cache = None if args.no_cache else DiskCache(Path(args.cache_dir))
    main_llm = LLMClient(LLMConfig(args.base_url, args.api_key, args.model, max_tokens=args.max_tokens,
                                   json_mode=args.json_mode, thinking=args.thinking), cache)
    extract_llm = (LLMClient(LLMConfig(args.base_url, args.api_key, args.extract_model, max_tokens=min(4096, args.max_tokens),
                                       json_mode=args.json_mode, thinking=args.thinking), cache, fallback_model=args.model)
                   if args.extract_model and args.extract_model != args.model else main_llm)
    r = Researcher(cache=cache, search_provider="none", confirm_adult=(opts.nsfw or args.confirm_adult) and not args.no_age_click,
                   browser_mode=args.browser)
    sources, _, _ = cli.gather_sources(r, args)

    facts, aliases, used = (extract_facts(extract_llm, sources, opts) if sources and args.refine_scope != "voice"
                            else ([], [], []))
    new_dossier = build_dossier(main_llm, facts, aliases, opts) if facts else {}
    vp = None
    if args.refine_scope != "facts" and getattr(args, "_voice_messages", None):
        from .voice import build_voice_profile

        vp = build_voice_profile(main_llm, name, args._voice_messages, nsfw=opts.nsfw)
    if not facts and not vp:
        raise SystemExit("Nothing new to add: the sources gave no facts and not enough of their own messages for a "
                         "voice profile (needs about 15+). Add more feeds or sources.")

    # ── rewrite the card
    current = {k: data.get(k, "") for k in CARD_FIELDS}
    current["depth_prompt"] = (ext.get("depth_prompt") or {}).get("prompt", "")
    current["appearance"], current["backstory"] = ext.get("appearance", ""), ext.get("backstory", "")
    user = ("CURRENT CARD:\n" + json.dumps(current, ensure_ascii=False, indent=1)
            + ("\n\nNEW RESEARCH (dossier from the new sources):\n" + json.dumps(new_dossier, ensure_ascii=False, indent=1)
               if new_dossier else "")
            + ("\n\nVOICE PROFILE:\n" + json.dumps(vp, ensure_ascii=False, indent=1) if vp else ""))
    log("→ Refining the card…")
    try:
        upd = main_llm.chat_json(_refine_system(opts, args.refine_scope), user, temperature=0.6, max_tokens=args.max_tokens)
    except Exception as e:  # noqa: BLE001
        log(f"! Refining failed: {e}")
        return 1
    if not isinstance(upd, dict):
        upd = {}
    changes = [str(c) for c in upd.pop("changes", []) or []]
    changed_fields = []
    before = {"first_mes": data.get("first_mes", "")}
    for k in CARD_FIELDS:
        v = upd.get(k)
        if k == "alternate_greetings":
            if isinstance(v, list) and [x for x in v if str(x).strip()]:
                data[k] = [str(x).strip() for x in v if str(x).strip()]
                changed_fields.append(k)
        elif isinstance(v, str) and v.strip() and v.strip() != str(data.get(k, "")).strip():
            data[k] = _fix_mes_example(v) if k == "mes_example" else v.strip()
            changed_fields.append(k)
    for k in ("appearance", "backstory"):
        if isinstance(upd.get(k), str) and upd[k].strip() and upd[k].strip() != ext.get(k, ""):
            ext[k] = upd[k].strip()
            changed_fields.append(k)
    if isinstance(upd.get("depth_prompt"), str) and upd["depth_prompt"].strip():
        ext.setdefault("depth_prompt", {"depth": 4, "role": "system"})["prompt"] = upd["depth_prompt"].strip()
        changed_fields.append("depth_prompt")
    # never lose the creator's hard boundaries or the authorisation line
    old_sp = current["system_prompt"]
    if BOUNDARY_MARK in old_sp and BOUNDARY_MARK not in data.get("system_prompt", ""):
        data["system_prompt"] = data.get("system_prompt", "").rstrip() + "\n\n" + old_sp[old_sp.index(BOUNDARY_MARK):]
    if pf.get("consent_note") and not str(data.get("creator_notes", "")).startswith("Authorization:"):
        data["creator_notes"] = f"Authorization: {pf['consent_note']}\n\n{data.get('creator_notes', '')}"

    # ── lorebook: add new entries, keep every existing one
    added = []
    if new_dossier and args.refine_scope != "voice":
        new_entries = write_lorebook(main_llm, new_dossier, opts)
        book = data.get("character_book") or build_character_book(name, [], scan_depth=3, token_budget=2048,
                                                                    description=f"Facts about {name}.")
        existing = {_norm(str(e.get("name") or e.get("comment") or "")) for e in book.get("entries", [])}
        next_id = max([int(e.get("id", 0) or 0) for e in book.get("entries", [])] + [-1]) + 1
        for e in new_entries:
            if _norm(e["name"]) in existing:
                continue
            book.setdefault("entries", []).append(_book_entry(next_id, e))
            existing.add(_norm(e["name"]))
            added.append(e["name"])
            next_id += 1
        data["character_book"] = book

    old_version = data.get("character_version", "1.0")
    data["character_version"] = _bump(old_version)
    if vp:
        ext["voiceProfileSummary"] = vp.get("summary", "")
    pf.setdefault("refinements", []).append({
        "date": time.strftime("%Y-%m-%d"), "scope": args.refine_scope, "from": path.name,
        "sources": [s.url for s in sources][:40], "changes": changes, "new_lore": added,
    })
    ext["persona_forge"] = pf

    # ── write the new version
    slug = slugify(name)
    out = Path(args.out) / slug
    out.mkdir(parents=True, exist_ok=True)
    v = data["character_version"]
    card_v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    card_v3 = to_v3(card_v2, [])
    avatar = image or placeholder_avatar(name)
    png_path = out / f"{slug}_v{v}.png"
    png_path.write_bytes(embed_card_in_png(avatar, card_v2, card_v3))
    (out / f"{slug}_v{v}.card.json").write_text(json.dumps(card_v3 if args.spec == "v3" else card_v2,
                                                           ensure_ascii=False, indent=2), "utf-8")
    if vp:
        (out / "voice_profile.json").write_text(json.dumps(vp, ensure_ascii=False, indent=2), "utf-8")
    t = getattr(getattr(args, "_audio", None), "transcripts", None)
    if t:
        (out / "transcripts").mkdir(exist_ok=True)
        for tn, text in t:
            (out / "transcripts" / f"{slugify(tn)[:60]}.txt").write_text(text + "\n", "utf-8")

    # keep this app's own research in step, so later rebuilds include the refinement
    dossier_path = out / "dossier.json"
    if dossier_path.exists():
        try:
            d = json.loads(dossier_path.read_text("utf-8"))
            if vp:
                from .voice import merge_voice_into_dossier

                merge_voice_into_dossier(d, vp)
            for key in ("relationships", "places", "organizations", "key_events", "works_and_achievements", "timeline"):
                if isinstance(new_dossier.get(key), list):
                    d.setdefault(key, [])
                    d[key] += [x for x in new_dossier[key] if x not in d[key]]
            dossier_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
            if facts:
                with (out / "facts.jsonl").open("a", encoding="utf-8") as fh:
                    for f in facts:
                        fh.write(json.dumps(f.__dict__, ensure_ascii=False) + "\n")
        except (ValueError, OSError) as e:
            log(f"  ! couldn't update dossier.json: {e}")

    report = [f"# Refinement of {name}: v{old_version} → v{v}", "",
              f"From `{path.name}` · scope: **{args.refine_scope}** · {time.strftime('%Y-%m-%d %H:%M')}", "",
              "## What changed", ""] + ([f"- {c}" for c in changes] or ["- (the AI listed no changes)"])
    report += ["", f"Fields updated: {', '.join(changed_fields) or 'none'}"]
    if added:
        report += ["", "## New lorebook entries", ""] + [f"- {a}" for a in added]
    if vp:
        report += ["", "## Their voice (measured from their own posts)", "", vp.get("summary", ""), ""]
        report += [f"- {r_}" for r_ in vp.get("rules", [])]
        m = vp.get("measured", {})
        if m:
            report += ["", f"Measured from {m.get('messages_analysed')} messages: median {m.get('median_words')} words, "
                       f"{m.get('starts_lowercase_pct')}% start lower-case, {m.get('emoji_per_msg')} emoji per message, "
                       f"top emoji {' '.join(m.get('top_emojis', [])[:5]) or '–'}."]
    report += ["", "## Opening message", "", "**Before**", "", "> " + before["first_mes"].replace("\n", "\n> "),
               "", "**After**", "", "> " + data.get("first_mes", "").replace("\n", "\n> "),
               "", "## Sources", ""] + [f"- {s.title or s.url}" for s in sources]
    (out / "refine_report.md").write_text("\n".join(report) + "\n", "utf-8")

    log(f"\n✓ Refined card v{v}: {png_path}")
    log(f"   refine_report.md  ← what changed ({len(changes)} changes, {len(added)} new lorebook entries)")
    if args.push:
        log(f"→ Importing into Marinara at {args.push}…")
        try:
            push_to_marinara(args.push, card_v2, avatar, user=args.marinara_user, password=args.marinara_pass)
            log("✓ Imported into Marinara")
        except Exception as e:  # noqa: BLE001
            log(f"! Push failed: {e}")
            return 1
    return 0
