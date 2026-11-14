"""Image side of the card: Marinara's image-prompt appearance, creator-supplied sprites, and an optional
generated avatar.

* `imageAppearance` (+ `imageAppearanceEnabled`) is what Marinara's Illustrator, Conversation selfies and AI
  sprite generator use to draw the character. It is built from text only.
* Sprites are embedded the way Marinara's own PNG export does it: `extensions.marinara.sprites` =
  [{"filename": "happy.png", "data": "data:image/png;base64,…"}]. Names starting `full_` are full-body poses.
  Only images you supply (`--sprites DIR`) are embedded.
* `--generate-avatar` asks an OpenAI-compatible image endpoint for a clothed, SFW portrait from the text
  description alone. No photos are ever used as a reference, so it is an illustration of the persona's look,
  not a likeness of the real person.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path

import requests

from .util import log

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
MAX_SPRITES = 256  # Marinara's MAX_EMBEDDED_SPRITE_COUNT
MAX_SPRITE_BYTES = 8 * 1024 * 1024

# Never allowed in an image prompt: anything that could make the character look like a minor.
YOUTHFUL_RE = re.compile(
    r"\b(teen(age|ager)?s?|child(like|ish)?|kid(dish)?|loli(ta)?|shota|school ?(girl|boy|uniform)|young[- ]looking|"
    r"baby[- ]?faced?|under ?age|minor|little girl|little boy|pre-?teen|flat[- ]chested child|petite child)\b",
    re.I,
)
ADULT_ANCHOR_RE = re.compile(r"\b(adult|woman|man|[2-9]\d[- ]?(year|yo|s\b))", re.I)


def clean_image_appearance(text: str, *, fallback: str = "") -> str:
    """Normalise the model's image-appearance text: strip youth-coded terms and anchor it as an adult."""
    text = (text or fallback or "").strip()
    if not text:
        return ""
    text = YOUTHFUL_RE.sub("", text)
    text = re.sub(r"\s*,\s*(,\s*)+", ", ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;")
    if not ADULT_ANCHOR_RE.search(text):
        text = "adult, " + text
    return text[:1200]


def load_sprites(folder: str) -> list[dict]:
    """Read a folder of sprite images. File name (without extension) = expression label, e.g. happy.png.
    Files in a subfolder called full-body/ (or names starting full_) become full-body sprites."""
    root = Path(folder)
    if not root.is_dir():
        raise SystemExit(f"--sprites: {folder} is not a folder")
    out: list[dict] = []
    total = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = p.relative_to(root)
        stem = re.sub(r"[^a-zA-Z0-9_\- ]", "_", p.stem)[:80]
        full = any(part.lower() in ("full-body", "full_body", "fullbody", "full") for part in rel.parts[:-1])
        if full and not stem.lower().startswith("full_"):
            stem = f"full_{stem}"
        data = p.read_bytes()
        if len(data) > MAX_SPRITE_BYTES:
            log(f"  ! sprite {rel} is over 8 MB – skipped")
            continue
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        out.append({"filename": f"{stem}{p.suffix.lower()}", "data": f"data:{mime};base64," + base64.b64encode(data).decode()})
        total += len(data)
        if len(out) >= MAX_SPRITES:
            log(f"  ! more than {MAX_SPRITES} sprites – the rest were skipped (Marinara's limit)")
            break
    log(f"→ {len(out)} sprites from {folder} ({total / 1e6:.1f} MB)")
    return out


