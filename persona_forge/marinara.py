"""Marinara Engine output formats.

Field mapping follows Marinara's own importers (packages/server/src/services/import/
st-character.importer.ts and st-lorebook.importer.ts):

* Character: Character Card V2 (`chara_card_v2`) or V3 (`chara_card_v3`) envelope. Marinara keeps the
  standard fields plus its own extensions: `extensions.appearance`, `extensions.backstory`,
  `extensions.nameAliases`, `extensions.nameColor`, `extensions.depth_prompt`, and a top-level `summary`.
* Embedded lorebook: `data.character_book`. On import Marinara offers to turn it into a standalone,
  linked lorebook; via the API we ask for that with `importEmbeddedLorebook: true`.
* Standalone lorebook: SillyTavern World Info JSON (`entries` keyed by uid). Marinara also reads its
  own extra entry fields: `description` (Knowledge Router summary), `matchWholeWords`, `preventRecursion`…
* PNG card: base64 JSON in tEXt chunks `chara` (V2) and `ccv3` (V3). Marinara prefers `ccv3`.
* Order: in Marinara a LOWER order number is inserted first and survives budget trimming.
"""

from __future__ import annotations

import base64
import colorsys
import copy
import hashlib
import json
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import requests

CSRF_HEADER = "x-marinara-csrf"  # packages/shared/src/constants/security.ts
CSRF_VALUE = "1"

IMPORTANCE_ORDER = {1: 10, 2: 50, 3: 100}


# ═══════════════════════════════════════════════════════════ lorebook entries
def _wi_entry(uid: int, e: dict) -> dict:
    """One entry in SillyTavern World Info shape + Marinara's extra fields."""
    selective = bool(e.get("secondary_keys"))
    order = IMPORTANCE_ORDER.get(int(e.get("importance", 2)), 50) + uid  # stable, unique ordering
    return {
        "uid": uid,
        "key": list(e["keys"]),
        "keysecondary": list(e.get("secondary_keys") or []),
        "comment": e["name"],
        "content": e["content"],
        "description": e.get("description", ""),  # read only by Marinara's Knowledge Router agent
        "constant": bool(e.get("constant", False)),
        "selective": selective,
        "selectiveLogic": 0,  # AND Any
        "order": order,
        "position": 0,  # before character definitions
        "disable": False,
        "probability": 100,
        "useProbability": True,
        "depth": 4,
        "role": 0,
        "group": "",
        "groupWeight": 100,
        "scanDepth": None,
        "caseSensitive": False,
        "matchWholeWords": True,
        "useRegex": False,
        "preventRecursion": True,
        "excludeRecursion": False,
        "delayUntilRecursion": False,
        "sticky": None,
        "cooldown": None,
        "delay": None,
        "ephemeral": None,
        "outletName": "",
        "locked": False,
        "addMemo": True,
        "displayIndex": uid,
        "extensions": {},
    }


def _book_entry(uid: int, e: dict) -> dict:
    """One entry in Character Card V2 `character_book` shape, with the ST/Marinara extras that both
    SillyTavern (via `extensions`) and Marinara (top-level camelCase) read."""
    wi = _wi_entry(uid, e)
    return {
        "id": uid,
        "keys": wi["key"],
        "secondary_keys": wi["keysecondary"],
        "comment": wi["comment"],
        "name": wi["comment"],
        "content": wi["content"],
        "description": wi["description"],
        "constant": wi["constant"],
        "selective": wi["selective"],
        "selectiveLogic": 0,
        "insertion_order": wi["order"],
        "enabled": True,
        "position": "before_char",
        "case_sensitive": False,
        "priority": 10,
        "matchWholeWords": True,
        "probability": 100,
        "useProbability": True,
        "preventRecursion": True,
        "extensions": {
            "position": 0,
            "depth": 4,
            "role": 0,
            "probability": 100,
            "useProbability": True,
            "selectiveLogic": 0,
            "match_whole_words": True,
            "case_sensitive": False,
            "prevent_recursion": True,
            "exclude_recursion": False,
            "delay_until_recursion": False,
            "group": "",
            "group_weight": 100,
            "scan_depth": None,
            "display_index": uid,
            "vectorized": False,
        },
    }


def build_lorebook(name: str, entries: list[dict], *, scan_depth: int, token_budget: int, description: str) -> dict:
    return {
        "name": f"{name}'s Lorebook",
        "description": description,
        "scan_depth": scan_depth,
        "token_budget": token_budget,
        "recursive_scanning": False,
        "max_recursion_depth": 3,
        "extensions": {},
        "entries": {str(i): _wi_entry(i, e) for i, e in enumerate(entries)},
    }


def build_character_book(name: str, entries: list[dict], *, scan_depth: int, token_budget: int, description: str) -> dict:
    return {
        "name": f"{name}'s Lorebook",
        "description": description,
        "scan_depth": scan_depth,
        "token_budget": token_budget,
        "recursive_scanning": False,
        "extensions": {},
        "entries": [_book_entry(i, e) for i, e in enumerate(entries)],
    }


