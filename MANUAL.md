# Persona Forge: Step-by-Step Manual

This manual takes you from a fresh computer to a finished character card and lorebook running in
Marinara Engine. Follow the parts in order the first time. After that, use **Part 6 (the creator
workflow)** and the **quick reference** at the end.

**Contents**

1. What you need
2. Install
3. Connect an AI model (`.env`)
4. Test run with a public figure
5. Set up a talent folder
6. The creator workflow, step by step
7. Read the review before you ship
8. Edit and rebuild without re-researching
9. Import into Marinara and set it up
10. Sprites and images
11. Every option, grouped
12. Troubleshooting
13. Time, cost and tips
14. What the app will and won't do

---

## 1. What you need

| Item | Notes |
| --- | --- |
| A computer | Windows 10/11, macOS or Linux. |
| **Python 3.9 or newer** | Check with `python --version` (Windows) or `python3 --version` (Mac/Linux). Get it at python.org. On Windows, tick **"Add python.exe to PATH"** during install. |
| **Git** (optional) | To download and update the project. You can also download the ZIP from GitHub. |
| **An AI model** | Any OpenAI-compatible service, such as OpenRouter, OpenAI or DeepSeek, or a local model in Ollama or LM Studio. For **adult (`--nsfw`) cards**, choose one that allows adult content (Part 3). |
| **Marinara Engine** | Installed and running if you want to import straight into it. The default address is `http://127.0.0.1:7860`. |
| Optional | An image-generation service for `--generate-avatar`, and a Tavily or Brave search key for better search results. |

---

## 2. Install

### 2.1 Download the project

**With Git:**

```bash
git clone https://github.com/RyuNinja00/character-research
cd character-research
```

**Without Git:** on the GitHub page click **Code → Download ZIP**, unzip it, and open a terminal in
that folder. On Windows, open the folder in File Explorer, click the address bar, type `powershell` and press Enter.

### 2.2 Create a virtual environment (recommended)

This keeps the app's packages separate from the rest of your system.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell says scripts are disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
once, answer **Y**, then try again.

**Mac / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt now starts with `(.venv)`. Activate it again each time you open a new terminal.

### 2.3 Install the packages

```bash
pip install -r requirements.txt
```

This installs `requests` and `beautifulsoup4`, which are required, plus three recommended extras: `ddgs` for better
free search, `trafilatura` for cleaner page text, and `pillow` for image conversion and resizing. If one of the extras
fails to install, the app still runs without it.

### 2.4 Check it works

```bash
python -m persona_forge --help
```

You should see the list of options. On Mac/Linux use `python3` if `python` isn't found. The rest of this
manual writes `python`.

---

## 3. Connect an AI model (`.env`)

The app reads its settings from a file called `.env` in the project folder.

1. Copy the example: `cp .env.example .env` (Windows: `copy .env.example .env`).
2. Open `.env` in a text editor and fill in **one** provider block.

### 3.1 Choosing a provider

| Use | Suggested setup |
| --- | --- |
| **Adult creator cards (`--nsfw`)** | **Featherless.ai** (example below), **OpenRouter** with a model that permits adult content, or a capable **local model**. Mainstream models (OpenAI, Anthropic, Google) often refuse adult extraction and writing. |
| SFW cards (public figures, fictional characters) | Any good model: OpenAI, Anthropic, OpenRouter or DeepSeek. |
| Free / private | A local model in Ollama or LM Studio. Use 14B parameters or more for decent cards. |

**OpenRouter example:**

```ini
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
LLM_MODEL=<a model id from openrouter.ai/models>
LLM_EXTRACT_MODEL=<a cheaper model id, optional>
```

**Featherless.ai example (recommended for adult creator cards):**

Featherless is a flat-price service for open-weight models that uses the same API format as OpenAI, so it
works with no code changes. Many of its models are uncensored, so they don't refuse adult extraction or writing.

```ini
LLM_BASE_URL=https://api.featherless.ai/v1
LLM_API_KEY=your-featherless-key
LLM_MODEL=huihui-ai/Huihui-Qwen3.5-27B-abliterated
LLM_MAX_TOKENS=6144
LLM_JSON_MODE=auto
```

Then add `--workers 2` to your commands (see the notes below). A full creator run looks like this:

```bash
python -m persona_forge "Ivy Wren" --talent --nsfw --deep --workers 2 \
  --seed https://linktr.ee/iivywren --descriptor "adult content creator" \
  --boundaries talent/ivy_wren/limits.txt \
  --consent-note "Authorised by Ivy Wren (release on file), 2026-11-14" --style chat
```

