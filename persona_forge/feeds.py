"""Public feeds: the person's own public words, from interfaces platforms openly provide (no logins).

Supported (one per line in the web UI's "Public feeds" box, or --feed):
    reddit: u/name            or  https://www.reddit.com/user/name
    bluesky: name.bsky.social or  https://bsky.app/profile/name.bsky.social
    mastodon: @user@instance  or  https://instance/@user
    youtube: @handle          or  https://www.youtube.com/@handle  (titles + descriptions from the channel feed)
    tumblr: blogname          or  https://blogname.tumblr.com
    rss: https://…            (Substack, blogs, any RSS/Atom feed; podcast feeds bring audio episodes)
    podcast: <search words>   (episodes they appeared on, via Apple's public podcast directory → transcribed)

X/Twitter, Instagram, TikTok, Threads and Twitch have no open feed: in creator mode, socials.py reads their PUBLIC
profile pages instead (bio + whatever posts the page serves without a login), once the profile is confirmed as the
creator's. Their official data export (added as a text source) is still the richest option.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

from .research import BROWSER_HEADERS, Source
from .util import collapse_ws, log

FEED_UA = "PersonaForge/1.0 (public-feed reader for a creator's own character card)"


@dataclass
class FeedSpec:
    platform: str
    ident: str

    def label(self) -> str:
        return f"{self.platform}: {self.ident}"


@dataclass
class FeedResult:
    spec: FeedSpec
    items: list[dict] = field(default_factory=list)     # {date, text, where, kind, title?}
    audio: list[dict] = field(default_factory=list)     # podcast episodes {url, title, date}
    note: str = ""


# ═══════════════════════════════════════════════════════════ parsing what the user typed
def parse_feed_line(line: str) -> FeedSpec | None:
    line = line.strip().strip(",")
    if not line or line.startswith("#"):
        return None
    m = re.match(r"^(reddit|bluesky|bsky|mastodon|youtube|yt|tumblr|rss|feed|podcast|substack)\s*:\s*(.+)$", line, re.I)
    if m:
        plat, ident = m.group(1).lower(), m.group(2).strip()
        plat = {"bsky": "bluesky", "yt": "youtube", "feed": "rss"}.get(plat, plat)
        if plat == "substack":
            plat, ident = "rss", (ident if "://" in ident else f"https://{ident.split('.')[0]}.substack.com") + "/feed"
        if plat == "rss":
            return FeedSpec("rss", ident) if "://" in ident else None
        if plat == "podcast":
            return FeedSpec("podcast", ident)
        if "://" in ident:  # a profile link after the prefix, e.g. "reddit: https://reddit.com/user/x"
            return spec_from_url(ident) or FeedSpec(plat, ident)
        if plat == "reddit":
            ident = re.sub(r"^/?u(ser)?/", "", ident).strip("/")
        return FeedSpec(plat, ident)
    if re.fullmatch(r"@?[\w.]+@[\w-]+(\.[\w-]+)+", line):
        return FeedSpec("mastodon", line if line.startswith("@") else "@" + line)
    if re.fullmatch(r"/?u/[\w-]+/?", line):
        return FeedSpec("reddit", line.strip("/").split("/")[-1])
    return spec_from_url(line if "://" in line else "https://" + line)


def spec_from_url(url: str) -> FeedSpec | None:
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("old.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host == "reddit.com" and len(parts) >= 2 and parts[0] in ("user", "u"):
        return FeedSpec("reddit", parts[1])
    if host == "bsky.app" and len(parts) >= 2 and parts[0] == "profile":
        return FeedSpec("bluesky", parts[1])
    if host == "youtube.com" and parts and (parts[0].startswith("@") or parts[0] in ("channel", "c", "user")):
        return FeedSpec("youtube", parts[0] if parts[0].startswith("@") else "/".join(parts[:2]))
    if host.endswith(".tumblr.com") and host != "www.tumblr.com":
        return FeedSpec("tumblr", host.split(".")[0])
    if host == "tumblr.com" and parts:
        return FeedSpec("tumblr", parts[0])
    if host.endswith(".substack.com"):
        return FeedSpec("rss", f"https://{host}/feed")
    if parts and parts[0].startswith("@") and "." in host and not host.endswith(("youtube.com", "tiktok.com",
                                                                                 "threads.net", "medium.com")):
        return FeedSpec("mastodon", f"{parts[0]}@{host}")       # https://instance/@user
    if url.rstrip("/").endswith(("/feed", "/rss", ".rss", ".xml", "/atom")):
        return FeedSpec("rss", url)
    return None


def specs_from_links(urls: list[str]) -> list[FeedSpec]:
    """Feeds found among the creator's own links (e.g. their Linktree)."""
    out: list[FeedSpec] = []
    for u in urls:
        s = spec_from_url(u)
        if s and s.platform in ("reddit", "bluesky", "youtube", "tumblr", "mastodon", "rss") and s not in out:
            out.append(s)
    return out


