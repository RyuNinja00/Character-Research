"""Minimal OpenAI-compatible chat client (works with OpenAI, OpenRouter, Anthropic's
OpenAI-compatible endpoint, DeepSeek, Ollama, LM Studio, KoboldCpp, llama.cpp server, etc.)."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from .util import DiskCache, debug, log, parse_json_loose


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.6
    max_tokens: int = 4096
    timeout: int = 300
    json_mode: str = "auto"  # auto | on | off
    thinking: str = "auto"   # auto | on | off – "off"/"auto" ask reasoning models (Qwen3…) to answer directly


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig, cache: DiskCache | None = None, fallback_model: str = ""):
        self.cfg = cfg
        self.fallback_model = fallback_model
        self.cache = cache
        self._json_mode_supported = cfg.json_mode != "off"
        self._tokens_param = "max_tokens"      # newer OpenAI models want max_completion_tokens
        self._send_temperature = True          # reasoning models only accept the default temperature
        self._send_template_kwargs = True      # vLLM-style chat_template_kwargs (enable_thinking)
        self._tl = threading.local()           # last reply per thread, for diagnostics
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

        no_think = self.no_think()

        def build_payload() -> dict[str, Any]:
            pl: dict[str, Any] = {
                "model": self.cfg.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user + ("\n\n/no_think" if no_think else "")},
                ],
                self._tokens_param: max_tokens,
            }
            if no_think and self._send_template_kwargs:
                pl["chat_template_kwargs"] = {"enable_thinking": False}
            if self._send_temperature:
                pl["temperature"] = temperature
            if want_json and self._json_mode_supported:
                pl["response_format"] = {"type": "json_object"}
            return pl

        payload = build_payload()

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
            # Anthropic's OpenAI-compatible endpoint also accepts x-api-key
            if "anthropic.com" in url:
                headers["x-api-key"] = self.cfg.api_key

        last_err: Exception | None = None
        for attempt in range(7):
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code in (400, 404, 422) and self.fallback_model and self.cfg.model != self.fallback_model:
                err = resp.text.lower()
                if "model" in err and any(w in err for w in ("not found", "invalid", "does not exist", "unknown",
                                                             "not exist", "unsupported", "no such", "not available")):
                    log(f"  ! The page-reading model “{self.cfg.model}” was rejected by the provider – using the main "
                        f"model “{self.fallback_model}” instead. (Check LLM_EXTRACT_MODEL in .env.)")
                    self.cfg.model = self.fallback_model
                    payload = build_payload()
                    continue
            if resp.status_code in (400, 422) and "chat_template_kwargs" in payload and (
                    "chat_template" in resp.text.lower() or "extra" in resp.text.lower() or "unrecognized" in resp.text.lower()):
                self._send_template_kwargs = False
                payload = build_payload()
                continue
            if resp.status_code == 400:
                err = resp.text.lower()
                if "max_tokens" in err and self._tokens_param == "max_tokens":
                    debug("endpoint wants max_completion_tokens; retrying")
                    self._tokens_param = "max_completion_tokens"
                    payload = build_payload()
                    continue
                if "temperature" in err and self._send_temperature:
                    debug("endpoint rejected temperature; retrying with the default")
                    self._send_temperature = False
                    payload = build_payload()
                    continue
                if "response_format" in payload and self.cfg.json_mode == "auto":
                    debug("endpoint rejected response_format; retrying without JSON mode")
                    self._json_mode_supported = False
                    payload = build_payload()
                    continue
            if resp.status_code in (429, 500, 502, 503, 504, 529):
                wait = float(resp.headers.get("retry-after") or 0) or 3 * (attempt + 1)
                debug(f"LLM HTTP {resp.status_code}; retrying in {wait:.0f}s")
                time.sleep(min(wait, 60))
                last_err = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                continue
            if resp.status_code >= 400:
                hint = {401: " – check the API key", 403: " – the key isn't allowed to use this model",
                        404: " – check the base URL and the model name"}.get(resp.status_code, "")
                raise LLMError(f"LLM request failed (HTTP {resp.status_code}{hint}) for model “{self.cfg.model}”: "
                               f"{resp.text[:400]}")
            data = resp.json()
            try:
                choice = data["choices"][0]
                msg = choice["message"]
                content = msg.get("content") or ""
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"Unexpected LLM response: {str(data)[:500]}") from e
            finish = choice.get("finish_reason") or ""
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            thinking_only = ("<think>" in content and "</think>" not in content) or (not content.strip() and bool(reasoning))
            self._tl.last = {"content": content, "finish": finish, "thinking_only": thinking_only,
                             "reasoning_chars": len(reasoning) + (len(content) if thinking_only else 0)}
            usage = data.get("usage") or {}
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            self.calls += 1
            if self.cache and finish != "length" and not thinking_only and content.strip():
                self.cache.set("llm", cache_key, content)  # never cache cut-off or empty replies
            return content
        raise LLMError(f"LLM request failed after retries: {last_err}")

    # ---------------------------------------------------------------- json
    def _forget(self, system: str, user: str, temperature: float | None, max_tokens: int | None, want_json: bool) -> None:
        """Drop a cached reply that turned out to be unusable, so a re-run asks the model again."""
        if self.cache:
            temperature = self.cfg.temperature if temperature is None else temperature
            key = json.dumps([self.cfg.model, system, user, temperature, max_tokens or self.cfg.max_tokens, want_json])
            self.cache.delete("llm", key)

    def no_think(self) -> bool:
        """Ask hybrid reasoning models to skip their visible "thinking" (it can use up the whole reply)."""
        if self.cfg.thinking == "off":
            return True
        if self.cfg.thinking == "on":
            return False
        return bool(re.search(r"qwen-?3|qwq|glm-?4\.[5-9]|glm-?5|hermes-?4|deepseek-?v3\.[1-9]", self.cfg.model, re.I))

    def last_reply_preview(self) -> str:
        """One line describing the last reply on this thread – shown in the console when a step fails."""
        last = getattr(self._tl, "last", None)
        if not last:
            return ""
        c = last["content"]
        bits = []
        if last["thinking_only"]:
            bits.append(f"the model only 'thought' ({last['reasoning_chars']:,} chars) and never gave an answer")
        if last["finish"] == "length":
            bits.append("cut off at the reply length limit")
        visible = re.sub(r"\s+", " ", re.sub(r"<think>.*?(</think>|$)", "[thinking…] ", c, flags=re.S)).strip()
        bits.append(f"reply ({len(c):,} chars) began: {visible[:160]!r}" if visible else "reply was empty")
        return "; ".join(bits)

    def chat_json(self, system: str, user: str, **kw: Any) -> Any:
        system = system + "\n\nRespond with ONLY a single valid JSON object. No markdown, no commentary."
        text = self.chat(system, user, want_json=True, **kw)
        last = getattr(self._tl, "last", {}) or {}
        if last.get("thinking_only") or (not text.strip()):
            raise LLMError("the model used its whole reply on 'thinking' and gave no answer – raise “Max reply "
                           "tokens”, or set Thinking → Off" if last.get("thinking_only") else "the model sent an empty reply")
        try:
            return parse_json_loose(text)
        except ValueError:
            self._forget(system, user, kw.get("temperature"), kw.get("max_tokens"), True)
            if looks_like_refusal(text):
                raise LLMRefusal(
                    f"The model ({self.cfg.model}) refused this step: {text.strip()[:160]!r}. "
                    "Mainstream models often refuse adult content – use a model/provider that allows it for --nsfw "
                    "cards (e.g. via OpenRouter or a local model), or try a different --model."
                )
            if len(text) > 12_000:
                raise ValueError("the reply was too long and was cut off before it ended")
            debug("JSON parse failed; asking the model to fix its output")
            fix = self.chat(
                "You repair malformed JSON. Output only the corrected JSON object.",
                f"Fix this so it is valid JSON, keeping all content:\n\n{text}",
                temperature=0,
                want_json=True,
            )
            return parse_json_loose(fix)


class LLMRefusal(LLMError):
    pass


_REFUSAL_RE = None


def looks_like_refusal(text: str) -> bool:
    global _REFUSAL_RE
    import re

    if _REFUSAL_RE is None:
        _REFUSAL_RE = re.compile(
            r"^\W*(i'?m sorry|i am sorry|sorry,|i can(?:'|no)t|i cannot|i won'?t|i'?m (?:not able|unable)|"
            r"i am (?:not able|unable)|as an ai|i must decline|unfortunately,? i)",
            re.I,
        )
    return bool(text) and "{" not in text[:200] and bool(_REFUSAL_RE.search(text.strip()))