Featherless notes:

- **Concurrency (`--workers`):** Featherless limits how many requests run at once with *concurrent units*.
  Each request reserves units based on model size, so bigger models allow fewer parallel requests. The
  $25 Chat plan has 4 units. A ~27B model most likely costs 2 units, so `--workers 2` should use the plan
  fully without throttling. Check the exact cost on the model's page: use `--workers 4` if it's 1 unit, or
  `--workers 1` if it's 4. On the Developer or Pay-As-You-Go plans
  you can raise `--workers`. To see your limits, call `GET https://api.featherless.ai/v1/plan` with your key.
- **Context:** the Chat plan serves models with up to 32K context, prompt plus reply together.
  `LLM_MAX_TOKENS=6144` leaves room for the app's largest requests. Don't use models served at 4K or 8K; the
  model page shows the context size.
- **One model or two:** the 27B model above handles every step well. To save time, set
  `LLM_EXTRACT_MODEL=TrevorJS/gemma-4-26B-A4B-it-uncensored`, a fast mixture-of-experts model, for the
  page-reading step.
- **Different writing voice:** if the card's prose feels flat, rebuild just the writing with a creative-writing
  model. Your research stays as it is:
  `python -m persona_forge --from-dossier output/Ivy_Wren/dossier.json --model huihui-ai/Huihui-Mistral-Small-3.2-24B-Instruct-2506-abliterated`
- **Images:** Featherless is text-only. `--generate-avatar` needs a separate image service in the `IMAGE_*` settings.
  Everything else (card, lorebook, sprite kit) works without one.

**Local (Ollama) example:**

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:14b
LLM_JSON_MODE=off
```

For local models, also add `--workers 1` to your commands so the model isn't overloaded.

### 3.2 Two models: a cheap one and a strong one (optional)

The app reads many pages and pulls small facts out of each, then writes the card from those facts. The first job
is large but easy, and the second is small but needs quality. Set `LLM_EXTRACT_MODEL` to a cheap model and
`LLM_MODEL` to a strong one to cut costs a lot.

### 3.3 Optional extras in `.env`

```ini
# Better web search (otherwise DuckDuckGo is used for free)
TAVILY_API_KEY=...
# or BRAVE_API_KEY=...

# Image service for --generate-avatar
IMAGE_BASE_URL=https://api.openai.com/v1
IMAGE_API_KEY=...
IMAGE_MODEL=gpt-image-1

# If your Marinara server has a login
MARINARA_USER=...
MARINARA_PASS=...

# Your name or brand as the card's creator
CARD_CREATOR=YourAgencyName
```

**Never commit `.env` to GitHub.** The project's `.gitignore` already excludes it.

---

## 4. Test run with a public figure

Do this once to confirm everything works before you use it on a creator.

```bash
python -m persona_forge "Marie Curie" --era 1903
```

What happens:

1. It shows the Wikipedia matches. Press **Enter** to accept the first one, or type a number.
2. It reads Wikipedia, Wikiquote and about 12 web pages. Progress lines start with `→`.
3. It extracts facts, builds the dossier, then writes the card and the lorebook.
4. It finishes with `✓ Marie Curie: card + N lorebook entries written to output/Marie_Curie/`.

Open `output/Marie_Curie/review.md` to see what it found. If that works, your setup is good.

---

## 5. Set up a talent folder

Keep each creator's material in one folder. The `talent/` folder is git-ignored, so nothing private gets
uploaded.

```
talent/
  ivy_wren/
    references/          2–4 approved photos for sprite generation (optional)
    sprites/             finished expression images, e.g. happy.png (optional)
      full-body/         finished full-body poses (optional)
    brief.md             creator brief (optional but strongly recommended)
    limits.txt           hard limits / boundaries (recommended for adult cards)
    urls.txt             extra pages, one URL per line (optional)
```

### 5.1 Boundaries file (`limits.txt`)

Start from `templates/boundaries_template.txt`. The app adds this text **word for word** to the card's
system prompt, so the AI can't water it down. Edit the "off-limits themes" line with the creator.

### 5.2 Creator brief (`brief.md`), optional

Start from `templates/creator_brief_template.md`. A brief is the best source for voice, inner life and
preferences, because it's in the creator's own words. The app treats it as authoritative. Leave out anything
the creator doesn't want in the card. You don't *need* one: `--deep` research works without it.

### 5.3 Extra pages (`urls.txt`), optional

One URL per line. Lines starting with `#` are ignored. Good candidates are interviews, podcast pages, the
creator's own website, and their profile or storefront pages. Social profiles work but usually only give the bio.

