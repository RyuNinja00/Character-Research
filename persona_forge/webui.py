"""Local web UI for Persona Forge.

    python -m persona_forge.webui            # opens http://127.0.0.1:8765

Standard library only. Binds to 127.0.0.1 so only this computer can reach it.

Every creator gets a folder under talent/ (created on first run):

    talent/<slug>/
      settings.json        form values (never API keys or passwords)
      brief.md             creator brief (from the template; only used once edited)
      limits.txt           hard limits (from the template; placeholder lines are ignored)
      urls.txt             extra pages, one per line
      references/          photos for the sprite kit
      sprites/             finished expression sprites
        full-body/         finished full-body sprites
      avatar/              card image
      sources/             extra text sources (transcripts, notes…)
      output/              everything the app writes (card, lorebook, review, dossier, sprite kit…)
      runs/                a log of every run
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .util import slugify

ROOT = Path(__file__).resolve().parents[1]
TALENT_ROOT = Path(os.environ.get("PERSONA_FORGE_TALENT_DIR", ROOT / "talent"))
TEMPLATES = ROOT / "templates"
PAGE = Path(__file__).with_name("webui_page.html")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
TEXT_EXTS = {".txt", ".md", ".json", ".html", ".htm"}
UPLOAD_KINDS = {  # kind → (subfolder, allowed extensions)
    "references": ("references", IMAGE_EXTS),
    "sprites": ("sprites", IMAGE_EXTS),
    "fullbody": ("sprites/full-body", IMAGE_EXTS),
    "avatar": ("avatar", IMAGE_EXTS),
    "sources": ("sources", TEXT_EXTS),
    "audio": ("audio", {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".webm", ".flac", ".aac", ".mp4", ".m4b",
                        ".mkv", ".mov", ".wma"}),
}
UPLOAD_KINDS["card"] = ("cards", {".png", ".json"})
UPLOAD_LIMITS = {"audio": 1024 * 1024 * 1024}  # per kind; everything else uses MAX_UPLOAD
SECRET_FIELDS = {"api_key", "image_api_key", "marinara_pass", "transcribe_api_key", "reddit_client_secret"}
MAX_UPLOAD = 25 * 1024 * 1024

FEEDS_HEADER = ("# Public feeds for Persona Forge – one per line, e.g.  reddit: u/name   bluesky: name.bsky.social   "
                "youtube: @handle   podcast: Stage Name\n")
URLS_HEADER = "# Extra pages for Persona Forge – one URL per line. Lines starting with # are ignored.\n"


# ═══════════════════════════════════════════════════════════ talent folders
def ensure_talent_root() -> None:
    TALENT_ROOT.mkdir(parents=True, exist_ok=True)
    readme = TALENT_ROOT / "README.txt"
    if not readme.exists():
        readme.write_text(
            "One folder per creator/character, created by the Persona Forge web UI.\n"
            "This folder is git-ignored: photos, briefs and outputs stay on this computer.\n", "utf-8")


def _template(name: str) -> str:
    p = TEMPLATES / name
    return p.read_text("utf-8") if p.exists() else ""


def talent_dir(slug: str) -> Path:
    if not re.fullmatch(r"[\w-]{1,80}", slug or ""):
        raise ValueError("invalid talent name")
    d = (TALENT_ROOT / slug).resolve()
    if TALENT_ROOT.resolve() not in d.parents:
        raise ValueError("invalid talent path")
    return d


def create_talent(name: str) -> str:
    """Create (or complete) the folder structure for a creator. Safe to call repeatedly."""
    slug = slugify(name)[:80] or "character"
    d = talent_dir(slug)
    for sub in ("references", "sprites/full-body", "avatar", "sources", "audio", "cards", "output", "runs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    if not (d / "brief.md").exists():
        (d / "brief.md").write_text(_template("creator_brief_template.md").replace("<Stage name>", name), "utf-8")
    if not (d / "limits.txt").exists():
        (d / "limits.txt").write_text(_template("boundaries_template.txt"), "utf-8")
    if not (d / "feeds.txt").exists():
        (d / "feeds.txt").write_text(FEEDS_HEADER, "utf-8")
    if not (d / "urls.txt").exists():
        (d / "urls.txt").write_text(URLS_HEADER, "utf-8")
    if not (d / "settings.json").exists():
        (d / "settings.json").write_text(json.dumps({"name": name}, indent=2), "utf-8")
    return slug


def list_talents() -> list[dict]:
    out = []
    for d in sorted(TALENT_ROOT.iterdir()) if TALENT_ROOT.exists() else []:
        if d.is_dir() and (d / "settings.json").exists():
            try:
                name = json.loads((d / "settings.json").read_text("utf-8")).get("name") or d.name
            except ValueError:
                name = d.name
            out.append({"slug": d.name, "name": name, "has_output": bool(find_dossier(d))})
    return out


def _files(d: Path, sub: str, exts: set[str]) -> list[str]:
    p = d / sub
    return sorted(f.name for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts) if p.exists() else []


def find_dossier(d: Path) -> Path | None:
    cands = sorted((d / "output").glob("*/dossier.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def output_files(d: Path) -> list[dict]:
    dossier = find_dossier(d)
    if not dossier:
        return []
    od = dossier.parent
    order = [".png", ".md", ".json", ".jsonl"]
    files = [f for f in od.rglob("*") if f.is_file()]
    files.sort(key=lambda f: (order.index(f.suffix) if f.suffix in order else 9, str(f)))
    return [{"name": str(f.relative_to(od)), "path": str(f.relative_to(d)), "size": f.stat().st_size} for f in files]


def refinable_cards(d: Path) -> list[str]:
    """Cards in the creator folder that can be refined: the app's own outputs (newest first) and uploads."""
    out = []
    for p in sorted((d / "output").glob("*/*.png"), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.name != "avatar_generated.png":
            out.append(str(p.relative_to(d)))
    out += [str(p.relative_to(d)) for p in sorted((d / "cards").glob("*")) if p.suffix.lower() in (".png", ".json")]
    return out


def talent_info(slug: str) -> dict:
    d = talent_dir(slug)
    if not d.exists():
        raise FileNotFoundError(slug)
    settings = json.loads((d / "settings.json").read_text("utf-8")) if (d / "settings.json").exists() else {}
    urls = (d / "urls.txt").read_text("utf-8") if (d / "urls.txt").exists() else ""
    return {
        "slug": slug,
        "folder": str(d),
        "settings": settings,
        "brief": (d / "brief.md").read_text("utf-8") if (d / "brief.md").exists() else "",
        "limits": (d / "limits.txt").read_text("utf-8") if (d / "limits.txt").exists() else "",
        "urls": "\n".join(l for l in urls.splitlines() if l.strip() and not l.lstrip().startswith("#")),
        "feeds": "\n".join(l for l in ((d / "feeds.txt").read_text("utf-8") if (d / "feeds.txt").exists() else "").splitlines()
                           if l.strip() and not l.lstrip().startswith("#")),
        "cards": refinable_cards(d),
        "files": {k: _files(d, sub, exts) for k, (sub, exts) in UPLOAD_KINDS.items()},
        "outputs": output_files(d),
        "has_dossier": bool(find_dossier(d)),
        "has_confirm": bool(confirm_path(d)),
        "has_card": bool(working_out_dir(d) and editor_cards(d)),
    }


# ═══════════════════════════════════════════════════════════ form → command line
def _s(form: dict, key: str) -> str:
    v = form.get(key)
    return str(v).strip() if v is not None else ""


def _flag(form: dict, key: str) -> bool:
    return form.get(key) in (True, "true", "on", "1", 1)


def _int(form: dict, key: str, notes: list[str], lo: int = 0, hi: int = 10_000) -> str | None:
    raw = _s(form, key)
    if not raw:
        return None
    try:
        v = int(float(raw))
    except ValueError:
        notes.append(f"Ignored “{key}”: “{raw}” is not a number – the default was used.")
        return None
    return str(max(lo, min(hi, v)))


def _lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").replace(",", "\n").splitlines() if l.strip()]


def _meaningful_brief(text: str) -> bool:
    """True if the brief has real content: any line that isn't in the untouched template (an answered
    "- Label: answer" line differs from the template's "- Label:", so it counts)."""
    norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()  # noqa: E731
    template_lines = {norm(l) for l in _template("creator_brief_template.md").splitlines()}
    for line in (text or "").splitlines():
        n = norm(line)
        if n and not n.startswith("#") and n not in template_lines:
            return True
    return False


def _clean_limits(text: str) -> str:
    """Hard limits for the card: drop comments and unfilled template placeholders (e.g. <list them>)."""
    keep = [l.rstrip() for l in (text or "").splitlines()
            if l.strip() and not l.lstrip().startswith("#") and not re.search(r"<[^>]+>", l)]
    return "\n".join(keep).strip()


def save_talent_inputs(slug: str, form: dict) -> None:
    d = talent_dir(slug)
    settings = {k: v for k, v in form.items() if k not in SECRET_FIELDS and k not in ("brief", "limits", "urls", "feeds")}
    (d / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    if "brief" in form:
        (d / "brief.md").write_text(form.get("brief") or "", "utf-8")
    if "limits" in form:
        (d / "limits.txt").write_text(form.get("limits") or "", "utf-8")
    if "feeds" in form:
        (d / "feeds.txt").write_text(FEEDS_HEADER + "\n".join(l.strip() for l in (form.get("feeds") or "").splitlines()
                                                              if l.strip()) + "\n", "utf-8")
    if "urls" in form:
        (d / "urls.txt").write_text(URLS_HEADER + "\n".join(_lines(form.get("urls", ""))) + "\n", "utf-8")


def validate(form: dict) -> list[str]:
    """Only the few things the run truly cannot do without. Everything else is optional."""
    errors = []
    mode = _s(form, "mode") or "talent"
    if not _s(form, "name"):
        errors.append("Enter a name (the stage name, person or character).")
    if mode == "talent" and not _s(form, "consent_note"):
        errors.append("Creator mode needs a consent note – who authorised this card and when.")
    return errors


def build_command(slug: str, form: dict, rebuild: bool = False, refine: str = "") -> tuple[list[str], dict, list[str]]:
    """Turn the form into CLI arguments. Empty boxes are simply left out, so the app's defaults apply."""
    d = talent_dir(slug)
    notes: list[str] = []
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    mode = _s(form, "mode") or "talent"
    name = _s(form, "name")
    a: list[str] = [name, "-y", "--out", str(d / "output")]

    if rebuild:
        dossier = find_dossier(d)
        if not dossier:
            raise ValueError("No saved research yet – run a full build first.")
        a += ["--from-dossier", str(dossier)]

    if refine:
        card = (d / refine).resolve()
        if d.resolve() not in card.parents or not card.is_file():
            raise ValueError("Pick a card to refine (section 8).")
        a += ["--refine", str(card)]
        if _s(form, "refine_scope") in ("voice", "facts", "both"):
            a += ["--refine-scope", _s(form, "refine_scope")]
    if mode == "talent":
        a += ["--talent", "--consent-note", _s(form, "consent_note")]
    elif mode == "fictional":
        a += ["--no-wikipedia"]

    # research sources (ignored by the CLI on a rebuild)
    if not rebuild:
        if mode == "public" and _s(form, "wiki_title"):
            a += ["--wiki-title", _s(form, "wiki_title")]
        for seed in _lines(form.get("seeds", "")):
            a += ["--seed", seed if "://" in seed else "https://" + seed]
        urls = [l for l in (d / "urls.txt").read_text("utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")] if (d / "urls.txt").exists() else []
        if urls:
            a += ["--urls-file", str(d / "urls.txt")]
        brief = (d / "brief.md").read_text("utf-8") if (d / "brief.md").exists() else ""
        if _meaningful_brief(brief):
            a += ["--file", str(d / "brief.md")]
        else:
            notes.append("Creator brief is still the empty template – it was left out of this run.")
        for f in _files(d, "sources", TEXT_EXTS):
            a += ["--file", str(d / "sources" / f)]
        feeds = [l for l in ((d / "feeds.txt").read_text("utf-8") if (d / "feeds.txt").exists() else "").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        if feeds:
            a += ["--feeds-file", str(d / "feeds.txt")]
        if form.get("auto_feeds", True) in (False, "false", "0", 0):
            a.append("--no-auto-feeds")
        for h in _lines(form.get("handles", "")):
            a += ["--handle", h.lstrip("@")]
        if refine and not (feeds or _files(d, "audio", UPLOAD_KINDS["audio"][1]) or urls
                           or _files(d, "sources", TEXT_EXTS) or _lines(form.get("seeds", ""))):
            raise ValueError("Refining needs new material: add public feeds (section 2), audio, text sources or pages.")
        if not refine and mode in ("talent", "fictional") and not (
            _lines(form.get("seeds", "")) or urls or _meaningful_brief(brief) or _files(d, "sources", TEXT_EXTS) or feeds
        ):
            if _flag(form, "no_web"):
                raise ValueError("Nothing to read: add a link hub, a page, a brief or a source file, "
                                 "or untick “Offline”.")
            if not (_flag(form, "deep") or _flag(form, "search_web")):
                notes.append("No link hub, pages, brief or files given – web search on the name was turned on so "
                             "the run has something to read.")
            a += ["--search-web"]

    limits = _clean_limits((d / "limits.txt").read_text("utf-8") if (d / "limits.txt").exists() else "")
    if limits:
        clean = d / "runs" / "limits_clean.txt"
        clean.write_text(limits + "\n", "utf-8")
        a += ["--boundaries", str(clean)]

    for key, flag in (("nsfw", "--nsfw"), ("deep", "--deep"), ("search_web", "--search-web"),
                      ("no_web", "--no-web"), ("generate_avatar", "--generate-avatar"),
                      ("no_cache", "--no-cache"), ("verbose", "--verbose"), ("merge_fields", "--merge-fields"),
                      ("no_image_prompt", "--no-image-prompt"), ("no_onboarding", "--no-onboarding")):
        if _flag(form, key) and flag not in a:
            if rebuild and key in ("deep", "search_web", "no_web"):
                continue
            a.append(flag)
    if not rebuild:  # "I'm 18+" box: answer simple “Are you 18?” pages (never real ID/face/card checks)
        a.append("--confirm-adult" if form.get("confirm_adult", True) not in (False, "false", "0", 0, "") else "--no-age-click")
    if _flag(form, "audience_research") and mode == "talent" and not rebuild and not _flag(form, "no_web"):
        a.append("--audience-research")
    if not rebuild and mode == "talent":  # on by default; the boxes switch them off
        if form.get("socials", True) in (False, "false", "0", 0):
            a.append("--no-socials")
        if form.get("clips", True) in (False, "false", "0", 0):
            a.append("--no-clips")

    # Page-reading model: an empty box means "same as Model" – a leftover LLM_EXTRACT_MODEL in .env is ignored.
    a.append("--extract-model=" + _s(form, "extract_model"))
    for key, flag in (("descriptor", "--descriptor"), ("era", "--era"), ("scenario", "--scenario"),
                      ("language", "--language"), ("creator", "--creator"), ("model", "--model"),
                      ("base_url", "--base-url"),
                      ("image_base_url", "--image-base-url"), ("image_model", "--image-model"),
                      ("avatar_style", "--avatar-style"), ("marinara_user", "--marinara-user")):
        if _s(form, key):
            a += [flag, _s(form, key)]
    for key, flag, choices in (("thinking", "--thinking", {"auto", "on", "off"}),
                               ("browser", "--browser", {"auto", "off", "always", "visible"}),
                               ("style", "--style", {"roleplay", "chat"}), ("pov", "--pov", {"third", "first"}),
                               ("spec", "--spec", {"v2", "v3"}), ("json_mode", "--json-mode", {"auto", "on", "off"}),
                               ("search_provider", "--search-provider",
                                {"auto", "ddg", "searxng", "brave", "tavily", "none"})):
        v = _s(form, key)
        if v in choices:
            a += [flag, v]

    for key, flag, lo, hi in (("greetings", "--greetings", 0, 20), ("lore_entries", "--lore-entries", 1, 200),
                              ("max_pages", "--max-pages", 0, 100), ("max_rounds", "--max-rounds", 0, 20),
                              ("pages_per_round", "--pages-per-round", 1, 50), ("site_pages", "--site-pages", 0, 30),
                              ("seed_depth", "--seed-depth", 1, 3), ("seed_max_links", "--seed-max-links", 1, 100),
                              ("feed_items", "--feed-items", 10, 2000), ("podcast_episodes", "--podcast-episodes", 0, 20),
                              ("workers", "--workers", 1, 16), ("max_tokens", "--max-tokens", 512, 64_000),
                              ("sprite_expressions", "--sprite-expressions", 1, 16),
                              ("sprite_poses", "--sprite-poses", 1, 16), ("audience_pages", "--audience-pages", 1, 60),
                              ("scan_depth", "--scan-depth", 1, 20), ("token_budget", "--token-budget", 256, 32_000),
                              ("card_budget", "--card-budget", 0, 32_000), ("clip_pages", "--clip-pages", 1, 20),
                              ("clip_details", "--clip-details", 0, 200)):
        v = _int(form, key, notes, lo, hi)
        if v is not None:
            if rebuild and key in ("max_pages", "max_rounds", "pages_per_round", "site_pages", "seed_depth",
                                   "seed_max_links", "audience_pages", "clip_pages", "clip_details"):
                continue
            a += [flag, v]
    tc = _s(form, "target_coverage").rstrip("%")
    if tc and not rebuild:
        try:
            v = float(tc)
            a += ["--target-coverage", str(max(0.1, min(1.0, v / 100 if v > 1 else v)))]
        except ValueError:
            notes.append(f"Ignored target coverage “{tc}” – not a number.")

    # images from the talent folder
    refs = _files(d, "references", IMAGE_EXTS)
    if refs:
        a += ["--references", str(d / "references")]
        if _flag(form, "avatar_from_reference"):
            a.append("--avatar-from-reference")
    avatars = _files(d, "avatar", IMAGE_EXTS)
    if avatars and not (refs and _flag(form, "avatar_from_reference")):
        a += ["--avatar", str(d / "avatar" / avatars[0])]
    audio_exts = UPLOAD_KINDS["audio"][1]
    if not rebuild and _files(d, "audio", audio_exts):
        a += ["--audio", str(d / "audio")]
    if not rebuild and _flag(form, "transcribe_pages"):
        a.append("--transcribe-pages")
    for key, flag in (("reddit_client_id", "--reddit-client-id"),
                      ("whisper_model", "--whisper-model"), ("transcribe_base_url", "--transcribe-base-url"),
                      ("transcribe_model", "--transcribe-model"), ("audio_language", "--audio-language")):
        if _s(form, key):
            a += [flag, _s(form, key)]
    if _s(form, "whisper_device") in ("auto", "cpu", "cuda"):
        a += ["--whisper-device", _s(form, "whisper_device")]
    if _s(form, "transcriber") in ("auto", "local", "api", "off"):
        a += ["--transcriber", _s(form, "transcriber")]
    if _files(d, "sprites", IMAGE_EXTS) or _files(d, "sprites/full-body", IMAGE_EXTS):
        a += ["--sprites", str(d / "sprites")]

    if _flag(form, "push"):
        a += ["--push", _s(form, "push_url") or "http://127.0.0.1:7860"]

    # secrets travel by environment variable, never on the command line or in settings.json
    for key, var in (("api_key", "LLM_API_KEY"), ("image_api_key", "IMAGE_API_KEY"), ("marinara_pass", "MARINARA_PASS"),
                     ("transcribe_api_key", "TRANSCRIBE_API_KEY"), ("reddit_client_secret", "REDDIT_CLIENT_SECRET")):
        if _s(form, key):
            env[var] = _s(form, key)
    return a, env, notes


def save_env(form: dict) -> list[str]:
    """Write the model/provider boxes to .env (only the ones that are filled)."""
    mapping = {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY", "model": "LLM_MODEL",
               "extract_model": "LLM_EXTRACT_MODEL", "max_tokens": "LLM_MAX_TOKENS", "json_mode": "LLM_JSON_MODE",
               "image_base_url": "IMAGE_BASE_URL", "image_api_key": "IMAGE_API_KEY", "image_model": "IMAGE_MODEL",
               "marinara_user": "MARINARA_USER", "marinara_pass": "MARINARA_PASS",
               "transcribe_base_url": "TRANSCRIBE_BASE_URL", "transcribe_api_key": "TRANSCRIBE_API_KEY",
               "transcribe_model": "TRANSCRIBE_MODEL", "whisper_model": "WHISPER_MODEL", "whisper_device": "WHISPER_DEVICE",
               "reddit_client_id": "REDDIT_CLIENT_ID", "reddit_client_secret": "REDDIT_CLIENT_SECRET"}
    updates = {var: _s(form, key) for key, var in mapping.items() if _s(form, key)}
    if not updates:
        return []
    env_path = ROOT / ".env"
    lines = env_path.read_text("utf-8").splitlines() if env_path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    env_path.write_text("\n".join(lines) + "\n", "utf-8")
    return sorted(updates)


def _local_whisper() -> bool:
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None


def env_defaults() -> dict:
    """Non-secret values already in .env, shown as placeholders so the user knows what an empty box means."""
    vals = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return {
        "base_url": vals.get("LLM_BASE_URL", ""), "model": vals.get("LLM_MODEL", ""),
        "extract_model": vals.get("LLM_EXTRACT_MODEL", ""), "image_model": vals.get("IMAGE_MODEL", ""),
        "image_base_url": vals.get("IMAGE_BASE_URL", ""), "max_tokens": vals.get("LLM_MAX_TOKENS", ""),
        "has_api_key": bool(vals.get("LLM_API_KEY")), "has_image_key": bool(vals.get("IMAGE_API_KEY")),
        "transcribe_base_url": vals.get("TRANSCRIBE_BASE_URL", ""), "has_transcribe_key": bool(vals.get("TRANSCRIBE_API_KEY")),
        "local_whisper": _local_whisper(),
    }


# ═══════════════════════════════════════════════════════════ editing assistant, confirm sheet, budget
def working_out_dir(d: Path) -> Path | None:
    dossier = find_dossier(d)
    if dossier:
        return dossier.parent
    cands = sorted((d / "output").glob("*/*.png"), key=lambda p: p.stat().st_mtime, reverse=True) if (d / "output").exists() else []
    return cands[0].parent if cands else None


def confirm_path(d: Path) -> Path | None:
    od = working_out_dir(d)
    p = od / "confirm_sheet.json" if od else None
    return p if p and p.exists() else None


def editor_cards(d: Path) -> list[Path]:
    from .editor import card_files

    od = working_out_dir(d)
    return card_files(od) if od else []


def _editor_state_path(d: Path) -> Path:
    (d / "editor").mkdir(parents=True, exist_ok=True)
    return d / "editor" / "chat.json"


def load_editor_state(d: Path) -> dict:
    p = _editor_state_path(d)
    try:
        st = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except ValueError:
        st = {}
    st.setdefault("messages", [])
    st.setdefault("proposals", {})
    return st


def save_editor_state(d: Path, st: dict) -> None:
    st["messages"] = st["messages"][-200:]
    _editor_state_path(d).write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


def working_card(d: Path, st: dict | None = None) -> Path | None:
    st = st if st is not None else load_editor_state(d)
    if st.get("card"):
        p = (d / st["card"]).resolve()
        if d.resolve() in p.parents and p.is_file():
            return p
    cards = editor_cards(d)
    return cards[0] if cards else None


def _load(path: Path) -> tuple[dict, bytes | None]:
    from .refine import load_card

    card, image = load_card(path)
    return card.get("data", card), image


def _is_nsfw(data: dict) -> bool:
    return "nsfw" in [str(t).lower() for t in data.get("tags") or []]


def llm_from_form(form: dict):
    from .cli import load_dotenv
    from .llm import LLMClient, LLMConfig

    load_dotenv(ROOT / ".env")

    def g(key: str, env: str, default: str = "") -> str:
        return _s(form, key) or (os.environ.get(env) or "").strip() or default

    try:
        max_tokens = int(g("max_tokens", "LLM_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    return LLMClient(LLMConfig(g("base_url", "LLM_BASE_URL", "https://api.openai.com/v1"),
                               g("api_key", "LLM_API_KEY") or (os.environ.get("OPENAI_API_KEY") or ""),
                               g("model", "LLM_MODEL", "gpt-4o-mini"), max_tokens=max_tokens,
                               json_mode=g("json_mode", "LLM_JSON_MODE", "auto"), thinking=g("thinking", "LLM_THINKING", "auto")))


def editor_info(slug: str) -> dict:
    from .budget import card_tokens

    d = talent_dir(slug)
    st = load_editor_state(d)
    card = working_card(d, st)
    info: dict = {"slug": slug, "messages": st["messages"], "card": None, "versions": [], "tokens": None, "refined": None}
    for p in editor_cards(d):
        info["versions"].append({"path": str(p.relative_to(d)), "name": p.name,
                                 "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
    if card:
        data, _ = _load(card)
        info["card"] = str(card.relative_to(d))
        info["card_name"] = card.name
        info["tokens"] = card_tokens(data)
        info["version"] = data.get("character_version", "")
        stem = re.sub(r"(_v\d+(\.\d+)*)?$", "", card.stem)
        ref = card.parent / f"{stem}_refined.png"
        if ref.exists():
            rdata, _ = _load(ref)
            info["refined"] = {"path": str(ref.relative_to(d)), "name": ref.name, "tokens": card_tokens(rdata)["permanent"],
                               "stale": ref.stat().st_mtime < card.stat().st_mtime,
                               "from": ((rdata.get("extensions") or {}).get("persona_forge") or {}).get("refined", {}).get("from", "")}
    try:
        info["budget"] = int(json.loads((d / "settings.json").read_text("utf-8")).get("card_budget") or 0)
    except (ValueError, OSError):
        info["budget"] = 0
    return info


def _editor_extra(d: Path, data: dict) -> dict:
    from .synthesize import flag_excluded

    extra: dict = {}
    view = {k: v for k, v in data.items() if isinstance(v, (str, list))}
    hits = flag_excluded({k: v for k, v in view.items() if k != "character_book"}, [])
    if hits:
        extra["flagged"] = hits[:10]
    cp = confirm_path(d)
    if cp:
        try:
            sheet = json.loads(cp.read_text("utf-8"))
            open_q = [it["prompt"] + (": " + it["guess"] if it.get("guess") else "") for it in sheet.get("items", [])
                      if not it.get("answer")]
            if open_q and not sheet.get("applied"):
                extra["unanswered_questions"] = {"count": len(open_q), "first": open_q[:6]}
        except (ValueError, OSError):
            pass
    return extra


def editor_action(slug: str, action: str, body: dict) -> dict:
    from . import editor as ed

    d = talent_dir(slug)
    st = load_editor_state(d)
    form = body.get("form") or {}
    now = time.strftime("%H:%M")
    if action == "use":
        p = (d / str(body.get("card") or "")).resolve()
        if d.resolve() not in p.parents or not p.is_file():
            raise ValueError("Pick a card version from the list.")
        st["card"] = str(p.relative_to(d))
        st["messages"].append({"role": "system", "text": f"Now editing {p.name}.", "time": now})
        save_editor_state(d, st)
        return editor_info(slug)
    if action == "clear":
        st["messages"], st["proposals"] = [], {}
        save_editor_state(d, st)
        return editor_info(slug)
    card = working_card(d, st)
    if not card:
        raise ValueError("Build a card first – there's nothing to edit yet.")
    data, image = _load(card)
    nsfw = _is_nsfw(data)
    if action == "suggest":
        res = ed.suggest(llm_from_form(form), data, nsfw=nsfw, extra=_editor_extra(d, data))
        st["messages"].append({"role": "assistant", "text": res["reply"], "suggestions": res["suggestions"], "time": now})
    elif action == "ask":
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("Type what you'd like to change.")
        st["messages"].append({"role": "user", "text": text, "time": now})
        res = ed.propose(llm_from_form(form), data, text, st["messages"][:-1], nsfw=nsfw, extra=_editor_extra(d, data))
        msg = {"role": "assistant", "text": res["reply"], "summary": res["summary"], "warnings": res["warnings"],
               "time": now}
        if res["changes"]:
            pid = uuid.uuid4().hex[:10]
            st["proposals"][pid] = {"changes": res["changes"], "base": str(card.relative_to(d)), "request": text,
                                    "summary": res["summary"]}
            msg.update({"proposal": pid, "diffs": res["diffs"], "status": "proposed"})
        st["messages"].append(msg)
    elif action in ("apply", "discard"):
        pid = str(body.get("proposal") or "")
        prop = st["proposals"].get(pid)
        if not prop:
            raise ValueError("That proposal is no longer available.")
        for m in st["messages"]:
            if m.get("proposal") == pid:
                m["status"] = "applied" if action == "apply" else "discarded"
        if action == "apply":
            base = (d / prop["base"]).resolve()
            if base != card.resolve():  # someone switched versions since: apply onto the current one
                prop["note"] = "applied to the current version"
            new, changed, warnings = ed.apply_changes(data, prop["changes"], nsfw=nsfw)
            spec = (json.loads((d / "settings.json").read_text("utf-8")).get("spec") or "v2") if (d / "settings.json").exists() else "v2"
            png = ed.save_version(card.parent, new, image, note=prop["request"], fields=changed,
                                  spec=spec if spec in ("v2", "v3") else "v2")
            st["card"] = str(png.relative_to(d))
            text = f"Saved as {png.name}. The previous version is still in the list if you want to go back."
            ref = card.parent / (re.sub(r"(_v\d+(\.\d+)*)?$", "", card.stem) + "_refined.png")
            if ref.exists():
                text += " The refined copy is now out of date – press “Make refined copy” to update it."
            st["messages"].append({"role": "system", "text": text, "warnings": warnings, "time": now})
        st["proposals"].pop(pid, None)
    elif action == "refit":
        from .cli import make_refined_copy

        try:
            budget = int(body.get("budget") or 0)
        except ValueError:
            budget = 0
        if budget < 300:
            raise ValueError("Set a card token budget of at least 300.")
        stem = re.sub(r"(_v\d+(\.\d+)*)?$", "", card.stem)
        png = make_refined_copy(llm_from_form(form), {"data": data}, budget, card.parent, stem, image, nsfw=nsfw,
                                raw_name=card.name)
        settings = json.loads((d / "settings.json").read_text("utf-8")) if (d / "settings.json").exists() else {}
        settings["card_budget"] = str(budget)
        (d / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
        st["messages"].append({"role": "system", "text": f"Made {png.name} from {card.name} (budget {budget} tokens). "
                               "See refined_report.md for what was condensed.", "time": now})
    else:
        raise ValueError("unknown action")
    save_editor_state(d, st)
    return editor_info(slug)


def confirm_get(slug: str) -> dict:
    d = talent_dir(slug)
    p = confirm_path(d)
    if not p:
        return {"items": [], "missing": True}
    sheet = json.loads(p.read_text("utf-8"))
    sheet["md"] = str((p.with_name("confirm_sheet.md")).relative_to(d)) if p.with_name("confirm_sheet.md").exists() else ""
    return sheet


def confirm_save(slug: str, answers: dict) -> dict:
    d = talent_dir(slug)
    p = confirm_path(d)
    if not p:
        raise ValueError("No confirm sheet yet – build the card first (creator mode).")
    sheet = json.loads(p.read_text("utf-8"))
    for it in sheet.get("items", []):
        a = answers.get(it["id"])
        if a is None:
            continue
        if not a or not a.get("action"):
            it["answer"] = None
        else:
            it["answer"] = {"action": str(a["action"])[:20], "value": str(a.get("value") or "")[:2000]}
    p.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), "utf-8")
    from .confirm import render_md

    p.with_name("confirm_sheet.md").write_text(render_md(sheet), "utf-8")
    return confirm_get(slug)


# ═══════════════════════════════════════════════════════════ jobs
class Job:
    def __init__(self, slug: str, argv: list[str], env: dict, notes: list[str]):
        self.id = uuid.uuid4().hex[:12]
        self.slug = slug
        self.argv = argv
        self.lines: list[str] = [f"ℹ {n}" for n in notes]
        self.lines += [f"ℹ Creator folder: {talent_dir(slug)}", f"ℹ Working folder: {ROOT}",
                       f"ℹ Python: {sys.executable} ({sys.version.split()[0]})",
                       "ℹ Model settings: boxes you filled in override .env; empty boxes use .env"]
        self.status = "running"
        self.code: int | None = None
        self.started = time.time()
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.log_path = talent_dir(slug) / "runs" / f"{stamp}.log"
        self.lines.append("$ persona_forge " + " ".join(f'"{x}"' if " " in x else x for x in argv))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "persona_forge", *argv], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        with self.log_path.open("w", encoding="utf-8") as log:
            for l in self.lines:
                log.write(l + "\n")
            assert self.proc.stdout
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self.lines.append(line)
                log.write(line + "\n")
                log.flush()
            self.code = self.proc.wait()
            end = f"ℹ Finished with exit code {self.code} after {round(time.time() - self.started)}s"
            self.lines.append(end)
            log.write(end + "\n")
        if self.status != "cancelled":
            self.status = "done" if self.code == 0 else "failed"

    def cancel(self) -> None:
        if self.proc.poll() is None:
            self.status = "cancelled"
            self.proc.terminate()

    def view(self, since: int) -> dict:
        return {"id": self.id, "slug": self.slug, "status": self.status, "code": self.code,
                "log_file": str(self.log_path.relative_to(talent_dir(self.slug))),
                "rebuild": "--from-dossier" in self.argv,
                "lines": self.lines[since:], "next": len(self.lines),
                "elapsed": round(time.time() - self.started),
                "outputs": output_files(talent_dir(self.slug)) if self.status != "running" else []}


JOBS: dict[str, Job] = {}


# ═══════════════════════════════════════════════════════════ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PersonaForgeUI/1.0"

    def log_message(self, fmt, *args):  # quiet console
        pass

    # helpers
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._json({"error": msg}, status)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("file too large (25 MB max)")
        return self.rfile.read(n) if n else b""

    def _same_origin(self) -> bool:
        """Block other websites from driving this local server (CSRF)."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    # routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/talents":
                self._json({"talents": list_talents(), "root": str(TALENT_ROOT), "env": env_defaults()})
            elif u.path.startswith("/api/talent/"):
                self._json(talent_info(unquote(u.path.split("/", 3)[3])))
            elif u.path.startswith("/api/editor/"):
                self._json(editor_info(unquote(u.path.split("/")[3])))
            elif u.path.startswith("/api/confirm/"):
                self._json(confirm_get(unquote(u.path.split("/")[3])))
            elif u.path.startswith("/api/job/"):
                job = JOBS.get(u.path.split("/")[3])
                if not job:
                    return self._err("unknown job", 404)
                self._json(job.view(int(q.get("since", ["0"])[0])))
            elif u.path.startswith("/files/"):
                _, _, slug, rel = u.path.split("/", 3)
                d = talent_dir(unquote(slug))
                f = (d / unquote(rel)).resolve()
                if d.resolve() not in f.parents or not f.is_file():
                    return self._err("not found", 404)
                data = f.read_bytes()
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix in (".md", ".jsonl", ".log", ".txt"):
                    ctype = "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if "download" in q:
                    self.send_header("Content-Disposition", f'attachment; filename="{f.name}"')
                self.end_headers()
                self.wfile.write(data)
            else:
                self._err("not found", 404)
        except (ValueError, FileNotFoundError) as e:
            self._err(str(e), 404)

    def do_POST(self):
        if not self._same_origin():
            return self._err("cross-origin request refused", 403)
        u = urlparse(self.path)
        try:
            if u.path == "/api/talent":
                form = json.loads(self._body() or b"{}")
                if not _s(form, "name"):
                    return self._err("Enter a name first.")
                slug = create_talent(_s(form, "name"))
                return self._json(talent_info(slug))
            if u.path == "/api/save":
                form = json.loads(self._body() or b"{}")
                if not _s(form, "name"):
                    return self._err("Enter a name first.")
                slug = create_talent(_s(form, "name"))
                save_talent_inputs(slug, form)
                saved_env = save_env(form) if _flag(form, "save_env") else []
                return self._json({"slug": slug, "saved_env": saved_env})
            if u.path in ("/api/run", "/api/rebuild", "/api/refine"):
                form = json.loads(self._body() or b"{}")
                errors = validate(form)
                if errors:
                    return self._json({"errors": errors}, 400)
                slug = create_talent(_s(form, "name"))
                save_talent_inputs(slug, form)
                saved_env = save_env(form) if _flag(form, "save_env") else []
                if any(j.slug == slug and j.status == "running" for j in JOBS.values()):
                    return self._json({"errors": ["A run for this creator is already in progress."]}, 409)
                if u.path.endswith("refine") and not _s(form, "refine_card"):
                    return self._json({"errors": ["Pick or upload the card to refine (section 8)."]}, 400)
                argv, env, notes = build_command(slug, form, rebuild=u.path.endswith("rebuild"),
                                                 refine=_s(form, "refine_card") if u.path.endswith("refine") else "")
                if saved_env:
                    notes.append("Saved to .env: " + ", ".join(saved_env))
                job = Job(slug, argv, env, notes)
                JOBS[job.id] = job
                return self._json({"job": job.id, "slug": slug})
            if u.path.startswith("/api/editor/"):
                parts = u.path.split("/")
                if len(parts) != 5:
                    return self._err("not found", 404)
                body = json.loads(self._body() or b"{}")
                try:
                    return self._json(editor_action(unquote(parts[3]), parts[4], body))
                except ValueError as e:
                    return self._json({"errors": [str(e)]}, 400)
                except Exception as e:  # noqa: BLE001 – AI/provider errors: show them, keep the server up
                    return self._json({"errors": [f"The AI step failed: {str(e)[:400]}"]}, 502)
            if u.path.startswith("/api/confirm/") and u.path.endswith("/apply"):
                slug = unquote(u.path.split("/")[3])
                body = json.loads(self._body() or b"{}")
                if body.get("answers"):
                    confirm_save(slug, body["answers"])
                form = body.get("form") or {}
                errors = validate(form)
                if errors:
                    return self._json({"errors": errors}, 400)
                if any(j.slug == slug and j.status == "running" for j in JOBS.values()):
                    return self._json({"errors": ["A run for this creator is already in progress."]}, 409)
                sheet = confirm_path(talent_dir(slug))
                if not sheet:
                    return self._json({"errors": ["No confirm sheet yet – build the card first."]}, 400)
                save_talent_inputs(slug, form)
                argv, env, notes = build_command(slug, form, rebuild=True)
                argv += ["--confirm-sheet", str(sheet)]
                notes.append("Applying the confirm-sheet answers, then rewriting the card from the corrected research.")
                job = Job(slug, argv, env, notes)
                JOBS[job.id] = job
                return self._json({"job": job.id, "slug": slug})
            if u.path.startswith("/api/job/") and u.path.endswith("/cancel"):
                job = JOBS.get(u.path.split("/")[3])
                if job:
                    job.cancel()
                return self._json({"ok": True})
            self._err("not found", 404)
        except ValueError as e:
            self._json({"errors": [str(e)]}, 400)

    def do_PUT(self):
        if not self._same_origin():
            return self._err("cross-origin request refused", 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/api/confirm/"):
            try:
                body = json.loads(self._body() or b"{}")
                return self._json(confirm_save(unquote(u.path.split("/")[3]), body.get("answers") or {}))
            except ValueError as e:
                return self._json({"errors": [str(e)]}, 400)
        if u.path != "/api/upload":
            return self._err("not found", 404)
        try:
            kind = q.get("kind", "")
            if kind not in UPLOAD_KINDS:
                return self._err("unknown upload type")
            sub, exts = UPLOAD_KINDS[kind]
            name = Path(q.get("filename", "")).name
            ext = Path(name).suffix.lower()
            if ext not in exts:
                return self._err(f"{name}: allowed types are {', '.join(sorted(exts))}")
            stem = re.sub(r"[^\w.-]+", "_", Path(name).stem)[:80] or "file"
            d = talent_dir(q.get("talent", ""))
            target = d / sub
            target.mkdir(parents=True, exist_ok=True)
            if kind == "avatar":  # one card image at a time
                for old in target.iterdir():
                    if old.is_file():
                        old.unlink()
            n = int(self.headers.get("Content-Length") or 0)
            if n > UPLOAD_LIMITS.get(kind, MAX_UPLOAD):
                return self._err(f"{name} is too large ({n // 2**20} MB; limit {UPLOAD_LIMITS.get(kind, MAX_UPLOAD) // 2**20} MB)")
            dest = target / f"{stem}{ext}"
            with dest.open("wb") as fh:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    fh.write(chunk)
                    left -= len(chunk)
            self._json(talent_info(d.name))
        except ValueError as e:
            self._err(str(e))

    def do_DELETE(self):
        if not self._same_origin():
            return self._err("cross-origin request refused", 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            kind = q.get("kind", "")
            if u.path != "/api/upload" or kind not in UPLOAD_KINDS:
                return self._err("not found", 404)
            d = talent_dir(q.get("talent", ""))
            f = (d / UPLOAD_KINDS[kind][0] / Path(q.get("filename", "")).name).resolve()
            if d.resolve() in f.parents and f.is_file():
                f.unlink()
            self._json(talent_info(d.name))
        except ValueError as e:
            self._err(str(e))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="persona_forge.webui", description="Local web UI for Persona Forge")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    args = p.parse_args(argv)
    ensure_talent_root()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{srv.server_port}"
    print(f"Persona Forge web UI running at {url}  (Ctrl+C to stop)")
    print(f"Creator folders: {TALENT_ROOT}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        for j in JOBS.values():
            j.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
