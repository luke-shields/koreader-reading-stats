#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check a build is fit to release: python3 tools/check_release.py [tag]

Confirms the version agrees everywhere it appears, and that the zip holds what
calibre needs and nothing it shouldn't, such as a real statistics database.
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, 'dist', 'KOReader Reading Stats.zip')
REQUIRED = {
    '__init__.py', 'action.py', 'config.py', 'heatmap.py', 'prefs.py', 'stats.py',
    'images/icon.png', 'LICENSE', 'plugin-import-name-koreader_stats.txt',
}


def fail(message):
    sys.exit('Release check failed: ' + message)


with open(os.path.join(ROOT, 'koreader_stats', '__init__.py'), encoding='utf-8') as f:
    match = re.search(r'version = \((\d+), (\d+), (\d+)\)', f.read())
if not match:
    fail('no version in koreader_stats/__init__.py')
version = '.'.join(match.groups())

with open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8') as f:
    latest = re.search(r'^## \[?(\d+\.\d+\.\d+)', f.read(), re.M)
if not latest or latest.group(1) != version:
    fail('CHANGELOG.md does not start with %s' % version)

if len(sys.argv) > 1 and sys.argv[1].lstrip('v') != version:
    fail('tag %s does not match version %s' % (sys.argv[1], version))

if not os.path.exists(ZIP):
    fail('build the zip first with tools/build.py')
with zipfile.ZipFile(ZIP) as z:
    names = set(z.namelist())
missing = REQUIRED - names
if missing:
    fail('zip is missing %s' % sorted(missing))
unexpected = names - REQUIRED
if unexpected:
    fail('zip has unexpected files %s' % sorted(unexpected))
print('Release check passed for', version)
