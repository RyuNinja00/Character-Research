# Persona Forge for Marinara Engine

Give it a name and Persona Forge looks the person up on the web, gathers what it finds and writes a
**character card plus a matching lorebook** that you can import straight into
[Marinara Engine](https://github.com/Pasta-Devs/Marinara-Engine). It works best for public figures,
historical figures and well-documented fictional characters.

**New here? Read the step-by-step [MANUAL.md](MANUAL.md).**

**Prefer a form?** Run `python -m persona_forge.webui` for a local web UI with a box and an example for every option. It creates a `talent/<name>/` folder for each creator and keeps everything for that creator in it.

```
python -m persona_forge "Ada Lovelace" --era 1843 --push
```

## How it works

1. **Research.** It starts from the person's Wikipedia article (it asks you to pick when the name is ambiguous)
   and their Wikiquote page, which helps it get their voice right. It then runs about five web searches
   (biography, interviews, personality, quotes) and reads up to 12 pages. It skips social networks,
   people-search sites, PDFs and pages that barely mention the person.
2. **Extract (map).** An LLM reads each page in chunks and pulls out small, sourced facts in 15 categories:
   appearance, personality, speech, relationships, places, events, works, beliefs and so on. It is told not to
   record private details such as addresses, phone numbers or information about private relatives.
3. **Conglomerate (reduce).** The facts are merged and de-duplicated into a structured `dossier.json`
   covering persona, history and world.
4. **Write.** The card holds the core identity, personality, voice, greetings and example dialogue.
   The lorebook gets one entry per person, place, organization, event or work, plus a few entries on life periods,
   beliefs and sayings, each with specific trigger keys.
5. **Export, and optionally push.** The files are built in Marinara's import format (details below). With
   `--push`, it imports them into your running Marinara over its local API.

## Install

Python 3.9 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env      # then add your LLM endpoint/key
```

Any **OpenAI-compatible** endpoint works: OpenAI, OpenRouter, Anthropic's compatible endpoint, DeepSeek,
or a local model through Ollama, LM Studio, KoboldCpp or the llama.cpp server. For local models, set
`LLM_JSON_MODE=off` if the server rejects `response_format`, and use `--workers 1`. Models of 14B or larger
give much better cards. Newer OpenAI models that only accept `max_completion_tokens` or the default
temperature are detected and handled automatically.

**For `--nsfw` cards, pick a model that allows adult content.** Mainstream hosted models (OpenAI, Anthropic, Google)
often refuse adult extraction or writing. A model that permits it, through OpenRouter or a local model, avoids
that. If a model refuses, the run stops with a clear message, keeps all the research, and prints the
`--from-dossier` command to rebuild with another `--model`. Nothing is re-scraped, and refusals are never cached.

Requires Python 3.9 or newer. `.env` is read from the folder you run in, or from the project folder.

## Usage

```bash
# Basic
python -m persona_forge "Marie Curie"

# Portray them at a specific time, with your own premise, and import into Marinara
python -m persona_forge "Neil Armstrong" --era "July 1969" \
  --scenario "{{user}} is a journalist granted 20 minutes with him in quarantine after Apollo 11" --push

# Conversation-mode (texting style) card
python -m persona_forge "Richard Feynman" --style chat

# Fictional character: add its fan-wiki page as an extra source
python -m persona_forge "Sherlock Holmes" --url https://bakerstreet.fandom.com/wiki/Sherlock_Holmes

# Fictional character with no Wikipedia article: use only the pages you give it
python -m persona_forge "Some Character" --no-wikipedia --url https://x.fandom.com/wiki/Some_Character

# Save money: cheap model for page extraction, strong model for writing
python -m persona_forge "Frida Kahlo" --extract-model gpt-4o-mini --model gpt-4o

# Didn't like the writing? Re-roll the card + lorebook from the saved research (no re-scraping)
python -m persona_forge --from-dossier output/Frida_Kahlo/dossier.json --era 1938
```

### Managed creators (`--talent`), including 18+ cards

This mode is for building an official AI persona of a creator you manage, with their consent:

```bash
python -m persona_forge "Ivy Wren" --talent --nsfw \
  --seed https://linktr.ee/iivywren \
  --file creator_brief.md --boundaries limits.txt \
  --url https://some-interview.example.com/ivy --url https://podcast.example.com/ep42 \
  --consent-note "Authorised by Ivy Wren (release on file), 2026-11-14" \
  --descriptor "adult content creator" --search-web
```

| Option | What it does |
| --- | --- |
| `--talent` | Skips Wikipedia and applies the creator-safety rules below. Needs `--consent-note`. |
| `--seed URL` | Reads a link-in-bio page (Linktree, Beacons, AllMyLinks, Carrd…) **and every page it links to**. It skips app-store, payment, wishlist and leak links. `--seed-depth 2` also follows hubs linked from hubs. |
| `--url URL` / `--urls-file list.txt` | Adds any page you choose, one by one or from a file with one URL per line. Social profiles are allowed here. |
| `--site-pages N` | On the creator's own websites (reached through the link hub or `--url`), also reads up to N sub-pages per site, picking about, bio, FAQ, interview and blog pages first. Default 5; 0 turns it off. |
| `--file brief.md` | Adds a local text, Markdown, JSON or HTML source, such as the creator's filled-in brief (`templates/creator_brief_template.md`), interview transcripts or approved DMs. It counts as authoritative. |
| `--boundaries limits.txt` | The creator's hard limits, appended word for word to the card's system prompt (`templates/boundaries_template.txt`). |
| `--nsfw` | Allows adult themes. The card stays suggestive rather than graphic, and the chat sets the intensity. Everyone is an adult. |
| `--search-web` | Also searches the stage name for interviews and podcasts. Off by default in talent mode. |
| `--descriptor` | A short hint about who this is, used in searches and prompts. |

#### Deep research (`--deep`)

`--deep` keeps researching until the profile is complete. No creator brief is needed:

1. After each round, it scores how filled-in the profile is: appearance, personality, voice, quotes, motivations,
   insecurities, contradictions, emotional patterns, reactions, relationship style, values, tastes, backstory and
   work.
2. It turns whatever is missing into targeted searches (interviews, podcasts, Q&As, "opens up", "why I started",
   "day in the life"…), reads the new pages, merges the facts and rebuilds the profile.
3. It stops at `--target-coverage` (default 85%), after `--max-rounds` (default 4), or when a round finds
   nothing new. `--pages-per-round` (default 10) caps how much each round reads.

```bash
python -m persona_forge "Ivy Wren" --talent --nsfw --deep \
  --seed https://linktr.ee/iivywren --descriptor "adult content creator" \
  --consent-note "Authorised by Ivy Wren (release on file), 2026-11-14"
```

#### Adult platforms and content themes (`--talent --nsfw`)

- **The creator's own adult-platform pages come first.** The tool searches OnlyFans, Fansly, ManyVids,
  Clips4Sale, IWantClips, LoyalFans, FanCentro, JustFor.Fans, Fanvue, verified Pornhub model pages and the main
  cam sites for the creator's **own** profile or storefront. A page counts as theirs only if its URL contains
  one of their handles. Handles come from the link-hub username, the usernames in the hub's own platform links
  (e.g. its OnlyFans link), the stage name, and anything you add with `--handle`. This search runs on every
  `--talent --nsfw` run (no `--search-web` needed), with a handful of batched searches so the free search
  engine isn't rate-limited. With `--search-web`/`--deep`, adult-industry press (XBIZ, AVN) interviews are
  searched first.
- **It skips pages the creator doesn't control:** tube-site video pages (often reuploads by others),
  aggregators, other creators' pages, and leak or repost sites.
- **Kinks are worked out from posted content.** Every recurring theme, niche, tag, series or roleplay scenario
  in the creator's own listings becomes a content theme, ranked signature, frequent or occasional with evidence
  (e.g. "18 of 40 clips"). The persona's kinks are then derived from the signature and frequent themes. The card
  makes those what the character leans into and brings up, and treats occasional themes as things they're open to.
  Each derived kink is marked "(from posted content)", and anything the creator states directly is marked "(stated)".
- **Always excluded:** minor-coded themes (teen, school, age-play and similar), bestiality, and sex with
  someone unconscious or incapacitated. These are dropped even when they appear in the creator's content.
  Step-family themes are kept as step-relation roleplay between adults. `review.md` counts what was removed and
  flags any passage in the finished card that still contains a word on the list, so a person checks it.
- **Third-party sources don't count.** Claims about the creator's sexual preferences from anywhere else
  (forums, gossip, fan sites) are discarded and never inferred from personality or appearance.

#### Images: avatar, sprites and image generation

Marinara draws the character with its own image tools: the Illustrator agent, Conversation selfies and AI sprite
generation. The card sets these up:

- **Image prompt:** every card gets `imageAppearance` (with `imageAppearanceEnabled` on). That's a compact
  description of the character's look (hair, eyes, build, tattoos, signature outfits), and Marinara uses it in
  every image prompt, so the character looks the same across avatar, sprites, selfies and scenes. It always starts
  as an adult and never uses youthful descriptors. Turn it off with `--no-image-prompt`.
- **`--sprites folder/`:** embeds the creator's own expression images (`happy.png`, `flustered.png`…). Put
  full-body poses in a `full-body/` subfolder or name them `full_…`. Marinara restores them on import, and in
  Roleplay chats the Expression Engine picks the right one. Up to 256 images, 8 MB each.
- **`--avatar photo.png`:** uses one of the creator's own photos or artwork as the card image.
- **`--generate-avatar`:** creates an illustrated, clothed portrait from the text description alone, through any
  OpenAI-compatible image endpoint (`IMAGE_BASE_URL`, `IMAGE_API_KEY`, `IMAGE_MODEL`, default `gpt-image-1`).
  Set the look with `--avatar-style`, e.g. `"anime portrait"` or `"photorealistic headshot"`. No photos are used
  as reference.

- **`--references photos/`:** builds a `sprite_kit/` folder for Marinara's **Generate Sprite** window: up to
  four reference images, scaled and converted to PNG; `appearance_description.txt` ready to paste; and
  `SPRITE_KIT.md`, a click-by-click guide with the expressions and full-body poses that suit this persona,
  chosen from Marinara's own lists, each with a note on how the character looks doing it.
  `--avatar-from-reference` makes the first reference the card avatar, so the **use current avatar as
  reference** box works too. Change the plan size with `--sprite-expressions` and `--sprite-poses`. Generation
  itself runs in Marinara, on your image connection.

Buyers can generate more sprites inside Marinara with its own image model (**Sprites → Add sprites →
Generate**). The card's image prompt keeps them on-model.

#### Audience report (`--audience-research`)

This writes a separate `audience_report.md`, built from public fan discussion of the stage name and handles:
themes fans associate with the content, praise, requests, criticisms and comparisons. It's market research for
the creator and is **never** used in the card.

Talent mode portrays the **public persona** only. Unless the creator brief supplies them, it leaves out
the legal name, location, schedule, contact or payment details, and any private people in the creator's life.
It also tells the character not to deny being an AI when sincerely asked, and never to arrange real-life
meetings. You can change that rule in the boundaries file.

**Social media:** Instagram, X, TikTok, OnlyFans and Fansly mostly hide content behind logins, so the tool
usually only gets the profile bio from those pages. When a page you supply has almost no readable text,
the tool says so. Paste the text you want (post captions, a stream transcript, an FAQ) into a file and add
it with `--file`. A filled-in creator brief does more for depth than anything scraped.

Run `python -m persona_forge --help` for every option. The most useful ones:

| Option | What it does |
| --- | --- |
| `--era` | Time to portray them at. Later events stay out of the card. |
| `--scenario` | Your premise for the scenario and the greetings. |
| `--style roleplay\|chat` | Prose greetings for Roleplay chats, or texting style for Conversation chats. |
| `--greetings N` / `--lore-entries N` | How many alternate greetings / the most lorebook entries to keep. |
| `--avatar file.png` | Your own card image. Without it you get a generated initials placeholder. |
| `--url URL` | Extra source page, repeatable. |
| `--wiki-title` | Exact Wikipedia article, which skips the picker. Use `-y` to always take the top match. |
| `--search-provider` | `ddg` (default, no key), `tavily`, `brave`, `searxng` or `none`. |
| `--push [URL]` | Import into Marinara. The default URL is `http://127.0.0.1:7860`. |
| `--merge-fields` | Folds Appearance and Backstory into Description, for SillyTavern. Leave it off for Marinara. |

Page downloads and LLM replies are cached in `.persona_forge_cache/` for a week, so a re-run is cheap.
Turn this off with `--no-cache`.

## Output

Files go in `output/<Name>/`:

| File | Use |
| --- | --- |
| `<Name>.png` | The card image with the data inside. **Import this.** |
| `<Name>.card.json` | The same card as JSON, V2 by default (`--spec v3` for V3). |
| `<Name>.lorebook.json` | A standalone lorebook, in case you'd rather import and link it yourself. |
| `review.md` | **Start here.** Every page read and how many facts it gave, every link skipped and why, and every extracted fact grouped by category with its source. |
| `dossier.json`, `facts.jsonl`, `sources.md` | The research, so you can check and edit it. |

To change what goes into the card, edit `dossier.json` (or the creator brief) and rebuild with
`--from-dossier output/<Name>/dossier.json`. That rebuild skips the research and the scraping. It also reuses
the original run's settings from `run_settings.json` (talent/NSFW mode, consent note, boundaries, era, style…),
so you only type what you want to change.

Research is saved **before** the card is written, so a failure late in the run never costs you the research.
Creator (`--talent`) cards carry no list of research sources and never local file paths, which matters for cards
you sell. The full list stays in `sources.md` and `review.md`.

### Importing into Marinara by hand

1. **Characters → Import**, then drop in `<Name>.png` (or the `.card.json`).
2. At **Embedded lorebook found**, choose **Import Lorebook**. Marinara creates the lorebook and links it to
   the character.

`--push` does both steps for you.

## How it matches Marinara's format

The format follows Marinara's own importers (`packages/server/src/services/import/st-character.importer.ts`,
`st-lorebook.importer.ts`):

