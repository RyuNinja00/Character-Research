"""Offline tests for: clip catalogues, social discovery, confirm-or-correct sheet, fan onboarding, card token budget
(refined copy) and the editing assistant (incl. its web endpoints).

Run:  python -m tests.test_features
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("PERSONA_FORGE_PACE", "0")
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from persona_forge import budget, clips, confirm, editor, marinara, onboarding, socials  # noqa: E402
from persona_forge.research import Researcher, Source  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


class FakeLLM:
    """chat_json(system, user) → fn(system, user)."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def chat_json(self, system, user, **kw):
        self.calls.append((system, user))
        return self.fn(system, user)


# ═══════════════════════════════════════════════════════════ clip catalogues
MV = "https://www.manyvids.com"
STORE = f"{MV}/Profile/123/ivywren/Store/Videos/"
LIST1 = f"""<html><head><title>Ivy Wren Store</title></head><body>
<nav><a href="{MV}/tags/popular">popular tag in menu</a></nav>
<script type="application/ld+json">{json.dumps({"@type": "ItemList", "itemListElement": [
    {"@type": "VideoObject", "name": "Stepbro Caught Me Gaming", "url": f"{MV}/Video/1/stepbro-caught-me",
     "keywords": "step-sibling, gamer girl, POV", "description": "<p>You walk in while I'm streaming…</p>"},
    {"@type": "VideoObject", "name": "Teen Schoolgirl Fantasy", "url": f"{MV}/Video/9/x", "keywords": "teen, schoolgirl"},
]})}</script>
<ul>
 <li><a href="{MV}/Video/2/pov-gamer-tease" title="POV Gamer Girl Tease 12:31"><img alt="thumb"></a><span class="tag">gamer girl</span></li>
 <li><a href="{MV}/Video/3/joi-countdown"><img alt="JOI Countdown in Cosplay"></a></li>
 <li><a href="{MV}/Video/3/joi-countdown">JOI Countdown in Cosplay</a></li>
</ul>
<a rel="next" href="{STORE}?page=2">Next</a></body></html>"""
LIST2 = f"""<html><body><ul><li><a href="{MV}/Video/4/stepbro-again" title="Stepbro Again">x</a></li></ul></body></html>"""
DETAIL3 = f"""<html><head><meta property="og:title" content="JOI Countdown in Cosplay">
<meta property="og:description" content="I count you down in my favourite cosplay."></head><body>
<aside><a href="{MV}/tags/sidebar-noise">sidebar noise</a></aside>
<div class="tags-list"><a href="{MV}/tags/joi">JOI</a> <a href="{MV}/tags/cosplay">cosplay</a> <a href="{MV}/tags/hd">HD</a></div>
</body></html>"""
DETAIL4 = f"""<html><head><meta name="keywords" content="step-sibling, taboo roleplay"></head><body><h1>Stepbro Again</h1></body></html>"""
DETAIL2 = """<html><head><meta property="og:title" content="POV Gamer Girl Tease"></head><body>
<a href="/tags/pov">POV</a><a href="/tags/gamer-girl">gamer girl</a></body></html>"""
PAGES = {STORE: LIST1, f"{STORE}?page=2": LIST2, f"{MV}/Video/3/joi-countdown": DETAIL3,
         f"{MV}/Video/4/stepbro-again": DETAIL4, f"{MV}/Video/2/pov-gamer-tease": DETAIL2}


def fake_fetch(self, url, title="", kind="web", keep_html=False, via=""):
    html = PAGES.get(url)
    self._fetched = getattr(self, "_fetched", []) + [url]
    return Source(url, title or url, "page text " * 20, kind, {"html": html}) if html else None


