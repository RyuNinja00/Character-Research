"""Audience research (--audience-research): what fans and the public say about a creator's published content.

Output is a separate report for the creator (audience_report.md / audience_insights.json). It is never merged
into the dossier, card or lorebook, and insights are framed as audience perception, not as facts about the
creator's own preferences. Searches use the stage name and handles only. Leak/repost and people-search sites
stay blocked, and anything about the creator's real identity or private life is discarded.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .llm import LLMClient
from .research import Researcher, Source, mentions_person
from .synthesize import Options
from .util import chunk_text, log

INSIGHT_TYPES = [
    "associated_theme",   # themes/niches/dynamics fans associate with the content
    "praised",            # what audiences praise most
    "request",            # what fans ask for / wish existed
    "criticism",          # complaints, what turns people off
    "comparison",         # creators/genres they're compared to
    "fan_language",       # recurring words/nicknames fans use
    "platform_signal",    # where/how fans engage, price/value comments
]

QUERIES = [
    '{h} reddit', '{h} fans', '{h} review', '{h} best content', '{h} requests',
    '{h} subscribers say', '{h} worth it', '{h} similar creators',
]


def _extract_system(name: str, handles: list[str]) -> str:
    return f"""You analyse public audience discussion about the adult content creator known as {name}
(handles: {", ".join(handles) or "none given"}). You help the creator understand their audience so they can
tailor their content.