```
# interviews
https://example.com/interview-with-ivy
https://podcast.example.com/episode-42
```

---

## 6. The creator workflow, step by step

### Step 1: Gather what you need

- **Stage name**, spelled exactly as the creator uses it.
- **Link-in-bio URL** (Linktree, Beacons, AllMyLinks, Carrd…).
- **Consent note**: a short line saying who authorised the card, e.g.
  `"Authorised by Ivy Wren (release on file), 2026-11-14"`. It's required and stored in the card.
- Their **usernames** on platforms, if they differ from the stage name or Linktree name. Most are found
  automatically from the Linktree links.

### Step 2: Run the research and build

Copy this, change the name, URL, paths and note, and run it from the project folder.

**Mac / Linux:**

```bash
python -m persona_forge "Ivy Wren" \
  --talent --nsfw --deep \
  --seed https://linktr.ee/iivywren \
  --descriptor "adult content creator" \
  --boundaries talent/ivy_wren/limits.txt \
  --consent-note "Authorised by Ivy Wren (release on file), 2026-11-14" \
  --creator "Ivy Wren" \
  --style chat
```

**Windows (PowerShell), all on one line:**

```powershell
python -m persona_forge "Ivy Wren" --talent --nsfw --deep --seed https://linktr.ee/iivywren --descriptor "adult content creator" --boundaries talent/ivy_wren/limits.txt --consent-note "Authorised by Ivy Wren (release on file), 2026-11-14" --creator "Ivy Wren" --style chat
```

Add any of these as needed:

| Add | When |
| --- | --- |
| `--file talent/ivy_wren/brief.md` | You have a creator brief. |
| `--urls-file talent/ivy_wren/urls.txt` | You collected extra pages. |
| `--url https://...` | A single extra page. Repeatable. |
| `--handle ivywrenxo` | A platform username the app can't find from the Linktree. |
| `--references talent/ivy_wren/references --avatar-from-reference` | Build a sprite kit and use photo 1 as the card image (Part 10). |
| `--sprites talent/ivy_wren/sprites` | Embed finished sprite images in the card. |
| `--audience-research` | Also write a separate report on what fans say. It's never used in the card. |
| `--push` | Import into a running Marinara automatically (Part 9). |
| `--style roleplay` | Prose and narration greetings for Roleplay chats instead of texting style. |

### Step 3: Watch the progress

You'll see stages like these:

```
→ Link hub https://linktr.ee/iivywren: 14 links, reading 11
→ Handles found on the link hub: ivywrenxo
→ Following 4 sub-pages on the creator's own sites
→ Adult-platform profiles found: 3
→ Extracting facts from 19 sources (24 chunks)…
→ Conglomerating facts into a dossier…
→ Profile coverage: 52% (target 85%)
→ Gap-fill round 1: Speech style, Motivations, Insecurities & fears…
  +23 facts from 8 pages → coverage 52% → 71%
→ Research saved to output/Ivy_Wren/ (dossier.json, review.md)
→ Writing character card…
→ Writing lorebook (17 candidate topics)…
✓ Ivy Wren: card + 24 lorebook entries written to output/Ivy_Wren/
```

Lines starting with `!` are warnings, such as a skipped page or a login wall. They're normal. Everything
skipped is listed in `review.md` with the reason.

A typical deep run takes **5 to 20 minutes**, depending on the model, the search engine and how much there is to
find.

### Step 4: Check the output folder

`output/Ivy_Wren/` contains:

| File | What it is |
| --- | --- |
| `Ivy_Wren.png` | **The card.** The image has the character and lorebook inside it. Import this. |
| `Ivy_Wren.card.json` | The same card as JSON. |
| `Ivy_Wren.lorebook.json` | The lorebook on its own, if you'd rather import it separately. |
| `review.md` | **Read this first** (Part 7). |
| `dossier.json` | The structured research the card is written from. Edit it to change the card (Part 8). |
| `facts.jsonl`, `sources.md`, `sources.json` | Every extracted fact and every source used. |
| `run_settings.json` | The settings from this run, reused when you rebuild. |
| `sprite_kit/` | Only with `--references` (Part 10). |
| `audience_report.md` | Only with `--audience-research`. |

---