def test_clips():
    print("clip catalogues")
    check(clips.is_clip_link(f"{MV}/Video/1/x") and not clips.is_clip_link(f"{MV}/Profile/123/ivy"),
          "clip links recognised per platform")
    check(clips.store_listing_urls(f"{MV}/Profile/123/ivywren")[0] == STORE, "ManyVids profile → its Store/Videos list")
    check(clips.store_listing_urls("https://www.pornhub.com/model/ivy-wren")[0] == "https://www.pornhub.com/model/ivy-wren/videos",
          "Pornhub model page → its videos tab")
    r = Researcher(search_provider="none")
    orig = Researcher.fetch
    Researcher.fetch = fake_fetch
    try:
        cat = clips.read_store(r, STORE, max_pages=3, max_details=10)
    finally:
        Researcher.fetch = orig
    titles = [c.title for c in cat.clips]
    check(len(cat.clips) == 4, f"4 clips catalogued from JSON-LD + links + page 2 (got {titles})")
    check(not any("Teen" in t for t in titles) and cat.dropped_excluded >= 1, "minor-coded clip dropped and counted")
    check(f"{STORE}?page=2" in r._fetched, "pagination followed (rel=next)")
    joi = next(c for c in cat.clips if c.title.startswith("JOI"))
    check("joi" in joi.tags and "cosplay" in joi.tags and "hd" not in joi.tags and "sidebar noise" not in joi.tags,
          f"detail page tags read, noise and sidebar ignored ({joi.tags})")
    check(joi.description.startswith("I count you down"), "description from the clip page")
    counts = cat.tag_counts()
    check(counts["step sibling"] == 2 and counts["gamer girl"] >= 2, f"tags counted across clips ({dict(counts)})")
    check(any(len(c.title) < 40 and c.title == "POV Gamer Girl Tease" for c in cat.clips), "duration stripped from link titles")
    src = clips.catalog_source(cat, "Ivy Wren")
    check(src.kind == "platform" and "step sibling ×2" in src.text and "OWN manyvids.com store" in src.text,
          "catalogue becomes a creator-own source with tag counts")
    md = clips.render_catalog_md("Ivy Wren", [cat])
    check("| step sibling | 2 |" in md, "clip_catalog.md has the tag table")
    # only the creator's own store is read
    r.handles = ["ivywren"]
    picked = []
    clips_read = clips.read_store
    clips.read_store = lambda rr, u, **kw: picked.append(u)
    try:
        r.clip_catalogs([f"{MV}/Profile/123/ivywren", f"{MV}/Profile/555/someoneelse", "https://example.com/x"],
                        ["ivywren"])
    finally:
        clips.read_store = clips_read
    check(picked == [f"{MV}/Profile/123/ivywren"], "other people's stores are never read")
    return cat


