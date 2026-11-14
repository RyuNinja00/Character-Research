"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .llm import LLMClient, LLMConfig
from .marinara import (
    build_card,
    build_lorebook,
    embed_card_in_png,
    load_avatar,
    placeholder_avatar,
    push_to_marinara,
    to_v3,
)
from .audience import audience_research
from .audience import save as save_audience
from .gapfill import coverage, gap_fill
from .images import (
    build_sprite_kit,
    clean_image_appearance,
    collect_references,
    generate_avatar,
    load_sprites,
    pick_sprite_set,
)
from .research import ADULT_PRESS_QUERIES, Researcher, Source, creator_handles, load_local_file, mentions_person
from .synthesize import Options, flag_excluded, synthesize, write_card, write_lorebook
from .util import DiskCache, log, set_verbose, slugify


def load_dotenv(path: Path) -> None:
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / ".env"  # project folder, so it works from anywhere
        if not path.exists():
            return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    def env(key: str, default: str = "") -> str:
        """A .env / environment value; an empty one (KEY=) counts as not set."""
        return (os.environ.get(key) or "").strip() or default

    p = argparse.ArgumentParser(
        prog="persona-forge",
        description="Research a public, historical or fictional figure on the web and build a Marinara Engine "
        "character card + lorebook.",
    )
    p.add_argument("name", nargs="?", help='Who to build, e.g. "Marie Curie"')
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = p.add_argument_group("research")
    g.add_argument("--wiki-title", help="Exact Wikipedia article title (skips the picker)")
    g.add_argument("--lang", default=env("WIKI_LANG", "en"), help="Wikipedia language code (default en)")
    g.add_argument("--url", action="append", default=[], help="Extra page to include (repeatable), e.g. a Fandom wiki page")
    g.add_argument("--urls-file", help="Text file with one URL per line (lines starting with # are ignored)")
    g.add_argument("--seed", action="append", default=[],
                   help="Link-in-bio page (Linktree, Beacons, AllMyLinks…): read it AND every page it links to")
    g.add_argument("--seed-depth", type=int, default=1, help="Follow link hubs found on link hubs this many levels")
    g.add_argument("--seed-max-links", type=int, default=25, help="Max links to read per link hub")
    g.add_argument("--site-pages", type=int, default=5,
                   help="On the creator's own websites (found via --seed or --url), also read up to N sub-pages per "
                        "site, preferring about/bio/FAQ/interview/blog pages (0 = off)")
    g.add_argument("--file", action="append", default=[],
                   help="Local .txt/.md/.json/.html source, e.g. a creator brief or interview transcript (repeatable). "
                        "Treated as authoritative.")
    g.add_argument("--search-web", action="store_true",
                   help="In --talent/--no-wikipedia mode, also run web searches on the name (off by default)")
    g.add_argument("--deep", action="store_true",
                   help="Keep researching: score how complete the profile is, search the web for whatever is missing, "
                        "and repeat until --target-coverage or --max-rounds (implies --search-web)")
    g.add_argument("--target-coverage", type=float, default=0.85, help="Stop --deep research at this coverage (0-1)")
    g.add_argument("--max-rounds", type=int, default=4, help="Max --deep research rounds")
    g.add_argument("--pages-per-round", type=int, default=10, help="Max new pages read per --deep round")
    g.add_argument("--no-wikipedia", action="store_true",
                   help="Don't anchor on Wikipedia; use --seed/--url/--file sources (web search only with --search-web/--deep)")
    g.add_argument("--no-web", action="store_true", help="Skip web search; use Wikipedia/Wikiquote/--url only")
    g.add_argument("--max-pages", type=int, default=12, help="Max web pages to read (default 12)")
    g.add_argument("--search-provider", default=env("SEARCH_PROVIDER", "auto"),
                   choices=["auto", "ddg", "searxng", "brave", "tavily", "none"])
    g.add_argument("--searxng-url", default=env("SEARXNG_URL", ""))
    g.add_argument("--brave-key", default=env("BRAVE_API_KEY", ""))
    g.add_argument("--tavily-key", default=env("TAVILY_API_KEY", ""))
    g.add_argument("--yes", "-y", action="store_true", help="Never prompt; take the top Wikipedia match")
    g.add_argument("--handle", action="append", default=[],
                   help="Creator username used on platforms (repeatable). Link-hub usernames and stage-name slugs "
                        "are added automatically. Used to recognise the creator's OWN adult-platform profiles.")
    g.add_argument("--descriptor", default="", help='Short "who is this" hint, e.g. "adult content creator and streamer"')

    g = p.add_argument_group("audio (interviews, podcasts → transcripts)")
    g.add_argument("--audio", action="append", default=[],
                   help="Audio/video file or folder to transcribe and use as a source (mp3, m4a, wav, ogg, opus, mp4…)")
    g.add_argument("--audio-url", action="append", default=[], help="Direct link to an audio file (e.g. a podcast .mp3)")
    g.add_argument("--transcribe-pages", action="store_true",
                   help="Also transcribe podcast/audio players found on pages you add with --url")
    g.add_argument("--transcriber", choices=["auto", "local", "api", "off"], default=env("TRANSCRIBER", "auto"),
                   help="local = faster-whisper on this computer (free, private); api = TRANSCRIBE_BASE_URL; "
                        "auto = local if installed, else the API")
    g.add_argument("--whisper-model", default=env("WHISPER_MODEL", "small"),
                   help="Local Whisper size: tiny, base, small (default), medium, large-v3 – bigger = better, slower")
    g.add_argument("--whisper-device", choices=["auto", "cpu", "cuda"], default=env("WHISPER_DEVICE", "auto"),
                   help="Local Whisper: auto = NVIDIA GPU if it works, else CPU (automatic fallback); cpu; cuda")
    g.add_argument("--transcribe-base-url", default=env("TRANSCRIBE_BASE_URL", ""),
                   help="OpenAI-compatible transcription API, e.g. https://api.openai.com/v1 or https://api.groq.com/openai/v1")
    g.add_argument("--transcribe-api-key", default=env("TRANSCRIBE_API_KEY", ""))
    g.add_argument("--transcribe-model", default=env("TRANSCRIBE_MODEL", "whisper-1"),
                   help="API model, e.g. whisper-1, gpt-4o-transcribe, whisper-large-v3 (Groq)")
    g.add_argument("--audio-language", default=env("AUDIO_LANGUAGE", ""), help="Spoken language code, e.g. en (empty = detect)")

    g = p.add_argument_group("LLM (any OpenAI-compatible endpoint)")
    g.add_argument("--base-url", default=env("LLM_BASE_URL", "https://api.openai.com/v1"))
    g.add_argument("--api-key", default=env("LLM_API_KEY", env("OPENAI_API_KEY", "")))
    g.add_argument("--model", default=env("LLM_MODEL", "gpt-4o-mini"), help="Model for dossier, card and lorebook")
    g.add_argument("--extract-model", default=env("LLM_EXTRACT_MODEL", ""),
                   help="Cheaper/faster model for per-page fact extraction (defaults to --model)")
    g.add_argument("--max-tokens", type=int, default=int(env("LLM_MAX_TOKENS", "8192")))
    g.add_argument("--json-mode", choices=["auto", "on", "off"], default=env("LLM_JSON_MODE", "auto"))
    g.add_argument("--workers", type=int, default=4, help="Parallel LLM calls (lower this for local models)")
    g.add_argument("--chunk-chars", type=int, default=12000, help="Source chunk size sent to the extractor")

    g = p.add_argument_group("managed creator / adult cards")
    g.add_argument("--talent", action="store_true",
                   help="Managed-creator mode: no Wikipedia, public-persona only, creator-safety privacy rules")
    g.add_argument("--consent-note", default="",
                   help='Who authorised this card, e.g. "Approved by <creator> via <agency>, 2026-11-14" (required with --talent)')
    g.add_argument("--nsfw", action="store_true", help="18+ card: allow adult themes (adults only)")
    g.add_argument("--confirm-adult", action="store_true",
                   help="I am 18+: let the page reader answer simple “Are you 18?” click-through pages "
                        "(on by default with --nsfw). Real ID/face/card age verification is never bypassed.")
    g.add_argument("--browser", choices=["auto", "off", "always", "visible"], default=env("PERSONA_FORGE_BROWSER", "auto"),
                   help="Real-browser page reader (needs: pip install playwright). auto = only when the fast reader "
                        "can't get a page you chose; always = every page you chose; visible = like auto but the window "
                        "is shown so you can complete “verify you're human” checks yourself; off = fast reader only")
    g.add_argument("--no-age-click", action="store_true",
                   help="Never answer “Are you 18?” pages, even with --nsfw")
    g.add_argument("--audience-research", action="store_true",
                   help="Also write a separate audience_report.md: what fans/the public say about the creator's content "
                        "(themes, praise, requests, criticism) found under the stage name and handles. Never used in the card.")
    g.add_argument("--audience-pages", type=int, default=20, help="Max discussion pages for --audience-research")
    g.add_argument("--boundaries", help="Text file with the creator's hard limits/rules; embedded verbatim in the card")

    g = p.add_argument_group("character")
    g.add_argument("--era", default="", help='Portray them as of a time, e.g. "1969", "during the Apollo program"')
    g.add_argument("--scenario", default="", help="Premise for the scenario and greeting")
    g.add_argument("--style", choices=["roleplay", "chat"], default="roleplay",
                   help="roleplay = narrative prose greetings; chat = Marinara Conversation-mode texting style")
    g.add_argument("--pov", choices=["third", "first"], default="third", help="Narration person for roleplay style")
    g.add_argument("--greetings", type=int, default=3, help="Number of alternate greetings")
    g.add_argument("--lore-entries", type=int, default=40, help="Max lorebook entries")
    g.add_argument("--language", default="English", help="Language to write the card in")
    g.add_argument("--creator", default=env("CARD_CREATOR", "Persona Forge"))
    g.add_argument("--avatar", help="PNG (or any image with Pillow installed) to use as the card image")
    g.add_argument("--scan-depth", type=int, default=3, help="Lorebook scan depth (messages)")
    g.add_argument("--token-budget", type=int, default=2048, help="Lorebook token budget")

    g = p.add_argument_group("images (avatar, sprites, image-generation prompt)")
    g.add_argument("--sprites", help="Folder of the creator's own sprite images to embed (happy.png, sad.png…; "
                                     "a full-body/ subfolder or full_ prefix = full-body poses)")
    g.add_argument("--generate-avatar", action="store_true",
                   help="Generate a SFW illustrated portrait avatar from the text description (no photos used)")
    g.add_argument("--image-base-url", default=env("IMAGE_BASE_URL", env("LLM_BASE_URL", "https://api.openai.com/v1")))
    g.add_argument("--image-api-key", default=env("IMAGE_API_KEY", env("LLM_API_KEY", env("OPENAI_API_KEY", ""))))
    g.add_argument("--image-model", default=env("IMAGE_MODEL", "gpt-image-1"))
    g.add_argument("--avatar-style", default="digital painting, portrait, head and shoulders, soft studio lighting",
                   help="Art style for --generate-avatar")
    g.add_argument("--references", action="append", default=[],
                   help="Reference images of the character (files or a folder, repeatable). Builds sprite_kit/ for "
                        "Marinara's Generate Sprites window: up to 4 prepared references, the appearance description, "
                        "and the expressions/poses that suit the persona")
    g.add_argument("--avatar-from-reference", action="store_true",
                   help="Use the first reference image as the card avatar (so 'use current avatar as reference' works)")
    g.add_argument("--sprite-expressions", type=int, default=12, help="How many expressions the kit plans (max 16)")
    g.add_argument("--sprite-poses", type=int, default=6, help="How many full-body poses the kit plans (max 16)")
    g.add_argument("--no-image-prompt", action="store_true",
                   help="Don't set Marinara's image-generation appearance (imageAppearance)")

    g = p.add_argument_group("output")
    g.add_argument("--out", default="output", help="Output folder (default ./output)")
    g.add_argument("--spec", choices=["v2", "v3"], default="v2", help="JSON card spec (PNG always carries both)")
    g.add_argument("--merge-fields", action="store_true",
                   help="Fold Appearance/Backstory into Description (for SillyTavern; Marinara has separate fields)")
    g.add_argument("--from-dossier", help="Skip research: rebuild card + lorebook from a saved dossier.json")
    g.add_argument("--cache-dir", default=env("PERSONA_FORGE_CACHE", ".persona_forge_cache"))
    g.add_argument("--no-cache", action="store_true")
    g.add_argument("--verbose", "-v", action="store_true")

    g = p.add_argument_group("push straight into a running Marinara Engine")
    g.add_argument("--push", nargs="?", const="http://127.0.0.1:7860", default=None, metavar="URL",
                   help="Import into Marinara (default URL http://127.0.0.1:7860)")
    g.add_argument("--marinara-user", default=env("MARINARA_USER", ""), help="Basic-auth user if your server has a login")
    g.add_argument("--marinara-pass", default=env("MARINARA_PASS", ""))
    return p