def generate_avatar(
    image_appearance: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    style: str = "digital painting, portrait, head and shoulders, soft studio lighting",
    size: str = "1024x1536",
    timeout: int = 180,
) -> bytes:
    """One SFW, clothed portrait from the text description only (no reference images)."""
    prompt = (
        f"Character portrait of a fictional adult persona. {style}. Appearance: {clean_image_appearance(image_appearance)}. "
        "Fully clothed, tasteful, safe for work, neutral background, no text, no watermark."
    )
    url = base_url.rstrip("/") + "/images/generations"
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code == 400 and "size" in resp.text.lower():
        payload["size"] = "1024x1024"
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"image generation failed (HTTP {resp.status_code}): {resp.text[:300]}")
    item = (resp.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        raw = requests.get(item["url"], timeout=timeout).content
    else:
        raise RuntimeError("image endpoint returned no image")
    return _to_png(raw)


def _to_png(raw: bytes) -> bytes:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw
    try:
        import io

        from PIL import Image  # type: ignore

        buf = io.BytesIO()
        Image.open(io.BytesIO(raw)).convert("RGBA").save(buf, "PNG")
        return buf.getvalue()
    except ImportError as e:
        raise RuntimeError("The image endpoint returned a non-PNG image; install Pillow to convert it.") from e


# ═══════════════════════════════════════════════════════════ sprite kit
# Marinara's Generate Sprites window (client/src/components/ui/SpriteGenerationModal.tsx): fixed label lists,
# up to four reference images, an Appearance Description, and an optional "use current avatar" reference.
MARINARA_EXPRESSIONS = [
    "neutral", "happy", "sad", "angry", "surprised", "scared", "disgusted", "thinking", "laughing", "crying",
    "blushing", "smirk", "embarrassed", "determined", "confused", "sleepy",
]
MARINARA_POSES = [
    "idle", "walk", "run", "battle_stance", "attack", "defend", "casting", "hurt", "jump", "thinking", "cheer",
    "victory", "wave", "sit", "kneel", "point",
]
MAX_REFERENCES = 4


def collect_references(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
        else:
            log(f"  ! reference {p} is not an image or folder – skipped")
    return files


def _prepare_reference(src: Path, dest: Path, max_side: int = 1024) -> Path:
    """Copy a reference image as PNG, scaled so the longest side is at most `max_side` (needs Pillow; otherwise
    the file is copied unchanged, which Marinara also accepts)."""
    try:
        from PIL import Image, ImageOps  # type: ignore

        img = ImageOps.exif_transpose(Image.open(src)).convert("RGBA")
        img.thumbnail((max_side, max_side))
        dest = dest.with_suffix(".png")
        img.save(dest, "PNG")
    except ImportError:
        dest = dest.with_suffix(src.suffix.lower())
        dest.write_bytes(src.read_bytes())
    return dest


def pick_sprite_set(llm, dossier: dict, name: str, n_expr: int = 12, n_pose: int = 6) -> dict:
    """Choose the expressions and poses that suit this persona from Marinara's fixed lists."""
    default = {"expressions": MARINARA_EXPRESSIONS[:n_expr], "poses": ["idle", "wave", "sit", "thinking", "cheer", "walk"][:n_pose],
               "notes": {}}
    if llm is None:
        return default
    persona = {k: dossier.get(k) for k in ("personality_traits", "inner_life", "habits_and_quirks", "speech_style", "intimacy")
               if dossier.get(k)}
    system = (
        f"You plan Marinara Engine sprites for the character {name}. Choose the {n_expr} expressions (always include "
        f"'neutral') and {n_pose} full-body poses from these exact lists that this persona will show most in chat, "
        f"ordered most-used first, with a 4-10 word note per pick on how {name} would look doing it.\n"
        f"Expressions: {', '.join(MARINARA_EXPRESSIONS)}\nPoses: {', '.join(MARINARA_POSES)}\n"
        'JSON: {"expressions": [], "poses": [], "notes": {"label": "note"}}'
    )
    try:
        data = llm.chat_json(system, json.dumps(persona, ensure_ascii=False)[:20000], temperature=0.3, max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        log(f"  ! sprite planning failed ({e}); using Marinara's defaults")
        return default
    expr = [e for e in data.get("expressions", []) if e in MARINARA_EXPRESSIONS]
    poses = [p for p in data.get("poses", []) if p in MARINARA_POSES]
    if not expr:
        return default
    if "neutral" not in expr:
        expr.insert(0, "neutral")
    expr = list(dict.fromkeys(expr))[:n_expr] or default["expressions"]
    poses = list(dict.fromkeys(poses))[:n_pose] or default["poses"]
    notes = {k: str(v)[:120] for k, v in (data.get("notes") or {}).items() if k in expr or k in poses}
    return {"expressions": expr, "poses": poses, "notes": notes}


def build_sprite_kit(out: Path, name: str, image_appearance: str, references: list[Path], plan: dict,
                     avatar_is_reference: bool) -> Path:
    kit = out / "sprite_kit"
    refdir = kit / "references"
    refdir.mkdir(parents=True, exist_ok=True)
    for old in refdir.iterdir():
        old.unlink()
    chosen = references[:MAX_REFERENCES]
    prepared = [_prepare_reference(src, refdir / f"ref_{i}_{re.sub(r'[^a-zA-Z0-9]+', '_', src.stem)[:40]}")
                for i, src in enumerate(chosen, 1)]
    if len(references) > MAX_REFERENCES:
        log(f"  ! Marinara takes {MAX_REFERENCES} reference images; used the first {MAX_REFERENCES} of {len(references)}")
    (kit / "appearance_description.txt").write_text(image_appearance + "\n", "utf-8")

    n_e, n_p = len(plan["expressions"]), len(plan["poses"])
    preset = lambda n: next(c for c in (1, 6, 9, 12, 16) if c >= n)  # noqa: E731 – Marinara's count presets
    note = lambda k: f" – {plan['notes'][k]}" if plan["notes"].get(k) else ""  # noqa: E731
    lines = [
        f"# Sprite kit: {name}",
        "",
        "Everything Marinara's **Generate Sprites** window needs, ready to use after you import the card.",
        "",
        "## Before you start",
        "",
        "- Import the card into Marinara.",
        "- Add an image-generation connection that supports reference images (Connections panel). The better its "
        "reference support, the closer the sprites match.",
        "",
        "## Expressions (portraits)",
        "",
        "1. Open the character → **Sprites** tab → **Facial Expressions** → **Add sprites** → **Generate Sprite**.",
        "2. Source: **Expressions (Portrait)**. Pick your **Image Generation Connection**.",
        f"3. **Reference Images**: add the files in `sprite_kit/references/` ({len(prepared)} of max 4)"
        + (", and/or tick **use the current avatar as a reference** (the card's avatar is reference 1)." if avatar_is_reference else "."),
        "4. **Appearance Description**: paste `sprite_kit/appearance_description.txt`:",
        "",
        "   ```",
        f"   {image_appearance}",
        "   ```",
        "",
        "5. Turn on **Transparent sprite background**.",
        f"6. **Expression Count**: choose **{preset(n_e)}**, then make sure these {n_e} are ticked "
        "(fill any spare slots with whatever you like):",
        "",
        *[f"   - [ ] `{e}`{note(e)}" for e in plan["expressions"]],
        "",
        "7. Click **Generate**. Keep, crop or rename the results and save them.",
        "",
        "## Full-body poses",
        "",
        "Repeat with source **Full-body**, the same references and description.",
        f"Either tick **Match existing expression sprites**, or set **Pose Count** to **{preset(n_p)}** and make sure "
        f"these {n_p} are ticked:",
        "",
        *[f"   - [ ] `{p}`{note(p)}" for p in plan["poses"]],
        "",
        "## In chat",
        "",
        "Add the **Expression Engine** agent to Roleplay chats so it picks the right sprite for each reply.",
        "",
        "## Reference images",
        "",
        *[f"- `{p.relative_to(out)}` (from {src.name})" for p, src in zip(prepared, chosen)],
    ]
    (kit / "SPRITE_KIT.md").write_text("\n".join(lines) + "\n", "utf-8")
    return kit