# ═══════════════════════════════════════════════════════════ socials
def test_socials():
    print("social profiles")
    check(socials.handle_from_url("https://x.com/IvyWren") == "ivywren", "X handle")
    check(socials.handle_from_url("https://www.tiktok.com/@ivy.wren") == "ivy.wren", "TikTok handle")
    check(socials.handle_from_url("https://www.tiktok.com/discover/x") == "", "TikTok non-profile ignored")
    check(socials.handle_from_url("https://bsky.app/profile/ivy.bsky.social") == "ivy", "Bluesky handle")
    check(socials.handle_from_url("https://ivywren.tumblr.com") == "ivywren", "Tumblr handle")
    check(socials.names_them("Ivy Wren 💋 gamer", "Ivy Wren", ["ivywren"], []), "bio naming them counts")
    check(not socials.names_them("Ivy from Ohio, plumber", "Ivy Wren", ["ivywren"], [], exclude="ivywren"),
          "a stranger with the same username does not count")
    check(socials.names_them("links: linktr.ee/iivywren", "X Y", [], ["https://linktr.ee/iivywren"]), "link hub in bio counts")
    html = ('<script id="__UNIVERSAL_DATA__" type="application/json">' + json.dumps({"items": [
        {"desc": "new cosplay drop tonight 😈 who's ready", "author": {"uniqueId": "ivywren"}},
        {"desc": "someone else's caption here!!", "author": {"uniqueId": "other"}},
        {"desc": "lowkey nervous about this one lol", "author": {"uniqueId": "ivywren"}}]}) + "</script>")
    caps = socials.captions_from_html(html, "ivywren")
    check(caps == ["new cosplay drop tonight 😈 who's ready", "lowkey nervous about this one lol"],
          f"their captions read, other people's skipped ({caps})")

    r = Researcher(search_provider="none")
    r.search = lambda q, max_results=8: [("https://x.com/ivywren", "Ivy Wren (@ivywren) / X"),
                                         ("https://www.instagram.com/ivywren/", "ivywren • Instagram photos"),
                                         ("https://www.tiktok.com/@someoneelse", "Some One (@someoneelse)")]
    pages = {"https://www.twitch.tv/ivywren": "<html><head><title>ivywren - Twitch</title>"
                                              '<meta property="og:description" content="Ivy Wren plays horror games"></head></html>',
             "https://www.tiktok.com/@ivywren": "<html><head><title>Ivy Wren (@ivywren) | TikTok</title></head><body>"
                                                + html + "</body></html>",
             "https://x.com/ivywren": "<html><head><meta property='og:description' content='gamer, cosplayer, menace'></head></html>"}

    def fetch(url, title="", kind="web", keep_html=False, via=""):
        h = pages.get(url)
        return Source(url, title or url, "x" * 80, kind, {"html": h}) if h else None

    r.fetch = fetch

    class Resp:
        def __init__(self, code, data=None):
            self.status_code, self._d = code, data or {}

        def json(self):
            return self._d

    r.http.get = lambda url, **kw: (Resp(200, {"displayName": "Ivy Wren", "description": "hi"})
                                    if "getProfile" in url else Resp(404))
    profs = socials.discover(r, "Ivy Wren", ["ivywren"], ["https://www.reddit.com/user/ivywren"],
                             hub_urls=["https://linktr.ee/iivywren"])
    by = {(p.platform, p.handle): p for p in profs}
    check(by[("reddit", "ivywren")].confirmed and "link hub" in by[("reddit", "ivywren")].how, "hub-linked profile confirmed")
    check(by[("x", "ivywren")].confirmed, "search result titled with their name confirmed")
    check(not by[("instagram", "ivywren")].confirmed, "search result that doesn't name them is not confirmed")
    check(("tiktok", "someoneelse") not in by, "other usernames ignored")
    check(by[("twitch", "ivywren")].confirmed and by[("bluesky", "ivywren")].confirmed, "guessed profiles confirmed by their own bio")
    check(not by[("threads", "ivywren")].confirmed, "guessed profile that didn't load stays unconfirmed")
    specs = socials.feed_specs(profs)
    check(any(s.platform == "bluesky" and s.ident == "ivywren.bsky.social" for s in specs)
          and any(s.platform == "reddit" for s in specs), "confirmed feed platforms become public feeds")
    srcs = socials.read_pages(r, profs, "Ivy Wren")
    tik = next(s for s in srcs if "tiktok" in s.url)
    check(tik.kind == "social" and "new cosplay drop" in tik.text, "page profiles read: bio + captions")
    check(not any("instagram" in s.url for s in srcs), "unconfirmed profiles never read")
    check("| instagram |" in socials.render_md("Ivy Wren", profs), "social_profiles.md lists every profile")
    from persona_forge.synthesize import OWN_CHANNEL_KINDS

    check("social" in OWN_CHANNEL_KINDS, "confirmed socials count as the creator's own channel")


# ═══════════════════════════════════════════════════════════ confirm sheet
DOSSIER = {
    "name": "Ivy Wren", "one_line": "Horror-game streamer and cosplayer with a teasing streak",
    "personality_traits": ["teasing", "shy offline", "competitive"],
    "speech_style": {"tone": "lowercase, fast", "verbal_tics": ["says 'babe' a lot", "lots of 😭"]},
    "signature_quotes": ["no thoughts just vibes"],
    "intimacy": {"flirting_style": "slow-burn teasing",
                 "content_themes": [{"theme": "step-sibling roleplay", "prominence": "signature", "evidence": ""},
                                    {"theme": "gamer girl", "prominence": "frequent"}],
                 "persona_kinks": ["step-sibling roleplay between adults (from posted content)", "being watched (from posted content)"],
                 "stated_limits": ["no degradation"]},
}