## 7. Read the review before you ship

Open `review.md`. It has these sections, in order:

1. **Profile coverage**: every part of the profile marked *filled* or *missing*, the overall score, and each
   research round with its searches and results.
2. **Notes on adult content**: where sexual preferences and content themes came from, how many third-party
   claims were discarded, and how many excluded themes were removed.
3. **Sources read**: every page and how many facts it gave. Pages marked `0 (not about them)` were read
   but contained nothing about the person.
4. **Skipped**: every link that wasn't used, and why: people-search site, leak site, login wall, or a page with
   no readable text.
5. **Facts by category**: every fact with a link to its source. Low-confidence facts are marked.
6. **Check before using**: only appears if a passage in the finished card contains a word on the
   excluded-themes list. Read each one. Most are harmless, like "kids" in a non-sexual sentence.

**What to look for:**

- Facts about the **wrong person**, such as someone with the same name. Remove them from `dossier.json` (Part 8).
- **Missing** sections you can fill. Add a page that covers them with `--url`, or add a line to the brief.
- Anything the creator **doesn't want** in the card. Delete it from `dossier.json`.

---

## 8. Edit and rebuild without re-researching

The card is written from `dossier.json`. To change the card:

1. Open `output/Ivy_Wren/dossier.json` in a text editor. VS Code or Notepad++ show JSON nicely.
2. Edit, add or delete entries. Keep the JSON valid: quotes around text, commas between items, no
   comma after the last item.
3. Rebuild:

```bash
python -m persona_forge --from-dossier output/Ivy_Wren/dossier.json
```

The rebuild reuses everything from the original run, including talent/adult mode, the consent note, boundaries,
era and style. Add only the options you want to **change**, for example:

```bash
python -m persona_forge --from-dossier output/Ivy_Wren/dossier.json --style roleplay --greetings 5
python -m persona_forge --from-dossier output/Ivy_Wren/dossier.json --model <another-model>
```

A rebuild only rewrites the card and lorebook, so it's fast and cheap. Run it as often as you like to get
different greetings or wording.

**Add more research** by running the full command again with extra `--url`/`--file` sources. Pages already read
come from the cache, so you only pay for what's new.

---

## 9. Import into Marinara and set it up

### 9.1 Automatic import (`--push`)

With Marinara running, add `--push` to any command, including a `--from-dossier` rebuild:

```bash
python -m persona_forge --from-dossier output/Ivy_Wren/dossier.json --push
```

This creates the character and its linked lorebook, with avatar and sprites. If Marinara runs on another
address or computer, give it: `--push http://192.168.1.20:7860`. If it has a login, set `MARINARA_USER` and
`MARINARA_PASS` in `.env`.

### 9.2 Manual import

1. In Marinara, click **Characters** (it may be under **More**), then **Import**.
2. Drop in `Ivy_Wren.png`.
3. When it shows **Embedded lorebook found**, choose **Import Lorebook**. This creates the lorebook and links
   it to the character.

### 9.3 Recommended Marinara settings

- **Chat mode:** cards made with `--style chat` are written for **Conversation** chats. `--style roleplay` cards
  are written for **Roleplay** chats.
- **Lorebook:** it's linked to the character automatically and switches on in that character's chats. Check it
  under **Chat Settings → Lorebooks**.
- **Sprites in Roleplay:** add the **Expression Engine** agent (**Chat Settings → Agents**) so the right
  expression shows for each reply.
- **Images in chat:** install the **Illustrator** agent and set an image model under **Connections**. The card's
  image description keeps the character looking the same.

### 9.4 Selling or sharing the card

- Use `--creator "Name"` so the card shows the right creator.
- Creator (`--talent`) cards contain no research source list and no file paths from your computer.
- The consent note appears at the top of the card's creator notes. Phrase it the way you want buyers to see it.

---

## 10. Sprites and images

### 10.1 What every card gets automatically

Every card gets an **image description**: hair, eyes, build, tattoos and signature outfits. Marinara uses it for avatars,
sprites, selfies and illustrations. You'll find the same text in `sprite_kit/appearance_description.txt` when
you build a kit. Turn it off with `--no-image-prompt`.

### 10.2 Card image (avatar)

Pick one:

- `--avatar photo.png` uses an approved photo or artwork.
- `--references folder/ --avatar-from-reference` uses the first reference photo.
- `--generate-avatar` creates an illustrated, clothed portrait from the description. It needs the `IMAGE_*`
  settings. Change the look with `--avatar-style "anime portrait"`, `"photorealistic headshot"` and so on.
