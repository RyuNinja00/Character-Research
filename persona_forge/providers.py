"""AI provider presets and model listing for the web UI's provider dropdown and "Browse models" picker.

NanoGPT (nano-gpt.com) is a pay-per-use, OpenAI-compatible provider. One key covers:
* the AI model (chat completions)            https://api.nano-gpt.com/api/v1
* portrait images (/images/generations)       same base URL
* audio/video transcription (/audio/transcriptions, e.g. Whisper-Large-V3)
* web search (POST /api/web)                  used by --search-provider nanogpt

Its model list (/models?detailed=true) includes prices per million tokens, context length and capabilities, so the
picker can show what each model costs. /api/subscription/v1/models lists only the models included in a NanoGPT
subscription. The api.nano-gpt.com host allows longer requests than the website host, which suits card writing.

Other providers' /models lists are read the same way (OpenRouter's per-token prices are converted to per-million).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import requests

NANOGPT_API = "https://api.nano-gpt.com/api/v1"
NANOGPT_SUBSCRIPTION_MODELS = "https://api.nano-gpt.com/api/subscription/v1/models"
NANOGPT_WEB_SEARCH = "https://api.nano-gpt.com/api/web"


@dataclass
class Provider:
    key: str
    label: str
    base_url: str
    key_hint: str = ""
    image_base_url: str = ""
    image_model: str = ""
    transcribe_base_url: str = ""
    transcribe_model: str = ""
    transcribe_models: tuple[str, ...] = ()
    web_search: bool = False
    subscription: bool = False
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "nanogpt": Provider(
        "nanogpt", "NanoGPT", NANOGPT_API, key_hint="Create a key at nano-gpt.com/api",
        image_base_url=NANOGPT_API, transcribe_base_url=NANOGPT_API, transcribe_model="Whisper-Large-V3",
        transcribe_models=("Whisper-Large-V3", "openai-whisper-with-video"), web_search=True, subscription=True,
        note="One key for the AI model, portraits, transcription and web search. Prices are shown per million tokens."),
    "openrouter": Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", key_hint="openrouter.ai/keys"),
    "featherless": Provider("featherless", "Featherless", "https://api.featherless.ai/v1",
                            key_hint="featherless.ai account page", note="Set Parallel requests to your plan's limit."),
    "openai": Provider("openai", "OpenAI", "https://api.openai.com/v1", key_hint="platform.openai.com/api-keys",
                       image_base_url="https://api.openai.com/v1", image_model="gpt-image-1",
                       transcribe_base_url="https://api.openai.com/v1", transcribe_model="whisper-1",
                       transcribe_models=("whisper-1", "gpt-4o-transcribe")),
    "ollama": Provider("ollama", "Ollama (on this PC)", "http://localhost:11434/v1", note="Set Parallel requests to 1."),
}


def provider_for(base_url: str) -> str:
    """The preset a base URL belongs to ('' for anything else)."""
    u = (base_url or "").lower()
    if "nano-gpt.com" in u or "nanogpt" in u:
        return "nanogpt"
    for k, p in PROVIDERS.items():
        host = p.base_url.split("//", 1)[-1].split("/", 1)[0]
        if host and host in u:
            return k
    return ""


def presets() -> list[dict]:
    return [asdict(p) | {"transcribe_models": list(p.transcribe_models)} for p in PROVIDERS.values()]


# ═══════════════════════════════════════════════════════════ model lists
def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def normalize_model(m: dict, *, per_token_prices: bool = False) -> dict:
    """One row for the picker, whatever the provider's list looks like."""
    pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
    pin, pout = _num(pricing.get("prompt")), _num(pricing.get("completion"))
    unit = str(pricing.get("unit") or "")
    if per_token_prices or (unit != "per_million_tokens" and ((pin and pin < 0.001) or (pout and pout < 0.001))):
        pin = pin * 1_000_000 if pin is not None else None  # OpenRouter-style USD per token
        pout = pout * 1_000_000 if pout is not None else None
    caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    params = m.get("supported_parameters") or []
    return {
        "id": str(m.get("id") or ""),
        "name": str(m.get("name") or m.get("id") or ""),
        "description": str(m.get("description") or "")[:240],
        "context": m.get("context_length") or (m.get("top_provider") or {}).get("context_length") or m.get("max_model_len"),
        "max_output": m.get("max_output_tokens") or (m.get("top_provider") or {}).get("max_completion_tokens"),
        "price_in": round(pin, 4) if pin is not None else None,
        "price_out": round(pout, 4) if pout is not None else None,
        "reasoning": bool(caps.get("reasoning") or "reasoning" in params),
        "json": bool(caps.get("structured_output") or "response_format" in params or "structured_outputs" in params),
        "vision": bool(caps.get("vision")),
        "nsfw": caps.get("nsfw") if "nsfw" in caps else None,
        "category": str(m.get("category") or ""),
    }


