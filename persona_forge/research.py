"""Web research: Wikipedia anchor article, Wikiquote, web search, page fetching + text extraction."""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .util import DiskCache, collapse_ws, debug, log

USER_AGENT = (
    "PersonaForge/1.0 (Marinara Engine character builder; "
    "+https://github.com/Pasta-Devs/Marinara-Engine) python-requests"
)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# A complete, ordinary browser request. Many site firewalls answer bare scripted requests with 403/404/406.
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
ALT_HEADERS = {  # second attempt: a different, equally ordinary browser
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.google.com/",
}
# ── Age gates ────────────────────────────────────────────────────────────────────────────────────────
# Simple "Are you 18? Enter / Leave" pages are a self-declaration, which the app may answer for an adult user
# (--confirm-adult; on by default with --nsfw). Real age verification – ID upload, face scan, card checks,
# third-party verifiers – is a legal requirement in some regions and is never bypassed: the page is reported
# so the user can save it from their own browser.
AGE_GATE_RE = re.compile(
    r"(are you (at least |over )?(18|21)|(18|21) (years|yrs)( of age)? or (older|over)|i am (18|21|over 18|an adult)|"
    r"age (verification|confirmation|check|gate)|verify (that )?you are (18|21|of legal age|an adult)|adult content warning|"
    r"you must be (18|21|of legal age)|(enter|continue) if you are (18|21|over)|this (website|site) contains (adult|"
    r"sexually explicit|age-restricted)|confirm (your age|you are (18|21|an adult)))",
    re.I,
)
STRICT_AGE_CHECK_RE = re.compile(
    r"(government[- ]issued|photo id|upload (your|an) id|id document|driver'?s licen[cs]e|passport|selfie|face scan|"
    r"facial (age )?estimation|credit card|verify with (yoti|ageid|veriff|go\.cam|verifymy)|yoti|agego|age ?id\b|"
    r"go\.cam|verifymy|veriff|digital id|age assurance provider)",
    re.I,
)
AGE_COOKIES = {  # names commonly used by click-through gates to remember the answer
    "age_verified": "1", "ageVerified": "true", "age_verification": "1", "agegate": "1", "age_gate": "1",
    "over18": "1", "over_18": "1", "is_adult": "1", "adult": "1", "adult_content": "1", "mature": "1",
    "ageconfirmed": "1", "age_confirmed": "true", "av": "1", "ac": "1", "warning": "accepted", "disclaimer": "1",
    "agreed": "1", "18plus": "1", "18_plus": "1", "legal_age": "1", "entered": "1", "enter": "1",
}
CONFIRM_WORDS = re.compile(r"\b(enter|yes|i am (18|21|over|an adult)|i'?m (18|21|over)|i agree|agree|continue|"
                           r"i accept|accept|confirm|proceed|over 18|18\+|adult)\b", re.I)
DENY_WORDS = re.compile(r"\b(no|leave|exit|under 18|i am not|i'?m not|take me back|decline|go back)\b", re.I)


def age_gate_kind(html: str) -> str:
    """'' (no gate), 'click' (simple self-declaration) or 'strict' (real verification)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:300_000], "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not AGE_GATE_RE.search(title + " " + text[:4000]):
        return ""
    # A long article that merely mentions an age warning (e.g. in a hidden overlay) is readable as is.
    if len(text) > 2500 and not AGE_GATE_RE.search(title):
        return ""
    return "strict" if STRICT_AGE_CHECK_RE.search(text[:6000]) else "click"


TRACKING_PARAMS = re.compile(r"^(si|utm_[a-z]+|fbclid|gclid|igshid|mc_[a-z]+|ref_src|feature|pp)$", re.I)
STATUS_ADVICE = {
    403: "the site blocks automated readers",
    406: "the site blocks automated readers",
    429: "the site is rate-limiting – try again later",
    451: "not available in this region",
    503: "the site blocks automated readers or is down",
}
BLOCKED_TIP = ("open it in your browser, save it (Ctrl+S / Cmd+S → “Webpage, HTML only” or copy the text into a .txt) "
               "and add it under Text sources / --file")


def simpler_queries(query: str) -> list[str]:
    """Simpler forms of a query for a retry: first alternative of each OR-list, then without quotes."""
    q = re.sub(r"\(([^()]*)\)", lambda m: m.group(1).split(" OR ")[0], query)  # (a OR b) → a
    q = re.sub(r"(\S+)(?: OR \S+)+", r"\1", q)                                   # a OR b OR c → a
    q = " ".join(q.split())
    out = [q] if q != query else []
    unquoted = q.replace('"', "")
    if unquoted != q:
        out.append(unquoted)
    return out


def normalize_url(url: str) -> str:
    """Tidy a pasted link: add https://, drop tracking parameters (?si=, utm_…), expand YouTube short links."""
    from urllib.parse import parse_qsl, urlencode, urlunparse

    url = url.strip().strip("<>\"'")
    if url and "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    path = u.path
    if host == "youtu.be" and path.strip("/"):
        return f"https://www.youtube.com/watch?v={path.strip('/').split('/')[0]}"
    if host == "youtube.com" and path.startswith(("/shorts/", "/live/")):
        return f"https://www.youtube.com/watch?v={path.split('/')[2]}"
    return urlunparse((u.scheme, u.netloc, path, u.params, urlencode(query), ""))

