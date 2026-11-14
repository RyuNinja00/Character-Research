"""Audio → text: interviews, podcasts, stream clips and voice notes become sources.

Engines (chosen with --transcriber, default auto):
* local – faster-whisper on this computer (pip install faster-whisper). Free and private: audio never leaves
          the machine. Handles long files on its own.
* api   – any OpenAI-compatible /audio/transcriptions endpoint (OpenAI whisper-1 / gpt-4o-transcribe, Groq
          whisper-large-v3, a local whisper server…). Files over ~24 MB are split with ffmpeg first.
* auto  – local if faster-whisper is installed, otherwise the API if one is configured.

Transcripts are cached (re-runs are free), saved next to the run's output, and handed to the fact extractor
with a note that speakers aren't labelled, so only statements clearly from the person are attributed to them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .util import log

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".webm", ".flac", ".aac", ".mp4", ".m4b",
              ".mkv", ".mov", ".wma"}
AUDIO_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:mp3|m4a|aac|ogg|oga|opus|wav|flac|m4b)(?:\?[^\s\"'<>]*)?", re.I)
API_MAX_BYTES = 24 * 1024 * 1024
MAX_DOWNLOAD = 700 * 1024 * 1024


class TranscriptionUnavailable(RuntimeError):
    pass


@dataclass
class TranscribeConfig:
    engine: str = "auto"            # auto | local | api | off
    whisper_model: str = "small"    # local: tiny, base, small, medium, large-v3
    base_url: str = ""              # api
    api_key: str = ""
    model: str = "whisper-1"
    language: str = ""              # e.g. "en"; empty = detect


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Transcriber:
    def __init__(self, cfg: TranscribeConfig, cache_dir: Path | None = None, api_max_bytes: int = API_MAX_BYTES):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.api_max_bytes = api_max_bytes
        self._local_model = None
        self.engine = self._resolve()

    # ── which engine
    @staticmethod
    def local_available() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _resolve(self) -> str:
        e = self.cfg.engine
        if e == "off":
            return "off"
        if e in ("auto", "local") and self.local_available():
            return "local"
        if e == "local":
            raise TranscriptionUnavailable("local transcription needs faster-whisper: pip install faster-whisper")
        if self.cfg.base_url:
            return "api"
        raise TranscriptionUnavailable(
            "no transcription engine. Either install the free, private local engine: pip install faster-whisper – "
            "or set TRANSCRIBE_BASE_URL / TRANSCRIBE_API_KEY (e.g. OpenAI https://api.openai.com/v1, or Groq "
            "https://api.groq.com/openai/v1 with TRANSCRIBE_MODEL=whisper-large-v3). Featherless has no audio models.")

    # ── public
    def transcribe(self, path: Path) -> str:
        path = Path(path)
        key = hashlib.sha256(f"{path.name}|{path.stat().st_size}|{self.engine}|{self.cfg.whisper_model}|"
                             f"{self.cfg.model}|{self.cfg.language}".encode()).hexdigest()[:24]
        hit = self.cache_dir / "transcripts" / f"{key}.txt" if self.cache_dir else None
        if hit and hit.exists():
            log(f"  … transcript of {path.name} from cache")
            return hit.read_text("utf-8")
        log(f"→ Transcribing {path.name} ({path.stat().st_size / 1e6:.1f} MB, {self.engine}"
            + (f" Whisper {self.cfg.whisper_model}" if self.engine == "local" else f" {self.cfg.model}") + ")…")
        text = self._local(path) if self.engine == "local" else self._api(path)
        if hit and text.strip():
            hit.parent.mkdir(parents=True, exist_ok=True)
            hit.write_text(text, "utf-8")
        return text

    # ── local faster-whisper
    def _local(self, path: Path) -> str:
        from faster_whisper import WhisperModel  # type: ignore

        if self._local_model is None:
            log(f"  … loading Whisper “{self.cfg.whisper_model}” (first time downloads the model)")
            self._local_model = WhisperModel(self.cfg.whisper_model, device="auto", compute_type="auto")
        segments, info = self._local_model.transcribe(str(path), language=self.cfg.language or None, vad_filter=True)
        duration = float(getattr(info, "duration", 0) or 0)
        lines, next_report, block, block_start = [], 300.0, [], None
        for seg in segments:
            if block_start is None:
                block_start = seg.start
            block.append(seg.text.strip())
            if seg.end - block_start >= 45:  # a timestamped paragraph about every 45 s
                lines.append(f"[{_fmt(block_start)}] " + " ".join(block))
                block, block_start = [], None
            if seg.end >= next_report:
                log(f"  … transcribed {_fmt(seg.end)}" + (f" / {_fmt(duration)}" if duration else ""))
                next_report += 300
        if block:
            lines.append(f"[{_fmt(block_start or 0)}] " + " ".join(block))
        return "\n\n".join(lines)

    # ── OpenAI-compatible API
    def _api(self, path: Path) -> str:
        parts = [path]
        tmp = None
        if path.stat().st_size > self.api_max_bytes:
            if not shutil.which("ffmpeg"):
                raise TranscriptionUnavailable(
                    f"{path.name} is larger than the API's ~25 MB limit and ffmpeg isn't installed to split it. "
                    "Install ffmpeg, or use the local engine (pip install faster-whisper).")
            tmp = Path(tempfile.mkdtemp(prefix="pf_audio_"))
            seconds = self._chunk_seconds(path)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                            "-b:a", "48k", "-f", "segment", "-segment_time", str(seconds), str(tmp / "part%03d.mp3")],
                           check=True)
            parts = sorted(tmp.glob("part*.mp3"))
            log(f"  … split into {len(parts)} parts for the API")
        out = []
        try:
            for i, part in enumerate(parts):
                with part.open("rb") as fh:
                    resp = requests.post(
                        self.cfg.base_url.rstrip("/") + "/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {},
                        data={"model": self.cfg.model, "response_format": "text",
                              **({"language": self.cfg.language} if self.cfg.language else {})},
                        files={"file": (part.name, fh)},
                        timeout=900,
                    )
                if resp.status_code >= 400:
                    raise RuntimeError(f"transcription API error (HTTP {resp.status_code}): {resp.text[:300]}")
                text = resp.text
                if resp.headers.get("content-type", "").startswith("application/json"):
                    try:
                        text = json.loads(text).get("text", text)
                    except ValueError:
                        pass
                out.append(text.strip())
                if len(parts) > 1:
                    log(f"  … part {i + 1}/{len(parts)} done")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        return "\n\n".join(t for t in out if t)

    def _chunk_seconds(self, path: Path) -> int:
        """Segment length that keeps each 48 kbps mono part under the API limit (~10 min normally)."""
        bytes_per_sec = 48_000 / 8
        return max(30, min(600, int(self.api_max_bytes * 0.9 / bytes_per_sec)))


# ═══════════════════════════════════════════════════════════ finding and downloading audio
def find_page_audio(html: str, base_url: str) -> list[str]:
    """Audio files offered by a page: podcast players, <audio>, og:audio, download links, Spreaker episodes."""
    from bs4 import BeautifulSoup

    found: list[str] = []

    def add(u: str | None) -> None:
        if u:
            u = urljoin(base_url, u.strip())
            if u.startswith("http") and u not in found:
                found.append(u)

    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    for prop in ("og:audio", "og:audio:url", "og:audio:secure_url", "twitter:player:stream"):
        for tag in soup.find_all("meta", attrs={"property": prop}) + soup.find_all("meta", attrs={"name": prop}):
            add(tag.get("content"))
    for tag in soup.find_all(["audio", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src and (tag.name == "audio" or "audio" in (tag.get("type") or "") or AUDIO_URL_RE.search(urljoin(base_url, src))):
            add(src)
    for a in soup.find_all("a", href=True):
        if AUDIO_URL_RE.fullmatch(urljoin(base_url, a["href"]).split("#")[0]):
            add(a["href"])
    for m in AUDIO_URL_RE.finditer(html[:2_000_000].replace("\\/", "/")):
        add(m.group(0))
    m = re.search(r"spreaker\.com/episode/[^/?#]*?(\d{6,})", base_url)
    if m:  # Spreaker episode pages load their player by script; the episode's public audio link
        add(f"https://api.spreaker.com/v2/episodes/{m.group(1)}/download.mp3")
    return found[:5]


def download_audio(url: str, dest_dir: Path, session: requests.Session | None = None) -> Path | None:
    from .research import BROWSER_HEADERS

    http = session or requests.Session()
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with http.get(url, headers=BROWSER_HEADERS, stream=True, timeout=60, allow_redirects=True) as r:
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400 or not (ctype.startswith(("audio/", "video/")) or "octet-stream" in ctype
                                            or AUDIO_URL_RE.search(r.url)):
                log(f"  ! {url}: not downloadable audio (HTTP {r.status_code}, {ctype or 'unknown type'})")
                return None
            ext = Path(urlparse(r.url).path).suffix.lower()
            if ext not in AUDIO_EXTS:
                ext = {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/ogg": ".ogg",
                       "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac", "audio/webm": ".webm",
                       "audio/flac": ".flac"}.get(ctype.split(";")[0], ".mp3")
            name = re.sub(r"[^\w.-]+", "_", Path(urlparse(url).path).stem)[:60] or "audio"
            target = dest_dir / f"{name}_{hashlib.sha1(url.encode()).hexdigest()[:8]}{ext}"
            if target.exists() and target.stat().st_size > 0:
                return target
            size = 0
            with target.open("wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        fh.close()
                        target.unlink(missing_ok=True)
                        log(f"  ! {url}: audio is over {MAX_DOWNLOAD // 2**20} MB – skipped")
                        return None
                    fh.write(chunk)
            log(f"  … downloaded {target.name} ({size / 1e6:.1f} MB)")
            return target
    except requests.RequestException as e:
        log(f"  ! {url}: audio download failed ({e.__class__.__name__})")
        return None


def transcript_source_text(name: str, transcript: str, person: str, origin: str = "") -> str:
    return (f"[Automatic transcript of audio: {name}{(' – from ' + origin) if origin else ''}. Speakers are NOT "
            f"labelled: it may contain an interviewer and other people. Attribute only statements that clearly come "
            f"from {person} (answers about their own life, opinions, feelings) to them.]\n\n{transcript}")