# ─────────────────────────────────────────────────────────────── research
def pick_wikipedia(r: Researcher, name: str, args) -> Source:
    import requests

    try:
        return _pick_wikipedia(r, name, args)
    except ValueError as e:  # disambiguation page
        raise SystemExit(str(e)) from None
    except requests.RequestException as e:
        raise SystemExit(f"Could not reach Wikipedia ({e.__class__.__name__}). Check your connection, or use "
                         "--no-wikipedia with --seed/--url/--file.") from None


def _pick_wikipedia(r: Researcher, name: str, args) -> Source:
    if args.wiki_title:
        src = r.wikipedia_article(args.wiki_title)
        if not src:
            raise SystemExit(f'No Wikipedia article titled "{args.wiki_title}".')
        return src
    cands = r.wiki_candidates(name)
    if not cands:
        raise SystemExit(
            f'No Wikipedia article found for "{name}".\n'
            "Persona Forge is built for public, historical and fictional figures. Try the exact article title with "
            "--wiki-title, or for a fictional character with no article, pass its wiki pages with --url and "
            "--no-wikipedia."
        )
    choice = 0
    if not args.yes and sys.stdin.isatty() and len(cands) > 1:
        log("\nWikipedia matches:")
        for i, c in enumerate(cands, 1):
            log(f"  {i}. {c.title} — {c.description or c.snippet[:80]}")
        ans = input("Pick the right person [1]: ").strip()
        if ans.isdigit() and 1 <= int(ans) <= len(cands):
            choice = int(ans) - 1
    src = r.wikipedia_article(cands[choice].title)
    if not src:
        raise SystemExit("Could not load the Wikipedia article.")
    return src


