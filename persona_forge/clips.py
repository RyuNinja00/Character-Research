"""Clip catalogues: the titles, tags and descriptions a creator gives their OWN clips.

On a creator's own storefront or verified model page (ManyVids, Clips4Sale, IWantClips, LoyalFans, FanCentro,
JustFor.Fans, Pornhub/Modelhub model pages, xHamster creator pages…), the clip list is the best evidence of what
their content focuses on. This module reads:

1. the store / model page and up to N listing pages (pagination),
2. each clip's title, tags/categories and description, from structured data (JSON-LD, embedded JSON) first and
   the page's links second,
3. up to M individual clip pages for the description and full tag list when the listing doesn't show them.

Only pages reached from a store that `Researcher.is_creator_profile` accepts are read, so other people's uploads
(tube re-uploads, aggregators, leak sites) never count. Excluded themes (minor-coded, bestiality, incapacitation)
are dropped here already and counted for review.md.

The result is a catalogue per store plus a combined tag count, turned into one source per store for fact
extraction ("content_theme" facts with real counts like "18 of 40 clips").
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from .util import collapse_ws, log

if TYPE_CHECKING:  # pragma: no cover
    from .research import Researcher, Source

# Platform → regex matching the path (+query) of ONE clip on that platform.
CLIP_LINK_RE: dict[str, re.Pattern] = {
    "manyvids.com": re.compile(r"^/Video/\d+", re.I),
    "clips4sale.com": re.compile(r"^/studio/\d+/\d+/", re.I),
    "iwantclips.com": re.compile(r"^/store/\d+/[^/]+/\d+", re.I),
    "pornhub.com": re.compile(r"^/view_video\.php\?(.*&)?viewkey=", re.I),
    "modelhub.com": re.compile(r"^/video/", re.I),
    "loyalfans.com": re.compile(r"^/[^/]+/video/", re.I),
    "justfor.fans": re.compile(r"(\?|&)(post|video)=|/video/", re.I),
    "fancentro.com": re.compile(r"^/[^/]+/(clips?|videos?)/[^/]+", re.I),
    "xhamster.com": re.compile(r"^/videos/[^/]+", re.I),
    "fanvue.com": re.compile(r"^/[^/]+/(post|video)/", re.I),
}
CLIP_PLATFORMS = set(CLIP_LINK_RE)
TAG_LINK_RE = re.compile(r"(/tags?/|/categor(y|ies)/|[?&](tag|tags|category|cat|keyword)=|/hashtag/|/niche/|/search/tag)",
                         re.I)
NOISE_TAGS = {
    "hd", "4k", "1080p", "720p", "uhd", "full hd", "new", "exclusive", "verified", "premium", "free", "sale",
    "video", "videos", "clip", "clips", "custom", "customs", "best seller", "bestseller", "top", "popular", "trending",
    "mp4", "wmv", "mov", "solo female", "female", "girl", "woman", "model", "creator", "pornstar", "porn", "sex",
    "xxx", "nsfw", "18+", "adult", "amateur model", "onlyfans", "manyvids", "clips4sale", "pornhub",
}


@dataclass
class Clip:
    title: str
    url: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""

    def key(self) -> str:
        return self.url.split("#")[0].rstrip("/").lower() or re.sub(r"\W+", " ", self.title.lower()).strip()


@dataclass
class ClipCatalog:
    platform: str
    store_url: str
    clips: list[Clip] = field(default_factory=list)
    pages_read: int = 0
    details_read: int = 0
    dropped_excluded: int = 0

    def tag_counts(self) -> Counter:
        c: Counter = Counter()
        for clip in self.clips:
            for t in set(clip.tags):
                c[t] += 1
        return c

    def to_json(self) -> dict:
        return {"platform": self.platform, "store_url": self.store_url, "clips": [c.__dict__ for c in self.clips],
                "tag_counts": self.tag_counts().most_common(), "pages_read": self.pages_read,
                "details_read": self.details_read, "dropped_excluded": self.dropped_excluded}


# ═══════════════════════════════════════════════════════════ helpers
def platform_of(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return next((p for p in CLIP_PLATFORMS if host == p or host.endswith("." + p)), "")


def is_clip_link(url: str) -> bool:
    plat = platform_of(url)
    if not plat:
        return False
    u = urlparse(url)
    return bool(CLIP_LINK_RE[plat].search(u.path + (("?" + u.query) if u.query else "")))


def clean_tag(t) -> str:
    if isinstance(t, dict):
        t = t.get("name") or t.get("title") or t.get("label") or t.get("tag") or t.get("slug") or ""
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip("#,;|").strip().lower()
    t = t.replace("-", " ") if re.fullmatch(r"[a-z0-9-]+", t) else t
    if not t or len(t) > 40 or len(t) < 2 or t in NOISE_TAGS or t.isdigit():
        return ""
    return t


def _split_tags(v) -> list[str]:
    if isinstance(v, str):
        parts = re.split(r"[,|;]+|\s#", v)
    elif isinstance(v, list):
        parts = v
    else:
        return []
    return [t for t in (clean_tag(p) for p in parts) if t]


def store_listing_urls(url: str) -> list[str]:
    """Where the clip list lives for a store/profile URL (its 'Videos' tab), plus the page itself."""
    u = urlparse(url)
    plat = platform_of(url)
    parts = [p for p in u.path.split("/") if p]
    out = []
    base = f"{u.scheme}://{u.netloc}"
    if plat == "pornhub.com" and len(parts) >= 2 and parts[0] in ("model", "pornstar", "channels"):
        out.append(f"{base}/{parts[0]}/{parts[1]}/videos")
    elif plat == "manyvids.com" and len(parts) >= 3 and parts[0].lower() == "profile":
        out.append(f"{base}/Profile/{parts[1]}/{parts[2]}/Store/Videos/")
    elif plat == "xhamster.com" and len(parts) >= 2 and parts[0] in ("creators", "users", "channels"):
        out.append(f"{base}/{parts[0]}/{parts[1]}/videos" if parts[0] != "users" else f"{base}/users/{parts[1]}/videos")
    out.append(url)
    return list(dict.fromkeys(out))


def next_page_url(html: str, url: str, page_no: int) -> str:
    """The listing's next page: rel=next, a 'Next' link, or ?page=N+1 when the page already uses ?page=."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:600_000], "html.parser")
    link = soup.find("link", rel="next") or soup.find("a", rel="next")
    if link and link.get("href"):
        return urljoin(url, link["href"])
    for a in soup.find_all("a", href=True):
        label = (a.get_text(" ", strip=True) + " " + (a.get("aria-label") or "") + " " + (a.get("title") or "")).strip().lower()
        if re.fullmatch(r"(next( page)?|›|»|>|next ›|next »)", label) or "next page" in label:
            return urljoin(url, a["href"])
    u = urlparse(url)
    q = parse_qs(u.query)
    if "page" in q or re.search(rf"[?&]page={page_no}\b", html):
        q["page"] = [str(page_no + 1)]
        return urlunparse(u._replace(query=urlencode(q, doseq=True)))
    return ""