- The card is a Character Card **V2** envelope. The PNG has both a `chara` (V2) and a `ccv3` (V3) tEXt chunk,
  and Marinara prefers `ccv3`.
- Marinara's own fields are filled in: `extensions.appearance` (also used for image generation),
  `extensions.backstory`, `extensions.nameAliases` (alias names get coloured in chat),
  `extensions.nameColor`, `extensions.depth_prompt` and the library `summary`.
- The lorebook is embedded as `character_book`, and the standalone copy is SillyTavern World Info JSON. Each entry
  also carries Marinara's `description` field, which the **Knowledge Router** agent reads.
- Entry settings follow Marinara's lorebook advice: 3 to 8 specific keys, generic keys and the character's own
  name removed, **Whole Words** on, no Constant entries, and recursion off. An entry with secondary keys becomes
  **Selective** (AND Any).
- **Order:** in Marinara a *lower* order is inserted first and survives trimming, so core entries get 10+,
  notable ones 50+ and minor ones 100+.
- `--push` calls `POST /api/import/st-character` with the `x-marinara-csrf: 1` header and
  `importEmbeddedLorebook: true`. It uses Basic Auth if you set `MARINARA_USER`/`MARINARA_PASS`.

## Responsible use

The tool is built for people with a public record (public, historical and fictional figures) and for creators
who have authorised their own AI persona (`--talent`, which requires a consent note that is stored in the card).
By default it only searches the web for someone who has a Wikipedia article. Talent and `--no-wikipedia` modes
read only the pages and files you supply unless you add `--search-web`. People-search and leak/repost
sites are never read. The prompts tell the model to leave out private details (contact info,
addresses, information about private relatives), and the card's creator notes say it is a fictionalised
portrayal built from public sources. Check the facts before you share a card of a real, living person.

## Test

```bash
python -m tests.test_offline   # fake LLM + fake Marinara, no network needed
```
