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
}
SECRET_FIELDS = {"api_key", "image_api_key", "marinara_pass"}
MAX_UPLOAD = 25 * 1024 * 1024

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
    for sub in ("references", "sprites/full-body", "avatar", "sources", "output", "runs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    if not (d / "brief.md").exists():
        (d / "brief.md").write_text(_template("creator_brief_template.md").replace("<Stage name>", name), "utf-8")
    if not (d / "limits.txt").exists():
        (d / "limits.txt").write_text(_template("boundaries_template.txt"), "utf-8")
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
        "files": {k: _files(d, sub, exts) for k, (sub, exts) in UPLOAD_KINDS.items()},
        "outputs": output_files(d),
        "has_dossier": bool(find_dossier(d)),
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
    settings = {k: v for k, v in form.items() if k not in SECRET_FIELDS and k not in ("brief", "limits", "urls")}
    (d / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    if "brief" in form:
        (d / "brief.md").write_text(form.get("brief") or "", "utf-8")
    if "limits" in form:
        (d / "limits.txt").write_text(form.get("limits") or "", "utf-8")
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


def build_command(slug: str, form: dict, rebuild: bool = False) -> tuple[list[str], dict, list[str]]:
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
        for h in _lines(form.get("handles", "")):
            a += ["--handle", h.lstrip("@")]
        if mode in ("talent", "fictional") and not (
            _lines(form.get("seeds", "")) or urls or _meaningful_brief(brief) or _files(d, "sources", TEXT_EXTS)
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
                      ("no_image_prompt", "--no-image-prompt")):
        if _flag(form, key) and flag not in a:
            if rebuild and key in ("deep", "search_web", "no_web"):
                continue
            a.append(flag)
    if _flag(form, "audience_research") and mode == "talent" and not rebuild and not _flag(form, "no_web"):
        a.append("--audience-research")

    # Page-reading model: an empty box means "same as Model" – a leftover LLM_EXTRACT_MODEL in .env is ignored.
    a.append("--extract-model=" + _s(form, "extract_model"))
    for key, flag in (("descriptor", "--descriptor"), ("era", "--era"), ("scenario", "--scenario"),
                      ("language", "--language"), ("creator", "--creator"), ("model", "--model"),
                      ("base_url", "--base-url"),
                      ("image_base_url", "--image-base-url"), ("image_model", "--image-model"),
                      ("avatar_style", "--avatar-style"), ("marinara_user", "--marinara-user")):
        if _s(form, key):
            a += [flag, _s(form, key)]
    for key, flag, choices in (("style", "--style", {"roleplay", "chat"}), ("pov", "--pov", {"third", "first"}),
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
                              ("workers", "--workers", 1, 16), ("max_tokens", "--max-tokens", 512, 64_000),
                              ("sprite_expressions", "--sprite-expressions", 1, 16),
                              ("sprite_poses", "--sprite-poses", 1, 16), ("audience_pages", "--audience-pages", 1, 60),
                              ("scan_depth", "--scan-depth", 1, 20), ("token_budget", "--token-budget", 256, 32_000)):
        v = _int(form, key, notes, lo, hi)
        if v is not None:
            if rebuild and key in ("max_pages", "max_rounds", "pages_per_round", "site_pages", "seed_depth",
                                   "seed_max_links", "audience_pages"):
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
    if _files(d, "sprites", IMAGE_EXTS) or _files(d, "sprites/full-body", IMAGE_EXTS):
        a += ["--sprites", str(d / "sprites")]

    if _flag(form, "push"):
        a += ["--push", _s(form, "push_url") or "http://127.0.0.1:7860"]

    # secrets travel by environment variable, never on the command line or in settings.json
    for key, var in (("api_key", "LLM_API_KEY"), ("image_api_key", "IMAGE_API_KEY"), ("marinara_pass", "MARINARA_PASS")):
        if _s(form, key):
            env[var] = _s(form, key)
    return a, env, notes


def save_env(form: dict) -> list[str]:
    """Write the model/provider boxes to .env (only the ones that are filled)."""
    mapping = {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY", "model": "LLM_MODEL",
               "extract_model": "LLM_EXTRACT_MODEL", "max_tokens": "LLM_MAX_TOKENS", "json_mode": "LLM_JSON_MODE",
               "image_base_url": "IMAGE_BASE_URL", "image_api_key": "IMAGE_API_KEY", "image_model": "IMAGE_MODEL",
               "marinara_user": "MARINARA_USER", "marinara_pass": "MARINARA_PASS"}
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
    }


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
            if u.path in ("/api/run", "/api/rebuild"):
                form = json.loads(self._body() or b"{}")
                errors = validate(form)
                if errors:
                    return self._json({"errors": errors}, 400)
                slug = create_talent(_s(form, "name"))
                save_talent_inputs(slug, form)
                saved_env = save_env(form) if _flag(form, "save_env") else []
                if any(j.slug == slug and j.status == "running" for j in JOBS.values()):
                    return self._json({"errors": ["A run for this creator is already in progress."]}, 409)
                argv, env, notes = build_command(slug, form, rebuild=u.path.endswith("rebuild"))
                if saved_env:
                    notes.append("Saved to .env: " + ", ".join(saved_env))
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
            data = self._body()
            (target / f"{stem}{ext}").write_bytes(data)
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
