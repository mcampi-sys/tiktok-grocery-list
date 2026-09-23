#!/usr/bin/env python3
"""
TikTok -> recipe extractor (prototype).

Pipeline:
    TikTok URL
      -> yt-dlp grabs metadata (title, caption, author) + auto subtitles
      -> caption + subtitles are combined as the "source text"
      -> an LLM turns the source text into a structured recipe (JSON)

Usage:
    python extract.py "https://www.tiktok.com/@user/video/123..."
    python extract.py "https://vm.tiktok.com/XXXX" --dump-text     # no LLM needed
    python extract.py "<url>" --llm gemini -o recipe.json

LLM backends (pick with --llm, key via environment):
    openai   OPENAI_API_KEY            (or any OpenAI-compatible API via OPENAI_BASE_URL)
    gemini   GEMINI_API_KEY            (free tier: https://aistudio.google.com/apikey)
    ollama   local model via OLLAMA_MODEL (default: llama3.1), no key needed

Requires: yt-dlp, ffmpeg on PATH.  No other dependencies (uses stdlib urllib).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

YT_DLP = os.environ.get("YT_DLP_BIN", "yt-dlp")

SYSTEM_PROMPT = """You extract cooking recipes from TikTok video text (captions and/or \
auto-generated subtitles). Respond with ONLY a JSON object, no markdown fences, \
no commentary. Use this exact shape:

{
  "name": "recipe name (short, appetizing)",
  "servings": "e.g. '4' or null if unknown",
  "ingredient_groups": [
    {"name": "group name or null",
     "items": [{"item": "ingredient name", "quantity": "e.g. '1/2' or null",
                "unit": "e.g. 'cup' or null", "notes": "e.g. 'minced' or null"}]}
  ],
  "steps": ["step 1", "step 2"],
  "confidence": "high|medium|low",
  "warnings": ["any guesswork you had to do, e.g. missing quantities"]
}

Rules:
- If the text has no usable recipe, return {"error": "no recipe found", "warnings": [...]}.
- Prefer the video caption/description over subtitles when they disagree; subtitles \
from gimmick or "interactive" videos are often just engagement prompts, not the recipe.
- Merge duplicate ingredients. Keep quantities exactly as stated; never invent amounts. \
If an amount is missing, set quantity/unit to null and note it in warnings.
- Steps should be concise, in order, and complete enough to cook from.
"""

USER_PROMPT_TMPL = """Video title: {title}
Video author: {uploader}

--- CAPTION ---
{caption}

--- SUBTITLES ---
{subtitles}