# ═══════════════════════════════════════════════════════════ character card
def build_card(
    name: str,
    card: dict,
    dossier: dict,
    lore_entries: list[dict],
    *,
    creator: str,
    sources: list[str],
    scan_depth: int = 3,
    token_budget: int = 2048,
    merge_fields: bool = False,
    embed_lorebook: bool = True,
    boundaries: str = "",
    consent_note: str = "",
    nsfw: bool = False,
    image_appearance: str = "",
    sprites: list[dict] | None = None,
    onboarding: dict | None = None,
) -> dict:
    """Return the V2 envelope. `merge_fields` appends Appearance/Backstory to Description for apps
    (e.g. SillyTavern) that do not have those fields – same as Marinara's 'Compatible' export."""
    description = card.get("description", "").strip()
    appearance = card.get("appearance", "").strip()
    backstory = card.get("backstory", "").strip()
    if merge_fields:
        if appearance:
            description += f"\n\nAppearance:\n{appearance}"
        if backstory:
            description += f"\n\nBackstory:\n{backstory}"

    lb_desc = f"Facts about the people, places, events and works in {name}'s life. Generated by Persona Forge."
    name_color = str(card.get("name_color") or "").strip()
    if not (name_color.startswith("#") and len(name_color) in (4, 7)):
        name_color = _color_for(name)

    extensions: dict[str, Any] = {
        "talkativeness": 0.5,
        "fav": False,
        "world": "",
        "depth_prompt": {"prompt": str(card.get("depth_prompt", "")).strip(), "depth": 4, "role": "system"},
        "appearance": "" if merge_fields else appearance,
        "backstory": "" if merge_fields else backstory,
        "nameAliases": [a for a in dossier.get("aliases", []) if isinstance(a, str)][:12],
        "nameColor": name_color,
        # Marinara: Illustrator, selfies and AI sprite generation draw from this instead of `appearance`.
        **({"imageAppearanceEnabled": True, "imageAppearance": image_appearance} if image_appearance else {}),
        # Marinara: Interactive Onboarding – questions the player answers when a chat starts.
        **({"onboarding": onboarding} if onboarding and onboarding.get("enabled") else {}),
        # Marinara: embedded sprites are restored on import (same shape as Marinara's own PNG export).
        **({"marinara": {"sprites": sprites}} if sprites else {}),
        "persona_forge": {"version": 1, "generated": int(time.time()), "sources": sources[:50],
                          **({"consent_note": consent_note} if consent_note else {}),
                          **({"confirmed_on": dossier["creator_confirmed"].get("answered_on", ""),
                              "confirmed_answers": dossier["creator_confirmed"].get("answers", 0)}
                             if isinstance(dossier.get("creator_confirmed"), dict)
                             and dossier["creator_confirmed"].get("answered_on") else {})},
    }

    system_prompt = str(card.get("system_prompt", "")).strip()
    if boundaries.strip():
        # The creator's own words, verbatim, so no model paraphrase can water them down.
        system_prompt += "\n\n[Hard boundaries set by the creator — never break these]\n" + boundaries.strip()
    creator_notes = str(card.get("creator_notes", "")).strip()
    if consent_note.strip():
        creator_notes = f"Authorization: {consent_note.strip()}\n\n{creator_notes}"
    tags = list(dict.fromkeys(t.lower() for t in card.get("tags") or []))
    if nsfw and "nsfw" not in tags:
        tags.append("nsfw")

    data = {
        "name": name,
        "summary": str(card.get("summary", "")).strip(),
        "description": description,
        "personality": str(card.get("personality", "")).strip(),
        "scenario": str(card.get("scenario", "")).strip(),
        "first_mes": str(card.get("first_mes", "")).strip(),
        "mes_example": str(card.get("mes_example", "")).strip(),
        "creator_notes": creator_notes,
        "system_prompt": system_prompt,
        "post_history_instructions": str(card.get("post_history_instructions", "")).strip(),
        "alternate_greetings": list(card.get("alternate_greetings") or []),
        "tags": tags,
        "creator": creator,
        "character_version": "1.0",
        "extensions": extensions,
        "character_book": (
            build_character_book(name, lore_entries, scan_depth=scan_depth, token_budget=token_budget, description=lb_desc)
            if embed_lorebook and lore_entries
            else None
        ),
    }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def save_card_files(out: Path, stem: str, data: dict, image: bytes | None, *, spec: str = "v2") -> Path:
    """Write <stem>.png (V2 + V3 chunks) and <stem>.card.json for a card's `data`. Returns the PNG path."""
    card_v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    card_v3 = to_v3(card_v2, list(data.get("source") or []))
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"{stem}.png"
    png.write_bytes(embed_card_in_png(image or placeholder_avatar(data.get("name", "?")), card_v2, card_v3))
    (out / f"{stem}.card.json").write_text(json.dumps(card_v3 if spec == "v3" else card_v2, ensure_ascii=False, indent=2),
                                          "utf-8")
    return png


