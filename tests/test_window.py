# SPDX-License-Identifier: GPL-3.0-only
"""Window behaviour. Run with:
QT_QPA_PLATFORM=offscreen calibre-debug tests/test_window.py

Uses a temporary calibre profile, so real settings are never touched.
"""
import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[-1])))

if 'KOREADER_STATS_TEST_PROFILE' not in os.environ:
    with tempfile.TemporaryDirectory() as profile:
        env = dict(os.environ, CALIBRE_CONFIG_DIRECTORY=profile, KOREADER_STATS_TEST_PROFILE='1')
        debug = (
            os.environ.get('CALIBRE_DEBUG') or shutil.which('calibre-debug') or
            '/Applications/calibre.app/Contents/MacOS/calibre-debug')
        sys.exit(subprocess.call([debug, sys.argv[-1]], env=env))

sys.path.insert(0, ROOT)
from calibre.constants import config_dir
if os.environ['CALIBRE_CONFIG_DIRECTORY'] != config_dir:
    raise SystemExit('Refusing to run against a real calibre profile')
from calibre.gui2 import Application, gprefs
app = Application([])
from koreader_stats import heatmap, stats


def check(condition, detail=''):
    # Not assert: calibre's bundled Python runs with assert statements stripped.
    if not condition:
        raise AssertionError(detail)


def ts(*args):
    return int(dt.datetime(*args).timestamp())


