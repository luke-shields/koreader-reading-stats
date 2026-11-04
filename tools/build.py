#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the installable plugin zip: python3 tools/build.py"""
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'koreader_stats')
OUT = os.path.join(ROOT, 'dist', 'KOReader Reading Stats.zip')
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(ROOT, 'LICENSE'), 'LICENSE')
    for folder, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in sorted(files):
            if name.startswith('.') or name.endswith('.pyc'):
                continue
            path = os.path.join(folder, name)
            z.write(path, os.path.relpath(path, SRC))
print(OUT)