# ═══════════════════════════════════════════════════════════ extraction
def _walk(node, visit, depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk(v, visit, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, visit, depth + 1)


TITLE_KEYS = ("title", "name", "clipTitle", "clip_title", "video_title", "videoTitle", "headline")
TAG_KEYS = ("tags", "keywords", "categories", "category_names", "categoryNames", "tagNames", "tag_names",
            "niches", "hashtags", "tagList", "category")
DESC_KEYS = ("description", "desc", "summary", "clip_description", "clipDescription", "body", "caption")
URL_KEYS = ("url", "href", "link", "permalink", "videoUrl", "clipUrl", "slug_url", "@id")


def _record(node: dict, base: str) -> Clip | None:
    title = next((str(node[k]) for k in TITLE_KEYS if isinstance(node.get(k), str) and node[k].strip()), "")
    if not title or len(title) > 200:
        return None
    tags: list[str] = []
    for k in TAG_KEYS:
        if k in node:
            tags += _split_tags(node[k])
    desc = next((str(node[k]) for k in DESC_KEYS if isinstance(node.get(k), str) and node[k].strip()), "")
    url = next((str(node[k]) for k in URL_KEYS if isinstance(node.get(k), str) and node[k].strip()), "")
    typ = str(node.get("@type", "")).lower()
    is_video = typ in ("videoobject", "product", "movie", "creativework", "clip") or bool(url and is_clip_link(urljoin(base, url)))
    if not (tags or (desc and is_video) or is_video):
        return None
    if desc:
        from bs4 import BeautifulSoup

        desc = collapse_ws(BeautifulSoup(desc, "html.parser").get_text(" ", strip=True))[:600]
    return Clip(title=collapse_ws(title)[:160], url=urljoin(base, url) if url else "", tags=list(dict.fromkeys(tags)),
                description=desc)


def clips_from_structured(html: str, base: str) -> list[Clip]:
    """JSON-LD and embedded JSON (Next.js/Nuxt state, application/json blocks)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    found: list[Clip] = []

    def visit(node: dict) -> None:
        rec = _record(node, base)
        if rec:
            found.append(rec)

    for s in soup.find_all("script"):
        typ = (s.get("type") or "").lower()
        raw = s.string or s.get_text() or ""
        data = None
        if typ in ("application/ld+json", "application/json") or s.get("id") in ("__NEXT_DATA__", "__NUXT_DATA__"):
            try:
                data = json.loads(raw)
            except ValueError:
                continue
        else:
            m = re.search(r"(?:__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__|__DATA__)\s*=\s*(\{.*\})\s*;?\s*$",
                          raw.strip(), re.S)
            if m:
                try:
                    data = json.loads(m.group(1))
                except ValueError:
                    continue
        if data is not None:
            _walk(data, visit)
    return found


def clips_from_links(html: str, base: str) -> list[Clip]:
    """Clip links on a listing page: title from the link text, its title/aria-label, or the thumbnail's alt."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    out: dict[str, Clip] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"]).split("#")[0]
        if not is_clip_link(url):
            continue
        img = a.find("img")
        title = (a.get("title") or a.get("aria-label") or a.get_text(" ", strip=True)
                 or (img.get("alt") if img else "") or "").strip()
        title = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*$", "", title)        # trailing duration
        title = re.sub(r"^\s*(\d+(\.\d+)?[kKmM]?\s+views?\s*)", "", title)
        if len(title) < 3:
            continue
        cur = out.get(url)
        if not cur or len(title) > len(cur.title):
            out[url] = Clip(title=title[:160], url=url)
        # tags printed on the card itself (e.g. <span class="tag">…</span> inside the tile)
        tile = a.find_parent(["li", "article", "div"])
        if tile is not None:
            tags = [clean_tag(t.get_text(" ", strip=True)) for t in tile.select('[class*="tag"], [class*="categor"]')]
            out[url].tags = list(dict.fromkeys(out[url].tags + [t for t in tags if t]))
    return list(out.values())


def clip_detail(html: str, url: str) -> Clip | None:
    """One clip page: structured data first, then meta tags and tag/category links."""
    from bs4 import BeautifulSoup

    structured = [c for c in clips_from_structured(html, url) if c.tags or c.description]
    best = max(structured, key=lambda c: len(c.tags) * 10 + len(c.description), default=None)
    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    # site-wide menus list "popular tags" on every page; those must not count for this clip
    for junk in soup(["nav", "header", "footer", "aside"]):
        junk.decompose()
    for junk in soup.select('[class*="sidebar"], [class*="related"], [class*="recommend"], [id*="sidebar"]'):
        junk.decompose()

    def meta(*names: str) -> str:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = (best.title if best else "") or meta("og:title", "twitter:title") or (
        soup.h1.get_text(" ", strip=True) if soup.h1 else "")
    if not title:
        return None
    tags = list(best.tags) if best else []
    tags += _split_tags(meta("keywords", "video:tag", "og:video:tag"))
    for a in soup.find_all("a", href=True):
        if TAG_LINK_RE.search(a["href"]):
            t = clean_tag(a.get_text(" ", strip=True))
            if t:
                tags.append(t)
    for el in soup.select('[class*="tags"] a, [class*="tag-list"] a, [class*="categories"] a, [class*="category"] a'):
        t = clean_tag(el.get_text(" ", strip=True))
        if t:
            tags.append(t)
    desc = (best.description if best else "") or meta("og:description", "description", "twitter:description")
    return Clip(title=collapse_ws(title)[:160], url=url, tags=list(dict.fromkeys(tags))[:30], description=desc[:600])


def merge_clips(*lists: list[Clip]) -> list[Clip]:
    out: dict[str, Clip] = {}
    title_index: dict[str, str] = {}
    for lst in lists:
        for c in lst:
            tkey = re.sub(r"\W+", " ", c.title.lower()).strip()
            key = c.key() if c.url else title_index.get(tkey, c.key())
            if key in out:
                cur = out[key]
                cur.tags = list(dict.fromkeys(cur.tags + c.tags))
                if len(c.description) > len(cur.description):
                    cur.description = c.description
                if len(c.title) > len(cur.title) and c.title.lower() != cur.title.lower():
                    cur.title = c.title
            else:
                out[key] = Clip(c.title, c.url, list(c.tags), c.description)
                title_index.setdefault(tkey, key)
    return list(out.values())


def drop_excluded(clips: list[Clip]) -> tuple[list[Clip], int]:
    """Remove excluded tags; drop a clip entirely when its title/description is in an excluded theme."""
    from .synthesize import excluded_theme

    kept, dropped = [], 0
    for c in clips:
        if excluded_theme(f"{c.title} {c.description}"):
            dropped += 1
            continue
        bad = [t for t in c.tags if excluded_theme(t)]
        dropped += len(bad)
        c.tags = [t for t in c.tags if not excluded_theme(t)]
        kept.append(c)
    return kept, dropped


# ═══════════════════════════════════════════════════════════ orchestration
def read_store(r: "Researcher", store_url: str, *, max_pages: int = 3, max_details: int = 20) -> ClipCatalog | None:
    """Read one of the creator's own stores/model pages into a clip catalogue."""
    plat = platform_of(store_url)
    if not plat:
        return None
    cat = ClipCatalog(platform=plat, store_url=store_url)
    collected: list[list[Clip]] = []
    seen_pages: set[str] = set()
    for start in store_listing_urls(store_url):
        url, page_no = start, 1
        while url and page_no <= max_pages and url not in seen_pages:
            seen_pages.add(url)
            page = r.fetch(url, kind="platform", keep_html=True, via=store_url if url != store_url else "")
            html = page.meta.pop("html", "") if page else ""
            if not html:
                break
            cat.pages_read += 1
            found = merge_clips(clips_from_structured(html, url), clips_from_links(html, url))
            if not found:
                break
            collected.append(found)
            page_no += 1
            url = next_page_url(html, url, page_no - 1)
        if collected:
            break  # the "Videos" tab worked; the profile page itself would repeat it
    clips = merge_clips(*collected)
    # detail pages for clips the listing didn't describe
    need = [c for c in clips if c.url and (not c.tags or not c.description)][:max(0, max_details)]
    for c in need:
        page = r.fetch(c.url, kind="platform", keep_html=True, via=store_url)
        html = page.meta.pop("html", "") if page else ""
        if html and (d := clip_detail(html, c.url)):
            cat.details_read += 1
            clips = merge_clips(clips, [d])
    cat.clips, cat.dropped_excluded = drop_excluded(clips)
    log(f"  i {plat}: {len(cat.clips)} clips catalogued from {store_url} ({cat.pages_read} listing page(s), "
        f"{cat.details_read} clip page(s))" + (f", {cat.dropped_excluded} excluded item(s) removed" if cat.dropped_excluded else ""))
    return cat if cat.clips else None


def catalog_source(cat: ClipCatalog, person: str, max_chars: int = 50_000) -> "Source":
    from .research import Source

    counts = cat.tag_counts()
    n = len(cat.clips)
    lines = [f"[Clip catalogue from {person}'s OWN {cat.platform} store ({cat.store_url}). {n} clips listed by the "
             "creator; titles, tags and descriptions are the creator's own. Use the tag counts as evidence of how "
             "prominent each theme is, e.g. '18 of 40 clips'.]", ""]
    if counts:
        lines.append("Tag counts (clips carrying the tag, most used first): "
                     + ", ".join(f"{t} ×{k}" for t, k in counts.most_common(60)))
        lines.append("")
    lines.append("Clips:")
    size = sum(len(l) for l in lines)
    for i, c in enumerate(cat.clips):
        entry = f'- "{c.title}"' + (f" — tags: {', '.join(c.tags[:15])}" if c.tags else "") + (
            f" — {c.description[:280]}" if c.description else "")
        if size + len(entry) > max_chars:
            lines.append(f"- … and {n - i} more (counted in the tag totals above)")
            break
        lines.append(entry)
        size += len(entry)
    return Source(url=cat.store_url, title=f"Clip catalogue – {cat.platform}", text="\n".join(lines), kind="platform",
                  meta={"clips": n})


def combined_tag_counts(catalogs: list[ClipCatalog]) -> list[tuple[str, int, int]]:
    """(tag, clips with it, total clips) across every store, most used first."""
    total = sum(len(c.clips) for c in catalogs)
    c: Counter = Counter()
    for cat in catalogs:
        c.update(cat.tag_counts())
    return [(t, k, total) for t, k in c.most_common()]


def render_catalog_md(person: str, catalogs: list[ClipCatalog]) -> str:
    lines = [f"# Clip catalogue: {person}", "",
             "Titles, tags and descriptions from the creator's own stores and model pages. This is what the "
             "content themes and persona kinks are based on. Excluded themes were removed before anything was used.", ""]
    combo = combined_tag_counts(catalogs)
    if combo:
        lines += ["## Tags across all stores", "", "| Tag | Clips | Share |", "| --- | ---: | ---: |"]
        for t, k, total in combo[:80]:
            lines.append(f"| {t} | {k} | {k / total:.0%} |")
        lines.append("")
    for cat in catalogs:
        lines += [f"## {cat.platform} — {len(cat.clips)} clips", "", f"Store: {cat.store_url}", ""]
        if cat.dropped_excluded:
            lines += [f"_{cat.dropped_excluded} excluded clip(s)/tag(s) removed._", ""]
        for c in cat.clips:
            lines.append(f"- **{c.title}**" + (f" — {', '.join(c.tags)}" if c.tags else "")
                         + (f"  \n  {c.description[:300]}" if c.description else ""))
        lines.append("")
    return "\n".join(lines)
