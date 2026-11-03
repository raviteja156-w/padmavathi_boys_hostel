#!/usr/bin/env bash
# NEXORA WORKPULSE - one-command local start for Mac/Linux:  bash start_local.sh
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
python app.py