def to_v3(card_v2: dict, sources: list[str]) -> dict:
    v3 = copy.deepcopy(card_v2)
    v3["spec"] = "chara_card_v3"
    v3["spec_version"] = "3.0"
    now = int(time.time())
    d = v3["data"]
    d.setdefault("nickname", "")
    d.setdefault("creator_notes_multilingual", {})
    d["source"] = sources[:50]
    d.setdefault("group_only_greetings", [])
    d["creation_date"] = now
    d["modification_date"] = now
    d.setdefault("assets", [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}])
    return v3


def _color_for(name: str) -> str:
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, 0.62, 0.55)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


# ═══════════════════════════════════════════════════════════ PNG
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)


def _iter_chunks(png: bytes):
    if not png.startswith(PNG_SIG):
        raise ValueError("not a PNG file")
    pos = 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        ctype = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        yield ctype, data
        pos += 12 + length


def embed_card_in_png(png: bytes, card_v2: dict, card_v3: dict | None = None) -> bytes:
    """Strip any old card chunks and add `chara` (V2) and `ccv3` (V3) tEXt chunks before IEND."""
    out = [PNG_SIG]
    for ctype, data in _iter_chunks(png):
        if ctype in (b"tEXt", b"zTXt", b"iTXt") and data.split(b"\x00", 1)[0] in (b"chara", b"ccv3"):
            continue
        if ctype == b"IEND":
            out.append(_chunk(b"tEXt", b"chara\x00" + base64.b64encode(json.dumps(card_v2, ensure_ascii=False).encode())))
            if card_v3:
                out.append(_chunk(b"tEXt", b"ccv3\x00" + base64.b64encode(json.dumps(card_v3, ensure_ascii=False).encode())))
        out.append(_chunk(ctype, data))
    return b"".join(out)


def read_card_from_png(png: bytes) -> dict | None:
    found = {}
    for ctype, data in _iter_chunks(png):
        if ctype == b"tEXt":
            key, _, val = data.partition(b"\x00")
            if key in (b"chara", b"ccv3"):
                found[key] = json.loads(base64.b64decode(val))
    return found.get(b"ccv3") or found.get(b"chara")


def placeholder_avatar(name: str, width: int = 512, height: int = 768) -> bytes:
    """A tasteful gradient portrait placeholder with initials (initials need Pillow; gradient works without)."""
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    top = colorsys.hls_to_rgb(h, 0.30, 0.45)
    bot = colorsys.hls_to_rgb((h + 0.08) % 1, 0.12, 0.40)
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore

        img = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / (height - 1)
            c = tuple(int((top[i] * (1 - t) + bot[i] * t) * 255) for i in range(3))
            draw.line([(0, y), (width, y)], fill=c)
        initials = "".join(p[0] for p in name.split()[:3] if p[:1].isalnum()).upper() or "?"
        try:
            font = ImageFont.load_default(size=int(width * 0.3))
        except TypeError:  # very old Pillow
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), initials, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((width - tw) / 2 - bbox[0], (height - th) / 2 - bbox[1]), initials, font=font,
                  fill=(255, 255, 255))
        import io

        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except ImportError:
        rows = []
        for y in range(height):
            t = y / (height - 1)
            c = bytes(int((top[i] * (1 - t) + bot[i] * t) * 255) for i in range(3))
            rows.append(b"\x00" + c * width)
        raw = zlib.compress(b"".join(rows), 9)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", raw) + _chunk(b"IEND", b"")


def load_avatar(path: str) -> bytes:
    """Load a user-supplied avatar; converts non-PNG images if Pillow is installed."""
    raw = Path(path).read_bytes()
    if raw.startswith(PNG_SIG):
        return raw
    try:
        import io

        from PIL import Image  # type: ignore

        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except ImportError as e:
        raise SystemExit(f"{path} is not a PNG. Install Pillow (pip install pillow) or convert it to PNG.") from e


# ═══════════════════════════════════════════════════════════ Marinara API
def push_to_marinara(
    base_url: str,
    card_v2: dict,
    avatar_png: bytes | None,
    *,
    user: str = "",
    password: str = "",
    import_lorebook: bool = True,
    timeout: int = 60,
) -> dict:
    """POST the card to a running Marinara Engine (`/api/import/st-character`). The embedded
    character_book becomes a standalone lorebook linked to the character."""
    body: dict[str, Any] = dict(card_v2)
    body["importEmbeddedLorebook"] = import_lorebook
    if avatar_png:
        body["_avatarDataUrl"] = "data:image/png;base64," + base64.b64encode(avatar_png).decode()
    url = base_url.rstrip("/") + "/api/import/st-character"
    headers = {CSRF_HEADER: CSRF_VALUE, "Content-Type": "application/json"}
    auth = (user, password) if user else None
    resp = requests.post(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers,
                         auth=auth, timeout=timeout)
    if resp.status_code == 401:
        raise RuntimeError("Marinara rejected the login (401). Pass --marinara-user/--marinara-pass.")
    if resp.status_code == 403:
        raise RuntimeError(f"Marinara refused the request (403): {resp.text[:300]}")
    resp.raise_for_status()
    result = resp.json()
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(f"Marinara import failed: {result.get('error')}")
    return result