def test_confirm(cat):
    print("confirm-or-correct sheet")
    d = json.loads(json.dumps(DOSSIER))
    llm = FakeLLM(lambda s, u: {"questions": [
        {"gap": "Insecurities & fears", "question": "What gets under her skin?", "options": ["being called fake", "bad lag", "teen stuff"]},
        {"gap": "Made up gap", "question": "?", "options": ["x"]}]})
    sheet = confirm.build_sheet(d, nsfw=True, catalogs=[cat], gaps=["Insecurities & fears", "Appearance"], llm=llm)
    items = sheet["items"]
    secs = {i["section"] for i in items}
    check({"Who they are", "How they talk", "Flirting & dynamics", "Content themes", "Kinks the persona leans into",
           "Limits", "Gaps (best guesses)"} <= secs, f"all sections present ({sorted(secs)})")
    theme = next(i for i in items if i["match"] == "step-sibling roleplay")
    check(theme["type"] == "choice" and "2 of 4 clips" in theme["evidence"], f"theme question carries clip evidence ({theme['evidence']})")
    new_tag = [i for i in items if i["match"].startswith("__new__:")]
    check(any("cosplay" in i["match"] or "pov" in i["match"] or "joi" in i["match"] for i in new_tag),
          "frequent clip tags missing from the persona are offered")
    gap = next(i for i in items if i["section"].startswith("Gaps"))
    check("teen stuff" not in gap["options"] and gap["options"][-1] == "None of these", "gap guesses filtered, 'None' added")
    check(not any("Made up gap" in i["prompt"] for i in items), "unknown gaps ignored")
    # answer
    a = {i["id"]: i for i in items}
    trait = next(i for i in items if i["guess"] == "shy offline")
    a[trait["id"]]["answer"] = {"action": "drop"}
    tone = next(i for i in items if i["path"] == "speech_style.tone")
    a[tone["id"]]["answer"] = {"action": "change", "value": "all lowercase, rapid-fire, lots of 'lmao'"}
    a[theme["id"]]["answer"] = {"action": "pick", "value": "Occasional"}
    gamer = next(i for i in items if i["match"] == "gamer girl")
    a[gamer["id"]]["answer"] = {"action": "pick", "value": "Drop it"}
    a[new_tag[0]["id"]]["answer"] = {"action": "pick", "value": "Frequent"}
    kink = next(i for i in items if i["guess"].startswith("being watched"))
    a[kink["id"]]["answer"] = {"action": "drop"}
    lim = next(i for i in items if i["type"] == "text")
    a[lim["id"]]["answer"] = {"action": "text", "value": "- never mention her real city\nno face-reveal talk"}
    a[gap["id"]]["answer"] = {"action": "pick", "value": "being called fake"}
    bad = next(i for i in items if i["guess"] == "competitive")
    a[bad["id"]]["answer"] = {"action": "change", "value": "teen at heart"}  # excluded word: must be skipped
    stats = confirm.apply_sheet(d, sheet)
    check("shy offline" not in d["personality_traits"] and "competitive" in d["personality_traits"],
          "drop removes a trait; excluded correction refused")
    check(d["speech_style"]["tone"].startswith("all lowercase"), "change replaces a value")
    th = {t["theme"]: t for t in d["intimacy"]["content_themes"]}
    check(th["step-sibling roleplay"]["prominence"] == "occasional" and "gamer girl" not in th, "themes re-ranked / dropped")
    check(any(t.get("evidence", "").endswith("(confirmed)") for t in th.values()), "new clip tag added as a theme")
    check(len(d["intimacy"]["persona_kinks"]) == 1, "kink dropped")
    check(d["inner_life"]["insecurities_and_fears"] == ["being called fake"] if isinstance(d.get("inner_life", {}).get("insecurities_and_fears"), list)
          else d["inner_life"]["insecurities_and_fears"] == "being called fake", "picked gap guess stored at its path")
    check(confirm.confirmed_limits(d) == ["never mention her real city", "no face-reveal talk"], "limits recorded")
    merged = confirm.merge_limits("- no degradation", d)
    check("never mention her real city" in merged and merged.startswith("- no degradation"), "limits merged into boundaries")
    check(stats["skipped"] == 1 and sheet["applied"], f"stats + applied stamp ({stats})")
    md = confirm.render_md(sheet)
    check("[ ] Keep  [ ] Drop" in md and "## Content themes" in md, "printable checklist")
    return d