class _Audio:
    """Transcribes audio for one run; started only when there is audio to do."""

    def __init__(self, args):
        self.args = args
        self.tr = None
        self.failed = False
        self.transcripts: list[tuple[str, str]] = []  # (file name, transcript) – saved to output/transcripts/

    def _get(self):
        if self.tr is None and not self.failed:
            from .audio import TranscribeConfig, Transcriber, TranscriptionUnavailable

            a = self.args
            try:
                self.tr = Transcriber(TranscribeConfig(a.transcriber, a.whisper_model, a.transcribe_base_url,
                                                       a.transcribe_api_key, a.transcribe_model, a.audio_language,
                                                       a.whisper_device),
                                      cache_dir=None if a.no_cache else Path(a.cache_dir))
                if self.tr.engine == "off":
                    self.failed, self.tr = True, None
                    log("  ! audio left out: transcription is switched off (--transcriber off)")
            except TranscriptionUnavailable as e:
                self.failed = True
                log(f"  ! audio left out: {e}")
        return self.tr

    def source(self, path: Path, person: str, origin: str = "") -> Source | None:
        from .audio import transcript_source_text

        tr = self._get()
        if not tr:
            return None
        try:
            text = tr.transcribe(path)
        except Exception as e:  # noqa: BLE001
            log(f"  ! could not transcribe {path.name}: {e}")
            return None
        if not text.strip():
            log(f"  ! {path.name}: no speech found")
            return None
        self.transcripts.append((path.stem, text))
        return Source(url=origin or f"file://{path.resolve()}", title=f"Transcript: {path.name}",
                      text=transcript_source_text(path.name, text, person, origin), kind="user")