with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, 'source.sqlite3')
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE book (id integer PRIMARY KEY, title text, authors text, md5 text, pages integer);
        CREATE TABLE page_stat_data (id_book integer, page integer, start_time integer,
                                     duration integer, total_pages integer);
    ''')
    for i in range(1, 7):
        conn.execute("INSERT INTO book VALUES (?, ?, 'A', ?, 100)", (i, 'Book %d' % i, str(i)))
    conn.execute('INSERT INTO page_stat_data VALUES (1, 1, ?, 600, 100)', (ts(2025, 2, 10, 12),))
    conn.execute('INSERT INTO page_stat_data VALUES (1, 2, ?, 600, 100)', (ts(2026, 3, 10, 12),))
    # Six books on one day: more than can be drawn.
    for i in range(1, 7):
        conn.execute('INSERT INTO page_stat_data VALUES (?, 9, ?, ?, 100)', (i, ts(2026, 5, 5, 12), 100 * i))
    # Inserted last and newest: out of order by months would look like a clock reset.
    conn.execute('INSERT INTO page_stat_data VALUES (1, 3, ?, 600, 100)', (ts(2026, 8, 20, 12),))
    conn.commit()
    conn.close()
    stats.pull(path, tmp, 'k', 'Test', live_device=False)

    w = heatmap.StatsWindow(tmp)
    w.show()

    def to_month():
        w.month_button.click()
        return (w.year, w.month)

    def to_year():
        w.year_button.click()

    # With no selection, the month view opens on the latest month with reading.
    check((w.year, w.month) == (2026, 8))
    check(to_month() == (2026, 8))
    to_year()

    # A selected day decides the month.
    w._select_day(dt.date(2026, 3, 10))
    check(to_month() == (2026, 3), (w.year, w.month))
    check(w.stack.currentWidget() is w.month_view)
    check((w.month_view.year, w.month_view.month) == (2026, 3))

    # Browsing months doesn't jump back on its own.
    w.next_button.click()
    check(to_month() == (2026, 4))
    to_year()

    # A day left selected in another year is ignored.
    w.prev_button.click()
    check(w.year == 2025)
    check(to_month() == (2025, w.month) and w.month != 3)
    to_year()

    # The colouring buttons belong to the year view and are remembered.
    check(not w.heat_button.isHidden() and w.heat.colour_mode == heatmap.COLOUR_HEAT)
    w.books_button.click()
    check(w.heat.colour_mode == heatmap.COLOUR_BOOKS and gprefs[heatmap.COLOUR_MODE_PREF] == heatmap.COLOUR_BOOKS)
    check(w.heat_legend.isHidden() and not w.books_legend.isHidden())
    w.grab()  # Paints the six-book day.
    # The choice also applies in the month view.
    w.month_button.click()
    check(not w.books_button.isHidden() and w.month_view.colour_mode == heatmap.COLOUR_BOOKS)
    w.heat_button.click()
    check(w.month_view.colour_mode == heatmap.COLOUR_HEAT)
    w.books_button.click()
    w.year_button.click()
    again = heatmap.StatsWindow(tmp)
    check(again.heat.colour_mode == heatmap.COLOUR_BOOKS and again.books_button.isChecked())

    # Layers stay inside the cell, and each is offset from the last.
    for count in range(1, heatmap.MAX_BOOK_LAYERS + 1):
        layers = heatmap.book_layers(10, 20, 13, count)
        check(len(layers) == count)
        check(abs(layers[-1].right() - 23) < 1e-6 and abs(layers[-1].bottom() - 33) < 1e-6)
        check(layers[0].x() == 10 and layers[0].y() == 20)
    # Settings change what the charts draw, with no restart.
    from koreader_stats.config import ConfigWidget
    from koreader_stats.prefs import prefs
    check(heatmap.level_labels(heatmap.HEAT_SCALES[120]) == [
        'Under 15 min', '15–30 min', '30–60 min', '1–2 h', '2 h or more'])
    check(heatmap.level_labels(heatmap.HEAT_SCALES[60])[-1] == '1 h or more')
    check(heatmap.level_labels(heatmap.HEAT_SCALES[240])[1] == '30–60 min')
    for name, (_label, ramp) in heatmap.HEAT_RAMPS.items():
        check(len(ramp) == 5, name)
    form = ConfigWidget()
    check(form.sync_mode.currentData() == 'auto' and form.heat_colour.currentData() == 'blue')
    form.heat_colour.setCurrentIndex(form.heat_colour.findData('green'))
    form.heat_scale.setCurrentIndex(form.heat_scale.findData(60))
    form.week_start.setCurrentIndex(form.week_start.findData(0))
    form.min_book_minutes.setCurrentIndex(form.min_book_minutes.findData(5))
    form.start_view.setCurrentIndex(form.start_view.findData('month'))
    form.sync_mode.setCurrentIndex(form.sync_mode.findData('manual'))
    form.save_settings()
    check(prefs['heat_colour'] == 'green' and prefs['sync_mode'] == 'manual' and prefs['heat_scale'] == 60)
    check(heatmap.week_start() == 0)
    check(heatmap.heat_colour(30 * 60).name() == heatmap.HEAT_RAMPS['green'][1][3])
    w.apply_settings()
    # Book 1 has 40 min; the others have under 5, except Books 3 to 6.
    check('book 1|a' in w.view.ds.books and 'book 2|a' not in w.view.ds.books)
    w.apply_start_view()
    check(w.stack.currentWidget() is w.month_view and w.month_button.isChecked())
    form = ConfigWidget()  # A new form shows what was saved.
    check(form.heat_colour.currentData() == 'green')
    w.grab()

    # No clock trouble here, so nothing mentions it.
    check(w.clock_button.isHidden())

    # A device with a reset clock: the question, and each answer to it.
    clock_path = os.path.join(tmp, 'clock.sqlite3')
    conn = sqlite3.connect(clock_path)
    conn.executescript('''
        CREATE TABLE book (id integer PRIMARY KEY, title text, authors text, md5 text, pages integer,
                           last_open integer);
        CREATE TABLE page_stat_data (id_book integer, page integer, start_time integer,
                                     duration integer, total_pages integer);
        INSERT INTO book VALUES (1, 'Clocked', 'B', 'c', 100, 0);
    ''')
    conn.execute('INSERT INTO page_stat_data VALUES (1, 1, ?, 600, 100)', (ts(2026, 3, 3, 22),))
    conn.execute('INSERT INTO page_stat_data VALUES (1, 2, ?, 600, 100)', (ts(2012, 1, 1, 14),))
    conn.execute('INSERT INTO page_stat_data VALUES (1, 3, ?, 600, 100)', (ts(2012, 1, 31, 14),))
    conn.commit()
    conn.close()
    stats.pull(clock_path, tmp, 'clock', 'Test Kobo', live_device=False)
    w.reload()
    check(not w.clock_button.isHidden() and 'Review' in w.clock_button.text())
    seg = w.view.ds.segments[0]
    check(seg.rows == 2 and seg.decision is None)
    check(dt.date(2012, 1, 1) in w.view.ds.days)  # Unmoved until decided.

    d = heatmap.ClockFixDialog(seg, w)
    text = ' '.join(label.text() for label in d.findChildren(heatmap.QLabel))
    for expected in ('Test Kobo', '2 reading records', '1 Jan 2012 – 31 Jan 2012', 'Would move to', '3 Mar 2026',
                     'Connect your Test Kobo', 'Backup made', 'show backup', 'make your own copy'):
        check(expected in text, (expected, text))
    # With the device away it can't be changed, and the dialog says so.
    check(not d.move_button.isEnabled())
    backups = os.path.join(tmp, 'backups', 'clock')
    check(len(os.listdir(backups)) == 1)

    # If the backup can't be made, the dialog must say so rather than claim it was.
    real_backup = heatmap.ensure_clock_backup
    def fail(*_args):
        raise OSError('disk full')
    heatmap.ensure_clock_backup = fail
    failed = heatmap.ClockFixDialog(seg, w)
    check('Backup failed' in failed.backup_label.text() and 'disk full' in failed.backup_label.text())
    check('Backup made' not in failed.backup_label.text())
    failed.reject()
    heatmap.ensure_clock_backup = real_backup
    # The end follows the start, and the stretch keeps its length.
    d.start_edit.setDateTime(heatmap.QDateTime.fromSecsSinceEpoch(ts(2026, 4, 1, 12)))
    check('1 May 2026' in d.end_label.text(), d.end_label.text())
    d.reject()  # Decide later changes nothing.
    w.reload()
    check(w.view.ds.segments[0].decision is None)

    # Connected, it says plainly that the device is what changes.
    d = heatmap.ClockFixDialog(w.view.ds.segments[0], w, clock_path)
    check(d.move_button.isEnabled() and d.move_button.text() == 'Change records on device')
    check("changes KOReader's statistics database on your device" in d.change_label.text())
    check('just before it is changed' in d.backup_label.text())

    def device_years():
        c = sqlite3.connect(clock_path)
        years = {dt.date.fromtimestamp(r[0]).year for r in c.execute('SELECT start_time FROM page_stat_data')}
        c.close()
        return years

    # Keep leaves the device alone.
    d.keep_button.click()
    w.reload()
    check(w.view.ds.segments[0].decision == stats.DECISION_KEEP)
    check(not w.view.ds.segments[0].needs_review)
    check(2012 in device_years() and dt.date(2012, 1, 1) in w.view.ds.days)
    check('Review' not in w.clock_button.text() and not w.clock_button.isHidden())

    # A refused write stays on the question, reports it, and changes nothing.
    d = heatmap.ClockFixDialog(w.view.ds.segments[0], w, clock_path)
    d.start_edit.setDateTime(heatmap.QDateTime.fromSecsSinceEpoch(ts(2030, 1, 1, 12)))
    d.move_button.click()
    check(not d.error_label.isHidden() and 'was not changed' in d.error_label.text())
    check(d.result_message is None and 2012 in device_years())
    w.reload()
    check(w.view.ds.segments[0].decision == stats.DECISION_KEEP)

    # A chosen start, written to the device.
    d = heatmap.ClockFixDialog(w.view.ds.segments[0], w, clock_path)
    d.start_edit.setDateTime(heatmap.QDateTime.fromSecsSinceEpoch(ts(2026, 6, 1, 12)))
    d.move_button.click()
    check(d.error_label.isHidden(), d.error_label.text())
    check('2 reading records' in d.result_message and 'statistics-before-device-change' in d.result_message)
    check(device_years() == {2026})
    w.reload()
    check(not w.view.ds.segments and w.clock_button.isHidden())
    check(dt.date(2026, 6, 1) in w.view.ds.days and not any(day.year == 2012 for day in w.view.ds.days))
    check(any(n.startswith('statistics-before-device-change') for n in os.listdir(backups)))

    # An imported file is never written to, and says so.
    imported_path = os.path.join(tmp, 'imported.sqlite3')
    conn = sqlite3.connect(imported_path)
    conn.executescript('''
        CREATE TABLE book (id integer PRIMARY KEY, title text, authors text, md5 text, pages integer);
        CREATE TABLE page_stat_data (id_book integer, page integer, start_time integer,
                                     duration integer, total_pages integer);
        INSERT INTO book VALUES (1, 'Imported', 'C', 'd', 100);
    ''')
    conn.execute('INSERT INTO page_stat_data VALUES (1, 1, ?, 600, 100)', (ts(2013, 1, 1, 14),))
    conn.commit()
    conn.close()
    with open(imported_path, 'rb') as f:
        imported_before = f.read()
    stats.pull(imported_path, tmp, 'imported', 'Imported file', live_device=False)
    w.reload()
    seg = next(s for s in w.view.ds.segments if s.imported)
    d = heatmap.ClockFixDialog(seg, w)
    check(d.move_button.isEnabled() and d.move_button.text() == 'Move in plugin only')
    check('only changes what this plugin shows' in d.change_label.text())
    d.move_button.click()
    with open(imported_path, 'rb') as f:
        check(f.read() == imported_before)
    w.reload()
    seg = next(s for s in w.view.ds.segments if s.imported)
    check(seg.decision == stats.DECISION_MOVE and not seg.needs_review)

    w.close()
    again.close()
    del w, again

print('window tests OK')
sys.stdout.flush()
os._exit(0)
