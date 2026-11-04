# SPDX-License-Identifier: GPL-3.0-only
"""The plugin's saved settings."""

from calibre.utils.config import JSONConfig

SYNC_AUTO, SYNC_ASK, SYNC_MANUAL = 'auto', 'ask', 'manual'

prefs = JSONConfig('plugins/koreader_stats')
prefs.defaults['placed_in_bars'] = False
prefs.defaults['sync_mode'] = SYNC_AUTO
prefs.defaults['sync_messages'] = True
prefs.defaults['heat_colour'] = 'blue'
prefs.defaults['heat_scale'] = 120       # Minutes a day for the darkest shade.
prefs.defaults['start_view'] = 'year'
prefs.defaults['week_start'] = 'locale'  # Or 0 = Monday, 6 = Sunday.
prefs.defaults['day_start_hour'] = 0
prefs.defaults['min_book_minutes'] = 0

# Version 1.1 had a single on/off switch for automatic pulls.
if prefs.get('auto_pull') is False and 'sync_mode' not in prefs:
    prefs['sync_mode'] = SYNC_MANUAL
