"""Minimal OpenAI-compatible chat client (works with OpenAI, OpenRouter, Anthropic's
OpenAI-compatible endpoint, DeepSeek, Ollama, LM Studio, KoboldCpp, llama.cpp server, etc.)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from .util import DiskCache, debug, parse_json_loose


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.6
    max_tokens: int = 4096
    timeout: int = 300
    json_mode: str = "auto"  # auto | on | off


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig, cache: DiskCache | None = None):
        self.cfg = cfg
        self.cache = cache
        self._json_mode_supported = cfg.json_mode != "off"
        self.session = requests.Session()
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    # ----------------------------------------------------------------- raw
    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        want_json: bool = False,
    ) -> str:
        temperature = self.cfg.temperature if temperature is None else temperature
        max_tokens = max_tokens or self.cfg.max_tokens
        cache_key = json.dumps([self.cfg.model, system, user, temperature, max_tokens, want_json])
        if self.cache:
            hit = self.cache.get("llm", cache_key)
            if hit is not None:
                debug("LLM cache hit")
                return hit

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if want_json and self._json_mode_supported:
            payload["response_format"] = {"type": "json_object"}

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
            # Anthropic's OpenAI-compatible endpoint also accepts x-api-key
            if "anthropic.com" in url:
                headers["x-api-key"] = self.cfg.api_key

        last_err: Exception | None = None
        for attempt in range(5):
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 400 and "response_format" in payload and self.cfg.json_mode == "auto":
                debug("endpoint rejected response_format; retrying without JSON mode")
                self._json_mode_supported = False
                payload.pop("response_format", None)
                continue
            if resp.status_code in (429, 500, 502, 503, 504, 529):
                wait = float(resp.headers.get("retry-after") or 0) or 3 * (attempt + 1)
                debug(f"LLM HTTP {resp.status_code}; retrying in {wait:.0f}s")
                time.sleep(min(wait, 60))
                last_err = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                continue
            if resp.status_code >= 400:
                raise LLMError(f"LLM request failed (HTTP {resp.status_code}): {resp.text[:500]}")
            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"Unexpected LLM response: {str(data)[:500]}") from e
            usage = data.get("usage") or {}
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            self.calls += 1
            if self.cache:
                self.cache.set("llm", cache_key, content)
            return content
        raise LLMError(f"LLM request failed after retries: {last_err}")

    # ---------------------------------------------------------------- json
    def chat_json(self, system: str, user: str, **kw: Any) -> Any:
        system = system + "\n\nRespond with ONLY a single valid JSON object. No markdown, no commentary."
        text = self.chat(system, user, want_json=True, **kw)
        try:
            return parse_json_loose(text)
        except ValueError:
            debug("JSON parse failed; asking the model to fix its output")
            fix = self.chat(
                "You repair malformed JSON. Output only the corrected JSON object.",
                f"Fix this so it is valid JSON, keeping all content:\n\n{text}",
                temperature=0,
                want_json=True,
            )
            return parse_json_loose(fix)
