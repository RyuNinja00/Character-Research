"""Offline end-to-end test: fake OpenAI-compatible server + stubbed web research.

Run:  python -m tests.test_offline
"""

from __future__ import annotations

import base64
import json
import os
os.environ.setdefault("PERSONA_FORGE_PACE", "0")
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from persona_forge import cli, marinara  # noqa: E402
from persona_forge import research as research_mod  # noqa: E402
from persona_forge.research import Researcher, Source, WikiCandidate  # noqa: E402
from persona_forge.util import parse_json_loose  # noqa: E402

WIKI_TEXT = (
    "Ada Lovelace (1815–1852) was an English mathematician and writer, chiefly known for her work on Charles "
    "Babbage's proposed mechanical general-purpose computer, the Analytical Engine. She was the daughter of Lord Byron "
    "and Anne Isabella Milbanke.\n\n## Early life\nLovelace was raised by her mother, who promoted her interest in "
    "mathematics and logic. Mary Somerville was her tutor and friend.\n\n## Work\nIn 1843 Lovelace published a "
    "translation of Luigi Menabrea's article with her own Notes, including Note G, an algorithm for Bernoulli numbers.\n"
) * 3

FAKE_FACTS = {
    "relevant": True,
    "aliases": ["Augusta Ada King", "Countess of Lovelace"],
    "facts": [
        {"category": c, "subject": s, "fact": f, "when": w, "confidence": "high"}
        for c, s, f, w in [
            ("identity", "Ada Lovelace", "English mathematician and writer", "1815-1852"),
            ("relationship", "Charles Babbage", "Collaborator and friend; designer of the Analytical Engine", "1833-"),
            ("relationship", "Lord Byron", "Her father, whom she never knew", ""),
            ("relationship", "Mary Somerville", "Tutor and friend", ""),
            ("work", "Note G", "Algorithm for computing Bernoulli numbers", "1843"),
            ("place", "London", "Lived in London society", ""),
            ("personality", "Ada Lovelace", "Imaginative, called her approach 'poetical science'", ""),
            ("speech", "Ada Lovelace", "Florid Victorian prose, playful self-aggrandising letters", ""),
            ("appearance", "Ada Lovelace", "Dark hair, often painted in Victorian gowns", ""),
            ("event", "Meeting Babbage", "Met Babbage at a party in 1833 and saw the Difference Engine", "1833"),
            ("quote", "Ada Lovelace", "That brain of mine is something more than merely mortal", ""),
        ]
    ],
}

FAKE_DOSSIER = {
    "persona": {
        "full_name": "Augusta Ada King, Countess of Lovelace", "aliases": ["Ada"], "one_line": "Victorian mathematician",
        "appearance": "Dark hair, pale, Victorian gowns.", "personality_traits": ["imaginative"],
        "speech_style": {"tone": "florid"}, "signature_quotes": ["That brain of mine..."], "tags": ["historical"],
    },
    "history": {
        "timeline": [{"period": "1833", "event": "Met Babbage"}],
        "key_events": [{"name": "Meeting Babbage", "when": "1833", "details": "At a party.", "keywords": ["party"]}],
        "works_and_achievements": [{"name": "Note G", "when": "1843", "details": "Bernoulli algorithm", "keywords": ["note g"]}],
        "controversies": [],
    },
    "world": {
        "relationships": [{"name": "Charles Babbage", "relation": "friend", "details": "Collaborator."}],
        "places": [{"name": "London", "significance": "Home."}],
        "organizations": [],
    },
}

FAKE_CARD = {
    "summary": "Ada Lovelace, 1843.", "description": "Overview: {{char}} is a mathematician. " * 20,
    "personality": "Imaginative, proud.", "appearance": "Dark hair.", "backstory": "Born 1815.",
    "scenario": "London, 1843.", "first_mes": "*Ada looks up.* \"Ah, {{user}}.\"",
    "alternate_greetings": ["Hello one", "Hello two", "Hello three"],
    "mes_example": "{{user}}: Hi\n{{char}}: Good day.\n<START>\n{{user}}: Engine?\n{{char}}: Poetical science!",
    "system_prompt": "Stay in 1843.", "post_history_instructions": "", "depth_prompt": "Florid voice.",
    "creator_notes": "Generated.", "tags": ["Historical", "real person"], "name_color": "#8e44ad",
    "image_appearance": "woman in her 20s, long auburn hair, green eyes, teen look, freckles, rose tattoo on wrist",
}