def gather_sources(r: Researcher, args) -> tuple[list[Source], str, str]:
    sources: list[Source] = []
    name = args.name
    descriptor = ""
    audio = args._audio = _Audio(args)
    from .audio import AUDIO_EXTS, download_audio, find_page_audio

    audio_files: list[Path] = []
    for a in args.audio:
        p = Path(a)
        if p.is_dir():
            audio_files += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
        elif p.is_file():
            audio_files.append(p)
        else:
            log(f"  ! audio {a}: not found")
    for f in audio_files:
        if src := audio.source(f, name):
            sources.append(src)
    for u in args.audio_url:
        if f := download_audio(u, Path(args.cache_dir) / "audio", r.http):
            if src := audio.source(f, name, origin=u):
                sources.append(src)

    for f in args.file:
        src = load_local_file(f)
        log(f"→ Creator source: {src.title} ({len(src.text):,} chars)")
        sources.append(src)

    r.handles = list(args.handles) + [name.replace(" ", "").lower()]
    for seed in args.seed:
        sources.extend(r.crawl_link_hub(seed, max_links=args.seed_max_links, depth=args.seed_depth,
                                        site_pages=args.site_pages))
    if r.discovered_handles:  # usernames found in the creator's own links (OnlyFans, Fansly, socials…)
        new = [h for h in r.discovered_handles if h not in args.handles]
        if new:
            log(f"→ Handles found on the link hub: {', '.join(new)}")
            args.handles.extend(new)
            r.handles.extend(new)

    if not args.no_wikipedia:
        wiki = pick_wikipedia(r, name, args)
        log(f"→ Wikipedia: {wiki.title} — {wiki.meta.get('description', '')}")
        name = wiki.title.split(" (")[0]  # "Mercury (planet)" style suffixes off
        descriptor = wiki.meta.get("description", "")
        sources.append(wiki)
        if q := r.wikiquote(wiki.title):
            log("→ Wikiquote page found")
            sources.append(q)
    elif not (args.url or args.seed or args.file or args.audio or args.audio_url
              or (args.search_web and not args.no_web)):
        raise SystemExit("--talent / --no-wikipedia needs at least one --seed, --url or --file "
                         "(or --search-web / --deep to search the name).")

    if args.url:
        user_pages = r.fetch_many([(u, "") for u in args.url], kind="user",
                                  keep_html=args.site_pages > 0 or args.transcribe_pages)
        log(f"→ Loaded {len(user_pages)}/{len(args.url)} pages you supplied"
            + ("" if len(user_pages) == len(args.url) else " – see the ! lines above for why, and how to add the rest"))
        sources.extend(user_pages)
        if args.transcribe_pages:  # podcast players and audio files on the pages you added
            for page in user_pages:
                for audio_url in find_page_audio(page.meta.get("html", ""), page.url):
                    f = download_audio(audio_url, Path(args.cache_dir) / "audio", r.http)
                    if f and (src := audio.source(f, name, origin=page.url)):
                        sources.append(src)
                        break
        sources.extend(r.follow_site_pages(user_pages, per_site=args.site_pages))
        for p in user_pages:
            p.meta.pop("html", None)
    # Creator's own adult-platform profiles: only their own pages are read, so this runs whenever web access is
    # allowed – no --search-web needed.
    if args.talent and args.nsfw and not args.no_web:
        plat = r.adult_platform_sweep(name, args.handles, {s.url.split("#")[0].rstrip("/") for s in sources},
                                      max_pages=args.max_pages)
        sources.extend(plat)
    # the same page can arrive via a hub, --url and the sweep; keep one copy
    uniq: dict[str, Source] = {}
    for s in sources:
        uniq.setdefault(s.url.split("#")[0].rstrip("/"), s)
    sources = list(uniq.values())

    if not args.no_web and (not args.no_wikipedia or args.search_web) and args.max_pages > 0:
        if args.no_wikipedia:  # creator mode: stage-name searches for interviews/podcasts/profiles
            descriptor = args.descriptor or ""
        hint = f" {descriptor}" if descriptor else ""
        short_hint = " ".join(descriptor.split()[:4]) if descriptor else ""
        queries = ([q.format(name=name) for q in ADULT_PRESS_QUERIES] if args.nsfw else []) + [
            f'"{name}" interview',
            f'"{name}" podcast',
            f'"{name}"{hint} creator',
        ] if args.talent else [
            f'"{name}"{hint}',
            f'"{name}" interview {short_hint}',
            f'"{name}" personality OR character OR temperament',
            f'"{name}" biography early life',
            f'"{name}" quotes OR said',
        ]
        seen = {s.url.split("#")[0].rstrip("/") for s in sources}
        per_domain: dict[str, int] = {}
        picked: list[tuple[str, str]] = []
        for q in queries:
            for url, title in r.search(q.strip(), max_results=8):
                url = url.split("#")[0].rstrip("/")
                dom = urlparse(url).netloc.lower().removeprefix("www.")
                if url in seen or dom.endswith("wikipedia.org") or dom.endswith("wikiquote.org"):
                    continue
                if args.talent and args.nsfw and r.is_creator_profile(url, args.handles):
                    seen.add(url)
                    sources.extend(r.fetch_many([(url, title)], kind="platform"))
                    continue
                if not r.allowed_url(url):
                    continue
                if per_domain.get(dom, 0) >= 2:
                    continue
                seen.add(url)
                per_domain[dom] = per_domain.get(dom, 0) + 1
                picked.append((url, title))
        picked = picked[: int(args.max_pages * 1.5)]  # over-fetch; some pages fail or are irrelevant
        log(f"→ Reading {len(picked)} web pages…")
        pages = r.fetch_many(picked)
        relevant = [p for p in pages if mentions_person(p.text, name, args.handles)][: args.max_pages]
        log(f"  {len(relevant)} relevant pages")
        sources.extend(relevant)
    return sources, name, descriptor