# ═══════════════════════════════════════════════════════════ onboarding
def test_onboarding():
    print("fan onboarding")
    raw = {"questions": [
        {"var": "player", "question": "what do i call u? 😏"},
        {"var": "name", "question": "Your nickname?", "options": []},                    # reserved → renamed
        {"var": "how-we-met", "question": "how do we know each other?", "options": [
            {"label": "Longtime sub"}, {"label": "Met at TwitchCon", "value": "met at TwitchCon"}], "allow_custom": True},
        {"var": "into", "question": "what are u into 👀", "multi": True, "options": [
            {"label": "step-sibling roleplay"}, {"label": "teen roleplay"}, {"label": "being watched"}]},
        {"var": "bad", "question": "pick one", "options": [{"label": "schoolgirl"}]},       # all options excluded → dropped
        {"var": "pace", "question": "fast or slow?", "options": ["slow", "fast"]},           # unused → appended
    ],
        "description": "{{player}} ({{name}}) knows {{char}}: {{how-we-met}}. Into {{into}}. {{mystery}} She is a teen.",
        "personality": "{{#if into}}Bold.", "backstory": "", "appearance": "", "scenario": "Tonight {{player}} visits."}
    ob = onboarding.validate(raw, {}, True)
    names = [v["variableName"] for v in ob["variables"]]
    check(names[0] == "player" and "my_name" in names and "how_we_met" in names and "bad" not in names,
          f"variable names valid and not reserved ({names})")
    import re as _re

    check(all(_re.match(r"^[A-Za-z_]\w{0,63}$", n) for n in names), "every name matches Marinara's rule")
    into = next(v for v in ob["variables"] if v["variableName"] == "into")
    check([o["label"] for o in into["options"]] == ["step-sibling roleplay", "being watched"] and into["multiSelect"],
          "excluded options removed, multi-select kept")
    check("{{my_name}}" in ob["description"] and "{{how_we_met}}" in ob["description"], "renamed variables rewritten in fields")
    check("{{mystery}}" not in ob["description"] and "teen" not in ob["description"].lower().replace("18+", ""),
          "unknown placeholder and excluded sentence removed")
    check(ob["description"].startswith(onboarding.ADULT_LINE), "persona states the player is an adult")
    check("{{#if" not in ob["personality"], "unbalanced #if stripped")
    check("{{pace}}" in ob["description"], "unused question gets used")
    check(ob["enabled"] and ob["cardVariables"] == [] and all(len({o["id"] for o in v["options"]}) == len(v["options"])
                                                               for v in ob["variables"]), "unique option ids")
    # AI path with fallback
    d = json.loads(json.dumps(DOSSIER))
    gen = onboarding.generate(FakeLLM(lambda s, u: {"oops": True}), d, {"scenario": "x"}, "Ivy Wren", True)
    check(len(gen["variables"]) >= 4 and any(v["variableName"] == "into" for v in gen["variables"]),
          "unusable AI answer → template built from the persona's themes")
    sysmsg = []
    onboarding.generate(FakeLLM(lambda s, u: sysmsg.append((s, u)) or raw), d, {"scenario": "x"}, "Ivy Wren", True)
    check("persona_kinks" in sysmsg[0][1] and "never invent" in sysmsg[0][0], "AI is given the persona's kinks and told not to invent")
    card = marinara.build_card("Ivy Wren", {"description": "d"}, d, [], creator="t", sources=[], onboarding=gen)
    check(card["data"]["extensions"]["onboarding"]["enabled"], "onboarding embedded in extensions.onboarding")
    return gen