# Never read: people-search / records aggregators (doxxing risk), whatever the source of the link.
PEOPLE_SEARCH_DOMAINS = {
    "spokeo.com", "whitepages.com", "beenverified.com", "truepeoplesearch.com", "fastpeoplesearch.com",
    "radaris.com", "mylife.com", "peoplefinders.com", "intelius.com", "zoominfo.com", "rocketreach.co",
    "familysearch.org", "ancestry.com", "findagrave.com", "thatsthem.com", "usphonebook.com",
    "clustrmaps.com", "nuwber.com", "idcrawl.com", "pimeyes.com", "facecheck.id",
}
# Login-walled or text-poor: skipped when they come from web search, but still tried when YOU supply
# the link (or it is on the creator's link hub), since public bios/meta descriptions are often readable.
LOGIN_WALLED_DOMAINS = {
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "linkedin.com", "pinterest.com",
    "threads.net", "onlyfans.com", "fansly.com", "patreon.com", "snapchat.com",
}
BLOCKED_DOMAINS = PEOPLE_SEARCH_DOMAINS | LOGIN_WALLED_DOMAINS  # kept for backwards compatibility
# Leak/piracy pages host non-consensual reposts and are never used.
LEAK_URL_RE = re.compile(r"leak|leaked|nudostar|thothub|fapello|coomer|kemono|erome|simpcity", re.I)
# Link-hub entries that are never worth reading as text.
HUB_SKIP_RE = re.compile(
    r"(apps\.apple\.com|play\.google\.com|paypal\.|cash\.app|venmo\.|ko-fi\.com/.*/shop|amazon\.[a-z.]+/.*wishlist|"
    r"throne\.com|wishtender|spotify\.com/track|linktr\.ee/(s|privacy|terms|discover)|/cookie|/privacy|/terms)",
    re.I,
)
LINK_HUB_DOMAINS = {"linktr.ee", "beacons.ai", "allmylinks.com", "lnk.bio", "solo.to", "bio.link", "hoo.be",
                    "carrd.co", "campsite.bio", "tap.bio", "snipfeed.co", "msha.ke", "linkin.bio", "fanlink.to"}
# Adult platforms where a creator runs their own profile / storefront. A page on these counts as the
# creator's own channel only if its URL path contains one of the creator's handles (see is_creator_profile).
# Tube "video" pages uploaded by others, aggregators and leak sites are never treated as the creator's own.
ADULT_PLATFORMS: dict[str, str] = {
    "onlyfans.com": "",
    "fansly.com": "",
    "manyvids.com": "Profile",
    "clips4sale.com": "studio",
    "iwantclips.com": "store",
    "loyalfans.com": "",
    "fancentro.com": "",
    "justfor.fans": "",
    "pornhub.com": "model",        # verified model / pornstar pages only, not video pages
    "modelhub.com": "",
    "chaturbate.com": "",
    "stripchat.com": "",
    "myfreecams.com": "",
    "camsoda.com": "",
    "bongacams.com": "",
    "fanvue.com": "",
    "xhamster.com": "creators",    # creator / verified channel pages only, not video pages
}
# Multi-user platforms: their other pages belong to other people, so sub-page following never runs on them.
NO_SUBPAGE_DOMAINS = (LOGIN_WALLED_DOMAINS | LINK_HUB_DOMAINS | set(ADULT_PLATFORMS) |
                      {"reddit.com", "youtube.com", "twitch.tv", "kick.com", "throne.com", "amazon.com", "etsy.com",
                       "medium.com", "substack.com", "tumblr.com", "discord.gg", "discord.com", "t.me", "telegram.me"})
ADULT_PRESS_QUERIES = ['"{name}" XBIZ OR AVN interview', '"{name}" adult creator interview']
SITE_PAGE_HINTS = re.compile(r"about|bio|faq|interview|press|story|blog|journal|meet|who|me\b|q-?and-?a|podcast", re.I)
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mp3", ".zip")


@dataclass
class Source:
    url: str
    title: str
    text: str
    kind: str = "web"  # wikipedia | wikiquote | web | user
    meta: dict = field(default_factory=dict)


@dataclass
class WikiCandidate:
    title: str
    snippet: str
    description: str = ""