- With none of these, you get a simple initials placeholder.

### 10.3 Sprite kit from reference photos

1. Put **2 to 4 approved photos** in `talent/<name>/references/`. Name them so the best ones sort first:
   `1_face.png`, `2_three_quarter.png`, `3_full_body.png`, `4_tattoo.png`. Plain backgrounds and one
   person per photo work best.
2. Add `--references talent/<name>/references --avatar-from-reference` to your command.
3. Open `output/<Name>/sprite_kit/SPRITE_KIT.md` and follow it in Marinara:
   **Character → Sprites → Facial Expressions → Add sprites → Generate Sprite**. Add the reference images, paste
   the appearance description, tick the listed expressions, and click **Generate**. Then repeat for **Full-body**.

The kit chooses the expressions and poses that suit this persona from Marinara's own lists. Change the
counts with `--sprite-expressions` (default 12) and `--sprite-poses` (default 6).

### 10.4 Finished sprite images

If you already have expression images, name them by mood (`happy.png`, `flustered.png`). Put full-body poses in
a `full-body/` subfolder, then add `--sprites talent/<name>/sprites`. They're embedded in the card and
restored when it's imported. The limit is 256 images, 8 MB each.

---

## 11. Every option, grouped

Run `python -m persona_forge --help` for the full list. These are the main options.

**Who and where to research**

| Option | Meaning |
| --- | --- |
| `"Name"` | The stage name or person's name. Required unless you use `--from-dossier`. |
| `--seed URL` | Link-in-bio page. The app reads it and every page it links to. Repeatable. |
| `--seed-depth N` | Also crawl link hubs found on link hubs. Default 1. |
| `--seed-max-links N` | The most links read per hub. Default 25. |
| `--site-pages N` | Sub-pages read on the creator's own websites. Default 5; 0 turns it off. |
| `--url URL` / `--urls-file F` | Extra pages to read. |
| `--file F` | Local text, Markdown, JSON or HTML source, such as a brief or transcript. Treated as authoritative. |
| `--handle NAME` | An extra platform username. Repeatable. |
| `--descriptor "..."` | A short hint about who this is, used in searches. |
| `--search-web` | Also run web searches for interviews and podcasts. |
| `--deep` | Keep researching until the profile is complete. Includes `--search-web`. |
| `--target-coverage 0.85` / `--max-rounds 4` / `--pages-per-round 10` | Limits for `--deep`. |
| `--max-pages N` | The most web pages read in the first pass. Default 12. |
| `--no-web` | No web access beyond the pages you give it. |
| `--search-provider` | `auto` (default), `ddg`, `tavily`, `brave`, `searxng` or `none`. |

**Mode and safety**

| Option | Meaning |
| --- | --- |
| `--talent` | Managed-creator mode. Skips Wikipedia and applies the creator-safety rules. Needs `--consent-note`. |
| `--consent-note "..."` | Who authorised the card. Stored in the card. |
| `--nsfw` | 18+ card. Adds adult-platform research and content themes. |
| `--boundaries F` | Hard limits, added word for word to the card's system prompt. |
| `--audience-research` | A separate fan-perception report. Never used in the card. |

**The character**

| Option | Meaning |
| --- | --- |
| `--style chat\|roleplay` | Texting style (Conversation chats) or prose (Roleplay chats). |
| `--pov third\|first` | Narration person in roleplay style. |
| `--scenario "..."` | Your premise for the opening scene. |
| `--era "..."` | The point in time to portray them at. |
| `--greetings N` | Number of alternate greetings. Default 3. |
| `--lore-entries N` | The most lorebook entries. Default 40. |
| `--language "..."` | The language to write the card in. |
| `--creator "..."` | The creator name shown on the card. |

**Images**: `--avatar`, `--avatar-from-reference`, `--generate-avatar`, `--avatar-style`, `--references`,
`--sprites`, `--sprite-expressions`, `--sprite-poses`, `--no-image-prompt`, `--image-base-url`,
`--image-api-key`, `--image-model`.

**AI model**

| Option | Meaning |
| --- | --- |
| `--model` / `--extract-model` | Override the models in `.env`. |
| `--base-url` / `--api-key` | Override the provider in `.env`. |
| `--workers N` | Parallel AI calls. Default 4; use 1 for local models. |
| `--json-mode off` | For servers that reject JSON mode. |

**Output**

