"""Find and read the creator's own PUBLIC social profiles.

Discovery (no logins, ever):
1. profiles linked from the creator's link hub — theirs by definition;
2. a web search for the stage name / handles on each platform — kept only when the result's username is one of
   the creator's handles AND the result title names them;
3. direct guesses (platform.com/<handle>) — kept only after the profile itself confirms it: its name or bio
   mentions the stage name, another of their handles, or their link hub.

A same-named stranger is never read: anything that can't be confirmed is listed in social_profiles.md as
"not confirmed" so a person can add it with --url / --feed if it really is theirs.

Reading:
* Reddit, Bluesky, YouTube, Tumblr, Mastodon → their open feeds (feeds.py), which also feed the voice profile.
* X, Instagram, TikTok, Threads, Twitch → the public profile page: bio plus whatever posts/captions the page
  serves without a login (often only the bio on X/Instagram; TikTok usually includes recent captions).
  The real-browser reader (--browser) gets more from JavaScript-heavy pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .util import collapse_ws, log

if TYPE_CHECKING:  # pragma: no cover
    from .research import Researcher, Source

# platform → (domains, profile URL template, feed platform or "")
PLATFORMS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "x": (("x.com", "twitter.com"), "https://x.com/{h}", ""),
    "instagram": (("instagram.com",), "https://www.instagram.com/{h}/", ""),
    "tiktok": (("tiktok.com",), "https://www.tiktok.com/@{h}", ""),
    "threads": (("threads.net", "threads.com"), "https://www.threads.net/@{h}", ""),
    "twitch": (("twitch.tv",), "https://www.twitch.tv/{h}", ""),
    "reddit": (("reddit.com",), "https://www.reddit.com/user/{h}", "reddit"),
    "bluesky": (("bsky.app",), "https://bsky.app/profile/{h}.bsky.social", "bluesky"),
    "youtube": (("youtube.com",), "https://www.youtube.com/@{h}", "youtube"),
    "tumblr": (("tumblr.com",), "https://{h}.tumblr.com", "tumblr"),
}
PAGE_PLATFORMS = {p for p, (_, _, feed) in PLATFORMS.items() if not feed}
_NOT_USER = {"home", "explore", "search", "i", "intent", "share", "hashtag", "tag", "tags", "p", "reel", "reels",
             "video", "videos", "watch", "status", "login", "signup", "about", "help", "privacy", "terms", "settings",
             "messages", "notifications", "discover", "trending", "user", "u", "r", "channel", "c", "profile"}
CAPTION_KEYS = ("desc", "text", "full_text", "caption", "description", "title", "bio", "signature", "biography")


@dataclass
class SocialProfile:
    platform: str
    handle: str
    url: str
    confirmed: bool = False
    how: str = ""                          # why it counts as theirs (or why it doesn't)
    posts: list[str] = field(default_factory=list)


def platform_for(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("old.")
    for plat, (domains, _, _) in PLATFORMS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return plat
    return ""


def handle_from_url(url: str) -> str:
    """The username in a profile link (x.com/ivy → ivy, tiktok.com/@ivy → ivy, ivy.tumblr.com → ivy)."""
    plat = platform_for(url)
    if not plat:
        return ""
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.")
    parts = [p for p in u.path.split("/") if p]
    if plat == "tumblr" and host.endswith(".tumblr.com") and host != "tumblr.com":
        return host.split(".")[0]
    if plat == "reddit":
        return parts[1].lower() if len(parts) >= 2 and parts[0] in ("user", "u") else ""
    if plat == "bluesky":
        return parts[1].lower().removesuffix(".bsky.social") if len(parts) >= 2 and parts[0] == "profile" else ""
    if not parts:
        return ""
    first = parts[0].lstrip("@").lower()
    if first in _NOT_USER or not re.fullmatch(r"[a-z0-9_.-]{2,40}", first):
        return ""
    if plat == "tiktok" and not parts[0].startswith("@"):
        return ""
    if plat == "youtube" and not parts[0].startswith("@"):
        return ""
    return first


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def names_them(text: str, name: str, handles: list[str], hub_urls: list[str], exclude: str = "") -> bool:
    """Does this profile text point back to the creator: stage name, another handle of theirs, or their link hub?"""
    low = (text or "").lower()
    if exclude:  # the probed username itself proves nothing ("ivywren" would otherwise "name" Ivy Wren)
        low = re.sub(rf"@?{re.escape(exclude.lower())}", " ", low)
    sq = _squash(low)
    if name and (name.lower() in low or (len(_squash(name)) >= 6 and _squash(name) in sq)):
        return True
    for h in handles:
        if h and h != exclude and len(h) >= 4 and re.search(rf"(^|[^a-z0-9_]){re.escape(h)}($|[^a-z0-9_])", low):
            return True
    for hub in hub_urls:
        host_path = re.sub(r"^https?://(www\.)?", "", hub.lower()).rstrip("/")
        if host_path and host_path in low:
            return True
    return False


def captions_from_html(html: str, handle: str, limit: int = 120) -> list[str]:
    """Post texts/captions that public profile pages embed as JSON (TikTok, Instagram, Threads…)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:3_000_000], "html.parser")
    out: list[str] = []

    def walk(node, depth=0):
        if depth > 18 or len(out) >= limit:
            return
        if isinstance(node, dict):
            # skip other people's objects when the node names its author
            author = node.get("author") or node.get("owner") or node.get("user")
            if isinstance(author, dict):
                uname = str(author.get("uniqueId") or author.get("username") or author.get("screen_name") or "").lower()
                if uname and handle and uname != handle:
                    return
            for k in CAPTION_KEYS:
                v = node.get(k)
                if isinstance(v, str) and 12 <= len(v.strip()) <= 2200 and not v.strip().startswith(("http", "{")):
                    t = collapse_ws(v)
                    if t not in out:
                        out.append(t)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    for s in soup.find_all("script"):
        raw = (s.string or s.get_text() or "").strip()
        if not raw or len(raw) < 40:
            continue
        data = None
        if (s.get("type") or "").lower() in ("application/json", "application/ld+json") or s.get("id"):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
        if data is None:
            m = re.search(r"=\s*(\{.*\})\s*;?\s*$", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(1))
                except ValueError:
                    data = None
        if data is not None:
            walk(data)
    return out[:limit]