def parse_search_results(data, n: int) -> list[tuple[str, str]]:
    """[(url, title)] from a search API reply whose result fields vary (url/link/href, title/name)."""
    items = data.get("data", data.get("results", [])) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("results") or items.get("sources") or items.get("data") or []
    out: list[tuple[str, str]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        url = it.get("url") or it.get("link") or it.get("href") or it.get("source") or ""
        if isinstance(url, str) and url.startswith("http"):
            out.append((url, str(it.get("title") or it.get("name") or "")))
        if len(out) >= n:
            break
    return out


class Researcher:
    def __init__(
        self,
        cache: DiskCache | None = None,
        lang: str = "en",
        search_provider: str = "auto",
        searxng_url: str = "",
        brave_key: str = "",
        tavily_key: str = "",
        nanogpt_key: str = "",
        nanogpt_search: str = "linkup",
        timeout: int = 20,
        confirm_adult: bool = False,
        browser_mode: str = "auto",          # off | auto (when needed) | always | visible (when needed, window shown)
        profile_dir: str | None = None,
        max_page_chars: int = 60_000,
    ):
        self.cache = cache
        self.lang = lang
        self.search_provider = search_provider
        self.searxng_url = searxng_url
        self.brave_key = brave_key
        self.tavily_key = tavily_key
        self.nanogpt_key = nanogpt_key
        self.nanogpt_search = nanogpt_search or "linkup"
        self.timeout = timeout
        self.confirm_adult = confirm_adult
        self.browser_mode = browser_mode
        self.profile_dir = profile_dir
        self._browser = None
        self._browser_failed = False
        self._browser_lock = threading.Lock()
        self._last_hit: dict[str, float] = {}
        # Adult runs need safe search off, or the engines hide exactly the sites being researched.
        self.safesearch = "off" if confirm_adult else "moderate"
        self.search_stats = {"queries": 0, "empty": 0, "failed": 0, "rate_limited": 0, "retried": 0}
        self._search_lock = threading.Lock()
        self._last_search = 0.0
        self._pace_lock = threading.Lock()
        import os

        pace = os.environ.get("PERSONA_FORGE_PACE", "1.0-2.5")  # seconds between pages on one site; "0" = off
        lo, _, hi = pace.partition("-")
        self._pace_range = (float(lo or 0), float(hi or lo or 0))
        self.max_page_chars = max_page_chars
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self.skipped: list[tuple[str, str]] = []  # (url, reason) – reported in review.md
        self.discovered_handles: list[str] = []      # usernames seen in the creator's own platform links
        self.hub_links: list[str] = []               # every link on the creator's link hubs (feeds are found here)
        self.handles: list[str] = []                 # the creator's usernames (set by the CLI)

    # ───────────────────────────────────────────── Wikipedia / Wikiquote
    def _wiki_api(self, site: str, params: dict) -> dict:
        url = f"https://{self.lang}.{site}.org/w/api.php"
        params = {"format": "json", "formatversion": "2", **params}
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if self.cache and (hit := self.cache.get("wiki", key)) is not None:
            return hit
        resp = self.http.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if self.cache:
            self.cache.set("wiki", key, data)
        return data

    def wiki_candidates(self, name: str, limit: int = 6) -> list[WikiCandidate]:
        data = self._wiki_api(
            "wikipedia",
            {"action": "query", "list": "search", "srsearch": name, "srlimit": str(limit)},
        )
        hits = data.get("query", {}).get("search", [])
        titles = [h["title"] for h in hits]
        descs: dict[str, str] = {}
        if titles:
            d = self._wiki_api(
                "wikipedia",
                {"action": "query", "prop": "description", "titles": "|".join(titles)},
            )
            for p in d.get("query", {}).get("pages", []):
                descs[p.get("title", "")] = p.get("description", "")
        out = []
        for h in hits:
            snippet = re.sub(r"<[^>]+>", "", h.get("snippet", ""))
            out.append(WikiCandidate(h["title"], snippet, descs.get(h["title"], "")))
        return out

    def wikipedia_article(self, title: str) -> Source | None:
        data = self._wiki_api(
            "wikipedia",
            {
                "action": "query",
                "prop": "extracts|description|pageprops|info",
                "explaintext": "1",
                "redirects": "1",
                "inprop": "url",
                "titles": title,
            },
        )
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return None
        page = pages[0]
        if "disambiguation" in (page.get("pageprops") or {}):
            raise ValueError(
                f'"{page["title"]}" is a disambiguation page. Re-run with --wiki-title set to the exact article.'
            )
        text = page.get("extract", "")
        # Drop reference-style tail sections
        text = re.split(r"\n==+\s*(References|External links|Further reading|Notes|Sources|Bibliography)\s*==+", text)[0]
        text = re.sub(r"\n(==+)\s*(.+?)\s*\1", lambda m: f"\n\n## {m.group(2)}\n", text)
        return Source(
            url=page.get("fullurl") or f"https://{self.lang}.wikipedia.org/wiki/{quote(page['title'])}",
            title=page["title"],
            text=collapse_ws(text)[: self.max_page_chars * 2],
            kind="wikipedia",
            meta={
                "description": page.get("description", ""),
                "wikibase_item": (page.get("pageprops") or {}).get("wikibase_item", ""),
            },
        )

    def wikiquote(self, title: str) -> Source | None:
        try:
            data = self._wiki_api(
                "wikiquote",
                {
                    "action": "query",
                    "prop": "extracts|info",
                    "explaintext": "1",
                    "redirects": "1",
                    "inprop": "url",
                    "titles": title,
                },
            )
        except requests.RequestException:
            return None
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("extract"):
            return None
        text = re.split(r"\n==+\s*(Quotes about|External links|See also)", pages[0]["extract"])[0]
        return Source(
            url=pages[0].get("fullurl", ""),
            title=f"{pages[0]['title']} (Wikiquote)",
            text=collapse_ws(text)[:30_000],
            kind="wikiquote",
        )

    # ───────────────────────────────────────────── Web search
    def search(self, query: str, max_results: int = 8) -> list[tuple[str, str]]:
        """Returns [(url, title)]. An empty result is retried once in a simpler form; empties are counted
        (see search_summary) instead of being reported one by one."""
        cache_key = f"{self.search_provider}|{self.safesearch}|{query}|{max_results}"
        if self.cache and (hit := self.cache.get("search", cache_key)) is not None:
            return [tuple(x) for x in hit]
        provider = self.search_provider
        if provider == "auto":
            if self.tavily_key:
                provider = "tavily"
            elif self.brave_key:
                provider = "brave"
            elif self.searxng_url:
                provider = "searxng"
            else:
                provider = "ddg"
        fn = getattr(self, f"_search_{provider}", None)
        if fn is None:
            raise ValueError(f"Unknown search provider: {provider}")
        self._provider_used = provider
        results: list[tuple[str, str]] = []
        for attempt, q in enumerate(dict.fromkeys([query, *simpler_queries(query)])):
            if attempt > 1:
                break
            if attempt:
                self.search_stats["retried"] += 1
            results = self._run_search(fn, provider, q, max_results)
            if results:
                break
        self.search_stats["queries"] += 1
        if not results:
            self.search_stats["empty"] += 1
            debug(f"no results: {query}")
        if self.cache and results:
            self.cache.set("search", cache_key, results)
        return results

    def _run_search(self, fn, provider: str, query: str, n: int) -> list[tuple[str, str]]:
        if provider == "ddg":  # free DuckDuckGo rate-limits bursts: keep searches a little apart
            with self._search_lock:
                wait = self._last_search + 1.2 - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last_search = time.time()
        for attempt in range(2):
            try:
                return fn(query, n)
            except Exception as e:  # noqa: BLE001 – a search failure shouldn't kill the run
                msg = str(e).lower()
                if "no result" in msg:
                    return []  # an empty search, not a failure
                if ("ratelimit" in msg or "rate limit" in msg or "202" in msg or "429" in msg
                        or e.__class__.__name__.lower().startswith("ratelimit")):
                    self.search_stats["rate_limited"] += 1
                    if attempt == 0:
                        time.sleep(6)
                        continue
                    log(f"  ! search is being rate-limited ({provider}) – slowing down")
                    time.sleep(10)
                    return []
                self.search_stats["failed"] += 1
                log(f"  ! search failed ({provider}): {e}")
                return []
        return []

    def search_summary(self) -> str:
        st = self.search_stats
        if not st["queries"]:
            return ""
        line = (f"→ Web search ({getattr(self, '_provider_used', self.search_provider)}, safe search "
                f"{self.safesearch}): {st['queries']} searches, {st['queries'] - st['empty']} with results")
        if st["rate_limited"]:
            line += f", rate-limited {st['rate_limited']}×"
        if st["failed"]:
            line += f", {st['failed']} failed"
        if getattr(self, "_provider_used", "") == "ddg" and st["empty"] >= max(3, st["queries"] // 2):
            line += ("\n  i Free DuckDuckGo search found little for this name. A Tavily or Brave API key in .env "
                     "(TAVILY_API_KEY / BRAVE_API_KEY) usually finds much more – or add pages directly.")
        return line

    def _search_ddg(self, query: str, n: int) -> list[tuple[str, str]]:
        try:  # prefer the maintained library if installed
            from ddgs import DDGS  # type: ignore

            with DDGS() as ddgs:
                return [(r["href"], r.get("title", "")) for r in
                        ddgs.text(query, region="us-en", safesearch=self.safesearch, max_results=n) or []]
        except ImportError:
            pass
        try:
            from duckduckgo_search import DDGS  # type: ignore

            with DDGS() as ddgs:
                return [(r["href"], r.get("title", "")) for r in
                        ddgs.text(query, region="us-en", safesearch=self.safesearch, max_results=n) or []]
        except ImportError:
            pass
        # Fallback: DuckDuckGo's HTML endpoint (kp: -2 = safe search off, -1 = moderate)
        from bs4 import BeautifulSoup

        resp = self.http.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "us-en", "kp": "-2" if self.safesearch == "off" else "-1"},
            headers={"User-Agent": BROWSER_UA},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        out = []
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            if href.startswith("http"):
                out.append((href, a.get_text(" ", strip=True)))
            if len(out) >= n:
                break
        return out

    def _search_searxng(self, query: str, n: int) -> list[tuple[str, str]]:
        resp = self.http.get(
            self.searxng_url.rstrip("/") + "/search",
            params={"q": query, "format": "json", "safesearch": "0" if self.safesearch == "off" else "1"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [(r["url"], r.get("title", "")) for r in resp.json().get("results", [])[:n]]

    def _search_brave(self, query: str, n: int) -> list[tuple[str, str]]:
        resp = self.http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n, "safesearch": "off" if self.safesearch == "off" else "moderate"},
            headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [(r["url"], r.get("title", "")) for r in resp.json().get("web", {}).get("results", [])[:n]]

    def _search_tavily(self, query: str, n: int) -> list[tuple[str, str]]:
        resp = self.http.post(
            "https://api.tavily.com/search",
            json={"api_key": self.tavily_key, "query": query, "max_results": n},
            headers={"Authorization": f"Bearer {self.tavily_key}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [(r["url"], r.get("title", "")) for r in resp.json().get("results", [])[:n]]

    def _search_nanogpt(self, query: str, n: int) -> list[tuple[str, str]]:
        """NanoGPT's web search API (POST /api/web). Every request names its search engine (--nanogpt-search,
        default Linkup standard, about $0.006 per search)."""
        from .providers import NANOGPT_WEB_SEARCH

        if not self.nanogpt_key:
            raise ValueError("NanoGPT search needs a NanoGPT API key (NANOGPT_API_KEY, or the AI model's key when the "
                             "AI model is on NanoGPT)")
        engine = self.nanogpt_search
        body = {"query": query, "provider": "linkup" if engine.startswith("linkup") else engine,
                "outputType": "searchResults"}
        if engine.startswith("linkup"):
            body["depth"] = "deep" if engine == "linkup-deep" else "standard"
        resp = self.http.post(NANOGPT_WEB_SEARCH, json=body,
                              headers={"Authorization": f"Bearer {self.nanogpt_key}", "Content-Type": "application/json"},
                              timeout=max(self.timeout, 40))
        resp.raise_for_status()
        return parse_search_results(resp.json(), n)

    def _search_none(self, query: str, n: int) -> list[tuple[str, str]]:
        return []

    # ───────────────────────────────────────────── Fetch + extract
    @staticmethod
    def _domain(url: str) -> str:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    @staticmethod
    def _in(dom: str, domains: set[str]) -> bool:
        return any(dom == b or dom.endswith("." + b) for b in domains)

    def allowed_url(self, url: str, user_supplied: bool = False) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        dom = self._domain(url)
        if self._in(dom, PEOPLE_SEARCH_DOMAINS) or LEAK_URL_RE.search(url):
            return False
        if not user_supplied and self._in(dom, LOGIN_WALLED_DOMAINS):
            return False
        if urlparse(url).path.lower().endswith(SKIP_EXTENSIONS):
            return False
        return True

    def _skip(self, url: str, reason: str) -> None:
        self.skipped.append((url, reason))

    def _pace(self, url: str) -> None:
        """Human pace: a short, varied pause between pages on the same site (other sites load in parallel)."""
        dom = self._domain(url)
        with self._pace_lock:
            now = time.time()
            if self._pace_range[1] <= 0:
                return
            start = max(now, self._last_hit.get(dom, 0) + random.uniform(*self._pace_range))
            self._last_hit[dom] = start
        if start > now:
            time.sleep(start - now)

    def browser(self):
        """The real-browser reader, started on first need. None if switched off or unavailable."""
        if self.browser_mode == "off" or self._browser_failed:
            return None
        with self._browser_lock:
            if self._browser is None and not self._browser_failed:
                from pathlib import Path

                from .browser import BrowserFetcher, BrowserUnavailable

                try:
                    root = Path(self.profile_dir) if self.profile_dir else Path(__file__).resolve().parents[1] / ".browser_profile"
                    self._browser = BrowserFetcher(root, visible=self.browser_mode == "visible",
                                                   confirm_adult=self.confirm_adult)
                    log(f"→ Real-browser reader ready ({self._browser.name}"
                        + (", visible window" if self.browser_mode == "visible" else "") + ")")
                except BrowserUnavailable as e:
                    self._browser_failed = True
                    log(f"  ! Real-browser reader not available: {e}")
                    return None
                import atexit

                atexit.register(self._browser.close)
        return self._browser

    def _needs_browser(self, resp, kind: str) -> bool:
        """Use the real browser for pages you chose (and the creator's own channels) when the fast reader
        didn't get the page: refused, a human check, an unanswered age page, or a JS-only page with no text."""
        if self.browser_mode == "off" or kind not in ("user", "hub", "platform", "social"):
            return False
        gate = getattr(resp, "age_gate", "")
        if gate in ("strict", "off"):
            return False  # real verification is never bypassed; "off" = the user didn't confirm 18+
        if gate or resp.status_code >= 400:
            return True
        if "html" not in resp.headers.get("content-type", ""):
            return False
        from .browser import CHALLENGE_RE

        text, title = extract_main_text(resp.text)
        return len(text) < 300 or bool(CHALLENGE_RE.search(title + " " + text[:1500]) and len(text) < 1500)

    def _pass_age_gate(self, url: str, resp: requests.Response) -> requests.Response | None:
        """Answer a simple click-through age gate the way a browser would, then load the page again.
        1) remember-me cookies the gate commonly checks, 2) the gate's own "Enter / I am 18" link or form."""
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup

        host = urlparse(url).hostname or ""
        base_domain = ".".join(host.split(".")[-2:])
        headers = dict(BROWSER_HEADERS, Referer=url)

        def still_gated(r: requests.Response) -> bool:
            return r.status_code >= 400 or "html" not in r.headers.get("content-type", "") or bool(age_gate_kind(r.text))

        for name, value in AGE_COOKIES.items():
            self.http.cookies.set(name, value, domain=base_domain, path="/")
        again = self.http.get(url, headers=headers, timeout=self.timeout)
        if not still_gated(again):
            return again

        soup = BeautifulSoup(resp.text[:300_000], "html.parser")
        # forms: submit the one whose button/labels say "Enter / I am 18…"
        for form in soup.find_all("form"):
            label = " ".join(x.get_text(" ", strip=True) + " " + (x.get("value") or "")
                             for x in form.find_all(["button", "input", "label", "a"]))
            if not CONFIRM_WORDS.search(label):
                continue
            data = {i.get("name"): i.get("value") or "1" for i in form.find_all("input")
                    if i.get("name") and ((i.get("type") or "").lower() not in ("checkbox", "radio", "submit", "button")
                                          or i.get("checked") is not None)}
            for btn in form.find_all(["button", "input"]):
                if btn.get("name") and CONFIRM_WORDS.search((btn.get_text(" ", strip=True) + " " + (btn.get("value") or ""))):
                    data[btn.get("name")] = btn.get("value") or "1"
                    break
            for box in form.find_all("input", attrs={"type": re.compile("checkbox", re.I)}):
                if box.get("name"):
                    data[box.get("name")] = box.get("value") or "on"
            target = urljoin(resp.url, form.get("action") or resp.url)
            method = (form.get("method") or "get").lower()
            try:
                if method == "post":
                    self.http.post(target, data=data, headers=headers, timeout=self.timeout)
                else:
                    self.http.get(target, params=data, headers=headers, timeout=self.timeout)
            except requests.RequestException:
                continue
            again = self.http.get(url, headers=headers, timeout=self.timeout)
            if not still_gated(again):
                return again
        # links/buttons: follow the "Enter / I am 18" one
        for a in soup.find_all(["a", "button"]):
            label = a.get_text(" ", strip=True) + " " + (a.get("title") or "") + " " + (a.get("aria-label") or "")
            href = a.get("href") or a.get("data-href") or a.get("data-url") or ""
            if not href or href.startswith(("#", "javascript:", "mailto:")) or DENY_WORDS.search(label):
                continue
            if CONFIRM_WORDS.search(label):
                try:
                    self.http.get(urljoin(resp.url, href), headers=headers, timeout=self.timeout)
                    again = self.http.get(url, headers=headers, timeout=self.timeout)
                except requests.RequestException:
                    continue
                if not still_gated(again):
                    return again
        return None

    def _get(self, url: str, retry: bool) -> requests.Response:
        self._pace(url)
        resp = self.http.get(url, headers=BROWSER_HEADERS, timeout=self.timeout)
        if resp.status_code < 400 and "html" in resp.headers.get("content-type", ""):
            kind = age_gate_kind(resp.text)
            if kind == "strict":
                resp.age_gate = "strict"  # type: ignore[attr-defined]
                return resp
            if kind == "click":
                if not self.confirm_adult:
                    resp.age_gate = "off"  # type: ignore[attr-defined]
                    return resp
                passed = self._pass_age_gate(url, resp)
                if passed is not None:
                    log(f"  i {url}: answered the “Are you 18?” page")
                    return passed
                resp.age_gate = "click"  # type: ignore[attr-defined]
                return resp
        if retry and resp.status_code in (403, 404, 406, 429, 503):
            time.sleep(1.5)
            alt = url
            if resp.status_code == 404:  # some servers only know one form of the address
                path = urlparse(url).path
                alt = url.replace(path, path[:-1] if path.endswith("/") else path + "/", 1) if path not in ("", "/") else url
            second = requests.get(alt, headers=ALT_HEADERS, timeout=self.timeout)
            if second.status_code < 400:
                return second
        return resp

    def _youtube(self, url: str) -> str:
        """Title/channel via YouTube's public oEmbed, plus the page description. Speech isn't available."""
        bits = []
        try:
            o = self.http.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"},
                              headers=BROWSER_HEADERS, timeout=self.timeout)
            if o.status_code == 200:
                j = o.json()
                bits.append(f"[YouTube video] “{j.get('title', '')}” – channel: {j.get('author_name', '')}")
        except (requests.RequestException, ValueError):
            pass
        return "\n".join(bits)

    @property
    def can_render(self) -> bool:
        """True when the real-browser reader is allowed (Page reader setting isn't "Fast reader only")."""
        return self.browser_mode != "off" and not self._browser_failed

    def fetch(self, url: str, title: str = "", kind: str = "web", keep_html: bool = False,
              via: str = "", render: bool = False) -> Source | None:
        """render=True: load this page with the real browser even if the fast reader could read it (used when the
        fast reader's copy had text but not the JavaScript-built part we need, e.g. a clip grid or a post feed).
        Ignored when the Page reader setting is "Fast reader only"."""
        url = normalize_url(url)
        user_supplied = kind in ("user", "hub", "platform", "social")
        keep_html = keep_html or kind == "hub"
        label = f"{url} (sub-page of {via})" if via else url
        if not self.allowed_url(url, user_supplied=user_supplied):
            dom = self._domain(url)
            reason = ("people-search / records site" if self._in(dom, PEOPLE_SEARCH_DOMAINS)
                      else "leak / repost site" if LEAK_URL_RE.search(url)
                      else "social site found by search (pass it with --url to read it)"
                      if self._in(dom, LOGIN_WALLED_DOMAINS) else "unsupported link")
            self._skip(url, reason)
            log(f"  ! skipped {label} ({reason})")
            return None
        if self.cache and (hit := self.cache.get("page", url)):  # failures are never cached
            if (not keep_html or hit.get("meta", {}).get("html")) and (not render or hit.get("meta", {}).get("rendered")):
                return Source(**hit)
        src = None
        is_youtube = self._domain(url) in ("youtube.com", "m.youtube.com") and "/watch" in url
        try:
            resp = None
            if ((self.browser_mode == "always" or render) and kind in ("user", "hub", "platform", "social")
                    and (b := self.browser())):
                self._pace(url)
                try:
                    resp = b.load(url)
                except Exception as e:  # noqa: BLE001
                    log(f"  ! {label}: real browser failed ({e.__class__.__name__}) – using the fast reader")
            if resp is None or (resp.status_code >= 400 and not getattr(resp, "age_gate", "")):
                resp = self._get(url, retry=user_supplied)
                if self._needs_browser(resp, kind) and (b := self.browser()):
                    self._pace(url)
                    try:
                        page = b.load(url)
                        if page.status_code < 400 and not page.age_gate:
                            log(f"  i {label}: read with the real browser")
                        resp = page if (page.status_code < 400 or page.age_gate) else resp
                    except Exception as e:  # noqa: BLE001
                        log(f"  ! {label}: real browser failed ({e.__class__.__name__})")
            ctype = resp.headers.get("content-type", "")
            gate = getattr(resp, "age_gate", "")
            if gate:
                why = {
                    "strict": "the site uses real age verification (ID, face or card check), which the app does not "
                              "bypass – " + BLOCKED_TIP,
                    "click": "an age-confirmation page the app couldn't answer automatically – " + BLOCKED_TIP,
                    "off": "an “Are you 18?” page – tick “I'm 18+” (web UI) or use --confirm-adult to let the app "
                           "answer it",
                    "challenge": ("the site asked to verify you're human – choose Page reader → “Visible browser” "
                                  "(web UI) or --browser visible, and complete the check yourself when the window "
                                  "opens" if self.browser_mode != "visible" else
                                  "the human check wasn't completed in time – run again and complete it in the "
                                  "browser window"),
                }[gate]
                self._skip(url, why)
                log(f"  ! {label}: {why}")
            elif resp.status_code < 400 and "html" in ctype:
                text, page_title = extract_main_text(resp.text, url)
                meta = extract_meta(resp.text)
                if (user_supplied or is_youtube) and meta and meta not in text:
                    text = f"[Profile/meta description] {meta}\n\n{text}"
                if is_youtube:
                    yt = self._youtube(url)
                    text = f"{yt}\n{('[Description] ' + meta) if meta else ''}".strip()
                    log(f"  i {url}: YouTube – only the title and description can be read. For what is said in the "
                        "video, paste a transcript (YouTube: … → Show transcript) into a .txt under Text sources.")
                # Pages you chose are kept even if short (a social bio can be one line).
                # Clip stores / social pages read for their markup (clip grids, embedded post data) are kept even
                # with little prose: the caller parses the HTML, not the text.
                markup_only = keep_html and len(resp.text) > 100 and (kind == "social" or (kind == "platform" and "<a" in resp.text))
                if markup_only or len(text) > (0 if kind == "hub" else 60 if user_supplied else 400):
                    src = Source(url=url, title=title or page_title or url, text=text[: self.max_page_chars],
                                 kind=kind, meta={"html": resp.text[:2_000_000 if kind in ("platform", "social")
                                                                   else 400_000]} if keep_html else {})
                    if type(resp).__name__ == "BrowserPage":
                        src.meta["rendered"] = True
                else:
                    self._skip(url, "almost no readable text (login wall or JS-only) – " + BLOCKED_TIP)
                    log(f"  ! {label}: almost no readable text (login wall or JS-only page) – {BLOCKED_TIP}")
            elif resp.status_code < 400:
                self._skip(url, f"not a web page ({ctype.split(';')[0] or 'unknown type'})")
                log(f"  ! {label}: not a web page ({ctype.split(';')[0]}) – skipped")
            else:
                code = resp.status_code
                if code == 404:
                    why = ("page not found – check the link opens in your browser; if it does, the site is "
                           "blocking automated readers: " + BLOCKED_TIP)
                else:
                    why = f"{STATUS_ADVICE.get(code, 'the site refused the request')} – {BLOCKED_TIP}"
                self._skip(url, f"HTTP {code}: {why}")
                log(f"  ! {label}: HTTP {code} – {why}" if not via else f"  · {label}: HTTP {code} (skipped)")
        except requests.RequestException as e:
            self._skip(url, f"could not connect ({e.__class__.__name__})")
            log(f"  ! {label}: could not connect ({e.__class__.__name__})")
        if self.cache and src:
            self.cache.set("page", url, dict(src.__dict__))
        return src

    def fetch_many(self, items: Iterable[tuple], workers: int = 6, kind: str = "web",
                   keep_html: bool = False) -> list[Source]:
        """items: (url, title) or (url, title, via)."""
        items = list(items)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(
                lambda it: self.fetch(it[0], it[1], kind, keep_html, it[2] if len(it) > 2 else ""), items))
        return [r for r in results if r]

    def is_own_site(self, url: str) -> bool:
        """A domain that carries the creator's handle or name (ivywren.com, ivy-wren.carrd.co…)."""
        dom = re.sub(r"[^a-z0-9]", "", self._domain(url))
        return any(len(h) >= 4 and re.sub(r"[^a-z0-9]", "", h) in dom for h in self.handles)

    # ───────────────────────────────────────────── Adult-platform profiles (creator's own)
    def is_creator_profile(self, url: str, handles: list[str]) -> bool:
        dom = self._domain(url)
        plat = next((p for p in ADULT_PLATFORMS if dom == p or dom.endswith("." + p)), None)
        if not plat or LEAK_URL_RE.search(url):
            return False
        path = urlparse(url).path.lower()
        if plat == "pornhub.com" and not re.match(r"^/(model|pornstar|channels)/", path):
            return False  # video pages are often reuploads by others
        if plat == "xhamster.com" and not re.match(r"^/(creators|channels|users)/", path):
            return False
        return any(h and re.search(rf"(^|[/_.-]){re.escape(h)}($|[/_.?-])", path) for h in handles)

    def adult_platform_sweep(self, name: str, handles: list[str], seen: set[str], max_pages: int = 12) -> list[Source]:
        """Search each adult platform for the creator's own profile/storefront pages and read them."""
        picked: list[tuple[str, str]] = []
        terms = list(dict.fromkeys([f'"{name}"'] + [f'"{h}"' for h in handles[:3]]))
        term = " OR ".join(terms[:3])
        plats = list(ADULT_PLATFORMS)
        # A few batched "site:a OR site:b …" searches instead of one per platform (keeps free search from
        # rate-limiting the run).
        for i in range(0, len(plats), 5):
            sites = " OR ".join(f"site:{p}" for p in plats[i : i + 5])
            for url, title in self.search(f"({term}) ({sites})", max_results=10):
                u = url.split("#")[0].rstrip("/")
                if u in seen or not self.is_creator_profile(u, handles):
                    continue
                seen.add(u)
                picked.append((url, title))
            if len(picked) >= max_pages:
                break
        if not picked:  # some engines ignore batched site: searches – plain searches catch most profiles
            for plat in ("onlyfans", "fansly", "manyvids", "pornhub model", "clips4sale", "chaturbate"):
                for url, title in self.search(f"{terms[0]} {plat}", max_results=8):
                    u = url.split("#")[0].rstrip("/")
                    if u not in seen and self.is_creator_profile(u, handles):
                        seen.add(u)
                        picked.append((url, title))
                if len(picked) >= max_pages:
                    break
        if picked:
            log(f"→ Adult-platform profiles found: {len(picked)}")
        return self.fetch_many(picked[:max_pages], kind="platform")

    def clip_catalogs(self, urls: Iterable[str], handles: list[str], max_pages: int = 3, max_details: int = 20,
                      max_stores: int = 8) -> list:
        """Clip catalogues (titles, tags, descriptions) for every one of the creator's OWN stores among `urls`."""
        from .clips import platform_of, read_store

        stores: list[str] = []
        for u in urls:
            u = normalize_url(u).split("#")[0].rstrip("/")
            if platform_of(u) and self.is_creator_profile(u, handles) and u not in stores:
                stores.append(u)
        if not stores:
            return []
        log(f"→ Reading clip catalogues on {len(stores[:max_stores])} of the creator's own store(s)…")
        out = []
        for u in stores[:max_stores]:
            try:
                if cat := read_store(self, u, max_pages=max_pages, max_details=max_details):
                    out.append(cat)
            except Exception as e:  # noqa: BLE001 – one odd store must not end the run
                log(f"  ! clip catalogue for {u} failed: {e.__class__.__name__}: {str(e)[:120]}")
        return out

    # ───────────────────────────────────────────── Sub-pages of the creator's own sites
    def follow_site_pages(self, pages: list[Source], per_site: int = 5, from_hub: bool = False) -> list[Source]:
        """For each fetched page on the creator's own site (not a social platform or link hub), read up to
        `per_site` more pages on the same site, preferring about/bio/FAQ/interview/blog pages.
        Pages you added yourself only get this when the domain carries the creator's name or handle –
        an interview on a magazine site must not pull in the magazine's pages about other people."""
        if per_site <= 0:
            return []
        pages = [p for p in pages if from_hub or self.is_own_site(p.url)]
        seen = {p.url.split("#")[0].rstrip("/") for p in pages}
        targets: list[tuple[str, str]] = []
        done_sites: set[str] = set()
        for p in pages:
            html = p.meta.pop("html", "")
            dom = self._domain(p.url)
            if not html or dom in done_sites or self._in(dom, NO_SUBPAGE_DOMAINS):
                continue
            done_sites.add(dom)
            cands = []
            for u, t in extract_links(html, p.url):
                if self._domain(u) != dom or u.split("#")[0].rstrip("/") in seen or HUB_SKIP_RE.search(u):
                    continue
                score = len(SITE_PAGE_HINTS.findall(u + " " + t)) * 10 - u.count("/")
                cands.append((score, u, t))
            cands.sort(key=lambda c: -c[0])
            for _, u, t in cands[:per_site]:
                seen.add(u.split("#")[0].rstrip("/"))
                targets.append((u, t, dom))
        if targets:
            log(f"→ Following {len(targets)} sub-pages on the creator's own sites")
        return self.fetch_many(targets, kind="user")

    # ───────────────────────────────────────────── Link-in-bio hubs (Linktree, Beacons, …)
    def crawl_link_hub(self, url: str, max_links: int = 25, depth: int = 1, site_pages: int = 5) -> list[Source]:
        """Read a link-in-bio page and every page it links to (one level by default, or `depth`
        levels when the linked page is itself a link hub), plus up to `site_pages` sub-pages on each
        of the creator's own websites."""
        hub = self.fetch(url, kind="hub")
        if not hub:
            return []
        html = hub.meta.pop("html", "")
        links = extract_links(html, url)
        self.hub_links += [u for u, _ in links if u not in self.hub_links]
        hub.text = hub.text + "\n\n## Links on this page\n" + "\n".join(f"- {t or u}: {u}" for u, t in links)
        out = [hub]
        targets = []
        for u, t in links:
            dom = self._domain(u)
            h = platform_handle(u)
            if h and h not in self.discovered_handles:
                self.discovered_handles.append(h)
            if self._in(dom, PEOPLE_SEARCH_DOMAINS):
                self._skip(u, "people-search / records site")
            elif LEAK_URL_RE.search(u):
                self._skip(u, "leak / repost site")
            elif HUB_SKIP_RE.search(u) or not self.allowed_url(u, user_supplied=True):
                self._skip(u, "not readable text (store, app, payment or legal page)")
            else:
                targets.append((u, t))
        log(f"→ Link hub {url}: {len(links)} links, reading {min(len(targets), max_links)}")
        for u, t in targets[:max_links]:
            debug(f"hub link: {t} → {u}")
        # Linked link-hubs (another Linktree, a Carrd site…) are crawled when depth allows, otherwise read as pages.
        sub_hubs = ([u for u, _ in targets[:max_links] if self._in(self._domain(u), LINK_HUB_DOMAINS)]
                    if depth > 1 else [])
        pages = self.fetch_many([(u, t) for u, t in targets[:max_links] if u not in sub_hubs], kind="user",
                                keep_html=site_pages > 0)
        out.extend(pages)
        out.extend(self.follow_site_pages(pages, per_site=site_pages, from_hub=True))
        for p in pages:
            p.meta.pop("html", None)
        if depth > 1:
            for sh in sub_hubs:
                out.extend(self.crawl_link_hub(sh, max_links=max_links, depth=depth - 1, site_pages=site_pages))
        return out


_NOT_HANDLES = {"profile", "model", "pornstar", "channels", "studio", "store", "u", "user", "users", "c", "p",
                "share", "watch", "video", "videos", "home", "explore", "search", "login", "signup", "i", "en", "www"}


def platform_handle(url: str) -> str:
    """The username in a link to a social or adult platform (onlyfans.com/ivy → 'ivy'), or ''."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    known = set(ADULT_PLATFORMS) | LOGIN_WALLED_DOMAINS | {"reddit.com", "twitch.tv", "youtube.com", "kick.com"}
    if not any(host == d or host.endswith("." + d) for d in known):
        return ""
    for seg in urlparse(url).path.split("/"):
        seg = seg.strip().lstrip("@").lower()
        if seg and seg not in _NOT_HANDLES and not seg.isdigit() and re.fullmatch(r"[a-z0-9_.-]{4,40}", seg):
            return seg
    return ""


def creator_handles(name: str, seeds: list[str], extra: list[str] | None = None) -> list[str]:
    """Likely usernames: explicit --handle values, link-hub usernames, and slugs of the stage name."""
    handles: list[str] = [h.strip().lstrip("@").lower() for h in (extra or []) if h.strip()]
    for s in seeds:
        seg = [p for p in urlparse(s).path.split("/") if p]
        host = urlparse(s).netloc.lower().removeprefix("www.")
        if seg:
            handles.append(seg[0].lstrip("@").lower())
        elif host.count(".") >= 2:  # ivywren.carrd.co
            handles.append(host.split(".")[0])
    parts = [p.lower() for p in re.split(r"\s+", name.strip()) if p]
    if parts:
        handles += ["".join(parts), "_".join(parts), "-".join(parts), ".".join(parts)]
    return [h for h in dict.fromkeys(handles) if len(h) >= 4]


def load_local_file(path: str) -> Source:
    """A creator-provided text source (brief, Q&A, transcript, bio). Treated as authoritative."""
    from pathlib import Path

    p = Path(path)
    raw = p.read_text("utf-8", errors="replace")
    if p.suffix.lower() in (".html", ".htm"):
        raw, _ = extract_main_text(raw)
    elif p.suffix.lower() == ".json":
        import json

        try:
            raw = json.dumps(json.loads(raw), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    return Source(url=f"file://{p.resolve()}", title=p.name, text=collapse_ws(raw), kind="brief")


def extract_meta(html: str) -> str:
    """og:/twitter:/meta description – often the only readable bio on social profiles."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:300_000], "html.parser")
    vals = []
    for attr, name in (("property", "og:title"), ("property", "og:description"), ("name", "description"),
                       ("name", "twitter:description")):
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content"):
            v = tag["content"].strip()
            if v and v not in vals:
                vals.append(v)
    return " | ".join(vals)


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Outbound links from a link hub: anchors plus URLs in embedded JSON (Linktree/Next.js pages)."""
    import json
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    base_clean = base_url.split("#")[0].rstrip("/")
    found: dict[str, str] = {}

    def add(u: str, t: str = "") -> None:
        u = urljoin(base_url, u.strip())
        if not u.startswith("http") or u.split("#")[0].rstrip("/") == base_clean:
            return
        if urlparse(u).path in ("", "/") and urlparse(u).netloc == urlparse(base_url).netloc:
            return  # the hub site's own home page
        u = u.split("#")[0]
        if u not in found or (t and not found[u]):
            found[u] = t.strip()[:120]

    for a in soup.find_all("a", href=True):
        add(a["href"], a.get_text(" ", strip=True))

    def walk(node):
        if isinstance(node, dict):
            u = node.get("url") or node.get("href")
            if isinstance(u, str) and u.startswith("http"):
                add(u, str(node.get("title") or node.get("name") or ""))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for script in soup.find_all("script"):
        if script.get("id") == "__NEXT_DATA__" or script.get("type") == "application/json":
            try:
                walk(json.loads(script.string or ""))
            except (ValueError, TypeError):
                continue
    junk = re.compile(r"(cdn\.|static\.|assets\.|\.(js|css|svg|png|jpe?g|webp|woff2?)(\?|$)|googletagmanager|"
                      r"facebook\.com/tr|sentry|linktr\.ee/(s|privacy|terms))", re.I)
    return [(u, t) for u, t in found.items() if not junk.search(u)]


def extract_main_text(html: str, url: str = "") -> tuple[str, str]:
    """Readable text from HTML. Uses trafilatura if installed, else a BeautifulSoup heuristic."""
    title = ""
    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False) or ""
        meta = trafilatura.extract_metadata(html)
        title = (meta.title if meta else "") or ""
        if len(text) > 400:
            return collapse_ws(text), title
    except ImportError:
        pass
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    for sel in ['[class*="cookie"]', '[class*="newsletter"]', '[class*="related"]', '[class*="comment"]',
                '[id*="comment"]', '[class*="share"]', '[class*="advert"]', '[class*="sidebar"]']:
        for tag in soup.select(sel):
            tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote", "dd"]):
        t = el.get_text(" ", strip=True)
        if len(t) < 25 and el.name not in ("h1", "h2", "h3"):
            continue
        if el.name in ("h1", "h2", "h3"):
            t = f"\n## {t}"
        parts.append(t)
    # de-duplicate nested li/p repeats
    seen: set[str] = set()
    uniq = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return collapse_ws("\n".join(uniq)), title


def mentions_person(text: str, name: str, aliases: list[str] | None = None) -> bool:
    """Cheap relevance filter: the page must mention the surname (or an alias) a few times."""
    names = [name] + list(aliases or [])
    low = text.lower()
    for n in names:
        tokens = [t for t in re.split(r"\s+", n.strip()) if len(t) > 2]
        if not tokens:
            continue
        surname = tokens[-1].lower()
        if low.count(surname) >= 3 or n.lower() in low:
            return True
    return False