# ═══════════════════════════════════════════════════════════ collecting
def _date(ts) -> str:
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return str(ts)[:10]
    except (ValueError, OSError):
        return ""


def _html_text(html: str) -> str:
    from bs4 import BeautifulSoup

    return collapse_ws(BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True))


class FeedCollector:
    # Endpoints (overridable, e.g. in tests)
    REDDIT = "https://www.reddit.com"
    REDDIT_OAUTH = "https://oauth.reddit.com"
    BLUESKY = "https://public.api.bsky.app"
    YOUTUBE = "https://www.youtube.com"
    ITUNES = "https://itunes.apple.com"
    TUMBLR_FMT = "https://{blog}.tumblr.com/rss"
    MASTODON_SCHEME = "https://"

    def __init__(self, max_items: int = 300, confirm_adult: bool = False, reddit_id: str = "", reddit_secret: str = "",
                 person: str = ""):
        self.max_items = max_items
        self.confirm_adult = confirm_adult
        self.reddit_id, self.reddit_secret = reddit_id, reddit_secret
        self.person = person
        self.http = requests.Session()
        self.http.headers["User-Agent"] = FEED_UA
        self._reddit_token = None

    def collect(self, spec: FeedSpec) -> FeedResult:
        fn = getattr(self, f"_{spec.platform}", None)
        if not fn:
            return FeedResult(spec, note="unsupported feed type")
        try:
            res = fn(spec)
        except requests.RequestException as e:
            return FeedResult(spec, note=f"could not connect ({e.__class__.__name__})")
        except Exception as e:  # noqa: BLE001
            return FeedResult(spec, note=str(e)[:200])
        res.items = [i for i in res.items if i.get("text", "").strip()][: self.max_items]
        return res

    # ── Reddit (public JSON; app-only OAuth if REDDIT_CLIENT_ID/SECRET are set)
    def _reddit_get(self, path: str, params: dict) -> dict:
        if self.reddit_id and self.reddit_secret and self._reddit_token is None:
            r = self.http.post(f"{self.REDDIT}/api/v1/access_token", auth=(self.reddit_id, self.reddit_secret),
                               data={"grant_type": "client_credentials"}, timeout=30)
            self._reddit_token = r.json().get("access_token", "") if r.ok else ""
        if self._reddit_token:
            r = self.http.get(f"{self.REDDIT_OAUTH}{path}", params=params, timeout=30,
                              headers={"Authorization": f"bearer {self._reddit_token}"})
        else:
            r = self.http.get(f"{self.REDDIT}{path}.json", params={**params, "raw_json": 1}, timeout=30,
                              cookies={"over18": "1"} if self.confirm_adult else None)
        if r.status_code in (403, 429):
            raise RuntimeError(f"Reddit refused the request (HTTP {r.status_code}). Reddit limits anonymous reading – "
                               "add REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET to .env (free “script” app at "
                               "reddit.com/prefs/apps) and run again.")
        if r.status_code == 404:
            raise RuntimeError("Reddit user not found (or suspended)")
        r.raise_for_status()
        return r.json()

    def _reddit(self, spec: FeedSpec) -> FeedResult:
        res = FeedResult(spec)
        user = spec.ident
        for kind in ("comments", "submitted"):
            after = None
            while len([i for i in res.items if i["kind"] == kind]) < self.max_items // 2:
                data = self._reddit_get(f"/user/{user}/{kind}", {"limit": 100, **({"after": after} if after else {})})
                children = data.get("data", {}).get("children", [])
                for c in children:
                    d = c.get("data", {})
                    if (d.get("author") or "").lower() != user.lower():
                        continue
                    body = d.get("body") if kind == "comments" else "\n".join(filter(None, [d.get("title"), d.get("selftext")]))
                    body = "\n".join(l for l in (body or "").splitlines() if not l.lstrip().startswith(">"))  # drop quotes
                    if body.strip() and body.strip() not in ("[deleted]", "[removed]"):
                        res.items.append({"date": _date(d.get("created_utc")), "text": body.strip(),
                                          "where": f"r/{d.get('subreddit', '')}", "kind": kind})
                after = data.get("data", {}).get("after")
                if not after or not children:
                    break
                time.sleep(1.0)
        return res

    # ── Bluesky (open AppView API)
    def _bluesky(self, spec: FeedSpec) -> FeedResult:
        res = FeedResult(spec)
        actor = spec.ident.lstrip("@")
        cursor = None
        while len(res.items) < self.max_items:
            r = self.http.get(f"{self.BLUESKY}/xrpc/app.bsky.feed.getAuthorFeed",
                              params={"actor": actor, "limit": 100, "filter": "posts_with_replies",
                                      **({"cursor": cursor} if cursor else {})}, timeout=30)
            if r.status_code == 400:
                raise RuntimeError("Bluesky account not found")
            r.raise_for_status()
            j = r.json()
            for f in j.get("feed", []):
                post = f.get("post", {})
                if f.get("reason") or (post.get("author", {}).get("handle", "").lower() != actor.lower()):
                    continue  # reposts / other people
                rec = post.get("record", {})
                res.items.append({"date": _date(rec.get("createdAt", "")), "text": rec.get("text", ""),
                                  "where": "Bluesky", "kind": "reply" if rec.get("reply") else "post"})
            cursor = j.get("cursor")
            if not cursor or not j.get("feed"):
                break
        return res

    # ── Mastodon (public API of the account's server)
    def _mastodon(self, spec: FeedSpec) -> FeedResult:
        m = re.match(r"^@?([^@\s]+)@([^@\s/]+)$", spec.ident)
        if not m:
            raise RuntimeError("write Mastodon accounts as @user@server")
        user, inst = m.groups()
        base = f"{self.MASTODON_SCHEME}{inst}"
        r = self.http.get(f"{base}/api/v1/accounts/lookup", params={"acct": user}, timeout=30)
        if r.status_code == 404:
            raise RuntimeError("Mastodon account not found")
        r.raise_for_status()
        acc_id = r.json()["id"]
        res, max_id = FeedResult(spec), None
        while len(res.items) < self.max_items:
            r = self.http.get(f"{base}/api/v1/accounts/{acc_id}/statuses",
                              params={"limit": 40, "exclude_reblogs": "true", **({"max_id": max_id} if max_id else {})},
                              timeout=30)
            r.raise_for_status()
            batch = r.json()
            for st in batch:
                res.items.append({"date": _date(st.get("created_at", "")), "text": _html_text(st.get("content", "")),
                                  "where": "Mastodon", "kind": "reply" if st.get("in_reply_to_id") else "post"})
            if not batch:
                break
            max_id = batch[-1]["id"]
        return res

    # ── YouTube (public channel feed: latest videos' titles + descriptions)
    def _youtube(self, spec: FeedSpec) -> FeedResult:
        ident = spec.ident
        if re.fullmatch(r"UC[\w-]{22}", ident):
            channel_id = ident
        else:
            path = ident if ident.startswith(("@", "channel/", "c/", "user/")) else "@" + ident
            page = self.http.get(f"{self.YOUTUBE}/{path}", headers=BROWSER_HEADERS, timeout=30,
                                 cookies={"CONSENT": "YES+1"})
            m = (re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', page.text)
                 or re.search(r'itemprop="identifier" content="(UC[\w-]{22})"', page.text)
                 or re.search(r'channel_id=(UC[\w-]{22})', page.text))
            if not m:
                raise RuntimeError("YouTube channel not found")
            channel_id = m.group(1)
        r = self.http.get(f"{self.YOUTUBE}/feeds/videos.xml", params={"channel_id": channel_id}, timeout=30)
        r.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
        root = ET.fromstring(r.content)
        res = FeedResult(spec, note="YouTube's public feed lists the latest ~15 videos")
        for e in root.findall("a:entry", ns):
            title = e.findtext("a:title", "", ns)
            desc = e.findtext("media:group/media:description", "", ns)
            res.items.append({"date": _date(e.findtext("a:published", "", ns)), "text": f"{title}\n{desc}".strip(),
                              "where": "YouTube video", "kind": "video", "title": title})
        return res

    # ── RSS / Atom (Substack, blogs, Tumblr, podcast feeds)
    def _tumblr(self, spec: FeedSpec) -> FeedResult:
        return self._rss(FeedSpec("rss", self.TUMBLR_FMT.format(blog=spec.ident)), label=spec)

    def _rss(self, spec: FeedSpec, label: FeedSpec | None = None) -> FeedResult:
        r = self.http.get(spec.ident, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        res = FeedResult(label or spec)
        atom = "{http://www.w3.org/2005/Atom}"
        items = root.findall(".//item") or root.findall(f".//{atom}entry")
        for it in items[: self.max_items]:
            def t(*tags):
                for tag in tags:
                    v = it.findtext(tag)
                    if v:
                        return v
                return ""
            title = t("title", f"{atom}title")
            body = t("{http://purl.org/rss/1.0/modules/content/}encoded", "description", f"{atom}content", f"{atom}summary")
            date = t("pubDate", f"{atom}published", f"{atom}updated")
            enc = it.find("enclosure")
            if enc is not None and (enc.get("type", "").startswith("audio") or
                                    re.search(r"\.(mp3|m4a|aac|ogg|opus)(\?|$)", enc.get("url", ""), re.I)):
                res.audio.append({"url": enc.get("url"), "title": title, "date": date[:16]})
            res.items.append({"date": date[:16], "text": collapse_ws(f"{title}\n{_html_text(body)}"),
                              "where": urlparse(spec.ident).netloc, "kind": "post", "title": title})
        return res

    # ── Podcasts they appeared on (Apple's public directory search)
    def _podcast(self, spec: FeedSpec) -> FeedResult:
        r = self.http.get(f"{self.ITUNES}/search", params={"term": spec.ident, "media": "podcast",
                                                            "entity": "podcastEpisode", "limit": 50}, timeout=30)
        r.raise_for_status()
        who = (self.person or spec.ident).lower()
        surname = who.split()[-1] if who.split() else who
        res = FeedResult(spec)
        for ep in r.json().get("results", []):
            hay = f"{ep.get('trackName', '')} {ep.get('description', '')}".lower()
            if who not in hay and not (len(surname) > 3 and surname in hay and who.split()[0] in hay):
                continue
            if ep.get("episodeUrl"):
                res.audio.append({"url": ep["episodeUrl"], "title": f"{ep.get('collectionName', '')} – {ep.get('trackName', '')}",
                                  "date": (ep.get("releaseDate") or "")[:10]})
        res.audio.sort(key=lambda a: a["date"], reverse=True)
        return res


def feed_source(res: FeedResult, person: str, max_chars: int = 60_000) -> Source | None:
    """All collected items of one feed as one source (newest first), clearly marked as the person's own words."""
    if not res.items:
        return None
    lines = [f"[Public {res.spec.platform} activity of {res.spec.ident} – {person}'s own words, newest first. "
             "Each line is one post/comment.]"]
    size = 0
    for it in sorted(res.items, key=lambda i: i.get("date", ""), reverse=True):
        entry = f"[{it.get('date', '')} · {it.get('where', '')} · {it.get('kind', '')}] {it['text']}"
        if size + len(entry) > max_chars:
            break
        lines.append(entry)
        size += len(entry)
    url = {"reddit": f"https://www.reddit.com/user/{res.spec.ident}",
           "bluesky": f"https://bsky.app/profile/{res.spec.ident}",
           "youtube": f"https://www.youtube.com/{res.spec.ident}"}.get(res.spec.platform, res.spec.ident)
    return Source(url=url if "://" in url else f"feed:{res.spec.label()}", title=f"Public feed – {res.spec.label()}",
                  text="\n\n".join(lines), kind="feed")


def own_messages(results: list[FeedResult]) -> list[str]:
    """The person's own short-form writing (posts, comments, replies) for the voice profile."""
    msgs = []
    for res in results:
        if res.spec.platform in ("reddit", "bluesky", "mastodon"):
            msgs += [i["text"] for i in res.items]
        elif res.spec.platform in ("youtube",):
            msgs += [i["text"].split("\n", 1)[-1][:600] for i in res.items if "\n" in i["text"]]
    return [m.strip() for m in msgs if m and len(m.strip()) > 2]