| Option | Meaning |
| --- | --- |
| `--out DIR` | Output folder. Default `output/`. |
| `--from-dossier F` | Rebuild from saved research. |
| `--spec v3` | Write the JSON card as V3. The PNG always carries both V2 and V3. |
| `--merge-fields` | Fold Appearance and Backstory into Description, for SillyTavern. Don't use it for Marinara. |
| `--push [URL]` | Import into Marinara. |
| `--no-cache` | Ignore cached pages and AI replies. |
| `-v` | Show more detail while it runs. |

---

## 12. Troubleshooting

| Problem | Fix |
| --- | --- |
| `python` not found | Use `python3` (Mac/Linux), or reinstall Python with **Add to PATH** ticked (Windows). |
| `No module named requests` | Activate the virtual environment (Part 2.2), then `pip install -r requirements.txt`. |
| `The model (…) refused this step` | The model won't write adult content. Set a different `LLM_MODEL` (Part 3.1) and rebuild with the `--from-dossier` command the app printed. Your research is kept. |
| `LLM request failed (HTTP 401)` | Wrong or missing `LLM_API_KEY`. |
| `LLM request failed (HTTP 404)` | Wrong `LLM_MODEL` id or `LLM_BASE_URL`. |
| Errors about `response_format` with a local model | Set `LLM_JSON_MODE=off`. |
| `--talent needs --consent-note` | Add `--consent-note "..."`. |
| `Only N facts found` | Not enough material. Add `--deep`, more `--url` pages, or a `--file` brief. Check `review.md` to see what was read and skipped. |
| Link hub reads 0 links | The site blocked the request. Add the hub's important links with `--url` or a `--urls-file`. |
| Lots of `almost no readable text` | Social and fan sites hide content behind logins. Paste the text you need (bio, captions, FAQ) into a file and add it with `--file`. |
| `search failed` / few web pages | The free search is rate-limiting you. Wait a few minutes, lower `--max-rounds`, or set `TAVILY_API_KEY` or `BRAVE_API_KEY`. |
| Wrong person's facts | Use a more specific `--descriptor`, add `--handle`, and delete the wrong facts from `dossier.json`, then rebuild. |
| `Push failed … 401` | Set `MARINARA_USER` and `MARINARA_PASS` in `.env`. |
| `Push failed … 403` | Marinara rejected the request. Run the app on the same computer as Marinara, or import the PNG by hand. |
| `Push failed` (can't connect) | Start Marinara first, and check the address with `--push http://host:port`. |
| Sprite kit skipped | You used `--no-image-prompt`. The kit needs the appearance description. |
| Avatar generation failed | Check the `IMAGE_*` settings. The card still builds with a placeholder. |

---

## 13. Time, cost and tips

- **Cost:** a deep creator run makes roughly 30 to 120 AI calls. Most are cheap fact extractions. Using a cheap
  `LLM_EXTRACT_MODEL` is the biggest saving. The app prints the total calls and tokens at the end.
- **Cache:** pages and AI replies are cached for a week in `.persona_forge_cache/`, so re-runs are fast and
  cheap. Use `--no-cache` to start fresh.
- **Best results:** a good Linktree, plus `--deep`, plus a few interview or podcast URLs. A creator brief, if you
  have one, is the strongest single source for voice.
- **Iterate on the writing, not the research:** if the facts are right but the card doesn't sound right,
  use `--from-dossier`, which is quick, rather than rerunning the research.
- **Test in Marinara** with a few messages in both a Conversation chat and a Roleplay chat before you ship.

---

## 14. What the app will and won't do

**It will:**

- Read the creator's link hub, their own websites and their own profile and store pages on adult platforms.
- Extract personality, voice, inner life, backstory, tastes and content themes, and work out persona kinks from
  recurring themes in the creator's own posted content.
- Keep researching until the profile is complete (`--deep`), and show you everything it found, used and
  skipped in `review.md`.
- Build the card, lorebook, image description and sprite kit, and import them into Marinara.

**It won't:**

- Read people-search or records sites, or leak and repost sites.
- Record legal names, locations, schedules, contact details or private people found on the web. A creator
  brief can include anything the creator chooses.
- Take sexual preferences from third-party sites (forums, gossip, fan sites) or guess them from
  personality or appearance.
- Include minor-coded themes (teen, school, age-play), bestiality, or sex with someone unconscious or
  incapacitated, even if they appear in content.
- Generate new images from photos of a real person. Reference-based sprite generation runs in Marinara, on
  photos the team approves.