FAKE_LORE = {
    "entries": [
        {"name": "Charles Babbage", "keys": ["Babbage", "Charles", "he", "Ada Lovelace"], "secondary_keys": [],
         "content": "Charles Babbage is the inventor of the Analytical Engine and {{char}}'s close friend and collaborator.",
         "description": "Babbage, inventor and friend", "importance": 1},
        {"name": "Charles Babbage", "keys": ["Analytical Engine"], "content": "dup " * 20, "importance": 2},
        {"name": "London", "keys": ["London"], "secondary_keys": ["society"],
         "content": "London is where {{char}} moves through high society salons and scientific soirées.",
         "description": "London", "importance": 3},
        {"name": "Too short", "keys": ["x"], "content": "short"},
    ]
}


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    transcribe_calls = 0

    def do_POST(self):
        if self.path.endswith("/audio/transcriptions"):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            assert b'name="model"' in raw and b'filename=' in raw, "multipart upload expected"
            FakeLLM.transcribe_calls += 1
            data = b"This is Nova Test on the podcast. I talk fast and I love slow-burn teasing."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/images/generations"):
            FakeLLM.image_prompt = body["prompt"]
            png = marinara.placeholder_avatar("gen", 32, 48)
            data = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if body.get("model") == "bad-extract":  # e.g. an OpenAI model name sent to Featherless
            data = json.dumps({"error": {"message": "The model `bad-extract` does not exist"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if body.get("model") == "new-model" and ("max_tokens" in body or "temperature" in body):
            bad = "max_tokens" if "max_tokens" in body else "temperature"
            data = json.dumps({"error": {"message": f"Unsupported parameter: '{bad}'"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        system = body["messages"][0]["content"]
        user_msg = body["messages"][1]["content"]
        if body.get("model") == "prude" and "character-card writer" in system:
            content = "I'm sorry, but I can't help with creating that content."
            resp = {"choices": [{"message": {"content": content}}]}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if "BROKENJSON" in user_msg or ("UNUSABLE" in user_msg and "(section" not in user_msg):
            if "BROKENJSON" in user_msg:  # long reply, unescaped quotes, cut off mid-item
                content = ('{"relevant": true, "aliases": [], "facts": [{"category": "quote", "subject": "Ada", '
                           '"fact": "She wrote "poetical science" in a letter", "confidence": "high"}, '
                           '{"category": "identity", "subject": "Ada", "fact": "Countess of Lovelace"}, '
                           '{"category": "work", "subject": "Note G", "fact": "An algorithm that was cut o')
            else:
                content = "Here are the facts: [[[ not json"
            data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if "Nova" in system:
            out = nova_reply(system, user_msg)
        elif "meticulous research assistant" in system:
            if "Ivy" in system:
                assert "Creator-safety rules" in system
            out = FAKE_FACTS
        elif "consolidate research notes" in system:
            key = "persona" if '"full_name"' in system else "history" if '"timeline"' in system else "world"
            out = FAKE_DOSSIER[key]
        elif "character-card writer" in system:
            assert ("ADULT CONTENT" in system) == ("Ivy" in system), "adult rules only on --nsfw card"
            if "Ivy" in system:
                assert "CREATOR BOUNDARIES" in system and "official AI persona" in system
            out = FAKE_CARD
        elif "lorebook (World Info) entries" in system:
            out = FAKE_LORE
        else:
            out = {}
        content = "Sure! Here you go:\n```json\n" + json.dumps(out) + "\n```"
        resp = {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def nova_reply(system: str, user: str) -> dict:
    """Fake LLM for the --deep test: profile fills in as gap pages arrive."""
    if "analyse public audience discussion" in system:
        return {"relevant": True, "insights": [
            {"type": "request", "insight": "AUDIENCE_MARKER fans keep requesting more cosplay sets", "mentions": 3}]}
    if "audience-insights report" in system:
        assert "AUDIENCE_MARKER" in user
        return {"summary": "Fans love the cosplay.", "sections": [{"title": "Requests", "items": [
            {"point": "AUDIENCE_MARKER more cosplay sets", "strength": "strong", "sources": 1}]}],
            "opportunities": ["A monthly cosplay series"]}
    if "meticulous research assistant" in system:
        f = lambda c, t: {"category": c, "subject": "Nova Test", "fact": t, "confidence": "high"}
        intimacy = f("intimacy", "Says on her own page she loves slow-burn teasing")
        themes = [dict(f("content_theme", "18 of 40 clips"), subject="step-sibling roleplay"),
                  dict(f("content_theme", "10 of 40 clips"), subject="deepthroat"),
                  dict(f("content_theme", "3 clips"), subject="teen schoolgirl roleplay")]
        if "/gap" in user:
            return {"relevant": True, "facts": [
                f("inner_life", "Says she started streaming to beat loneliness"),
                f("inner_life", "Admits she is insecure about her singing voice"),
                f("speech", "Talks fast, lots of 'babe' and 'literally'"),
                f("intimacy", "Forum rumour about her private kinks"),  # third-party: must be dropped
                dict(f("content_theme", "gossip thread claims"), subject="forum-claimed fetish"),  # dropped too
            ] + [f("personality", f"trait number {i} from gap page") for i in range(4)]}
        return {"relevant": True, "facts": [f("identity", "Streamer and creator"), intimacy] + themes
                + [f("personality", f"base trait {i}") for i in range(8)]}
    if "consolidate research notes" in system:
        if '"full_name"' not in system:
            return {}
        d = {"full_name": "Nova Test", "personality_traits": ["a", "b", "c", "d"]}
        if "[inner_life]" in user:
            d["inner_life"] = {"core_motivations": ["beat loneliness", "be seen"],
                               "insecurities_and_fears": ["singing voice", "being forgotten"]}
            d["speech_style"] = {"tone": "fast", "vocabulary": "babe, literally", "humor": "self-deprecating"}
        if "[intimacy]" in user:
            d["intimacy"] = {"flirting_style": "slow-burn teasing, stated on her own page"}
        if "[content_theme]" in user:
            assert "teen" not in user and "forum-claimed" not in user, "excluded/third-party themes reached dossier"
            d.setdefault("intimacy", {})
            d["intimacy"]["content_themes"] = [{"theme": "step-sibling roleplay", "prominence": "signature"},
                                               {"theme": "deepthroat", "prominence": "frequent"}]
            d["intimacy"]["persona_kinks"] = ["step-sibling roleplay between adults (from posted content)",
                                              "oral fixation (from posted content)",
                                              "teen roleplay (from posted content)"]  # sanitizer must drop
        return d
    if "plan Marinara Engine sprites" in system:
        return {"expressions": ["smirk", "blushing", "ahegao", "happy"], "poses": ["idle", "twerk", "wave"],
                "notes": {"smirk": "one brow up, teasing", "twerk": "invalid"}}
    if "character-card writer" in system:
        return FAKE_CARD
    if "lorebook (World Info) entries" in system:
        return FAKE_LORE
    return {}


class FakeMarinara(BaseHTTPRequestHandler):
    received: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeMarinara.received = {
            "path": self.path,
            "csrf": self.headers.get("x-marinara-csrf"),
            "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
        }
        data = json.dumps({"success": True, "characterId": "abc", "lorebook": {"entriesImported": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler) -> tuple[HTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


ORIG_FETCH = Researcher.fetch
ORIG_SEARCH = Researcher.search


class FakeWeb(BaseHTTPRequestHandler):
    base = ""

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        b = FakeWeb.base
        if self.path.startswith("/picky"):  # firewall: needs normal browser headers
            ok = "Accept-Language" in self.headers and "text/html" in (self.headers.get("Accept") or "")
            body = ("<html><body><article><p>" + "Picky Nova Test interview text. " * 30 + "</p></article></body></html>"
                    if ok else "blocked")
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())
            return
        if self.path.startswith("/flaky"):  # fails the first time only
            FakeWeb.flaky_hits = getattr(FakeWeb, "flaky_hits", 0) + 1
            ok = FakeWeb.flaky_hits > 1
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((("<html><body><article><p>" + "Flaky Nova Test page. " * 30 + "</p></article></body></html>")
                              if ok else "down").encode())
            return
        cookies = self.headers.get("Cookie") or ""
        article = "<html><head><title>Interview</title></head><body><article><p>" + "Nova Test talks about her career. " * 40 + "</p></article></body></html>"
        gate = ("<html><head><title>Age Verification</title></head><body><h1>Are you 18 or older?</h1>"
                "This website contains adult content.{extra}</body></html>")
        routes = {
            "/gated": ("age_verified=1" in cookies, gate.format(extra="")),                      # cookie-style gate
            "/gated2": ("g2=ok" in cookies, gate.format(extra='<a href="/leave">Leave</a> <a href="/enter2">I am 18 – Enter</a>')),
            "/gated3": ("g3=ok" in cookies, gate.format(extra='<form method="post" action="/verify3"><input type="hidden" '
                                                         'name="tok" value="x"><button name="ok" value="1">I am over 18</button></form>')),
            "/idgate": (False, gate.format(extra=" Please verify your age with Yoti or upload a photo ID.")),
        }
        if self.path in ("/enter2", "/verify3"):
            if self.path == "/verify3":
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Set-Cookie", ("g2=ok" if self.path == "/enter2" else "g3=ok") + "; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>ok</body></html>")
            return
        if self.path in routes:
            ok, gate_html = routes[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((article if ok else gate_html).encode())
            return
        js_routes = {
            # text only exists after JavaScript runs
            "/jsonly": "<html><head><title>Profile</title></head><body><div id=app></div><script>"
                       "document.getElementById('app').innerHTML='<article><p>' + 'Nova Test writes her own bio here. '.repeat(30) + '</p></article>';"
                       "</script></body></html>",
            # an age gate that is only a JavaScript button (no link or form for the fast reader)
            "/jsgate": (("<html><head><title>Interview</title></head><body>" if "jsok=1" in cookies else
                         "<html><head><title>Age Verification</title></head><body>")
                        + ("<article><p>" + "Nova Test talks about her career. " * 40 + "</p></article>"
                           if "jsok=1" in cookies else
                           "<h1>Are you 18 or older?</h1>This website contains adult content."
                           "<button onclick=\"document.cookie='jsok=1;path=/';location.reload()\">I am 18 or older</button>"
                           "<button onclick=\"location.href='/leave'\">Leave</button>")
                        + "</body></html>"),
            "/challenge": "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing the site.</body></html>",
        }
        if self.path in js_routes:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(js_routes[self.path].encode())
            return
        if self.path == "/podcast-page":
            body = (f'<html><head><meta property="og:audio" content="{b}/ep.mp3"></head><body><article><p>'
                    + "Episode 12: Nova Test joins the show. " * 30 + "</p></article></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())
            return
        if self.path == "/ep.mp3":
            data = Path(FakeWeb.audio_file).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/magazine/interview"):
            body = ("<html><body><article><p>" + "Magazine interview with Nova Test. " * 30 +
                    f'</p><a href="{b}/magazine/about-other-star">About another star</a></article></body></html>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())
            return
        pages = {
            "/hub": f"""<html><head><title>Ivy | Links</title>
<meta property="og:description" content="Ivy Example - creator, gamer, cosplayer"></head><body>
<a href="{b}/about">About me</a>
<script id="__NEXT_DATA__" type="application/json">{{"props":{{"links":[
 {{"title":"Podcast interview","url":"{b}/podcast"}},
 {{"title":"Leaks","url":"https://example-leaked-site.com/ivy"}},
 {{"title":"Find address","url":"https://www.spokeo.com/ivy"}},
 {{"title":"App","url":"https://apps.apple.com/app/x"}},
 {{"title":"My OF","url":"https://onlyfans.com/ivyxo"}}]}}}}</script></body></html>""",
            "/about": "<html><body><article><p>" + "Ivy Example loves horror games and teasing her chat. " * 20
                      + f'</p><a href="{b}/faq">FAQ</a> <a href="{b}/cart">Cart</a></article></body></html>',
            "/nova": "<html><body><article><p>" + "Nova Test is a streamer. " * 40 + "</p></article></body></html>",
            "/forum": "<html><body><article><p>" + "Thread: Nova Test content discussion. " * 40 + "</p></article></body></html>",
            "/gap1": "<html><body><article><p>" + "In this interview Nova Test opens up. " * 40 + "</p></article></body></html>",
            "/gap2": "<html><body><article><p>" + "Nova Test podcast notes. " * 40 + "</p></article></body></html>",
            "/faq": "<html><body><article><p>" + "Ivy Example FAQ: she streams on weekends and hates pineapple pizza. " * 20
                    + "</p></article></body></html>",
            "/podcast": "<html><body><article><p>" + "On the podcast Ivy Example talked about burnout and her shy side. " * 20
                        + "</p></article></body></html>",
        }
        body = pages.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write((body or "nope").encode())


def stub_research():
    Researcher.wiki_candidates = lambda self, n, limit=6: [WikiCandidate("Ada Lovelace", "", "English mathematician")]
    Researcher.wikipedia_article = lambda self, t: Source(
        "https://en.wikipedia.org/wiki/Ada_Lovelace", "Ada Lovelace", WIKI_TEXT, "wikipedia",
        {"description": "English mathematician (1815–1852)"},
    )
    Researcher.wikiquote = lambda self, t: Source("https://en.wikiquote.org/wiki/Ada_Lovelace", "Ada (Wikiquote)",
                                                   "Ada Lovelace said things. " * 40, "wikiquote")
    Researcher.search = lambda self, q, max_results=8: [
        ("https://example.org/ada-bio", "Bio"), ("https://facebook.com/ada", "FB"), ("https://example.org/x.pdf", "pdf"),
    ]
    Researcher.fetch = lambda self, url, title="", kind="web", keep_html=False, via="": Source(url, title, "Lovelace " * 300, kind)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def main():
    assert parse_json_loose('text {"a": [1, 2,], "b": "x"') == {"a": [1, 2], "b": "x"}
    stub_research()
    llm_srv, llm_url = serve(FakeLLM)
    mar_srv, mar_url = serve(FakeMarinara)
    with tempfile.TemporaryDirectory() as tmp:
        rc = cli.main([
            "Ada Lovelace", "--yes", "--base-url", llm_url, "--api-key", "x", "--model", "fake",
            "--out", tmp, "--no-cache", "--era", "1843", "--push", mar_url,
        ])
        check(rc == 0, "CLI finished with exit code 0")
        out = Path(tmp) / "Ada_Lovelace"
        card = json.loads((out / "Ada_Lovelace.card.json").read_text())
        d = card["data"]
        check(card["spec"] == "chara_card_v2" and card["spec_version"] == "2.0", "V2 envelope")
        for f in ("name", "description", "personality", "scenario", "first_mes", "mes_example", "creator_notes",
                  "system_prompt", "post_history_instructions", "alternate_greetings", "tags", "creator",
                  "character_version", "extensions", "character_book"):
            check(f in d, f"card has '{f}'")
        check(d["extensions"]["appearance"] == "Dark hair." and d["extensions"]["backstory"] == "Born 1815.",
              "Marinara appearance/backstory extensions populated")
        check(d["extensions"]["depth_prompt"]["role"] == "system", "depth_prompt shape")
        check(d["mes_example"].startswith("<START>"), "mes_example begins with <START>")
        check(d["tags"] == ["historical", "real person"], "tags lower-cased & deduped")
        book = d["character_book"]
        names = [e["name"] for e in book["entries"]]
        check(len(names) == len(set(names)), "lorebook entries de-duplicated")
        check("Too short" not in names, "too-short entries dropped")
        bab = next(e for e in book["entries"] if e["name"] == "Charles Babbage")
        check("he" not in bab["keys"] and "Ada Lovelace" not in bab["keys"], "generic/self-name keys removed")
        check("Analytical Engine" in bab["keys"], "duplicate entry keys merged")
        check(bab["insertion_order"] < next(e for e in book["entries"] if e["name"] == "London")["insertion_order"],
              "important entries get lower (Marinara: earlier) order")
        lon = next(e for e in book["entries"] if e["name"] == "London")
        check(lon["selective"] is True and lon["secondary_keys"] == ["society"], "secondary keys → selective")

        lb = json.loads((out / "Ada_Lovelace.lorebook.json").read_text())
        check(isinstance(lb["entries"], dict) and "0" in lb["entries"], "standalone lorebook in ST World Info shape")
        e0 = lb["entries"]["0"]
        for f in ("key", "keysecondary", "comment", "content", "description", "order", "position", "disable",
                  "matchWholeWords", "preventRecursion"):
            check(f in e0, f"WI entry has '{f}'")

        png = (out / "Ada_Lovelace.png").read_bytes()
        got = marinara.read_card_from_png(png)
        check(got["spec"] == "chara_card_v3", "PNG carries ccv3 chunk (Marinara prefers it)")
        chunks = [data.split(b"\x00", 1)[0] for t, data in marinara._iter_chunks(png) if t == b"tEXt"]
        check(chunks.count(b"chara") == 1 and chunks.count(b"ccv3") == 1, "PNG has exactly one chara and one ccv3 chunk")
        re_png = marinara.embed_card_in_png(png, card)
        chunks2 = [data.split(b"\x00", 1)[0] for t, data in marinara._iter_chunks(re_png) if t == b"tEXt"]
        check(chunks2.count(b"chara") == 1 and b"ccv3" not in chunks2, "re-embedding replaces old card chunks")

        srcs = json.loads((out / "sources.json").read_text())
        check(not any("facebook.com" in s or s.endswith(".pdf") for s in srcs), "blocked domains / PDFs skipped")

        rec = FakeMarinara.received
        check(rec["path"] == "/api/import/st-character", "pushed to /api/import/st-character")
        check(rec["csrf"] == "1", "sent x-marinara-csrf header")
        check(rec["body"]["importEmbeddedLorebook"] is True, "asked Marinara to create the linked lorebook")
        check(rec["body"]["_avatarDataUrl"].startswith("data:image/png;base64,"), "avatar sent as data URL")
        base64.b64decode(rec["body"]["_avatarDataUrl"].split(",", 1)[1])

        # Rebuild from dossier path
        rc = cli.main(["--from-dossier", str(out / "dossier.json"), "--base-url", llm_url, "--model", "fake",
                       "--out", tmp, "--no-cache", "--spec", "v3", "--style", "chat"])
        check(rc == 0, "--from-dossier rebuild works")
        v3 = json.loads((out / "Ada_Lovelace.card.json").read_text())
        check(v3["spec"] == "chara_card_v3" and v3["data"]["source"], "V3 JSON with sources")

        # ── Talent mode: link hub crawl + brief + boundaries + consent
        Researcher.fetch = ORIG_FETCH
        web_srv, web_url = serve(FakeWeb)
        FakeWeb.base = web_url
        brief = Path(tmp) / "brief.md"
        brief.write_text("# Creator brief\nIvy is warm, sarcastic, secretly insecure about her art.")
        limits = Path(tmp) / "limits.txt"
        limits.write_text("- No roleplay involving real-life meetups.\n- No degradation.")
        try:
            cli.main(["Ivy Example", "--talent", "--seed", web_url + "/hub", "--base-url", llm_url, "--model", "f",
                      "--out", tmp, "--no-cache", "-y"])
            raise AssertionError("talent mode without consent note should exit")
        except SystemExit as e:
            check("consent-note" in str(e), "--talent requires --consent-note")
        import contextlib, io
        hub_log = io.StringIO()
        with contextlib.redirect_stderr(hub_log):
            rc = cli.main(["Ivy Example", "--talent", "--nsfw", "--seed", web_url + "/hub", "--file", str(brief),
                           "--boundaries", str(limits), "--consent-note", "Approved by Ivy, release on file",
                           "--base-url", llm_url, "--model", "f", "--out", tmp, "--no-cache", "-y"])
        check(rc == 0, "talent run finished")
        check("Handles found on the link hub: ivyxo" in hub_log.getvalue(), "OnlyFans handle discovered from hub link")
        tout = Path(tmp) / "Ivy_Example"
        srcs = json.loads((tout / "sources.json").read_text())
        check(any(s.endswith("/hub") for s in srcs), "hub page used")
        check(any(s.endswith("/about") for s in srcs) and any(s.endswith("/podcast") for s in srcs),
              "anchor link and __NEXT_DATA__ link both crawled")
        check(not any("leak" in s or "spokeo" in s or "apple.com" in s for s in srcs), "leak/people-search/app links skipped")
        check(any(s.startswith("file://") for s in srcs), "creator brief file used")
        check(any(s.endswith("/faq") for s in srcs), "sub-page (FAQ) on the creator's own site followed")
        review = (tout / "review.md").read_text()
        check("## Sources read" in review and "## Facts by category" in review, "review.md lists sources and facts")
        check("people-search" in review and "leak" in review, "review.md lists skipped links with reasons")
        check(not any("wikipedia" in s for s in srcs), "no Wikipedia in talent mode")
        td = json.loads((tout / "Ivy_Example.card.json").read_text())["data"]
        check("No degradation." in td["system_prompt"], "boundaries embedded verbatim in system_prompt")
        check(td["creator_notes"].startswith("Authorization: Approved by Ivy"), "consent note recorded")
        check("nsfw" in td["tags"], "nsfw tag added")
        check(not td["extensions"]["persona_forge"]["sources"], "creator card carries no research source list")
        check("file://" not in json.dumps(td), "no local file paths inside the card")
        settings = json.loads((tout / "run_settings.json").read_text())
        check(settings["talent"] and settings["nsfw"] and "No degradation." in settings["boundaries_text"],
              "run settings saved for rebuilds")

        # Rebuild WITHOUT retyping --talent/--nsfw/--consent-note/--boundaries
        rc = cli.main(["--from-dossier", str(tout / "dossier.json"), "--base-url", llm_url, "--model", "f",
                       "--out", tmp, "--no-cache"])
        check(rc == 0, "rebuild from dossier reuses saved settings (no consent-note error)")
        rd = json.loads((tout / "Ivy_Example.card.json").read_text())["data"]
        check(rd["creator_notes"].startswith("Authorization: Approved by Ivy") and "No degradation." in rd["system_prompt"],
              "rebuilt card keeps consent note and boundaries")

        # Refusing model: research is kept, exit code 1, resume hint
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.main(["--from-dossier", str(tout / "dossier.json"), "--base-url", llm_url, "--model", "prude",
                           "--out", tmp, "--no-cache"])
        check(rc == 1 and "refused" in buf.getvalue() and "--from-dossier" in buf.getvalue(),
              "model refusal → clear message and resume command")
        # Newer-API model: max_tokens/temperature rejected → automatic fallback
        rc = cli.main(["--from-dossier", str(tout / "dossier.json"), "--base-url", llm_url, "--model", "new-model",
                       "--out", tmp, "--no-cache"])
        check(rc == 0, "max_completion_tokens / default-temperature fallback works")
        # ── Deep mode: gap-filling rounds, intimacy only from the creator's own channels
        def gap_search(self, q, max_results=8):
            if "opens up" in q or "why I started" in q:  # only the gap-fill queries find these pages
                return [(web_url + "/gap1", "i"), (web_url + "/gap2", "p"), ("https://www.spokeo.com/nova", "x")]
            if "reddit" in q:
                return [(web_url + "/forum", "f"), ("https://nova-leaked.example.com/x", "l")]
            return [("https://www.spokeo.com/nova", "x")]
        Researcher.search = gap_search
        rc = cli.main(["Nova Test", "--talent", "--nsfw", "--deep", "--url", web_url + "/nova",
                       "--consent-note", "Approved by Nova", "--site-pages", "0", "--base-url", llm_url,
                       "--model", "f", "--out", tmp, "--no-cache", "-y", "--max-rounds", "3",
                       "--audience-research", "--handle", "@novatest"])
        check(rc == 0, "deep run finished")
        nout = Path(tmp) / "Nova_Test"
        nsrcs = json.loads((nout / "sources.json").read_text())
        check(any(s.endswith("/gap1") for s in nsrcs), "gap-fill round read new pages")
        check("| 1 | 2 |" in (nout / "review.md").read_text(), "round 1 read 2 pages found by gap searches")
        check(not any("spokeo" in s for s in nsrcs), "people-search still blocked in gap-fill")
        facts = [json.loads(l) for l in (nout / "facts.jsonl").read_text().splitlines()]
        intim = [f for f in facts if f["category"] == "intimacy"]
        check(intim and all(f["source"].endswith("/nova") for f in intim),
              "intimacy facts kept only from the creator's own page")
        check(not any("rumour" in f["fact"] for f in facts), "third-party intimacy claim discarded")
        ndos = json.loads((nout / "dossier.json").read_text())
        check(ndos.get("inner_life", {}).get("core_motivations"), "inner life filled by gap research")
        rv = (nout / "review.md").read_text()
        check("## Profile coverage" in rv and "### Research rounds" in rv, "review.md shows coverage and rounds")
        check("discarded" in rv, "review.md reports discarded third-party intimacy claims")
        themes = {f["subject"] for f in facts if f["category"] == "content_theme"}
        check({"step-sibling roleplay", "deepthroat"} <= themes, "content themes from creator's own page kept")
        check(not any("teen" in t or "forum" in t for t in themes), "minor-coded and third-party themes dropped")
        kinks = ndos["intimacy"]["persona_kinks"]
        check(len(kinks) == 2 and not any("teen" in k for k in kinks), "sanitizer removed minor-coded persona kink")
        check("minor-coded" in rv and "Persona kinks are extrapolated" in rv, "review.md explains themes & removals")

        # ── Adult-platform profile detection + sweep
        rr = Researcher(cache=None, search_provider="none")
        hs = research_mod.creator_handles("Nova Test", ["https://linktr.ee/novaxo"], ["@NovaAlt"])
        check({"novaxo", "novatest", "nova_test", "novaalt"} <= set(hs), "handles from hub, name slugs and --handle")
        cands = [("https://www.manyvids.com/Profile/123/novatest/Store/Videos", "store"),
                 ("https://www.pornhub.com/view_video.php?viewkey=abc", "reupload"),
                 ("https://www.pornhub.com/model/novaxo", "model page"),
                 ("https://fapello.com/novatest", "leak"),
                 ("https://www.manyvids.com/Profile/9/someoneelse/Store", "other creator")]
        queries_made = []
        rr.search = lambda q, max_results=5: queries_made.append(q) or (cands if "manyvids" in q else [])
        fetched = []
        rr.fetch = lambda url, title="", kind="web", keep_html=False, via="": fetched.append((url, kind)) or Source(url, title, "x" * 500, kind)
        rr.adult_platform_sweep("Nova Test", hs, set())
        urls = [u for u, _ in fetched]
        check("https://www.manyvids.com/Profile/123/novatest/Store/Videos" in urls and
              "https://www.pornhub.com/model/novaxo" in urls, "creator's own store and model page read")
        check(not any("viewkey" in u or "fapello" in u or "someoneelse" in u for u in urls),
              "tube reuploads, leak sites and other creators skipped")
        check(all(k == "platform" for _, k in fetched), "platform pages tagged as the creator's own channel")
        check(len(queries_made) <= 4, f"platform sweep batched into {len(queries_made)} searches (was up to 32)")
        store = Source("https://www.manyvids.com/Profile/123/novatest/Store", "s", "x" * 500, "user",
                       {"html": '<a href="https://www.manyvids.com/Video/999/someone-else">vid</a>'})
        fetched.clear()
        rr.follow_site_pages([store], per_site=5)
        check(not fetched, "no sub-page following on multi-user platforms (other creators' pages)")
        ar = (nout / "audience_report.md").read_text()
        check("AUDIENCE_MARKER" in ar and "/forum" in ar, "audience_report.md written from discussion pages")
        check("leaked" not in ar, "leak sites excluded from audience research")
        everything = (nout / "Nova_Test.card.json").read_text() + (nout / "dossier.json").read_text() \
            + (nout / "Nova_Test.lorebook.json").read_text()
        check("AUDIENCE_MARKER" not in everything, "audience insights never reach the card, dossier or lorebook")
        try:
            cli.main(["X", "--audience-research", "--base-url", llm_url, "--out", tmp])
            raise AssertionError("audience research without --talent should exit")
        except SystemExit as e:
            check("--talent" in str(e), "--audience-research requires --talent")
        # ── Age gates: click-through answered for an adult user; real verification never bypassed
        from persona_forge.research import Researcher as R3
        ra = R3(cache=None, search_provider="none", confirm_adult=True)
        for path, what in (("/gated", "cookie gate"), ("/gated2", "“I am 18 – Enter” link"), ("/gated3", "confirm form")):
            got = ra.fetch(web_url + path, kind="user")
            check(got is not None and "talks about her career" in got.text, f"age gate passed ({what})")
        check(ra.fetch(web_url + "/idgate", kind="user") is None and
              any("real age verification" in why for _, why in ra.skipped), "real ID/face/card verification not bypassed")
        rn = R3(cache=None, search_provider="none", confirm_adult=False)
        check(rn.fetch(web_url + "/gated", kind="user") is None and
              any("I'm 18+" in why for _, why in rn.skipped), "without the 18+ box, gates are reported, not answered")

        # ── Real-browser reader (headless Chrome via Playwright)
        rb = R3(cache=None, search_provider="none", confirm_adult=True, browser_mode="auto",
                profile_dir=str(Path(tmp) / "bprofile"))
        if rb.browser() is None:
            print("  (skipped: no browser available for Playwright)")
        else:
            got = rb.fetch(web_url + "/jsonly", kind="user")
            check(got is not None and "writes her own bio" in got.text, "JavaScript-only page read with the real browser")
            got = rb.fetch(web_url + "/jsgate", kind="user")
            check(got is not None and "talks about her career" in got.text, "JavaScript age-gate button clicked in the browser")
            check(rb.fetch(web_url + "/challenge", kind="user") is None and
                  any("verify you're human" in why and "Visible browser" in why for _, why in rb.skipped),
                  "human check not bypassed; user pointed to Visible browser mode")
            check(rb.fetch(web_url + "/jsonly?from=search", kind="web") is None,
                  "search-result pages stay on the fast reader (browser only for pages you chose)")
            rb._browser.close()
        roff = R3(cache=None, search_provider="none", confirm_adult=True, browser_mode="off")
        check(roff.fetch(web_url + "/jsonly", kind="user") is None, "browser off: JavaScript-only page reported, not read")

        # ── Web search: safe search off for adult runs, "No results" is not a failure, simpler retry, rate limits
        import types
        import persona_forge.research as rmod
        calls = []

        class FakeDDGS:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def text(self, query, region=None, safesearch=None, max_results=10):
                calls.append((query, safesearch))
                if "ratelimited" in query and sum(1 for q, _ in calls if "ratelimited" in q) == 1:
                    raise Exception("https://html.duckduckgo.com/html 202 Ratelimit")
                if " OR " in query or "(" in query:
                    raise Exception("No results found.")
                return [{"href": "https://example.org/" + query.replace(" ", "_").replace('"', ""), "title": "t"}]

        saved = sys.modules.get("ddgs")
        sys.modules["ddgs"] = types.SimpleNamespace(DDGS=FakeDDGS)
        real_sleep = rmod.time.sleep
        rmod.time.sleep = lambda s: None
        stubbed_search = R3.search
        R3.search = ORIG_SEARCH
        try:
            rs = R3(cache=None, search_provider="ddg", confirm_adult=True)
            got = rs.search('"Nova Test" personality OR character OR temperament')
            check(got and calls[-1] == ('"Nova Test" personality', "off"),
                  "empty complex search retried in a simpler form, with safe search off")
            check(rs.search_stats["failed"] == 0 and rs.search_stats["empty"] == 0,
                  "“No results found” is not reported as a failure")
            check(rs.search("ratelimited query") and rs.search_stats["rate_limited"] == 1, "rate limit → wait and retry")
            rs2 = R3(cache=None, search_provider="ddg", confirm_adult=False)
            rs2.search("cats")
            check(calls[-1][1] == "moderate", "safe search stays moderate for non-adult runs")
            check("Web search (ddg, safe search off)" in rs.search_summary(), "one-line search summary instead of spam")
        finally:
            R3.search = stubbed_search
            rmod.time.sleep = real_sleep
            if saved is not None:
                sys.modules["ddgs"] = saved
            else:
                del sys.modules["ddgs"]

        # ── Audio: transcription API (with ffmpeg splitting), local Whisper, podcast pages, CLI integration
        import shutil
        import subprocess
        from persona_forge import audio as amod
        if shutil.which("ffmpeg"):
            wav = Path(tmp) / "interview.wav"
            long_wav = Path(tmp) / "long.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                            "-ac", "1", "-ar", "16000", str(wav)], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
                            "-ac", "1", "-ar", "16000", str(long_wav)], check=True)
            FakeWeb.audio_file = str(wav)
            cfg = amod.TranscribeConfig(engine="api", base_url=llm_url, model="whisper-1")
            tr = amod.Transcriber(cfg, cache_dir=Path(tmp) / "acache")
            before = FakeLLM.transcribe_calls
            check("slow-burn teasing" in tr.transcribe(wav), "audio transcribed through an OpenAI-compatible API")
            tr.transcribe(wav)
            check(FakeLLM.transcribe_calls == before + 1, "transcripts cached (no second API call)")
            big = amod.Transcriber(cfg, cache_dir=None, api_max_bytes=200_000)
            before = FakeLLM.transcribe_calls
            big.transcribe(long_wav)
            check(FakeLLM.transcribe_calls - before >= 3, "long audio split with ffmpeg under the API size limit")

            class _Seg:
                def __init__(self, a, b, t): self.start, self.end, self.text = a, b, t
            fake_fw = types.SimpleNamespace(WhisperModel=lambda *a, **k: types.SimpleNamespace(
                transcribe=lambda path, language=None, vad_filter=True: (
                    iter([_Seg(0, 20, "Hi, I'm Nova Test."), _Seg(20, 50, "I started streaming in 2019.")]),
                    types.SimpleNamespace(duration=50.0))))
            sys.modules["faster_whisper"] = fake_fw
            try:
                loc = amod.Transcriber(amod.TranscribeConfig(engine="auto"), cache_dir=None)
                text = loc.transcribe(wav)
                check(loc.engine == "local" and "[00:00:00] Hi, I'm Nova Test. I started streaming" in text,
                      "auto picks local Whisper when installed; timestamped paragraphs")
            finally:
                del sys.modules["faster_whisper"]
            # GPU without NVIDIA libraries (Windows: "cublas64_12.dll is not found") → automatic CPU fallback
            loads = []

            def _model(name, device="auto", compute_type="default"):
                loads.append((device, compute_type))

                def _tr(path, language=None, vad_filter=True):
                    def gen():
                        if device != "cpu":
                            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                        yield _Seg(0, 10, "Hello from the CPU.")
                    return gen(), types.SimpleNamespace(duration=10.0)
                return types.SimpleNamespace(transcribe=_tr)

            sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=_model)
            try:
                glog = io.StringIO()
                with contextlib.redirect_stderr(glog):
                    gpu = amod.Transcriber(amod.TranscribeConfig(engine="local"), cache_dir=None)
                    text = gpu.transcribe(wav)
                check("Hello from the CPU." in text and loads == [("auto", "default"), ("cpu", "int8")],
                      "missing cublas64_12.dll → switched to CPU automatically")
                check("Switching to the CPU" in glog.getvalue(), "GPU fallback explained in the log")
                loads.clear()
                gpu.transcribe(long_wav)
                check(loads == [], "stays on the CPU for the rest of the run (no reload)")
                loads.clear()
                amod.Transcriber(amod.TranscribeConfig(engine="local", device="cpu"), cache_dir=None).transcribe(wav)
                check(loads == [("cpu", "int8")], "Whisper device = CPU skips the GPU entirely")
            finally:
                del sys.modules["faster_whisper"]
            try:
                amod.Transcriber(amod.TranscribeConfig(engine="auto"), cache_dir=None)
                check(False, "no engine should raise")
            except amod.TranscriptionUnavailable as e:
                check("faster-whisper" in str(e) and "Featherless" in str(e), "no engine → clear install/setup message")
            html = ('<meta property="og:audio" content="/a.mp3"><audio src="b.m4a"></audio>'
                    '<a href="https://cdn.x.com/c.mp3?x=1">dl</a>')
            found = amod.find_page_audio(html, "https://pod.example.com/ep/1")
            check(found[:3] == ["https://pod.example.com/a.mp3", "https://pod.example.com/ep/b.m4a",
                                "https://cdn.x.com/c.mp3?x=1"], "podcast audio found on a page (og:audio, <audio>, links)")
            sp = amod.find_page_audio("<html></html>", "https://pt-br.spreaker.com/episode/adult-film-star-x--49025109")
            check(sp == ["https://api.spreaker.com/v2/episodes/49025109/download.mp3"], "Spreaker episode audio link derived")

            alog = io.StringIO()
            with contextlib.redirect_stderr(alog):
                rc = cli.main(["Nova Test", "--talent", "--consent-note", "ok", "--no-web", "--audio", str(wav),
                               "--url", web_url + "/podcast-page", "--transcribe-pages", "--transcriber", "api",
                               "--transcribe-base-url", llm_url, "--base-url", llm_url, "--model", "f",
                               "--out", tmp, "--no-cache", "--site-pages", "0"])
            aout = Path(tmp) / "Nova_Test"
            check(rc == 0 and len(list((aout / "transcripts").glob("*.txt"))) == 2,
                  "uploaded audio + podcast on a page both transcribed; transcripts saved in output/transcripts/")
            rv = (aout / "review.md").read_text()
            check("Transcript: interview.wav" in rv, "transcript listed as a source in review.md")
        else:
            print("  (audio tests skipped: ffmpeg not installed)")

        # ── Cut-off / malformed AI replies: keep complete facts, fix stray quotes, retry in halves
        from persona_forge.llm import LLMClient as _LC, LLMConfig as _LCfg
        from persona_forge.synthesize import Options as _Opt, extract_facts as _xf
        xlog = io.StringIO()
        with contextlib.redirect_stderr(xlog):
            facts_x, _, _ = _xf(_LC(_LCfg(llm_url, "", "x")), [
                Source("file://R:/Card AI Builder Project/talent/x/sources/Chapter_1_Intro.txt", "Chapter 1",
                       "BROKENJSON " + "Ada Lovelace text. " * 50, "brief"),
                Source("https://example.org/long", "Long", "UNUSABLE\n" + ("Ada Lovelace wrote notes.\n" * 200), "user"),
            ], _Opt(name="Ada Lovelace"))
        fx = [f.fact for f in facts_x]
        check('She wrote "poetical science" in a letter' in fx and "Countess of Lovelace" in fx,
              "unescaped quotes repaired; complete facts kept from a cut-off reply")
        check(not any("cut o" in f for f in fx), "half-written last fact dropped")
        check("kept 2 complete facts" in xlog.getvalue() and "Chapter_1_Intro.txt part 1" in xlog.getvalue(),
              "recovery reported with a short file name")
        check("retrying in two smaller halves" in xlog.getvalue() and any("Lovelace" in f.fact or f.fact for f in facts_x),
              "unusable reply → piece retried in halves")

        # ── A page-reading model the provider doesn't know falls back to the main model
        fb = io.StringIO()
        with contextlib.redirect_stderr(fb):
            rc = cli.main(["Ada Lovelace", "--yes", "--no-web", "--base-url", llm_url, "--model", "fake",
                           "--extract-model", "bad-extract", "--out", tmp, "--no-cache"])
        check(rc == 0 and "was rejected by the provider" in fb.getvalue(), "invalid page-reading model → main model used")
        check("page-reading model bad-extract" in fb.getvalue() and "API key" in fb.getvalue(),
              "effective AI setup printed at the start of the run")
        os.environ["LLM_MAX_TOKENS"] = ""
        os.environ["LLM_MODEL"] = ""
        check(cli.build_parser().parse_args(["x"]).model == "gpt-4o-mini", "empty .env values count as not set")
        del os.environ["LLM_MAX_TOKENS"], os.environ["LLM_MODEL"]

        # ── Page reader: browser headers, retry, no failure caching, no sub-pages on third-party sites
        from persona_forge.research import Researcher as R2, normalize_url
        from persona_forge.util import DiskCache
        cache_dir = Path(tmp) / "cache_reader"
        rd = R2(cache=DiskCache(cache_dir), search_provider="none")
        rd.handles = ["novatest"]
        check(rd.fetch(web_url + "/picky", kind="user") is not None, "site that 404s scripted requests is read")
        check(rd.fetch(web_url + "/flaky", kind="user") is not None, "temporary 503 retried and read")
        FakeWeb.flaky_hits = 0
        miss = rd.fetch(web_url + "/does-not-exist", kind="user")
        check(miss is None and any("HTTP 404" in why and "Text sources" in why for _, why in rd.skipped),
              "404 explained with what to do next")
        check(not list((cache_dir / "page").glob("*.json")) or all(json.loads(f.read_text()) for f in (cache_dir / "page").glob("*.json")),
              "failed pages are not cached")
        mag = rd.fetch(web_url + "/magazine/interview", kind="user", keep_html=True)
        check(not rd.follow_site_pages([mag], per_site=5), "no sub-pages followed on a third-party interview site")
        check(normalize_url("https://youtu.be/b_GvjCMoPds?si") == "https://www.youtube.com/watch?v=b_GvjCMoPds",
              "YouTube share link normalised, tracking tag dropped")

        # ── Images: image prompt, creator-supplied sprites, generated avatar
        sp = Path(tmp) / "sprites"
        (sp / "full-body").mkdir(parents=True)
        (sp / "happy.png").write_bytes(marinara.placeholder_avatar("h", 8, 8))
        (sp / "full-body" / "idle.png").write_bytes(marinara.placeholder_avatar("i", 8, 8))
        (sp / "notes.txt").write_text("not an image")
        rc = cli.main(["--from-dossier", str(nout / "dossier.json"), "--base-url", llm_url, "--model", "f",
                       "--out", tmp, "--no-cache", "--sprites", str(sp), "--generate-avatar",
                       "--image-base-url", llm_url, "--image-model", "img"])
        check(rc == 0, "image run finished")
        ic = json.loads((nout / "Nova_Test.card.json").read_text())["data"]["extensions"]
        check(ic["imageAppearanceEnabled"] is True and "auburn" in ic["imageAppearance"], "imageAppearance set for Marinara")
        check("teen" not in ic["imageAppearance"].lower(), "youth-coded words stripped from image prompt")
        names = sorted(x["filename"] for x in ic["marinara"]["sprites"])
        check(names == ["full_idle.png", "happy.png"], "sprites embedded; full-body folder → full_ prefix; non-images skipped")
        check(ic["marinara"]["sprites"][0]["data"].startswith("data:image/png;base64,"), "sprites stored as data URLs")
        check((nout / "avatar_generated.png").exists(), "generated avatar saved")
        check("Fully clothed" in FakeLLM.image_prompt and "teen" not in FakeLLM.image_prompt.lower(),
              "avatar prompt is SFW and text-only")
        png_card = marinara.read_card_from_png((nout / "Nova_Test.png").read_bytes())
        check(png_card["data"]["extensions"]["marinara"]["sprites"], "PNG card carries the sprites")
        # ── Sprite kit from reference images
        refs = Path(tmp) / "refs"
        refs.mkdir()
        for i in range(6):
            (refs / f"photo{i}.png").write_bytes(marinara.placeholder_avatar(f"r{i}", 2000, 1200))
        rc = cli.main(["--from-dossier", str(nout / "dossier.json"), "--base-url", llm_url, "--model", "f",
                       "--out", tmp, "--no-cache", "--references", str(refs), "--avatar-from-reference"])
        check(rc == 0, "sprite-kit run finished")
        kit = nout / "sprite_kit"
        kit_refs = sorted((kit / "references").iterdir())
        check(len(kit_refs) == 4, "kit keeps Marinara's max of 4 references")
        from PIL import Image
        check(max(Image.open(kit_refs[0]).size) <= 1024, "references scaled to ≤1024px")
        md = (kit / "SPRITE_KIT.md").read_text()
        check("`smirk` – one brow up, teasing" in md and "`blushing`" in md and "`neutral`" in md,
              "planned expressions (with neutral) and notes in the kit")
        check("ahegao" not in md and "twerk" not in md, "labels outside Marinara's lists dropped")
        check("auburn" in (kit / "appearance_description.txt").read_text(), "appearance description ready to paste")
        av = marinara._iter_chunks((nout / "Nova_Test.png").read_bytes())
        ihdr = next(d for t_, d in av if t_ == b"IHDR")
        check(int.from_bytes(ihdr[:4], "big") == 2000, "first reference used as the card avatar")
        web_srv.shutdown()
    llm_srv.shutdown()
    mar_srv.shutdown()
    print("\nAll offline checks passed.")


if __name__ == "__main__":
    main()