# ═══════════════════════════════════════════════════════════ discovery
def discover(r: "Researcher", name: str, handles: list[str], hub_links: list[str], *, hub_urls: list[str] | None = None,
             guess: bool = True, search: bool = True) -> list[SocialProfile]:
    """`hub_links`: every link found on the creator's link hubs. `hub_urls`: the link hubs themselves."""
    found: dict[tuple[str, str], SocialProfile] = {}
    hubs = list(hub_urls or [])

    def add(p: SocialProfile) -> None:
        key = (p.platform, p.handle)
        cur = found.get(key)
        if cur is None or (p.confirmed and not cur.confirmed):
            found[key] = p

    # 1. linked from their own link hub
    for u in hub_links:
        plat, h = platform_for(u), handle_from_url(u)
        if plat and h:
            add(SocialProfile(plat, h, u, True, "linked from their link hub"))

    # 2. web search: username must be theirs AND the result title must name them
    if search:
        sites = " OR ".join(f"site:{d}" for p in PLATFORMS.values() for d in p[0][:1])
        terms = list(dict.fromkeys([f'"{name}"'] + [f'"{h}"' for h in handles[:2]]))
        for t in terms[:3]:
            for url, title in r.search(f"{t} ({sites})", max_results=10):
                plat, h = platform_for(url), handle_from_url(url)
                if not plat or not h or (plat, h) in found:
                    continue
                if h in handles and names_them(title, name, handles, hubs, exclude=h):
                    add(SocialProfile(plat, h, url, True, f"search result titled “{title[:80]}”"))
                elif h in handles:
                    add(SocialProfile(plat, h, url, False, "username matches, but the result doesn't name them"))

    # 3. direct guesses for platforms still missing – confirmed only by the profile itself
    if guess:
        have = {p for p, _ in found if found[(p, _)].confirmed}
        for plat, (_, tmpl, _) in PLATFORMS.items():
            if plat in have:
                continue
            for h in handles[:2]:
                if (plat, h) in found:
                    continue
                url = tmpl.format(h=h)
                ok, how = confirm_profile(r, plat, h, url, name, handles, hubs)
                add(SocialProfile(plat, h, url, ok, how))
                if ok:
                    break
    profiles = sorted(found.values(), key=lambda p: (not p.confirmed, p.platform))
    conf = [p for p in profiles if p.confirmed]
    if profiles:
        log(f"→ Social profiles: {len(conf)} confirmed as theirs"
            + (f" ({', '.join(f'{p.platform} @{p.handle}' for p in conf)})" if conf else "")
            + (f", {len(profiles) - len(conf)} not confirmed (skipped)" if len(profiles) > len(conf) else ""))
    return profiles