# ═══════════════════════════════════════════════════════════ budget
def big_card():
    long = ("Ivy is a horror-game streamer who teases her chat relentlessly and hides how shy she is. " * 30).strip()
    data = {"name": "Ivy Wren", "description": long, "personality": "teasing, shy", "scenario": "Stream ends. " * 20,
            "first_mes": "hiii", "alternate_greetings": ["yo"], "mes_example": "<START>\n{{user}}: hi\n{{char}}: omg hiii 😭\n" * 6,
            "system_prompt": "Stay in character.\n\n[Hard boundaries set by the creator — never break these]\n- no degradation",
            "post_history_instructions": "", "creator_notes": "Authorization: signed release\n\nnotes", "tags": ["nsfw"],
            "character_version": "1.0", "summary": "Ivy.",
            "extensions": {"appearance": "Red hair. " * 40, "backstory": "Born online. " * 40,
                           "depth_prompt": {"prompt": "lowercase", "depth": 4, "role": "system"},
                           "persona_forge": {"consent_note": "signed release"}}}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def test_budget():
    print("card token budget")
    card = big_card()
    before = budget.card_tokens(card["data"])
    check(before["permanent"] > 900, f"test card is big (~{before['permanent']} tokens)")

    def condense(system, user):
        assert "TARGETS" in user and "Hard boundaries" not in user.split("CARD FIELDS:")[1], "boundaries never sent to condense"
        return {"description": "Overview: {{char}} streams horror games.\nPersonality: teasing, secretly shy.",
                "appearance": "Red hair, freckles.", "backstory": "Started streaming in 2022.",
                "scenario": "After a stream.", "system_prompt": "Stay in character. [Hard boundaries set by the creator — removed!]",
                "mes_example": "{{user}}: hi\n{{char}}: omg hiii 😭",
                "to_lorebook": [{"name": "Ivy's stream setup", "keys": ["setup", "stream room"],
                                 "content": "Ivy streams from a cluttered room full of plushies and a glowing PC she built herself."}]}

    data, rep = budget.fit_to_budget(FakeLLM(condense), card, 400, nsfw=True, source_name="Ivy_Wren.png")
    check(rep["within"] and rep["after"]["permanent"] <= 400, f"refined copy fits the budget (~{rep['after']['permanent']})")
    check(data["system_prompt"].endswith("- no degradation") and "removed!" not in data["system_prompt"],
          "creator boundaries kept word for word")
    check(data["name"] == "Ivy Wren" and data["character_version"] == "1.0-refined" and "refined" in data["tags"]
          and data["summary"].startswith("[Refined"), "copy marked refined, name unchanged")
    check(any(e["name"] == "Ivy's stream setup" for e in data["character_book"]["entries"]), "cut details moved to the lorebook")
    check(data["mes_example"].startswith("<START>"), "example chats keep <START>")
    check(card["data"]["description"].startswith("Ivy is a horror"), "raw card untouched")
    # model that won't shorten → hard trim still gets under budget
    data2, rep2 = budget.fit_to_budget(FakeLLM(lambda s, u: {}), big_card(), 500, nsfw=True)
    check(rep2["after"]["permanent"] <= 500 and data2["system_prompt"].endswith("- no degradation"),
          f"hard trim fallback fits too (~{rep2['after']['permanent']}) and keeps boundaries")
    md = budget.render_report("Ivy Wren", rep, "Ivy_Wren.png", "Ivy_Wren_refined.png")
    check("| **Permanent total** |" in md and "Moved into the lorebook" in md, "refined_report.md")


