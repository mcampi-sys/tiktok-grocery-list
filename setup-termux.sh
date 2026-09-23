#!/bin/bash
# Setup for running the grocery list app on your phone via Termux.
# 1. Install Termux from F-Droid (not Google Play - that version is outdated)
# 2. Extract the project zip somewhere, e.g. ~/grocery
# 3. Run: bash setup-termux.sh
set -e
pkg update -y
pkg install -y python ffmpeg
pip install --upgrade pip
pip install yt-dlp
echo
echo "=============================================="
echo " Done! Now:"
echo "  1. Get a free Gemini key: https://aistudio.google.com/apikey"
echo "  2. export GEMINI_API_KEY=paste_your_key_here"
echo "     (add it to ~/.bashrc to keep it)"
echo "  3. Run: python app.py"
echo "  4. Open http://localhost:8000 in your phone browser"
echo ""
echo " Tip: run 'termux-wake-lock' so Android doesn't kill"
echo " the server when the screen is off."
echo "=============================================="
