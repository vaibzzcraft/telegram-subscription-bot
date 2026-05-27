#!/bin/bash
set -e
python3 -m pip install -r requirements.txt --quiet
python3 bot.py
