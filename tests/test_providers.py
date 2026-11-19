"""Offline tests for provider presets, the model picker and NanoGPT support (no network).

Run:  python -m tests.test_providers
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PERSONA_FORGE_PACE", "0")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from persona_forge import providers  # noqa: E402
from persona_forge.research import Researcher, parse_search_results  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
        self.text = json.dumps(data)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


NANO_MODELS = {"object": "list", "data": [
    {"id": "acme/creative-70b", "name": "Creative 70B", "context_length": 131072, "description": "roleplay",
     "capabilities": {"reasoning": False, "structured_output": True}, "pricing": {"prompt": 0.4, "completion": 0.9,
                                                                                "unit": "per_million_tokens"}},
    {"id": "acme/thinker", "name": "Thinker", "context_length": 32768, "capabilities": {"reasoning": True},
     "pricing": {"prompt": 2, "completion": 10, "unit": "per_million_tokens"}},
    {"id": "text-embedding-3-small", "name": "Embedding", "category": "embedding"},
    {"id": "Whisper-Large-V3", "name": "Whisper"},
]}
SUB_MODELS = {"object": "list", "data": [NANO_MODELS["data"][0]]}
IMG_MODELS = {"object": "list", "data": [{"id": "img/flux", "name": "Flux", "capabilities": {"nsfw": True}},
                                         {"id": "img/safe", "name": "Safe", "capabilities": {"nsfw": False}}]}
OR_MODELS = {"data": [{"id": "x/y", "name": "Y", "context_length": 8000,
                       "pricing": {"prompt": "0.0000005", "completion": "0.0000015"}, "supported_parameters": ["response_format"]}]}


def fake_get(url, headers=None, params=None, timeout=None):
    fake_get.calls.append((url, headers or {}, params or {}))
    if url == providers.NANOGPT_SUBSCRIPTION_MODELS:
        return Resp(SUB_MODELS)
    if url.endswith("/images/models"):
        return Resp(IMG_MODELS)
    if "openrouter" in url:
        return Resp(OR_MODELS)
    if "bad-key" in url:
        return Resp({"error": "no"}, 401)
    return Resp(NANO_MODELS)


fake_get.calls = []


def test_presets_and_lists():
    print("provider presets and model lists")
    check(providers.provider_for("https://api.nano-gpt.com/api/v1") == "nanogpt"
          and providers.provider_for("https://nano-gpt.com/api/v1") == "nanogpt", "NanoGPT recognised on both hosts")
    check(providers.provider_for("https://openrouter.ai/api/v1") == "openrouter" and providers.provider_for("http://x") == "",
          "other presets recognised")
    nano = next(p for p in providers.presets() if p["key"] == "nanogpt")
    check(nano["base_url"] == nano["image_base_url"] == nano["transcribe_base_url"] == providers.NANOGPT_API
          and nano["transcribe_model"] == "Whisper-Large-V3" and nano["web_search"], "NanoGPT preset covers every step")
    orig = providers.requests.get
    providers.requests.get = fake_get
    try:
        r = providers.list_models(providers.NANOGPT_API, "k")
        ids = [m["id"] for m in r["models"]]
        check(ids == ["acme/creative-70b", "acme/thinker"], f"embedding/audio models left out ({ids})")
        m = r["models"][0]
        check(m["price_in"] == 0.4 and m["price_out"] == 0.9 and m["context"] == 131072 and m["json"],
              "prices per million, context and JSON support read")
        check(r["models"][1]["reasoning"], "reasoning flag read")
        url, hdr, params = fake_get.calls[-1]
        check(url == providers.NANOGPT_API + "/models" and params == {"detailed": "true"}
              and hdr["Authorization"] == "Bearer k", "detailed list asked with the key")
        r = providers.list_models(providers.NANOGPT_API, "k", subscription_only=True)
        check([m["id"] for m in r["models"]] == ["acme/creative-70b"] and "subscription" in r["note"],
              "subscription-only list uses NanoGPT's subscription endpoint")
        r = providers.list_models(providers.NANOGPT_API, "k", kind="image")
        check({m["id"]: m["nsfw"] for m in r["models"]} == {"img/flux": True, "img/safe": False},
              "image models listed with their NSFW flag")
        r = providers.list_models("https://openrouter.ai/api/v1", "k")
        check(r["models"][0]["price_in"] == 0.5 and r["models"][0]["price_out"] == 1.5 and r["models"][0]["json"],
              "OpenRouter per-token prices converted to per million")
        check(providers.list_models("https://api.openai.com/v1", "k", kind="image")["models"] == [],
              "no image list for providers without one")
        try:
            providers.list_models("https://bad-key.example/v1", "x")
            check(False, "a refused key should raise")
        except PermissionError:
            check(True, "a refused key gives a clear error")
    finally:
        providers.requests.get = orig


def test_web_search():
    print("NanoGPT web search")
    sample = {"data": [{"url": "https://a.com/x", "title": "A"}, {"link": "https://b.com", "name": "B"},
                       {"title": "no url"}, {"url": "ftp://c"}], "metadata": {"cost": 0.006}}
    check(parse_search_results(sample, 8) == [("https://a.com/x", "A"), ("https://b.com", "B")],
          "results parsed whatever the field names")
    check(parse_search_results({"data": {"results": [{"url": "https://z.com"}]}}, 8) == [("https://z.com", "")],
          "nested result lists handled")
    r = Researcher(search_provider="nanogpt", nanogpt_key="nk")
    sent = {}

    def post(url, json=None, headers=None, timeout=None, **kw):
        sent.update(url=url, body=json, headers=headers)
        return Resp(sample)

    r.http.post = post
    res = r.search("Ivy Wren cosplay", max_results=5)
    check(res[0] == ("https://a.com/x", "A") and sent["url"] == providers.NANOGPT_WEB_SEARCH
          and sent["body"]["provider"] == "linkup" and sent["body"]["depth"] == "standard"
          and sent["headers"]["Authorization"] == "Bearer nk", "search sent to NanoGPT /api/web with the key")
    r2 = Researcher(search_provider="nanogpt")
    r2.http.post = post
    check(r2.search("x") == [] and r2.search_stats["failed"] >= 1, "no key → search reported as failed, run continues")


def test_key_sharing():
    print("one NanoGPT key for every step")
    from persona_forge import cli

    os.environ.pop("IMAGE_API_KEY", None)
    import argparse

    ns = argparse.Namespace(base_url=providers.NANOGPT_API, api_key="nano-key", nanogpt_key="",
                            transcribe_base_url=providers.NANOGPT_API, transcribe_api_key="",
                            image_base_url=providers.NANOGPT_API, image_api_key="nano-key")
    cli.share_nanogpt_key(ns)
    check(ns.transcribe_api_key == "nano-key" and ns.image_api_key == "nano-key" and ns.nanogpt_key == "nano-key",
          "AI model on NanoGPT → transcription, portraits and search reuse its key")
    ns = argparse.Namespace(base_url="https://api.featherless.ai/v1", api_key="feather", nanogpt_key="nk",
                            transcribe_base_url=providers.NANOGPT_API, transcribe_api_key="",
                            image_base_url=providers.NANOGPT_API, image_api_key="feather")
    cli.share_nanogpt_key(ns)
    check(ns.api_key == "feather" and ns.transcribe_api_key == "nk" and ns.image_api_key == "nk",
          "AI model elsewhere → NanoGPT steps use NANOGPT_API_KEY, the AI key is untouched")
    ns = argparse.Namespace(base_url="https://api.openai.com/v1", api_key="sk", nanogpt_key="",
                            transcribe_base_url="", transcribe_api_key="", image_base_url="https://api.openai.com/v1",
                            image_api_key="sk")
    cli.share_nanogpt_key(ns)
    check(ns.image_api_key == "sk" and ns.nanogpt_key == "", "nothing changes without NanoGPT")


def test_webui():
    print("web UI: providers, model list endpoint, search option")
    from persona_forge import webui

    orig = providers.requests.get
    providers.requests.get = fake_get
    os.environ["LLM_BASE_URL"] = providers.NANOGPT_API
    os.environ["LLM_API_KEY"] = "env-key"
    try:
        fake_get.calls.clear()
        r = webui.models_for({"base_url": providers.NANOGPT_API})
        check(len(r["models"]) == 2 and fake_get.calls[-1][1]["Authorization"] == "Bearer env-key",
              "empty key box → the .env key is used for the list")
        r = webui.models_for({"base_url": "https://api.nano-gpt.com/api/v1", "kind": "image", "llm_api_key": "typed"})
        check(len(r["models"]) == 2, "image list via the AI model's key")
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("IMAGE_BASE_URL", None)
        try:
            webui.models_for({"base_url": ""})
            check(False, "no base URL should raise")
        except ValueError:
            check(True, "no provider chosen → clear message")
    finally:
        providers.requests.get = orig
        os.environ.pop("LLM_API_KEY", None)
    argv, env, _ = webui.build_command("ivy", {"name": "Ivy", "mode": "talent", "consent_note": "x",
                                               "search_provider": "nanogpt", "nanogpt_api_key": "nk", "search_web": True},
                                       rebuild=False)
    check("--search-provider" in argv and argv[argv.index("--search-provider") + 1] == "nanogpt"
          and env.get("NANOGPT_API_KEY") == "nk" and "nk" not in " ".join(argv), "search option passed, key only via env")
    check("nanogpt_api_key" in webui.SECRET_FIELDS, "NanoGPT key never saved in the creator folder")
    page = webui.PAGE.read_text("utf-8")
    check('id="provider"' in page and 'id="btnBrowse"' in page and 'value="nanogpt"' in page, "page has the new controls")


def test_models_per_step():
    print("every step names its model; NanoGPT is never called without one")
    import argparse
    from persona_forge import cli, webui

    def ns(**kw):
        d = dict(base_url=providers.NANOGPT_API, model="", extract_model="", image_base_url=providers.NANOGPT_API,
                 image_model="", transcribe_base_url="", transcribe_model="", nanogpt_search="")
        d.update(kw)
        return argparse.Namespace(**d)

    try:
        cli.resolve_models(ns())
        check(False, "NanoGPT without a model must stop")
    except SystemExit as e:
        check("NanoGPT needs to know which model" in str(e) and "Nothing was sent" in str(e),
              "NanoGPT + no AI model → the run stops before any request")
    a = ns(model="acme/creative-70b", transcribe_base_url=providers.NANOGPT_API)
    cli.resolve_models(a)
    check(a.image_model == "" and a.transcribe_model == "Whisper-Large-V3" and a.nanogpt_search == "linkup",
          "NanoGPT: no made-up image model; transcription uses NanoGPT's documented Whisper model; search names its engine")
    a = ns(base_url="https://api.openai.com/v1", image_base_url="https://api.openai.com/v1",
           transcribe_base_url="https://api.openai.com/v1")
    cli.resolve_models(a)
    check(a.model == "gpt-4o-mini" and a.image_model == "gpt-image-1" and a.transcribe_model == "whisper-1",
          "other providers keep the old defaults")
    # the full CLI stops before building any client
    calls = []
    orig = cli.LLMClient
    cli.LLMClient = lambda *a, **k: calls.append(1)
    try:
        cli.main(["Ivy", "--base-url", providers.NANOGPT_API, "--api-key", "k", "--model", "", "--no-web", "-y"])
        check(False, "should have stopped")
    except SystemExit:
        check(not calls, "command line: no AI client is created without a model on NanoGPT")
    finally:
        cli.LLMClient = orig
    # portrait step: no image model → skipped with a message, no request
    import persona_forge.cli as c

    import tempfile
    from tests.test_offline import FakeLLM as FakeServer, serve
    from tests.test_features import DOSSIER

    sent = []
    orig_gen = c.generate_avatar
    c.generate_avatar = lambda *a, **k: sent.append(k) or b""
    srv, base = serve(FakeServer)
    try:
        with tempfile.TemporaryDirectory() as t:
            od = Path(t) / "Ivy_Wren"
            od.mkdir()
            (od / "dossier.json").write_text(json.dumps(DOSSIER), "utf-8")
            (Path(t) / "limits.txt").write_text("- no degradation\n", "utf-8")
            code = cli.main(["--from-dossier", str(od / "dossier.json"), "--base-url", base + "/v1", "--api-key", "k",
                             "--model", "m", "--generate-avatar", "--image-base-url", providers.NANOGPT_API,
                             "--image-model", "", "--out", t, "--no-cache", "--talent", "--nsfw", "--consent-note", "x", "--boundaries", str(Path(t) / "limits.txt")])
            check(code == 0 and not sent and (od / "Ivy_Wren.png").exists(),
                  "portrait on NanoGPT without an image model: no image request, card still built")
            code = cli.main(["--from-dossier", str(od / "dossier.json"), "--base-url", base + "/v1", "--api-key", "k",
                             "--model", "m", "--generate-avatar", "--image-base-url", providers.NANOGPT_API,
                             "--image-model", "img/flux", "--out", t, "--no-cache", "--talent", "--nsfw",
                             "--consent-note", "x", "--boundaries", str(Path(t) / "limits.txt")])
            check(sent and sent[-1]["model"] == "img/flux", "with an image model chosen, the request names it")
    finally:
        srv.shutdown()
        c.generate_avatar = orig_gen

    # web UI: checks before a run, per-step summary, editing model
    os.environ.pop("LLM_MODEL", None)
    os.environ.pop("IMAGE_MODEL", None)
    os.environ.pop("IMAGE_BASE_URL", None)
    form = {"name": "Ivy", "mode": "public", "base_url": providers.NANOGPT_API}
    errs = webui.validate(form)
    check(any("Choose the AI model" in e for e in errs), "web UI: run blocked until a NanoGPT model is chosen")
    errs = webui.validate(dict(form, model="acme/creative-70b", generate_avatar=True))
    check(any("image model" in e for e in errs), "web UI: portrait on NanoGPT blocked until an image model is chosen")
    check(not webui.validate(dict(form, model="acme/creative-70b", generate_avatar=True, image_model="img/flux")),
          "web UI: nothing blocked once each step has a model")
    st = webui.step_models(dict(form, model="m1", extract_model="m2", editor_model="m3", image_model="img/flux",
                                transcribe_base_url=providers.NANOGPT_API))
    check((st["card"]["model"], st["extract"]["model"], st["editor"]["model"], st["image"]["model"], st["transcribe"]["model"])
          == ("m1", "m2", "m3", "img/flux", "Whisper-Large-V3"), "per-step summary shows each step's model")
    st = webui.step_models(dict(form, model="m1"))
    check(st["extract"]["model"] == "m1" and st["editor"]["model"] == "m1" and st["image"]["model"] == "",
          "empty boxes fall back to the card-writing model; NanoGPT images get no made-up default")
    try:
        webui.llm_from_form({"base_url": providers.NANOGPT_API})
        check(False, "editor without a model must refuse")
    except ValueError as e:
        check("Nothing was sent" in str(e), "editing assistant refuses to call NanoGPT without a model")
    check(webui.llm_from_form({"base_url": providers.NANOGPT_API, "model": "m1", "editor_model": "m9"}).cfg.model == "m9",
          "editing assistant uses its own model when one is chosen")
    argv, _, _ = webui.build_command("ivy", {"name": "Ivy", "mode": "public", "search_provider": "nanogpt",
                                             "nanogpt_search": "linkup-deep", "search_web": True})
    check(argv[argv.index("--nanogpt-search") + 1] == "linkup-deep", "NanoGPT search engine passed to the run")

    # search request names its engine
    r = Researcher(search_provider="nanogpt", nanogpt_key="k", nanogpt_search="exa")
    body = {}
    r.http.post = lambda url, json=None, **kw: body.update(json) or Resp({"data": []})
    r.search("x")
    check(body.get("provider") == "exa" and "depth" not in body, "search request names the chosen engine")
    r = Researcher(search_provider="nanogpt", nanogpt_key="k", nanogpt_search="linkup-deep")
    r.http.post = lambda url, json=None, **kw: body.clear() or body.update(json) or Resp({"data": []})
    r.search("y")
    check(body.get("provider") == "linkup" and body.get("depth") == "deep", "Linkup deep sent as provider + depth")

    # picker lists for image and transcription steps
    orig = providers.requests.get
    providers.requests.get = lambda url, **kw: Resp({"data": [{"id": "gpt-4o"}, {"id": "gpt-image-1"}, {"id": "dall-e-3"},
                                                              {"id": "whisper-1"}, {"id": "gpt-4o-transcribe"}]})
    try:
        img = [m["id"] for m in providers.list_models("https://api.openai.com/v1", "k", kind="image")["models"]]
        tr = [m["id"] for m in providers.list_models("https://api.openai.com/v1", "k", kind="transcribe")["models"]]
        txt = [m["id"] for m in providers.list_models("https://api.openai.com/v1", "k")["models"]]
        check(img == ["dall-e-3", "gpt-image-1"] and tr == ["gpt-4o-transcribe", "whisper-1"] and txt == ["gpt-4o"],
              f"each step's picker shows only its kind of model ({img} / {tr} / {txt})")
        providers.requests.get = lambda url, **kw: Resp(NANO_MODELS)
        r = providers.list_models(providers.NANOGPT_API, "k", kind="transcribe")
        check([m["id"] for m in r["models"]] == ["openai-whisper-with-video", "Whisper-Large-V3"],
              "NanoGPT transcription picker: listed + documented Whisper models")
    finally:
        providers.requests.get = orig


def main():
    test_presets_and_lists()
    test_web_search()
    test_key_sharing()
    test_webui()
    test_models_per_step()
    print("\nAll provider checks passed.")


if __name__ == "__main__":
    main()