def confirm_profile(r: "Researcher", plat: str, handle: str, url: str, name: str, handles: list[str],
                    hubs: list[str]) -> tuple[bool, str]:
    """Open a guessed profile and check it points back to the creator."""
    text = ""
    try:
        if plat == "bluesky":
            j = r.http.get(f"{SOCIAL_ENDPOINTS['bluesky']}/xrpc/app.bsky.actor.getProfile",
                           params={"actor": f"{handle}.bsky.social"}, timeout=r.timeout)
            if j.status_code != 200:
                return False, "no such account"
            d = j.json()
            text = f"{d.get('displayName', '')} {d.get('description', '')}"
        elif plat == "reddit":
            j = r.http.get(f"{SOCIAL_ENDPOINTS['reddit']}/user/{handle}/about.json", timeout=r.timeout,
                           headers={"User-Agent": "PersonaForge/1.0 (profile check)"})
            if j.status_code != 200:
                return False, "no such account (or Reddit refused)"
            d = (j.json() or {}).get("data", {})
            sub = d.get("subreddit") or {}
            text = f"{sub.get('title', '')} {sub.get('public_description', '')} {d.get('name', '')}"
        else:
            page = r.fetch(url, kind="social", keep_html=True)
            if not page:
                return False, "profile didn't load (login wall or not found)"
            html = page.meta.pop("html", "")
            text = f"{page.title} {page.text[:4000]} {html[:200_000]}"
    except Exception as e:  # noqa: BLE001
        return False, f"couldn't check ({e.__class__.__name__})"
    if names_them(text, name, handles, hubs, exclude=handle):
        return True, "the profile names them / links their other accounts"
    return False, "an account with this username exists, but nothing on it points to them"


SOCIAL_ENDPOINTS = {"bluesky": "https://public.api.bsky.app", "reddit": "https://www.reddit.com"}


# ═══════════════════════════════════════════════════════════ reading
def feed_specs(profiles: list[SocialProfile]) -> list:
    from .feeds import FeedSpec

    out = []
    for p in profiles:
        if not p.confirmed:
            continue
        feed = PLATFORMS[p.platform][2]
        if feed == "bluesky":
            out.append(FeedSpec("bluesky", f"{p.handle}.bsky.social" if "." not in p.handle else p.handle))
        elif feed == "youtube":
            out.append(FeedSpec("youtube", "@" + p.handle))
        elif feed:
            out.append(FeedSpec(feed, p.handle))
    return out


def read_pages(r: "Researcher", profiles: list[SocialProfile], person: str, max_posts: int = 120) -> list["Source"]:
    """Bio + publicly served posts of each confirmed page-only profile (X, Instagram, TikTok, Threads, Twitch)."""
    from .research import Source, extract_meta

    out = []
    for p in profiles:
        if not p.confirmed or p.platform not in PAGE_PLATFORMS:
            continue
        page = r.fetch(p.url, kind="social", keep_html=True)
        if not page:
            continue
        html = page.meta.pop("html", "")
        bio = extract_meta(html) if html else ""
        p.posts = [c for c in captions_from_html(html, p.handle, max_posts) if c not in bio] if html else []
        lines = [f"[Public {p.platform} profile @{p.handle} – {person}'s own bio and posts as the page shows them "
                 "without logging in.]"]
        if bio:
            lines.append(f"Bio: {bio}")
        if p.posts:
            lines.append("Posts / captions:")
            lines += [f"- {c}" for c in p.posts]
        elif page.text:
            lines.append(page.text[:6000])
        out.append(Source(url=p.url, title=f"{p.platform} @{p.handle}", text="\n".join(lines), kind="social"))
        log(f"  {'✓' if p.posts else 'i'} {p.platform} @{p.handle}: bio" + (f" + {len(p.posts)} posts" if p.posts else
                                                                         " only (posts need a login on this site)"))
    return out


def render_md(person: str, profiles: list[SocialProfile]) -> str:
    lines = [f"# Social profiles: {person}", "",
             "Only profiles confirmed as theirs were read. Not-confirmed ones were skipped; if one really is theirs, "
             "add it as a public feed (Reddit, Bluesky, YouTube, Tumblr) or an extra page.", "",
             "| Platform | Account | Read? | Why |", "| --- | --- | --- | --- |"]
    for p in profiles:
        lines.append(f"| {p.platform} | [@{p.handle}]({p.url}) | {'yes' if p.confirmed else 'no'}"
                     + (f" ({len(p.posts)} posts)" if p.posts else "") + f" | {p.how} |")
    return "\n".join(lines) + "\n"