# ═══════════════════════════════════════════════════════════ editing assistant
def test_editor(tmp: Path):
    print("editing assistant")
    card = big_card()
    data = card["data"]
    new, changed, warns = editor.apply_changes(data, {
        "system_prompt": "Be nice.", "creator_notes": "fresh notes", "first_mes": "hey {{user}} 😈",
        "scenario": "{{user}} is her teen neighbour", "tags": ["gamer"], "depth_prompt": "short replies"}, nsfw=True)
    check("[Hard boundaries" in new["system_prompt"] and new["system_prompt"].startswith("Be nice."),
          "boundaries survive a system-prompt edit")
    check(new["creator_notes"].startswith("Authorization: signed release"), "consent line survives")
    check("scenario" not in changed and any("excluded" in w for w in warns), "edit adding an excluded theme refused")
    check(new["tags"] == ["gamer", "nsfw"] and new["extensions"]["depth_prompt"]["prompt"] == "short replies", "tags/depth prompt")
    d = editor.word_diff("she is shy", "she is very shy")
    check(["+", "very "] in d or any(op == "+" and "very" in t for op, t in d), "word-level diff")

    def edit_llm(system, user):
        if "REQUEST:" in user:
            assert "Hard boundaries" not in user.split("REQUEST:")[0].split('"card"')[1].split("creator_boundaries")[0]
            return {"reply": "I made her opener more teasing.", "changes": {"first_mes": "omg u again?? 😏", "bogus": "x"},
                    "summary": ["Rewrote the first message"]}
        return {"reply": "Two things stand out.", "suggestions": [
            {"title": "Shorter greetings", "why": "They run long.", "instruction": "make the greetings shorter"},
            {"title": "", "instruction": "x"}]}

    llm = FakeLLM(edit_llm)
    p = editor.propose(llm, data, "make her opener more teasing", [], nsfw=True)
    check(list(p["changes"]) == ["first_mes"] and p["diffs"][0]["label"] == "First message", "proposal limited to real fields")
    sg = editor.suggest(llm, data, nsfw=True)
    check(len(sg["suggestions"]) == 1 and sg["suggestions"][0]["instruction"] == "make the greetings shorter",
          "suggestions carry a one-click request")
    check(editor.bump("1.0") == "1.1" and editor.bump("1.9-refined") == "1.10", "version bump")

    # web endpoints, end to end, in a temporary talent folder
    from persona_forge import webui

    webui.TALENT_ROOT = tmp / "talent"
    webui.ensure_talent_root()
    slug = webui.create_talent("Ivy Wren")
    tdir = webui.talent_dir(slug)
    od = tdir / "output" / "Ivy_Wren"
    od.mkdir(parents=True)
    (od / "dossier.json").write_text(json.dumps(DOSSIER), "utf-8")
    marinara.save_card_files(od, "Ivy_Wren", data, None)
    sheet = confirm.build_sheet(json.loads(json.dumps(DOSSIER)), nsfw=True, catalogs=None, gaps=[], llm=None)
    (od / "confirm_sheet.json").write_text(json.dumps(sheet), "utf-8")
    webui.llm_from_form = lambda form: llm
    info = webui.editor_info(slug)
    check(info["card"].endswith("Ivy_Wren.png") and info["tokens"]["permanent"] > 0, "editor finds the card and its size")
    info = webui.editor_action(slug, "suggest", {})
    check(info["messages"][-1]["suggestions"], "suggest → chat message with suggestions")
    info = webui.editor_action(slug, "ask", {"text": "make her opener more teasing"})
    m = info["messages"][-1]
    check(m["status"] == "proposed" and m["diffs"], "ask → proposal with before/after")
    info = webui.editor_action(slug, "apply", {"proposal": m["proposal"]})
    check(info["card"].endswith("Ivy_Wren_v1.1.png") and len(info["versions"]) == 2, "apply → new version, old kept")
    saved, _ = webui._load(tdir / info["card"])
    check(saved["first_mes"] == "omg u again?? 😏" and saved["extensions"]["persona_forge"]["edits"][0]["fields"] == ["first_mes"],
          "new version has the change and an edit log")
    info = webui.editor_action(slug, "use", {"card": "output/Ivy_Wren/Ivy_Wren.png"})
    check(info["card"].endswith("Ivy_Wren.png"), "switch back to an older version")
    llm.fn = lambda s, u: {}  # condenser that does nothing → hard trim
    info = webui.editor_action(slug, "refit", {"budget": 500})
    check(info["refined"] and info["refined"]["name"] == "Ivy_Wren_refined.png" and info["refined"]["tokens"] <= 500
          and not info["refined"]["stale"], "refit → refined copy within budget")
    check((od / "refined_report.md").exists() and (od / "Ivy_Wren.png").exists(), "raw card kept, report written")
    # confirm sheet endpoints
    cs = webui.confirm_get(slug)
    first = cs["items"][0]["id"]
    cs = webui.confirm_save(slug, {first: {"action": "drop", "value": ""}})
    check(cs["items"][0]["answer"]["action"] == "drop" and (od / "confirm_sheet.md").exists(), "answers saved + checklist")
    argv, _, _ = webui.build_command(slug, {"name": "Ivy Wren", "mode": "talent", "consent_note": "x", "card_budget": "900",
                                            "socials": False, "clips": True}, rebuild=False)
    check("--card-budget" in argv and "--no-socials" in argv and "--no-clips" not in argv, "form → new CLI options")


