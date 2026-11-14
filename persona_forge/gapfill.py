"""Iterative gap-filling research.

After each dossier build, score how complete each part of the profile is, turn the gaps into targeted web
searches, read the new pages, merge their facts and rebuild — until the profile reaches the target
coverage, a round finds nothing new, or the round limit is hit.

Sexual preferences / intimacy are never searched for on the open web. They are taken only from the
creator's own channels (link hub, their own sites, pages you pass with --url, creator briefs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .llm import LLMClient
from .research import ADULT_PRESS_QUERIES, Researcher, Source, mentions_person
from .synthesize import Fact, Options, _dedupe_facts, build_dossier, extract_facts
from .util import log


@dataclass
class Gap:
    key: str
    label: str
    weight: float
    queries: list[str]           # templates; {name} is substituted. Empty = never web-searched.
    filled: bool = False


def _get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _filled(value, min_items: int = 1, min_chars: int = 25) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return len(value.strip()) >= min_chars
    if isinstance(value, list):
        return len([v for v in value if v]) >= min_items
    if isinstance(value, dict):
        return sum(1 for v in value.values() if _filled(v, 1, 8)) >= max(1, min_items)
    return bool(value)


# (dossier path, label, weight, min_items, search query templates)
COVERAGE_SPEC: list[tuple[str, str, float, int, list[str]]] = [
    ("appearance", "Appearance", 1.0, 1,
     ['"{name}" appearance tattoos hair', '"{name}" height']),
    ("personality_traits", "Personality traits", 1.5, 4,
     ['"{name}" interview', '"{name}" personality']),
    ("habits_and_quirks", "Habits & quirks", 0.7, 2,
     ['"{name}" day in the life', '"{name}" fun facts']),
    ("speech_style", "Speech style", 1.5, 3,
     ['"{name}" podcast', '"{name}" stream OR livestream', '"{name}" Q&A OR AMA']),
    ("signature_quotes", "Signature quotes", 0.8, 3,
     ['"{name}" says', '"{name}" quote']),
    ("inner_life.core_motivations", "Motivations", 1.2, 2,
     ['"{name}" why I started', '"{name}" how she got started OR how he got started OR how they got started']),
    ("inner_life.insecurities_and_fears", "Insecurities & fears", 1.0, 2,
     ['"{name}" opens up', '"{name}" struggles OR anxiety OR insecure']),
    ("inner_life.contradictions", "Contradictions", 0.8, 1,
     ['"{name}" behind the scenes', '"{name}" in real life']),
    ("inner_life.emotional_patterns", "Emotional patterns", 0.8, 2,
     ['"{name}" mental health OR burnout', '"{name}" emotional']),
    ("inner_life.how_they_handle", "Reactions (praise/criticism/conflict)", 0.8, 2,
     ['"{name}" responds to haters OR criticism', '"{name}" drama']),
    ("inner_life.attachment_and_relationship_style", "Relationship style", 0.6, 1,
     ['"{name}" dating OR relationships interview']),
    ("inner_life.public_persona_vs_private_self", "Persona vs. private self", 0.6, 1,
     ['"{name}" real life vs online', '"{name}" honest interview']),
    ("core_values_and_beliefs", "Values & beliefs", 0.8, 2,
     ['"{name}" believes OR opinion', '"{name}" advice']),
    ("likes", "Likes", 0.7, 3, ['"{name}" favorite', '"{name}" hobbies']),
    ("dislikes", "Dislikes", 0.5, 2, ['"{name}" hates OR pet peeves']),
    ("skills", "Skills", 0.4, 1, ['"{name}" talent OR skills']),
    ("timeline", "Backstory timeline", 1.2, 3,
     ['"{name}" early life OR background', '"{name}" career timeline']),
    ("works_and_achievements", "Work & achievements", 0.6, 1,
     ['"{name}" awards OR nominated', '"{name}" career']),
    ("relationships", "Public relationships & collaborators", 0.5, 1,
     ['"{name}" collab OR collaboration', '"{name}" friends with']),
]

# Scored and reported, but only ever filled from the creator's own channels — never searched for.
OWN_CHANNEL_ONLY_SPEC: list[tuple[str, str, float, int]] = [
    ("intimacy.flirting_style", "Flirting style", 1.0, 1),
    ("intimacy.dynamics_and_themes", "Dynamics & themes", 1.0, 2),
    ("intimacy.fan_interaction_style", "Fan-interaction style", 0.8, 1),
    ("intimacy.stated_limits", "Stated limits", 0.8, 1),
    ("intimacy.content_themes", "Posted-content themes", 1.2, 2),
    ("intimacy.persona_kinks", "Persona kinks (from content)", 1.2, 2),
]


@dataclass
class Coverage:
    score: float
    gaps: list[Gap] = field(default_factory=list)
    own_channel_gaps: list[str] = field(default_factory=list)
    table: list[tuple[str, bool, str]] = field(default_factory=list)  # (label, filled, note)


def coverage(dossier: dict, opts: Options) -> Coverage:
    total = got = 0.0
    cov = Coverage(score=0.0)
    for path, label, weight, min_items, queries in COVERAGE_SPEC:
        ok = _filled(_get(dossier, path), min_items)
        total += weight
        got += weight if ok else 0
        cov.table.append((label, ok, ""))
        if not ok:
            cov.gaps.append(Gap(path, label, weight, queries))
    if opts.nsfw:
        for path, label, weight, min_items in OWN_CHANNEL_ONLY_SPEC:
            ok = _filled(_get(dossier, path), min_items)
            total += weight
            got += weight if ok else 0
            cov.table.append((label, ok, "creator's own channels only"))
            if not ok:
                cov.own_channel_gaps.append(label)
    cov.score = got / total if total else 1.0
    return cov


@dataclass
class GapFillResult:
    dossier: dict
    facts: list[Fact]
    aliases: list[str]
    used: list[Source]
    sources: list[Source]
    rounds: list[dict]
    coverage: Coverage


def gap_fill(
    r: Researcher,
    extract_llm: LLMClient,
    main_llm: LLMClient,
    opts: Options,
    *,
    dossier: dict,
    facts: list[Fact],
    aliases: list[str],
    used: list[Source],
    sources: list[Source],
    target: float = 0.85,
    max_rounds: int = 4,
    pages_per_round: int = 10,
    hint: str = "",
    handles: list[str] | None = None,
) -> GapFillResult:
    handles = handles or []
    swept = False
    seen_urls = {s.url.split("#")[0].rstrip("/") for s in sources} | {u for u, _ in r.skipped}
    used_queries: set[str] = set()
    rounds: list[dict] = []
    cov = coverage(dossier, opts)
    log(f"→ Profile coverage: {cov.score:.0%} (target {target:.0%})")

    for rnd in range(1, max_rounds + 1):
        if cov.score >= target:
            break
        # Biggest gaps first; two fresh queries per gap.
        queries: list[str] = []
        for gap in sorted(cov.gaps, key=lambda g: -g.weight):
            fresh = [q.format(name=opts.name) for q in gap.queries]
            fresh = [f"{q} {hint}".strip() for q in fresh if f"{q} {hint}".strip() not in used_queries]
            queries.extend(fresh[:2])
        if opts.nsfw:  # adult-industry press usually has the best interviews for adult creators
            queries = [q.format(name=opts.name) for q in ADULT_PRESS_QUERIES
                       if q.format(name=opts.name) not in used_queries] + queries
        queries = queries[:8]
        # Gaps in sexual preferences / content themes: look for the creator's own adult-platform profiles.
        platform_pages: list[Source] = []
        if opts.nsfw and cov.own_channel_gaps and not swept:
            swept = True
            platform_pages = r.adult_platform_sweep(opts.name, handles, seen_urls, max_pages=pages_per_round)
        if not queries and not platform_pages:
            log("  no new searches left to try")
            break
        used_queries.update(queries)
        log(f"→ Gap-fill round {rnd}: {', '.join(g.label for g in cov.gaps[:6])}{'…' if len(cov.gaps) > 6 else ''}")

        picked: list[tuple[str, str]] = []
        profiles: list[tuple[str, str]] = []
        per_domain: dict[str, int] = {}
        for q in queries:
            for url, title in r.search(q, max_results=8):
                u = url.split("#")[0].rstrip("/")
                dom = urlparse(u).netloc.lower().removeprefix("www.")
                if u in seen_urls or per_domain.get(dom, 0) >= 2:
                    continue
                if opts.nsfw and r.is_creator_profile(u, handles):
                    profiles.append((url, title))  # creator's own adult-platform page: read first
                elif r.allowed_url(u):
                    picked.append((url, title))
                else:
                    continue
                seen_urls.add(u)
                per_domain[dom] = per_domain.get(dom, 0) + 1
        pages = platform_pages + r.fetch_many(profiles, kind="platform")
        budget = max(0, pages_per_round - len(pages))
        others = [p for p in r.fetch_many(picked[: budget * 2]) if mentions_person(p.text, opts.name, list(aliases) + handles)]
        pages = pages + others[:budget]
        if not pages:
            rounds.append({"round": rnd, "queries": queries, "pages": 0, "new_facts": 0, "coverage": cov.score})
            log("  no new relevant pages found")
            continue
        sources.extend(pages)
        new_facts, new_aliases, new_used = extract_facts(extract_llm, pages, opts)
        before = len(facts)
        facts = _dedupe_facts(facts + new_facts)
        added = len(facts) - before
        used.extend(new_used)
        aliases = sorted(set(aliases) | set(new_aliases))
        if added == 0:
            rounds.append({"round": rnd, "queries": queries, "pages": len(pages), "new_facts": 0, "coverage": cov.score})
            log("  pages added nothing new")
            continue
        dossier = build_dossier(main_llm, facts, aliases, opts)
        prev = cov.score
        cov = coverage(dossier, opts)
        rounds.append({"round": rnd, "queries": queries, "pages": len(pages), "new_facts": added, "coverage": cov.score})
        log(f"  +{added} facts from {len(pages)} pages → coverage {prev:.0%} → {cov.score:.0%}")

    if cov.own_channel_gaps:
        log("  Not found on the creator's own channels (not searched elsewhere): " + ", ".join(cov.own_channel_gaps))
    return GapFillResult(dossier, facts, aliases, used, sources, rounds, cov)