def list_models(base_url: str, api_key: str = "", *, kind: str = "text", subscription_only: bool = False,
                timeout: int = 30) -> dict:
    """{"models": [...], "provider": key, "note": str}. kind: "text", "image" or "transcribe"."""
    base = (base_url or "").rstrip("/")
    prov = provider_for(base)
    headers = {"Authorization": f"Bearer {api_key}", "x-api-key": api_key} if api_key else {}
    if kind == "image" and prov == "nanogpt":
        url = base + "/images/models"
        rows_key, per_token = "data", False
    else:
        url = (NANOGPT_SUBSCRIPTION_MODELS if (prov == "nanogpt" and subscription_only) else base + "/models")
        rows_key, per_token = "data", prov == "openrouter"
    params = {"detailed": "true"} if prov == "nanogpt" else {}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if resp.status_code in (401, 403):
        raise PermissionError("The provider refused the API key – check it, or save it to .env.")
    resp.raise_for_status()
    data = resp.json()
    raw = data.get(rows_key) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raw = data.get("models", []) if isinstance(data, dict) else []
    models = [normalize_model(m, per_token_prices=per_token) for m in raw if isinstance(m, dict) and m.get("id")]
    note = ""

    def tagged(m: dict, words: tuple[str, ...]) -> bool:
        return any(w in (m["category"] + " " + m["id"] + " " + m["name"]).lower() for w in words)

    if kind == "text":  # embedding / audio / image-only models don't write cards
        models = [m for m in models if not tagged(m, ("embedding", "whisper", "tts", "moderation", "rerank",
                                                      "transcri", "speech", "dall-e", "gpt-image", "image-gen"))]
    elif kind == "image" and prov != "nanogpt":  # a general /models list: keep the image models
        models = [m for m in models if tagged(m, ("image", "dall-e", "flux", "stable-diffusion", "sdxl", "imagen",
                                                  "recraft", "ideogram", "midjourney", "hidream", "seedream"))]
        if not models:
            note = "This provider's list shows no image models; type the model name (e.g. gpt-image-1 for OpenAI)."
    elif kind == "transcribe":
        models = [m for m in models if tagged(m, ("whisper", "transcri", "speech-to-text", "stt", "asr", "audio"))]
        preset = PROVIDERS.get(prov)
        known = {m["id"] for m in models}
        for mid in (preset.transcribe_models if preset else ()):
            if mid not in known:
                models.append({"id": mid, "name": mid, "description": "", "context": None, "max_output": None,
                               "price_in": None, "price_out": None, "reasoning": False, "json": False,
                               "vision": False, "nsfw": None, "category": "transcription"})
        if not models:
            note = "This provider's list shows no transcription models; type the model name (e.g. whisper-large-v3)."
        elif preset and not known:
            note = f"{preset.label} doesn't list its transcription models; these are the ones its documentation names."
    models.sort(key=lambda m: m["name"].lower())
    if note:
        pass
    elif kind != "text":
        note = "Pick the model this step should run."
    elif prov == "nanogpt":
        note = ("Prices are USD per million tokens (input / output)."
                + (" Showing only models in your subscription." if subscription_only else ""))
    elif not any(m["price_in"] is not None for m in models):
        note = "This provider doesn't list prices."
    return {"models": models, "provider": prov, "note": note}
