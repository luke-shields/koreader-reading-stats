# SPDX-License-Identifier: GPL-3.0-only
"""Draw README screenshots from made-up data, never a real reading history.

Run with: QT_QPA_PLATFORM=offscreen CALIBRE_CONFIG_DIRECTORY=<empty temp folder> calibre-debug tools/make_screenshots.py
"""
import datetime as dt
import os
import random
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[-1])))
OUT = os.path.join(ROOT, 'docs', 'screenshots')
sys.path.insert(0, ROOT)

from calibre.constants import config_dir

if os.environ.get('CALIBRE_CONFIG_DIRECTORY') != config_dir:
    raise SystemExit('Set CALIBRE_CONFIG_DIRECTORY to an empty temporary folder')

from calibre.gui2 import Application

app = Application([])
from koreader_stats import config, heatmap, stats

# All long out of copyright, and nothing to do with anyone's real reading.
BOOKS = [
    ('Middlemarch', 'George Eliot', 880), ('Moby-Dick', 'Herman Melville', 640), ('Emma', 'Jane Austen', 470),
    ('Dracula', 'Bram Stoker', 420), ('The Time Machine', 'H. G. Wells', 120), ('Persuasion', 'Jane Austen', 260),
    ('Great Expectations', 'Charles Dickens', 520), ('Frankenstein', 'Mary Shelley', 280),
    ('Jane Eyre', 'Charlotte Brontë', 530), ('The Odyssey', 'Homer', 410), ('Bleak House', 'Charles Dickens', 960),
    ('Walden', 'Henry David Thoreau', 300), ('Treasure Island', 'R. L. Stevenson', 240),
    ('Wuthering Heights', 'Emily Brontë', 380), ('Ivanhoe', 'Walter Scott', 590), ('Cranford', 'Elizabeth Gaskell', 220),
]
rng = random.Random(7)
today = dt.date.today()


def stamp(day, hour, minute=0):
    return int(dt.datetime(day.year, day.month, day.day, hour, minute).timestamp())


with tempfile.TemporaryDirectory() as tmp:
    volume = os.path.join(tmp, 'device')
    settings = os.path.join(volume, '.adds', 'koreader', 'settings')
    os.makedirs(settings)
    db = os.path.join(settings, stats.DB_NAME)
    conn = sqlite3.connect(db)
    conn.executescript('''
        CREATE TABLE book (id integer PRIMARY KEY, title text, authors text, md5 text, pages integer, last_open integer);
        CREATE TABLE page_stat_data (id_book integer, page integer, start_time integer,
                                     duration integer, total_pages integer);
    ''')
    # Each book is read once, in order, ending about now, as a real history would.
    rows, day, book, page = [], today - dt.timedelta(days=175), 0, 1
    while day < today - dt.timedelta(days=1) and book < len(BOOKS):
        day += dt.timedelta(days=1)
        if rng.random() < 0.22:
            continue
        minutes = rng.choice((8, 15, 22, 30, 40, 55, 70, 95, 130))
        for i in range(minutes):
            if book >= len(BOOKS):
                break
            total = BOOKS[book][2]
            rows.append((book + 1, page, stamp(day, 21) + i * 60, 60, total))
            page += 1
            if page > total:
                book, page = book + 1, 1
        if book + 1 < len(BOOKS) and rng.random() < 0.15:
            rows += [(book + 2, 1 + i, stamp(day, 7) + i * 60, 60, BOOKS[book + 1][2]) for i in range(rng.randint(10, 35))]
    conn.executemany("INSERT INTO book VALUES (?, ?, ?, ?, ?, 0)", [
        (i + 1, b[0], b[1], 'md5%d' % i, b[2]) for i, b in enumerate(BOOKS)])
    conn.executemany('INSERT OR IGNORE INTO page_stat_data VALUES (?, ?, ?, ?, ?)', sorted(rows, key=lambda r: r[2]))
    conn.commit()
    conn.close()
    store = os.path.join(tmp, 'store')
    stats.pull(db, store, 'demo', 'Kobo Libra H2O', live_device=False)

    w = heatmap.StatsWindow(store)
    w.resize(1200, 720)
    w.show()
    app.processEvents()

    def shot(widget, name):
        app.processEvents()
        app.processEvents()
        widget.grab().save(os.path.join(OUT, name))

    days = w.view.ds.days
    w._select_day(max(d for d, entries in days.items() if len(entries) > 1))
    shot(w, 'year-heat-map.png')
    w.books_button.click()
    shot(w, 'year-books.png')
    w.year_button.click()
    w.month_button.click()
    shot(w, 'month.png')
    settings_form = config.ConfigWidget()
    settings_form.show()
    shot(settings_form, 'settings.png')
    w.close()

    # A separate made-up device, to show the clock pop-up.
    clock_db = os.path.join(tmp, 'clock.sqlite3')
    conn = sqlite3.connect(clock_db)
    conn.executescript('''
        CREATE TABLE book (id integer PRIMARY KEY, title text, authors text, md5 text, pages integer, last_open integer);
        CREATE TABLE page_stat_data (id_book integer, page integer, start_time integer,
                                     duration integer, total_pages integer);
        INSERT INTO book VALUES (1, 'Emma', 'Jane Austen', 'e', 470, 0);
    ''')
    clock_rows = [(i + 1, stamp(today - dt.timedelta(days=200), 21) + i * 60) for i in range(300)]
    start = dt.date(2012, 1, 1)
    clock_rows += [(i + 1, stamp(start + dt.timedelta(days=i // 40), 21) + (i % 40) * 60) for i in range(4200)]
    conn.executemany('INSERT INTO page_stat_data VALUES (1, ?, ?, 60, 470)', clock_rows)
    conn.commit()
    conn.close()
    clock_store = os.path.join(tmp, 'clock-store')
    stats.pull(clock_db, clock_store, 'demo', 'Kobo Libra H2O', live_device=False)
    segment = stats.load_dataset(clock_store).segments[0]
    dialog = heatmap.ClockFixDialog(segment, None, clock_db)
    dialog.show()
    shot(dialog, 'clock-popup.png')
    dialog.close()

print('Screenshots written to', OUT)
sys.stdout.flush()
os._exit(0)