Extract the recipe as JSON."""


# ---------------------------------------------------------------- fetch

def run_yt_dlp(args, **kw):
    cmd = [YT_DLP, "--no-warnings", "--no-progress"] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        sys.exit(f"error: {YT_DLP} not found on PATH. Install it: pip install yt-dlp")


def fetch_metadata(url):
    """Return dict with id/title/description/uploader/duration/webpage_url."""
    p = run_yt_dlp(["--skip-download", "--dump-single-json", url], timeout=120)
    if p.returncode != 0:
        sys.exit(f"error: could not fetch video info.\n{p.stderr.strip()[-500:]}")
    info = json.loads(p.stdout)
    return {
        "id": info.get("id"),
        "title": info.get("title") or "",
        "caption": info.get("description") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "url": info.get("webpage_url") or url,
    }


def fetch_subtitles(url, workdir):
    """Download best-effort subtitles, return cleaned plain text (may be '')."""
    p = run_yt_dlp([
        "--skip-download", "--write-subs", "--write-auto-subs", "--all-subs",
        "--sub-format", "vtt", "--convert-subs", "vtt",
        "-o", os.path.join(workdir, "%(id)s.%(ext)s"), url,
    ], timeout=180)
    if p.returncode != 0:
        return ""
    texts = []
    for fname in sorted(os.listdir(workdir)):
        if fname.endswith(".vtt"):
            with open(os.path.join(workdir, fname), encoding="utf-8", errors="ignore") as f:
                texts.append(clean_vtt(f.read()))
    return "\n".join(t for t in texts if t).strip()


def clean_vtt(vtt):
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)  # strip cue tags
        if not lines or lines[-1] != line:
            lines.append(line)
    return " ".join(lines)


def fetch_audio(url, workdir):
    """Download audio-only file for external transcription; returns path or ''."""
    p = run_yt_dlp([
        "-x", "--audio-format", "mp3", "--audio-quality", "5",
        "-o", os.path.join(workdir, "%(id)s.%(ext)s"), url,
    ], timeout=300)
    if p.returncode != 0:
        return ""
    for fname in os.listdir(workdir):
        if fname.endswith(".mp3"):
            return os.path.join(workdir, fname)
    return ""


# ---------------------------------------------------------------- llm

def llm_openai(prompt, model=None):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("error: set OPENAI_API_KEY (or use --llm gemini / ollama).")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = json.dumps({
        "model": model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


def llm_gemini(prompt, model=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("error: set GEMINI_API_KEY (free at https://aistudio.google.com/apikey).")
    model = model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={key}")
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1,
                             "responseMimeType": "application/json"},
    }).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["candidates"][0]["content"]["parts"][0]["text"]


def llm_ollama(prompt, model=None):
    model = model or os.environ.get("OLLAMA_MODEL", "llama3.1")
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    body = json.dumps({
        "model": model, "stream": False, "format": "json",
        "system": SYSTEM_PROMPT, "prompt": prompt,
        "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(host + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
    except OSError:
        sys.exit("error: cannot reach ollama. Is `ollama serve` running?")
    return data["response"]


LLM_BACKENDS = {"openai": llm_openai, "gemini": llm_gemini, "ollama": llm_ollama}


def extract_recipe_json(raw):
    """Pull the JSON object out of an LLM reply (tolerates fences)."""
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        sys.exit(f"error: LLM did not return JSON:\n{raw[:500]}")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        sys.exit(f"error: could not parse LLM JSON ({e}):\n{raw[:500]}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Extract a recipe from a TikTok video URL.")
    ap.add_argument("url", help="TikTok video URL (full or short vm.tiktok.com link)")
    ap.add_argument("--llm", choices=sorted(LLM_BACKENDS), default="gemini",
                    help="LLM backend for recipe parsing (default: gemini)")
    ap.add_argument("--model", default=None, help="override model name for the backend")
    ap.add_argument("--dump-text", action="store_true",
                    help="print caption + subtitles and exit (no LLM call)")
    ap.add_argument("--fetch-audio", action="store_true",
                    help="also download audio mp3 for external transcription")
    ap.add_argument("-o", "--output", default=None, help="write recipe JSON to file")
    ap.add_argument("--keep-workdir", action="store_true", help="keep temp files")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="tiktok_recipe_")
    try:
        print("Fetching video info...", file=sys.stderr)
        meta = fetch_metadata(args.url)
        print("Fetching subtitles...", file=sys.stderr)
        subs = fetch_subtitles(meta["url"], workdir)

        if args.dump_text:
            print(f"=== TITLE ===\n{meta['title']}\n")
            print(f"=== CAPTION ===\n{meta['caption'] or '(empty)'}\n")
            print(f"=== SUBTITLES ===\n{subs or '(none)'}")
            return

        audio_path = ""
        if args.fetch_audio:
            print("Downloading audio...", file=sys.stderr)
            audio_path = fetch_audio(meta["url"], workdir)

        prompt = USER_PROMPT_TMPL.format(
            title=meta["title"], uploader=meta["uploader"],
            caption=meta["caption"] or "(empty)",
            subtitles=subs or "(none)")
        print(f"Parsing recipe with {args.llm}...", file=sys.stderr)
        raw = LLM_BACKENDS[args.llm](prompt, args.model)
        recipe = extract_recipe_json(raw)
        recipe["source"] = {
            "url": meta["url"], "video_id": meta["id"],
            "uploader": meta["uploader"], "duration_s": meta["duration"],
        }
        if audio_path:
            recipe["source"]["audio_file"] = audio_path

        out = json.dumps(recipe, indent=2, ensure_ascii=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(out + "\n")
            print(f"Wrote {args.output}", file=sys.stderr)
        else:
            print(out)
    finally:
        if not args.keep_workdir:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
