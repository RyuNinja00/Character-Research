# Persona Forge: Step-by-Step Manual

This manual takes you from a fresh computer to a finished character card and lorebook running in
Marinara Engine. Follow the parts in order the first time. **Most people only need Parts 1–3 and then the
web UI (Part 3b).** Parts 4 onward cover the command line and the details behind each option.

**Contents**

1. What you need
2. Install
3. Connect an AI model (`.env`)
3b. Use the web UI (easiest)
4. Test run with a public figure
5. Set up a talent folder
6. The creator workflow, step by step
7. Read the review before you ship
8. Edit and rebuild without re-researching
9. Import into Marinara and set it up
10. Sprites and images
10b. Audio: interviews and podcasts
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

## 3b. Use the web UI (easiest)

The web UI is a form in your browser with a box for every option, a short example under each one, and a live
run log. It runs only on your computer and needs nothing extra installed.

### Start it

From the project folder, with the virtual environment active (Part 2.2):

```bash
python -m persona_forge.webui
```

Your browser opens at **http://127.0.0.1:8765**. Leave the terminal open while you use it, and press
**Ctrl+C** in it to stop. Options: `--port 9000` for another port, `--no-browser` to not open a tab.

The first start creates the `talent/` folder in the project.

### Build a card

1. **Section 1 · Who:** pick the mode (*Managed creator*, *Public / historical figure* or *Fictional
   character*), type the **Name**, and for creators the **Consent note**. These are the only required boxes,
   marked with \*. Typing the name creates the creator's folder (below).
2. **Section 2 · Where to look:** paste the **Link-in-bio** URL and any **Extra pages**. *Deep research* is
   on by default.
3. **Section 3 · Creator files & images** (optional): fill in the brief, review the hard limits, and drop
   in reference photos, a card image, finished sprites or text files (transcripts, Q&As). Uploads go straight
   into the creator's folder.
4. **Sections 4–7** (optional): chat style, opening premise, AI model, sending to Marinara, advanced limits.
   Leave anything empty to use the default. Empty model boxes use your `.env` values, shown in grey.
5. Click **Build card**. On the right, a **progress bar** shows the current stage (reading sources, reading
   pages with the AI, building the profile, deep research rounds, writing the card…). Under it, the
   **console** shows everything the app does:
   - **Purple:** the exact command that ran.
   - **Blue:** setup info, such as the creator folder, Python version, which AI model and provider are
     in use, and whether an API key is set.
   - **Yellow:** warnings, such as a skipped page and why.
   - **Red:** errors.

   Click **Problems** to see only warnings and errors, **Copy** to copy the whole log (useful when asking for
   help), **Expand** for a taller console, or **Log file** to open the saved log. When the run finishes, the
   card image and every output file appear below the console. Click any file to open it, and start with
   **review.md**.
6. To change the writing without researching again, edit sections 3–6 and click **Rebuild writing
   only**. It takes seconds to a minute.

To come back to a creator later, pick them from the drop-down at the top. Their settings, brief, limits,
links and files load automatically.

### Page reader: reading pages the way you would

The app normally reads pages with a fast reader that downloads the page without running it, like a quick
"view source". Some pages only show their text after JavaScript runs, and some sites turn away anything that
isn't a real browser. For those, the app can use a **real browser on your computer**. Set it under
**Page reader** (section 2) or with `--browser`:

| Option | What it does |
| --- | --- |
| **Smart** (default, `auto`) | Fast reader first. If a page **you chose** (your links, the link hub and its links, the creator's own profiles) comes back refused, empty or behind an age page, it's opened in the real browser instead. |
| **Visible browser** (`visible`) | Like Smart, but the browser window is shown. If a site asks you to **verify you're human** or shows a CAPTCHA, the run pauses (up to 3 minutes) for you to complete it in the window, then continues. |
| **Always** (`always`) | Every page you chose goes through the real browser. Slower, but best for JavaScript-heavy sites. |
| **Fast reader only** (`off`) | No browser. |

Setup, once: `pip install playwright`. The app uses your installed **Chrome** or **Edge**. If you have neither,
also run `python -m playwright install chromium`.

How it behaves:

- **It keeps its own profile** in `.browser_profile/` in the project, so cookies, cookie-banner choices and age
  confirmations are remembered between runs, like your own browser. Delete the folder to start fresh.
- **It reads at a human pace,** with a short, varied pause between pages on the same site. Pages on different
  sites still load in parallel.
- **It never hides what it is.** It doesn't use CAPTCHA-solving services, disguise browser automation, fake a
  browser fingerprint or rotate proxies. When a site wants a person, use **Visible browser** and do the check
  yourself.
- **Search results are always read with the fast reader.** The browser is only used for pages you chose and the
  creator's own channels.

### What happens with empty or odd boxes

- Empty boxes are left out of the run, so the app's defaults apply.
- A number box with something that isn't a number (e.g. `abc`) is ignored with a note in the log. It doesn't
  stop the run.
- The **brief** is only used once you've written something in it. The untouched template is left out.
- In **Hard limits**, lines still containing a `<placeholder>` are skipped, and the rest are used.
- If a creator has no link hub, pages, brief or files, web search on the name is switched on automatically,
  so there's always something to read. (With *Offline* ticked you get a clear message instead.)
- Grey italic text inside a box is only an example. It's never sent to the app.
- Empty AI-model boxes use your `.env`, and the value in use is shown in green under each box. The one
  exception is **Page-reading model**: empty means "same as Model", even if `.env` has an `LLM_EXTRACT_MODEL`.
  If a page-reading model is ever rejected by your provider, the app switches to the main model and says
  so in the console.
- API keys and passwords are never saved in the creator folder. Tick **Save these model settings to .env** to
  keep them.

### The creator folder it builds

```
talent/
  Ivy_Wren/
    settings.json      every box you filled in (no keys or passwords) – reloads into the form
    brief.md           creator brief (starts as the template)
    limits.txt         hard limits (starts as the template)
    urls.txt           the "Extra pages" box
    references/        reference photos  → sprite kit
    avatar/            card image
    sprites/           finished expression sprites
      full-body/       finished full-body sprites
    sources/           text files (transcripts, Q&As…)
    output/
      Ivy_Wren/        the card PNG, card JSON, lorebook, review.md, dossier.json, sprite_kit/…
    runs/              a log of every run, with date and time
```

The whole `talent/` folder is git-ignored, so photos, briefs and outputs never get uploaded to GitHub.

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

The web UI creates and fills this folder for you (Part 3b). This part is for running from the command line.
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

## 10b. Audio: interviews and podcasts

Spoken interviews are often the best source for someone's voice. The app can listen to them: audio is
turned into text (transcribed), then used like any interview.

### Pick a transcription engine (once)

Featherless only has text models, so audio needs its own engine:

| Engine | Setup | Notes |
| --- | --- | --- |
| **Local Whisper** (recommended) | `pip install faster-whisper` | Free and **private**: the audio never leaves your computer. The first use downloads the model (about 0.5 GB for "small"). On a normal laptop, "small" takes roughly a third to half of the audio's length. |
| **Transcription API** | In `.env`: `TRANSCRIBE_BASE_URL`, `TRANSCRIBE_API_KEY`, `TRANSCRIBE_MODEL` | Any OpenAI-compatible `/audio/transcriptions` service. Groq: `https://api.groq.com/openai/v1` with `whisper-large-v3` (fast, cheap). OpenAI: `https://api.openai.com/v1` with `whisper-1`. Long files are split automatically, which needs [ffmpeg](https://ffmpeg.org/download.html). |

**Auto** (the default) uses local Whisper if it's installed, otherwise the API. In the web UI these settings are in
section 5, and a green note there tells you which engine is ready.

### Using an NVIDIA GPU (optional, faster)

Local Whisper tries your NVIDIA graphics card first. On Windows this often fails with an error like
`cublas64_12.dll is not found`, because the GPU needs NVIDIA's **cuBLAS for CUDA 12** and **cuDNN 9 for CUDA 12**
libraries, which Windows doesn't include. **The app then switches to the CPU on its own and carries on.** To skip
the GPU attempt, set **Local Whisper runs on → CPU** (`--whisper-device cpu`, or `WHISPER_DEVICE=cpu` in `.env`).

To actually use the GPU, which is several times faster, give it the libraries in one of these ways, then set
**Local Whisper runs on → NVIDIA GPU**:

1. **Easiest:** on GitHub, open **Purfview / whisper-standalone-win → Releases → "libs"** and download the
   **cuBLAS and cuDNN archive for CUDA 12**. Unzip the `.dll` files into a folder called **`cuda_libs`** inside
   the project folder. The app finds them there automatically (the folder is git-ignored).
2. **Through pip:** run `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*`. The app adds those packages'
   DLL folders automatically. If NVIDIA doesn't publish Windows files for your version, use option 1.
3. **NVIDIA's installers:** install the **CUDA Toolkit 12** and **cuDNN 9 for CUDA 12** from developer.nvidia.com,
   add the cuDNN `bin` folder to your Windows `PATH`, and restart.

Also keep your NVIDIA driver up to date. Newer cards (e.g. the RTX 50 series) need recent CUDA 12 libraries.
If the GPU still fails, the console shows the error and the run continues on the CPU.

### Add audio

- **Your files:** drop them into **Audio & video** in section 3 of the web UI (mp3, m4a, wav, ogg, opus, flac,
  mp4…, up to 1 GB each), or use `--audio file-or-folder` on the command line. They're saved in
  `talent/<name>/audio/`.
- **Podcast pages:** with **Transcribe podcasts on pages I add** ticked (the default in the web UI;
  `--transcribe-pages` on the command line), any **Extra page** with a podcast player or audio file has its
  audio downloaded and transcribed. That includes Spreaker episode pages.
- **A direct audio link:** `--audio-url https://…/episode.mp3`.
- **YouTube** can't be downloaded. On the video, click **… → Show transcript**, copy it into a `.txt` and add it
  under **Text sources**.

### What you get

- Transcripts are saved in `output/<Name>/transcripts/` with a timestamp about every 45 seconds, and they're
  listed as sources in `review.md`.
- Transcripts are cached, so a re-run doesn't transcribe the same audio again.
- Transcription can't tell who is speaking (an interviewer versus the creator). The AI is told this and only
  credits statements that clearly come from the person, such as answers about their own life and opinions.
- The progress bar shows **Transcribing audio**, and the console shows progress every 5 minutes of audio.

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
| `--audio F` / `--audio-url U` / `--transcribe-pages` | Audio files or folders, direct audio links, and podcast players on pages you add, all transcribed and used as sources. |
| `--transcriber auto\|local\|api\|off`, `--whisper-model`, `--audio-language` | Transcription engine, local Whisper size (tiny…large-v3), and spoken language (empty = detect). |
| `--browser auto\|visible\|always\|off` | Page reader: real browser when needed (default), with a visible window so you can complete human checks, for every page you chose, or never. |
| `--confirm-adult` / `--no-age-click` | Answer simple "Are you 18?" click-through pages (on by default with `--nsfw`), or never answer them. Real ID/face/card verification is never bypassed. |
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
| Web UI page doesn't open | Check the terminal for the address, or open http://127.0.0.1:8765 yourself. If the port is in use, start it with `--port 9000`. |
| `python` not found | Use `python3` (Mac/Linux), or reinstall Python with **Add to PATH** ticked (Windows). |
| `No module named requests` | Activate the virtual environment (Part 2.2), then `pip install -r requirements.txt`. |
| `The model (…) refused this step` | The model won't write adult content. Set a different `LLM_MODEL` (Part 3.1) and rebuild with the `--from-dossier` command the app printed. Your research is kept. |
| `LLM request failed (HTTP 401)` | Wrong or missing `LLM_API_KEY`. |
| `LLM request failed (HTTP 404)` | Wrong `LLM_MODEL` id or `LLM_BASE_URL`. |
| "Page-reading model … was rejected" | Your `.env` has an `LLM_EXTRACT_MODEL` from a different provider (e.g. `gpt-4o-mini` left over from the OpenAI example while using Featherless). The run continues with the main model. Delete that line from `.env` or set it to a model from your provider. |
| Errors about `response_format` with a local model | Set `LLM_JSON_MODE=off`. |
| `--talent needs --consent-note` | Add `--consent-note "..."`. |
| `Only N facts found` | Not enough material. Add `--deep`, more `--url` pages, or a `--file` brief. Check `review.md` to see what was read and skipped. |
| Link hub reads 0 links | The site blocked the request. Add the hub's important links with `--url` or a `--urls-file`. |
| `HTTP 404` / `403` / `503` on a link you added | Open the link in your browser. If it **doesn't** load, the link is wrong or the page was removed (old interviews often are). If it **does** load, the site blocks automated readers. The app already retries with normal browser settings, so save the page instead: **Ctrl+S / Cmd+S → "Webpage, HTML only"**, or copy the text into a `.txt`, and add it under **Text sources** (web UI) or with `--file`. |
| "the site asked to verify you're human" | Set **Page reader → Visible browser** (or `--browser visible`). A browser window opens when the site asks, so complete the check yourself and the run continues. |
| "Real-browser reader not available" | Run `pip install playwright`. If you have neither Chrome nor Edge, also run `python -m playwright install chromium`. |
| A page you chose is "almost no readable text" | It's probably built with JavaScript. The **Smart** page reader opens it in the real browser automatically once Playwright is installed. Or choose **Always**. |
| `extraction failed … Expecting ',' delimiter` (older versions) | The AI's reply was cut off or had stray quote marks, which is common with long, dialogue-heavy sources like book chapters. Now fixed: stray quotes are repaired, every complete fact is kept from a cut-off reply (the console says "kept N complete facts"), a piece that still fails is retried in two halves, and sources are read in smaller pieces (8,000 characters; `--chunk-chars` changes it). |
| `unusable reply (the reply had no facts list)` | Usually a reasoning model (e.g. Qwen 3/3.5) writing long "thinking" that uses up its reply, or naming things differently. Now: known reasoning models are asked to answer directly (**Model "thinking" → Auto**), differently-shaped replies are accepted, and cut-off replies are never cached. The console line under the message (`↳ …`) shows what the model actually sent. If it says the model only "thought", set **Model "thinking" → Off**, or raise **Max reply tokens**. |
| `could not transcribe … cublas64_12.dll is not found` (older versions) | Fixed: the app now switches to the CPU by itself. To use the GPU, see **10b → Using an NVIDIA GPU**. Or set **Local Whisper runs on → CPU** to skip the GPU. |
| "audio left out: no transcription engine" | Run `pip install faster-whisper` (free, private), or set `TRANSCRIBE_BASE_URL` and `TRANSCRIBE_API_KEY` in `.env`. Featherless can't transcribe. |
| "larger than the API's ~25 MB limit and ffmpeg isn't installed" | Install ffmpeg (ffmpeg.org; Windows: `winget install ffmpeg`; Mac: `brew install ffmpeg`), or use local Whisper. |
| A podcast page's audio isn't found | Some players hide the file. Find the episode's download link (often under ⋯ or "Download") and add it with `--audio-url`, or download the file and drop it under **Audio & video**. |
| "Are you 18?" page instead of content | With **18+ adult card** on (or the **I'm 18+** box ticked in the web UI, which is the default), the app answers simple click-through age pages for you: it sets the usual "verified" cookie, or clicks the site's own "Enter / I am 18" link or button. Use `--confirm-adult` on the command line for non-NSFW runs, or `--no-age-click` to turn it off. |
| "the site uses real age verification" | The site requires an ID, face or card check (often required by law in some regions). The app never bypasses these. Open the page in your browser, complete the check yourself, save the page (Ctrl+S) and add it under **Text sources**. |
| YouTube link gives little | Only a video's title and description can be read, not what's said. On YouTube click **… → Show transcript**, copy it into a `.txt` and add it under **Text sources**. |
| A run still skips a page after you fixed it | Failed pages aren't cached, so just run again. |
| Lots of `almost no readable text` | Social and fan sites hide content behind logins. Paste the text you need (bio, captions, FAQ) into a file and add it with `--file`. |
| `search failed (ddg): No results found` (older versions) / few web pages | Fixed: adult runs now search with **safe search off** (DuckDuckGo's default, "moderate", hid adult sites), an empty search is retried once in a simpler form, and empties are no longer reported as failures. The run prints one summary line, e.g. `Web search (ddg, safe search off): 18 searches, 11 with results`. If free search still finds little, add `TAVILY_API_KEY` or `BRAVE_API_KEY` to `.env` (much better results), or add pages yourself under **Extra pages**. |
| `search is being rate-limited` | DuckDuckGo is throttling the free search. The app waits and slows down automatically. Fewer `--max-rounds`, or a Tavily/Brave key, avoids it. |
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