def test_cli_end_to_end(tmp: Path):
    """--from-dossier + --confirm-sheet + --card-budget against a fake OpenAI-compatible server."""
    print("command line: confirm sheet + onboarding + refined copy")
    from tests.test_offline import FAKE_CARD, FakeLLM as FakeServer, serve
    from persona_forge import cli

    srv, base = serve(FakeServer)
    out = tmp / "out"
    od = out / "Ivy_Wren"
    od.mkdir(parents=True)
    d = json.loads(json.dumps(DOSSIER))
    (od / "dossier.json").write_text(json.dumps(d), "utf-8")
    sheet = confirm.build_sheet(d, nsfw=True, catalogs=None, gaps=[], llm=None)
    for it in sheet["items"]:
        if it["type"] == "text":
            it["answer"] = {"action": "text", "value": "never mention her real city"}
    (od / "confirm_sheet.json").write_text(json.dumps(sheet), "utf-8")
    (tmp / "limits.txt").write_text("- no degradation\n", "utf-8")
    code = cli.main(["--from-dossier", str(od / "dossier.json"), "--talent", "--nsfw", "--consent-note", "signed",
                     "--boundaries", str(tmp / "limits.txt"), "--confirm-sheet", str(od / "confirm_sheet.json"),
                     "--card-budget", "400", "--base-url", base + "/v1", "--api-key", "k", "--model", "m",
                     "--out", str(out), "--no-cache"])
    srv.shutdown()
    check(code == 0, "run finished")
    raw = marinara.read_card_from_png((od / "Ivy_Wren.png").read_bytes())["data"]
    ref = marinara.read_card_from_png((od / "Ivy_Wren_refined.png").read_bytes())["data"]
    check("never mention her real city" in raw["system_prompt"] and "- no degradation" in raw["system_prompt"],
          "confirmed limits added to the card's hard boundaries")
    check(raw["extensions"]["persona_forge"].get("confirmed_on"), "card stamped as confirmed")
    check(raw["extensions"].get("onboarding", {}).get("enabled"), "fan onboarding in the card")
    check(budget.card_tokens(ref)["permanent"] <= 400 and raw["description"] == FAKE_CARD["description"].strip(),
          "refined copy fits, raw card untouched")
    check((od / "dossier.before_confirm.json").exists() and (od / "onboarding.md").exists() and (od / "refined_report.md").exists(),
          "backup, onboarding.md and refined_report.md written")
    saved = json.loads((od / "run_settings.json").read_text("utf-8"))
    check(saved.get("card_budget") == 400, "card budget remembered for rebuilds")


def main():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cat = test_clips()
        test_socials()
        test_confirm(cat)
        test_onboarding()
        test_budget()
        test_editor(tmp)
        test_cli_end_to_end(tmp)
    print("\nAll feature checks passed.")


if __name__ == "__main__":
    main()
