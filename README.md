# TikTok Recipe Extractor (prototype)

Paste a TikTok video link, get a structured recipe back as JSON.
This is step 1 of the grocery-list app: it proves the hard part
(TikTok -> recipe) works before we build the app around it.

## How it works

1. **Fetch** — `yt-dlp` grabs the video's caption, title, author, and
   auto-generated subtitles. No login needed.
2. **Parse** — an LLM turns the caption/subtitles into a recipe
   (name, servings, grouped ingredients with quantities, steps).
   The prompt tells the LLM to prefer the caption over subtitles,
   because subtitles on gimmick videos are often just engagement
   prompts ("double tap to add garlic!").

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg must be on PATH (only needed for --fetch-audio)
```

Pick an LLM backend and set its key:

| Backend | Env var | Notes |
|---|---|---|
| `gemini` (default) | `GEMINI_API_KEY` | Free tier: https://aistudio.google.com/apikey |
| `openai` | `OPENAI_API_KEY` | Or any OpenAI-compatible API via `OPENAI_BASE_URL` / `OPENAI_MODEL` |
| `ollama` | — | Local model, e.g. `OLLAMA_MODEL=llama3.1`, needs `ollama serve` running |

## Usage

```bash
# Full pipeline: link -> recipe.json
python extract.py "https://www.tiktok.com/@user/video/123..." -o recipe.json

# Just see what text was found (no LLM call, no key needed)
python extract.py "https://vm.tiktok.com/XXXX" --dump-text

# Also download audio for external transcription (e.g. Whisper)
python extract.py "<url>" --fetch-audio -o recipe.json

# Choose backend / model
python extract.py "<url>" --llm openai --model gpt-4o-mini -o recipe.json
```

Output shape (`recipe.json`): `name`, `servings`, `ingredient_groups`
(each with `items: [{item, quantity, unit, notes}]`), `steps`,
`confidence`, `warnings`, and `source` (url, video id, uploader).

## Known limitations

- TikTok rate-limits datacenter IPs. If fetches start failing, run from
  your home machine or add `--cookies-from-browser` / impersonation
  support (`pip install yt-dlp[default]`).
- Videos with no caption recipe and no subtitles need audio
  transcription (Whisper) — `--fetch-audio` downloads the mp3;
  wiring transcription in is the next step.
- Quantities are only as good as the video: if the creator doesn't say
  an amount, it's recorded as `null` with a warning, never invented.

## What's next (the app)

This extractor becomes the ingestion step. Still to build:
grocery-list aggregation across recipes, cost estimation, per-item
quantity scaling, and add/remove-recipe with automatic ingredient
adjustment.

---

# Grocery List App (prototype)

No price estimates — just the list. Run it:

```bash
python app.py          # http://localhost:8000  (set PORT to change)
```

On your phone, open `http://<your-computer-ip>:8000` on the same Wi-Fi.

## Run it on your phone (Termux, Android)

The app runs directly on your phone — no computer needed:

1. Install **Termux** from F-Droid (not Google Play, that version is dead).
2. Download `grocery-app.zip` and extract it, e.g. into `~/grocery`.
3. In Termux: `cd ~/grocery && bash setup-termux.sh`
4. Get a free key at https://aistudio.google.com/apikey, then
   `export GEMINI_API_KEY=your_key` (add to `~/.bashrc` to keep it).
5. `python app.py`, then open **http://localhost:8000** in your browser.
6. Optional: `termux-wake-lock` keeps the server alive with the screen off.

Bonus: TikTok blocks datacenter IPs but usually not phone networks,
so link extraction tends to work *better* from the phone.

**What it does**
- **Add from TikTok link** — runs `extract.py` on the URL (needs the
  same LLM key, e.g. `GEMINI_API_KEY`) and adds the recipe.
- **Add from pasted recipe JSON** — for recipes extracted earlier.
- **Remove a recipe** — its ingredients are automatically subtracted
  from the list.
- **Quantities aggregate** — "2 cloves garlic" + "1 clove garlic" = 3 cloves.
  Volume/weight units convert (1 tbsp + 3 tsp = 2 tbsp; 500 g + 1 lb = 953.59 g).
  Ingredients with no stated amount show "as needed"; if some recipes
  specify an amount and others don't, you get e.g. "¼ cup+".
- **Adjust per item** — −/+ steppers or the ✎ editor set a manual
  quantity (marked "edited"); "reset" restores the computed total.
- **Scale a recipe** — ×0.5 to ×10 stepper on each recipe card;
  all its ingredient quantities (and servings, when known) scale.
- **Check off while shopping** — tap the checkbox; bought items sink
  to the bottom with strikethrough. "clear ✓" unchecks all.
- **Pantry staples** — ingredients you always have (salt, olive oil…)
  never appear on the list. Tap suggestions or type your own.
- **Saved recipes** — ★ a recipe to bookmark it; re-add it to the
  active list anytime, or delete it.
- **Share** — one tap: uses the phone's share sheet (WhatsApp, etc.)
  or copies a clean text list to the clipboard.

**API** (JSON):
- `GET /api/state` → `{recipes, items}`
- `POST /api/recipes` → `{"url": "…"}` or `{"recipe": {…}}`
- `DELETE /api/recipes/<id>`
- `POST /api/items/adjust` → `{"key": "…", "delta": -1}` or
  `{"key": "…", "quantity": "2", "unit": "cups"}`
- `DELETE /api/overrides/<key>` → clear a manual edit

State lives in `data/grocery.json`. Stdlib only — no extra dependencies.