Extract AUDIENCE INSIGHTS about their published content: what fans and commenters say, ask for, praise or
criticise. Frame every insight as audience perception ("fans associate her content with…", "several
commenters request…"), never as a fact about the creator's own sexual preferences or private life.

Types: {", ".join(INSIGHT_TYPES)}

Discard and never record:
- anything about their real/legal name, location, family, partners, health or life outside their content;
- speculation about their body, sexuality or private behaviour beyond what their published content shows;
- leaked material, harassment, threats, doxxing attempts, or anything involving minors;
- explicit transcription — describe themes in neutral terms.

JSON: {{"relevant": true|false, "insights": [{{"type": "...", "insight": "...", "mentions": 1}}]}}
"mentions" = how many distinct commenters/places in this text express it. If the text is not about this
creator's content, return {{"relevant": false, "insights": []}}."""


def _report_system(name: str) -> str:
    return f"""You write an audience-insights report for the adult content creator {name}, based on extracted
insights from public discussion. Group and merge similar insights, rank by how often they appear, and keep the
framing as audience perception. Be neutral and practical; end with 3-6 content opportunities the data supports.
Never present any insight as the creator's own preference.

JSON: {{"summary": "2-4 sentences",
  "sections": [{{"title": "Themes fans associate with the content" | "What audiences praise" | "Requests" |
                 "Criticisms" | "Comparisons" | "Fan language" | "Platform & value signals",
                 "items": [{{"point": "...", "strength": "strong|moderate|weak", "sources": 2}}]}}],
  "opportunities": ["..."]}}"""


def audience_research(
    r: Researcher,
    extract_llm: LLMClient,
    main_llm: LLMClient,
    opts: Options,
    handles: list[str],
    max_pages: int = 20,
    seen_urls: set[str] | None = None,
) -> tuple[dict, list[dict], list[Source]]:
    terms = [f'"{opts.name}"'] + [f'"{h.lstrip("@")}"' for h in handles if h.strip()]
    log(f"→ Audience research for {', '.join(terms)}…")
    seen = set(seen_urls or set())
    picked: list[tuple[str, str]] = []
    per_domain: dict[str, int] = {}
    for t in terms:
        for q in QUERIES:
            for url, title in r.search(q.format(h=t), max_results=8):
                u = url.split("#")[0].rstrip("/")
                dom = urlparse(u).netloc.lower().removeprefix("www.")
                if u in seen or not r.allowed_url(u) or per_domain.get(dom, 0) >= 4:
                    continue
                seen.add(u)
                per_domain[dom] = per_domain.get(dom, 0) + 1
                picked.append((url, title))
    pages = r.fetch_many(picked[: max_pages * 2])
    aliases = [h.lstrip("@") for h in handles]
    pages = [p for p in pages if mentions_person(p.text, opts.name, aliases)][:max_pages]
    log(f"  {len(pages)} relevant discussion pages")
    if not pages:
        return {"summary": "No public audience discussion found.", "sections": [], "opportunities": []}, [], []

    system = _extract_system(opts.name, handles)
    jobs = [(p, ch) for p in pages for ch in chunk_text(p.text, opts.chunk_chars)]

    def run(job):
        p, ch = job
        try:
            return p, extract_llm.chat_json(system, f"SOURCE: {p.title}\nURL: {p.url}\n\n{ch}", temperature=0.1)
        except Exception as e:  # noqa: BLE001
            log(f"  ! audience extraction failed for {p.url}: {e}")
            return p, {}

    insights: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        for p, data in pool.map(run, jobs):
            if not isinstance(data, dict) or not data.get("relevant", bool(data.get("insights"))):
                continue
            for it in data.get("insights") or []:
                if isinstance(it, dict) and str(it.get("insight", "")).strip():
                    typ = str(it.get("type", "")).strip()
                    insights.append({
                        "type": typ if typ in INSIGHT_TYPES else "associated_theme",
                        "insight": str(it["insight"]).strip(),
                        "mentions": int(it.get("mentions") or 1) if str(it.get("mentions", "1")).isdigit() else 1,
                        "source": p.url,
                    })
    log(f"  {len(insights)} audience insights")
    if not insights:
        return {"summary": "Pages were found but contained no usable audience insights.", "sections": [],
                "opportunities": []}, [], pages
    lines = "\n".join(f"[{i['type']}] {i['insight']} (mentions: {i['mentions']}; {i['source']})" for i in insights)
    try:
        report = main_llm.chat_json(_report_system(opts.name), lines[:60_000], temperature=0.3)
    except Exception as e:  # noqa: BLE001
        log(f"  ! audience report failed: {e}")
        report = {"summary": "", "sections": [], "opportunities": []}
    return report if isinstance(report, dict) else {}, insights, pages


def render_report(name: str, report: dict, insights: list[dict], pages: list[Source]) -> str:
    out = [f"# Audience insights: {name}", "",
           "> What fans and the public say about this creator's **published content**, gathered from public "
           "discussion of the stage name and handles. This is audience perception, not fact, and it is **not** "
           "used in the character card. Leak sites, people-search sites and real-identity details were excluded.",
           ""]
    if report.get("summary"):
        out += ["## Summary", "", str(report["summary"]), ""]
    for sec in report.get("sections") or []:
        items = sec.get("items") or []
        if not items:
            continue
        out += [f"## {sec.get('title', 'Insights')}", ""]
        for it in items:
            if isinstance(it, dict):
                out.append(f"- {it.get('point', '')} _({it.get('strength', '?')}, {it.get('sources', '?')} sources)_")
            else:
                out.append(f"- {it}")
        out.append("")
    if report.get("opportunities"):
        out += ["## Content opportunities", ""] + [f"- {o}" for o in report["opportunities"]] + [""]
    out += ["## Pages read", ""] + [f"- [{(p.title or p.url)[:80]}]({p.url})" for p in pages]
    out += ["", f"Raw insights with sources: `audience_insights.json` ({len(insights)} items)."]
    return "\n".join(out) + "\n"


def save(out_dir, name: str, report: dict, insights: list[dict], pages: list[Source]) -> None:
    (out_dir / "audience_report.md").write_text(render_report(name, report, insights, pages), "utf-8")
    (out_dir / "audience_insights.json").write_text(
        json.dumps({"report": report, "insights": insights}, ensure_ascii=False, indent=2), "utf-8")
