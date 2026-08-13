#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
playwright install --with-deps chromium