# ─────────────────────────────────────────────────────────────── review report
def _coverage_section(opts: Options, gap_report) -> list[str]:
    if not gap_report:
        return []
    cov, rounds = gap_report
    lines = ["## Profile coverage", "", f"Overall: **{cov.score:.0%}**", "", "| Section | Status |", "| --- | --- |"]
    for label, ok, note in cov.table:
        status = "filled" if ok else "missing"
        lines.append(f"| {label} | {status}{f' ({note})' if note else ''} |")
    if rounds:
        lines += ["", "### Research rounds", "", "| Round | Pages | New facts | Coverage after | Searches |",
                  "| ---: | ---: | ---: | ---: | --- |"]
        for rd in rounds:
            lines.append(f"| {rd['round']} | {rd['pages']} | {rd['new_facts']} | {rd['coverage']:.0%} | "
                         + "; ".join(rd["queries"]) + " |")
    if opts.nsfw:
        lines += ["", "Sexual preferences and dynamics are taken only from the creator's own channels (link hub, "
                  "their own sites, pages you add with `--url`, briefs). They are never searched for elsewhere and "
                  "never inferred."]
        lines.append("Persona kinks are extrapolated from recurring themes in the creator's own posted content "
                     "(profiles and storefronts on adult platforms, their link hub and sites) and marked "
                     "'(from posted content)'. They describe the persona's content themes, not the person's private life.")
        if opts.dropped_third_party_intimacy:
            lines.append(f"{opts.dropped_third_party_intimacy} such claims from third-party sites were discarded.")
        if opts.dropped_excluded_themes:
            lines.append(f"{opts.dropped_excluded_themes} minor-coded or otherwise excluded themes were removed.")
        if cov.own_channel_gaps:
            lines.append("Not found on the creator's own channels: " + ", ".join(cov.own_channel_gaps)
                         + ". Add the creator's own page that covers it with `--url`, or a brief with `--file`.")
    return lines + [""]


def write_review(opts: Options, facts, sources: list[Source], used: list[Source], skipped, gap_report=None) -> str:
    """Everything the app read, used and skipped, plus every fact it extracted, grouped for human review."""
    from collections import defaultdict

    used_urls = {s.url for s in used}
    by_src = defaultdict(int)
    for f in facts:
        by_src[f.source] += 1
    lines = [f"# Research review: {opts.name}", "",
             f"{len(facts)} facts from {len(used)} of {len(sources)} sources read. "
             "Edit `dossier.json` and rebuild with `--from-dossier` to change what the card uses.", ""]
    if opts.talent:
        lines += ["> Creator-safety mode was on: legal names, locations, schedules, contact details and private "
                  "people found on the web were not recorded. Anything in a `--file` creator brief is used as written.",
                  ""]
    lines += _coverage_section(opts, gap_report)
    lines += ["## Sources read", "", "| Facts | Type | Source |", "| ---: | --- | --- |"]
    for s in sorted(sources, key=lambda s: -by_src.get(s.url, 0)):
        kind = {"brief": "creator brief", "platform": "adult-platform profile (own)", "hub": "link hub",
                "user": "creator's page / your URL"}.get(s.kind, s.kind)
        mark = by_src.get(s.url, 0) if s.url in used_urls else "0 (not about them)"
        lines.append(f"| {mark} | {kind} | [{(s.title or s.url)[:70]}]({s.url}) |")
    if skipped:
        lines += ["", "## Skipped", "", "| Reason | URL |", "| --- | --- |"]
        seen = set()
        for url, reason in skipped:
            if url not in seen:
                seen.add(url)
                lines.append(f"| {reason} | {url} |")
    groups = defaultdict(list)
    for f in facts:
        groups[f.category].append(f)
    lines += ["", "## Facts by category"]
    for cat in sorted(groups, key=lambda c: -len(groups[c])):
        lines += ["", f"### {cat.replace('_', ' ').title()} ({len(groups[cat])})", ""]
        for f in groups[cat]:
            conf = "" if f.confidence == "high" else f" _({f.confidence})_"
            when = f" ({f.when})" if f.when else ""
            subj = f"**{f.subject}**: " if f.subject and f.subject.lower() != opts.name.lower() else ""
            lines.append(f"- {subj}{f.fact}{when}{conf} [source]({f.source})")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────── run settings
