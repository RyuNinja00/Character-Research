"""Small shared helpers: logging, caching, text cleanup, JSON repair."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_VERBOSE = False


def set_verbose(value: bool) -> None:
    global _VERBOSE
    _VERBOSE = value


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def debug(msg: str) -> None:
    if _VERBOSE:
        print(f"  · {msg}", file=sys.stderr, flush=True)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    return re.sub(r"[\s_-]+", "_", text) or "character"


def collapse_ws(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int, overlap: int = 300) -> list[str]:
    """Split text into chunks of ~size chars, preferring paragraph boundaries."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(". ", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class DiskCache:
    """Tiny JSON-on-disk cache so re-runs don't re-download pages or re-pay for LLM calls."""

    def __init__(self, root: Path | None, ttl_seconds: int = 7 * 24 * 3600):
        self.root = root
        self.ttl = ttl_seconds
        if root:
            root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path | None:
        if not self.root:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{digest}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        path = self._path(namespace, key)
        if not path or not path.exists():
            return None
        if self.ttl and time.time() - path.stat().st_mtime > self.ttl:
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self, namespace: str, key: str) -> None:
        path = self._path(namespace, key)
        if path and path.exists():
            path.unlink(missing_ok=True)

    def set(self, namespace: str, key: str, value: Any) -> None:
        path = self._path(namespace, key)
        if not path:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)


# ── JSON extraction / repair for LLM output ─────────────────────────────

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _balanced_slice(text: str, open_ch: str, close_ch: str) -> str | None:
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]  # truncated output – let the repair step try


def _repair(candidate: str) -> str:
    s = candidate.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing commas
    s = s.replace("“", '"').replace("”", '"')
    # close unbalanced brackets from truncated output
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'
    s = re.sub(r",\s*$", "", s)
    return s + "".join(reversed(stack))


def _escape_inner_quotes(s: str) -> str:
    """Escape quote marks that sit *inside* a string value (e.g. dialogue: "she said "hi" to me").
    A quote only ends a string if the next non-space character is , } ] : or the end of the text."""
    out = []
    in_str = False
    esc = False
    n = len(s)
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j >= n or s[j] in ",}]:":
                    in_str = False
                else:
                    out.append("\\")
            elif ch in "\n\r":
                out.append("\\n" if ch == "\n" else "")
                continue
        elif ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def salvage_json_items(text: str, keys: tuple[str, ...] = ("facts", "entries", "insights")) -> dict | None:
    """Last resort for a cut-off or partly broken reply: keep every complete, valid object in the main list
    (e.g. "facts": [...]) and drop only the broken ones."""
    result: dict = {}
    for key in keys:
        m = re.search(r'"%s"\s*:\s*\[' % key, text)
        if not m:
            continue
        items, i, n = [], m.end(), len(text)
        while i < n:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i >= n or text[i] == "]":
                break
            if text[i] != "{":
                i += 1
                continue
            obj = _balanced_slice(text[i:], "{", "}")
            if not obj:
                break
            complete = obj.endswith("}")
            for attempt in (obj, _escape_inner_quotes(obj)):
                try:
                    v = json.loads(attempt)
                    if isinstance(v, dict) and complete:
                        items.append(v)
                    break
                except json.JSONDecodeError:
                    continue
            i += len(obj)
        if items:
            result[key] = items
    if not result:
        return None
    for flag in ("relevant",):
        fm = re.search(r'"%s"\s*:\s*(true|false)' % flag, text)
        if fm:
            result[flag] = fm.group(1) == "true"
    am = re.search(r'"aliases"\s*:\s*(\[[^\]]*\])', text)
    if am:
        try:
            result["aliases"] = json.loads(am.group(1))
        except json.JSONDecodeError:
            pass
    result["_salvaged"] = True
    return result


def parse_json_loose(text: str) -> Any:
    """Parse JSON from an LLM reply that may include prose, code fences or truncation."""
    if text is None:
        raise ValueError("empty LLM reply")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    for c in candidates:
        c = c.strip()
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
        brackets = [("{", "}"), ("[", "]")] if c.lstrip()[:1] != "[" else [("[", "]"), ("{", "}")]
        if "{" in c and c.lstrip()[:1] != "[":
            brackets = [("{", "}")]  # an object reply: never fall back to some inner list
        for open_ch, close_ch in brackets:
            sl = _balanced_slice(c, open_ch, close_ch)
            if not sl:
                continue
            fixed_quotes = _escape_inner_quotes(sl)
            for attempt in (sl, fixed_quotes):
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    continue
            # Cut-off reply: keep only complete items from the main list; closing the brackets would keep a
            # half-written last item.
            salvaged = salvage_json_items(c)
            if salvaged:
                return salvaged
            for attempt in (_repair(sl), _repair(fixed_quotes)):
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    continue
    salvaged = salvage_json_items(text)
    if salvaged:
        return salvaged
    raise ValueError(f"could not parse JSON from LLM reply: {text[:300]!r}")