# Saved next to dossier.json so --from-dossier rebuilds with the same mode, voice and safety settings
# without retyping them. Anything given explicitly on the command line wins.
RUN_SETTINGS = ["talent", "nsfw", "consent_note", "descriptor", "era", "scenario", "style", "pov", "greetings",
                "lore_entries", "language", "creator", "handle", "merge_fields", "spec", "scan_depth", "token_budget"]


def save_run_settings(out: Path, args, boundaries: str) -> None:
    data = {k: getattr(args, k) for k in RUN_SETTINGS}
    data["boundaries_text"] = boundaries
    (out / "run_settings.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def apply_saved_settings(args, parser: argparse.ArgumentParser) -> str:
    """Fill unset options from run_settings.json beside the dossier. Returns saved boundaries text."""
    f = Path(args.from_dossier).with_name("run_settings.json")
    if not f.exists():
        return ""
    saved = json.loads(f.read_text("utf-8"))
    applied = []
    for k in RUN_SETTINGS:
        if k in saved and getattr(args, k) == parser.get_default(k) and saved[k] != parser.get_default(k):
            setattr(args, k, saved[k])
            applied.append(k)
    if applied:
        log(f"→ Reusing settings from the original run: {', '.join(applied)}")
    return saved.get("boundaries_text", "")


def card_sources(urls: list[str], talent: bool) -> list[str]:
    """Sources written INTO the card. Never local file paths; for creator cards (often sold) none at all –
    the full list stays in sources.md / review.md."""
    if talent:
        return []
    return [u for u in urls if u.startswith(("http://", "https://"))]


def save_research(out: Path, slug: str, opts, dossier: dict, facts, source_urls: list[str],
                  transcripts: list[tuple[str, str]] | None = None) -> None:
    if transcripts:
        tdir = out / "transcripts"
        tdir.mkdir(exist_ok=True)
        for name, text in transcripts:
            (tdir / f"{slugify(name)[:60]}.txt").write_text(text + "\n", "utf-8")
    (out / "dossier.json").write_text(json.dumps(dossier, ensure_ascii=False, indent=2), "utf-8")
    (out / "sources.json").write_text(json.dumps(source_urls, indent=2), "utf-8")
    if facts:
        with (out / "facts.jsonl").open("w", encoding="utf-8") as fh:
            for f in facts:
                fh.write(json.dumps(f.__dict__, ensure_ascii=False) + "\n")
    (out / "sources.md").write_text(
        f"# Sources for {opts.name}\n\n" + "\n".join(f"- {u}" for u in source_urls) + "\n", "utf-8"
    )


# ─────────────────────────────────────────────────────────────── main
def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    set_verbose(args.verbose)
    if not args.name and not args.from_dossier:
        parser.print_help()
        return 2
    saved_boundaries = apply_saved_settings(args, parser) if args.from_dossier else ""
    if not args.api_key and "localhost" not in args.base_url and "127.0.0.1" not in args.base_url:
        log("! No API key set (LLM_API_KEY / --api-key). Local servers usually don't need one.")

    if args.urls_file:
        args.url += [ln.strip() for ln in Path(args.urls_file).read_text("utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
    args.handles = creator_handles(args.name or "", args.seed, args.handle)
    if args.deep:
        args.search_web = True
    if args.audience_research and not args.talent:
        raise SystemExit("--audience-research is for managed creators: use it with --talent and --consent-note.")
    if args.audience_research and args.no_web:
        raise SystemExit("--audience-research needs web search; remove --no-web.")
    if args.talent:
        args.no_wikipedia = True
        if not args.consent_note.strip():
            raise SystemExit('--talent needs --consent-note, e.g. --consent-note "Authorised by <creator> (signed release on file), 2026-11-14"')
    boundaries = Path(args.boundaries).read_text("utf-8") if args.boundaries else saved_boundaries
    if args.nsfw and args.talent and not boundaries:
        log("! Tip: pass the creator's hard limits with --boundaries limits.txt so the card enforces them.")

    cache = None if args.no_cache else DiskCache(Path(args.cache_dir))
    main_llm = LLMClient(
        LLMConfig(args.base_url, args.api_key, args.model, max_tokens=args.max_tokens, json_mode=args.json_mode), cache
    )
    extract_llm = (
        LLMClient(LLMConfig(args.base_url, args.api_key, args.extract_model, max_tokens=min(4096, args.max_tokens),
                            json_mode=args.json_mode), cache, fallback_model=args.model)
        if args.extract_model and args.extract_model != args.model
        else main_llm
    )
    log(f"→ AI: {args.base_url}  ·  model {args.model}"
        + (f"  ·  page-reading model {args.extract_model}" if extract_llm is not main_llm else "")
        + f"  ·  API key {'set' if args.api_key else 'NOT set'}  ·  {args.workers} parallel")

    opts = Options(
        name=args.name or "",
        era=args.era,
        scenario_hint=args.scenario,
        style=args.style,
        pov=args.pov,
        greetings=args.greetings,
        lore_max_entries=args.lore_entries,
        creator=args.creator,
        language=args.language,
        workers=args.workers,
        chunk_chars=args.chunk_chars,
        nsfw=args.nsfw,
        talent=args.talent,
        boundaries=boundaries,
        descriptor=args.descriptor,
    )

    source_urls: list[str] = []
    review_inputs = None
    gap_report = None
    audience_result = None
    if args.from_dossier:
        dossier = json.loads(Path(args.from_dossier).read_text("utf-8"))
        opts.name = args.name or dossier.get("name") or "Unknown"
        opts.descriptor = dossier.get("descriptor", "") or opts.descriptor
        src_file = Path(args.from_dossier).with_name("sources.json")
        if src_file.exists():
            source_urls = json.loads(src_file.read_text("utf-8"))
        facts = []
    else:
        r = Researcher(
            cache=cache,
            lang=args.lang,
            search_provider="none" if args.no_web else args.search_provider,
            searxng_url=args.searxng_url,
            brave_key=args.brave_key,
            tavily_key=args.tavily_key,
            confirm_adult=(args.nsfw or args.confirm_adult) and not args.no_age_click,
            browser_mode=args.browser,
        )
        sources, opts.name, desc = gather_sources(r, args)
        opts.descriptor = desc or opts.descriptor
        if not sources:
            raise SystemExit("No usable sources found.")
        # Use the cheap model for the map step, then the main model for everything else.
        from .synthesize import build_dossier, extract_facts

        facts, aliases, used = extract_facts(extract_llm, sources, opts)
        if len(facts) < 8 and not args.deep:
            raise SystemExit(f"Only {len(facts)} facts found about {opts.name}. Add pages with --url, or use --deep.")
        dossier = build_dossier(main_llm, facts, aliases, opts) if facts else {"name": opts.name}
        if args.deep:
            if args.no_web:
                log("! --deep needs web search; ignoring it because --no-web is set.")
            else:
                gf = gap_fill(
                    r, extract_llm, main_llm, opts, dossier=dossier, facts=facts, aliases=aliases, used=used,
                    sources=sources, target=args.target_coverage, max_rounds=args.max_rounds,
                    pages_per_round=args.pages_per_round, hint=args.descriptor if args.talent else "",
                    handles=args.handles,
                )
                dossier, facts, aliases, used, sources = gf.dossier, gf.facts, gf.aliases, gf.used, gf.sources
                gap_report = (gf.coverage, gf.rounds)
        if gap_report is None:
            gap_report = (coverage(dossier, opts), [])
        if summary := r.search_summary():
            log(summary)
        review_inputs = (sources, used, r.skipped)
        source_urls = [s.url for s in used]
        out_early = Path(args.out) / slugify(opts.name)
        out_early.mkdir(parents=True, exist_ok=True)
        save_research(out_early, slugify(opts.name), opts, dossier, facts, source_urls,
                      getattr(getattr(args, "_audio", None), "transcripts", None))
        save_run_settings(out_early, args, boundaries)
        (out_early / "review.md").write_text(write_review(opts, facts, *review_inputs, gap_report=gap_report), "utf-8")
        log(f"→ Research saved to {out_early}/ (dossier.json, review.md)")
        if len(facts) < 8:
            raise SystemExit(f"Only {len(facts)} facts found about {opts.name} – see {out_early}/review.md for what was "
                             "read and skipped. Add pages with --url, a brief with --file, or try --deep.")
        if args.audience_research:
            # Separate report only – nothing from it reaches the dossier, card or lorebook.
            audience_result = audience_research(
                r, extract_llm, main_llm, opts, args.handles, max_pages=args.audience_pages,
                seen_urls={u for u, _ in r.skipped},
            )
    try:
        card = write_card(main_llm, dossier, opts)
        lore = write_lorebook(main_llm, dossier, opts)
    except Exception as e:  # noqa: BLE001 – keep the research; tell the user how to resume
        hint = Path(args.out) / slugify(opts.name) / "dossier.json"
        log(f"\n! Writing the card failed: {e}")
        log(f"  Your research is saved. Fix the problem (e.g. another --model) and rebuild without re-researching:\n"
            f"    python -m persona_forge --from-dossier {hint}")
        return 1

    # ── outputs
    slug = slugify(opts.name)
    out = Path(args.out) / slug
    out.mkdir(parents=True, exist_ok=True)

    image_appearance = "" if args.no_image_prompt else clean_image_appearance(
        str(card.get("image_appearance", "")), fallback=str(card.get("appearance", ""))[:600])
    sprites = load_sprites(args.sprites) if args.sprites else None

    card_v2 = build_card(
        opts.name, card, dossier, lore, creator=args.creator, sources=card_sources(source_urls, args.talent),
        scan_depth=args.scan_depth, token_budget=args.token_budget, merge_fields=args.merge_fields,
        boundaries=boundaries, consent_note=args.consent_note, nsfw=args.nsfw,
        image_appearance=image_appearance, sprites=sprites,
    )
    card_v3 = to_v3(card_v2, card_sources(source_urls, args.talent))
    lorebook = build_lorebook(
        opts.name, lore, scan_depth=args.scan_depth, token_budget=args.token_budget,
        description=f"Facts about the people, places, events and works in {opts.name}'s life.",
    )

    avatar = None
    references = collect_references(args.references) if args.references else []
    if args.avatar:
        avatar = load_avatar(args.avatar)
    elif args.avatar_from_reference and references:
        avatar = load_avatar(str(references[0]))
    elif args.generate_avatar and image_appearance:
        log("→ Generating avatar from the text description…")
        try:
            avatar = generate_avatar(image_appearance, base_url=args.image_base_url, api_key=args.image_api_key,
                                     model=args.image_model, style=args.avatar_style)
            (out / "avatar_generated.png").write_bytes(avatar)
        except Exception as e:  # noqa: BLE001
            log(f"! Avatar generation failed ({e}); using the placeholder.")
    avatar = avatar or placeholder_avatar(opts.name)
    if references:
        if not image_appearance:
            log("! The sprite kit needs an appearance description; it is skipped because --no-image-prompt is set.")
        else:
            log(f"→ Building sprite kit from {len(references)} reference image(s)…")
            plan = pick_sprite_set(main_llm, dossier, opts.name, min(16, max(1, args.sprite_expressions)),
                                   min(16, max(1, args.sprite_poses)))
            kit = build_sprite_kit(out, opts.name, image_appearance, references, plan,
                                   avatar_is_reference=bool(args.avatar_from_reference and not args.avatar))
            log(f"   {kit.name}/SPRITE_KIT.md  ← step-by-step for Marinara's Generate Sprites window")
    (out / f"{slug}.png").write_bytes(embed_card_in_png(avatar, card_v2, card_v3))
    (out / f"{slug}.card.json").write_text(
        json.dumps(card_v3 if args.spec == "v3" else card_v2, ensure_ascii=False, indent=2), "utf-8"
    )
    (out / f"{slug}.lorebook.json").write_text(json.dumps(lorebook, ensure_ascii=False, indent=2), "utf-8")
    if not args.from_dossier:
        save_research(out, slug, opts, dossier, facts, source_urls,
                      getattr(getattr(args, "_audio", None), "transcripts", None))
    save_run_settings(out, args, boundaries)
    if review_inputs:
        review = write_review(opts, facts, *review_inputs, gap_report=gap_report)
        if opts.nsfw and (hits := flag_excluded(card, lore)):
            review += ("\n## Check before using\n\nThese passages mention words on the excluded-themes list. Most will "
                       "be harmless (e.g. 'kids' in a non-sexual context), but read each one:\n\n"
                       + "\n".join(f"- {h}" for h in hits) + "\n")
            log(f"! {len(hits)} passage(s) flagged for review – see review.md")
        (out / "review.md").write_text(review, "utf-8")
    if audience_result:
        save_audience(out, opts.name, *audience_result)
        log(f"   audience_report.md      ← what fans say about the content ({len(audience_result[1])} insights; not in the card)")

    log(f"\n✓ {opts.name}: card + {len(lore)} lorebook entries written to {out}/")
    log(f"   {slug}.png             ← import this in Marinara (Characters → Import); choose 'Import Lorebook'")
    log(f"   {slug}.card.json       ← same card as JSON")
    log(f"   {slug}.lorebook.json   ← standalone lorebook (Lorebooks → Import), if you'd rather link it yourself")
    log(f"   LLM calls: {main_llm.calls + (extract_llm.calls if extract_llm is not main_llm else 0)}, "
        f"tokens in/out: {main_llm.usage['prompt_tokens'] + (extract_llm.usage['prompt_tokens'] if extract_llm is not main_llm else 0)}"
        f"/{main_llm.usage['completion_tokens'] + (extract_llm.usage['completion_tokens'] if extract_llm is not main_llm else 0)}")

    if args.push:
        log(f"→ Importing into Marinara at {args.push}…")
        try:
            res = push_to_marinara(args.push, card_v2, avatar, user=args.marinara_user, password=args.marinara_pass)
            log(f"✓ Imported into Marinara: {json.dumps(res)[:300]}")
        except Exception as e:  # noqa: BLE001
            log(f"! Push failed: {e}\n  The files above are still ready to import by hand.")
            return 1
    return 0


# keep the one-call helper importable for scripts
__all__ = ["main", "synthesize"]
