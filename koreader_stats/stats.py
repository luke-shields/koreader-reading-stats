# SPDX-License-Identifier: GPL-3.0-only
"""Find, copy and parse KOReader's statistics database.

Standard library only (no Qt, no calibre), so it can run on a worker thread and
be tested on its own.

Clock repair
------------
KOReader stamps each reading record with the device clock. E-readers can lose
that clock when the battery runs flat, restarting from a date such as
2012-01-01, and from then on every record is off by a constant amount. The
order rows were inserted in still reflects real order, so a large backward jump
between consecutive rows marks a reset. Each run of affected rows is a "segment"
and gets one offset, worked out in this order of preference:

1. A saved fix: set by the user, or measured at an earlier pull.
2. "device-clock", measured while the clock is still wrong, as the difference
   between the real time now and the device's latest own file timestamp.
3. "continuity", the fallback: assume reading resumed right where the last
   correctly dated record stopped. This is the earliest possible placement.

An offset is only a proposal. Records stay as recorded until the user decides to
move them, and that decision is saved per segment, so later records from a reset
still in progress follow it without asking again.

Moving is normally written to KOReader's own database on the device, by
apply_clock_fix_to_device, only when the user asks. For an imported file, whose
source can't be assumed to be a device, it is applied only as the data is read.
"""

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
import time
from collections import namedtuple

DB_NAME = 'statistics.sqlite3'

# Where KOReader lives, relative to the root of each kind of device's storage.
KOREADER_DIRS = (
    '.adds/koreader',          # Kobo
    'koreader',                # Kindle, Android, Cervantes, reMarkable
    'applications/koreader',   # PocketBook
    'system/koreader',         # PocketBook, older installs
)

# Files KOReader itself writes, so their timestamps come from the device clock.
DEVICE_CLOCK_FILES = (
    'settings.reader.lua',
    'crash.log',
    'history.lua',
    'settings/battery_stats.lua',
    'settings/' + DB_NAME,
    # Kobo's own settings, updated on connecting even if KOReader wasn't running.
    '../../.kobo/Kobo/Kobo eReader.conf',
)

DAY = 86400
# A backward step bigger than this is a clock reset. Rows inserted together can
# be out of order by days if a book stays open, so this cannot be too small.
BACKWARD_JUMP = 60 * DAY
# Before this date the statistics plugin barely existed: treat as a bad clock.
EARLIEST_PLAUSIBLE = int(dt.datetime(2014, 1, 1, tzinfo=dt.timezone.utc).timestamp())
MAX_TZ_SKEW = 15 * 3600

SOURCE_MANUAL = 'manual'
SOURCE_DEVICE_CLOCK = 'device-clock'
SOURCE_CONTINUITY = 'continuity'

DECISION_MOVE = 'move'
DECISION_KEEP = 'keep'


class StatsError(Exception):
    pass


PullResult = namedtuple('PullResult', 'path rows new_rows device_key')
DeviceFixResult = namedtuple('DeviceFixResult', 'rows books backup_path')


# --------------------------------------------------------------- locating

def find_stats_db(roots):
    """Return the path of the statistics database under any storage root."""
    for root in roots:
        if not root:
            continue
        for rel in KOREADER_DIRS:
            path = os.path.join(root, *rel.split('/'), 'settings', DB_NAME)
            if os.path.isfile(path):
                return path
    return None


def safe_key(text):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', text or '').strip('._') or 'device'


def read_device_uuid(root):
    """calibre records a stable per-device ID in a file on the device."""
    try:
        with open(os.path.join(root, 'driveinfo.calibre'), encoding='utf-8') as f:
            return json.load(f).get('device_store_uuid') or None
    except (OSError, ValueError, AttributeError):
        return None


# --------------------------------------------------------------- clock segments

class Segment:
    """A run of rows recorded while the device clock was wrong."""

    def __init__(self, prev_good_end):
        self.prev_good_end = prev_good_end
        self.next_good_start = None
        self.min_t = None
        self.max_t = None
        self.max_end = None
        self.rows = 0
        self.seconds = 0

    @property
    def key(self):
        # Stable across pulls: later rows extend a segment forwards only.
        return str(self.min_t)

    def add(self, t, duration):
        self.min_t = t if self.min_t is None else min(self.min_t, t)
        self.max_t = t if self.max_t is None else max(self.max_t, t)
        end = t + duration
        self.max_end = end if self.max_end is None else max(self.max_end, end)
        self.rows += 1
        self.seconds += duration


def classify_rows(rows, now):
    """Sort rows into good and clock-reset runs.

    `rows` is a list of (start_time, duration) in insertion (rowid) order.
    Returns (segments, membership), where membership[i] is the segment holding
    row i, or None for a correctly dated row.
    """
    segments, membership = [], []
    high_water = 0
    good_end = None
    current = None
    future = now + 2 * DAY
    for t, duration in rows:
        bad = t < EARLIEST_PLAUSIBLE or t > future or t < high_water - BACKWARD_JUMP
        if bad:
            if current is None or t < current.max_t - BACKWARD_JUMP:
                current = Segment(good_end)
                segments.append(current)
            current.add(t, duration)
            membership.append(current)
        else:
            if current is not None:
                current.next_good_start = t
                current = None
            high_water = max(high_water, t)
            good_end = max(good_end or 0, t + duration)
            membership.append(None)
    return segments, membership


def continuity_offset(segment, now):
    """Earliest offset consistent with the correctly dated rows around it."""
    if segment.next_good_start is not None:
        upper = segment.next_good_start - segment.max_end
    else:
        upper = now - segment.max_end
    if segment.prev_good_end is None:
        return upper
    return segment.prev_good_end + 60 - segment.min_t


def measure_device_clock_error(stats_path, segment, now):
    """Seconds the device clock is behind real time, from file timestamps.

    Only meaningful when called at connection time, with `segment` being a
    reset that is still in progress. Returns None if it can't be measured.
    """
    koreader_dir = os.path.dirname(os.path.dirname(stats_path))
    newest = None
    for rel in DEVICE_CLOCK_FILES:
        try:
            mtime = os.path.getmtime(os.path.join(koreader_dir, *rel.split('/')))
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None or newest > now - DAY:
        return None  # Timestamps look real, so nothing can be measured.

    # FAT/exFAT store local wall-clock times, so the device's time zone and
    # this computer's skew the reading. Calibrate against the database file,
    # last written at about the time of its newest row. Zones are multiples of
    # 15 minutes, which also absorbs small flush delays.
    skew = 0
    try:
        raw = os.path.getmtime(stats_path) - segment.max_end
        if abs(raw) <= MAX_TZ_SKEW:
            skew = round(raw / 900) * 900
    except OSError:
        pass

    error = int(now - (newest - skew))
    if segment.prev_good_end is not None and segment.min_t + error < segment.prev_good_end:
        return None  # Would place this reading before reading it followed.
    return error


# --------------------------------------------------------------- pulling

def device_dir(store_root, device_key):
    return os.path.join(store_root, 'devices', safe_key(device_key))


def read_device_info(dir_path):
    try:
        with open(os.path.join(dir_path, 'device.json'), encoding='utf-8') as f:
            info = json.load(f)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def write_device_info(dir_path, info):
    tmp = os.path.join(dir_path, 'device.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(info, f, indent=2, sort_keys=True)
    os.replace(tmp, os.path.join(dir_path, 'device.json'))


def _count_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute('SELECT count(*) FROM page_stat_data').fetchone()[0]
    finally:
        conn.close()


def _read_clock_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            'SELECT start_time, duration FROM page_stat_data ORDER BY rowid').fetchall()
    finally:
        conn.close()


def _snapshot(src, dest):
    """Make a validated single-file copy of src, leaving the device untouched."""
    with tempfile.TemporaryDirectory(prefix='koreader_stats_') as tmp:
        work = os.path.join(tmp, DB_NAME)
        shutil.copy2(src, work)
        for suffix in ('-wal', '-journal'):
            if os.path.exists(src + suffix):
                shutil.copy2(src + suffix, work + suffix)
        conn = out = None
        try:
            conn = sqlite3.connect(work)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'book', 'page_stat_data'} <= tables:
                raise StatsError('This is not a KOReader statistics database.')
            if conn.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                raise StatsError('The statistics database failed its integrity check.')
            out = sqlite3.connect(dest)
            conn.backup(out)
            # KOReader's copy may be in WAL mode, which the backup inherits. A
            # plain journal keeps stored copies to one file, which can be safely
            # replaced and opened read-only.
            out.execute('PRAGMA journal_mode=DELETE').fetchone()
        except sqlite3.Error as e:
            raise StatsError('Could not read the statistics database: %s' % e)
        finally:
            if out is not None:
                out.close()
            if conn is not None:
                conn.close()


def pull(src, store_root, device_key, device_name=None, now=None, live_device=True, connected_at=None):
    """Copy `src` into the store. Never writes to `src`.

    If the copy has fewer rows than the stored one (KOReader was reinstalled,
    say), the old copy is archived rather than replaced, and the viewer reads
    both, so history isn't lost.

    Set live_device=False when src is a file chosen by hand, whose timestamps
    say nothing about a device clock. connected_at is when the device connected:
    the device's file times date from then, so a later pull must measure the
    clock against it, or the correction comes out too large.
    """
    now = time.time() if now is None else now
    dir_path = device_dir(store_root, device_key)
    os.makedirs(dir_path, exist_ok=True)
    dest = os.path.join(dir_path, DB_NAME)
    tmp_dest = dest + '.tmp'
    if os.path.exists(tmp_dest):
        os.remove(tmp_dest)
    _snapshot(src, tmp_dest)

    try:
        rows = _count_rows(tmp_dest)
        previous = _count_rows(dest) if os.path.exists(dest) else 0
        if rows < previous:
            stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
            os.replace(dest, os.path.join(dir_path, 'archive-%s.sqlite3' % stamp))
        os.replace(tmp_dest, dest)
    except sqlite3.Error as e:
        raise StatsError('Could not read the statistics database: %s' % e)
    finally:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)

    info = read_device_info(dir_path)
    info['name'] = device_name or info.get('name') or device_key
    info['last_pull'] = int(now)
    fixes = info.setdefault('clock_fixes', {})
    if live_device:
        segments, membership = classify_rows(_read_clock_rows(dest), now)
        # A reset still in progress is the only kind a clock reading applies to.
        if segments and membership[-1] is segments[-1] and segments[-1].key not in fixes:
            error = measure_device_clock_error(src, segments[-1], connected_at or now)
            if error is not None:
                fixes[segments[-1].key] = {
                    'offset': error, 'source': SOURCE_DEVICE_CLOCK, 'set_at': int(now)}
    write_device_info(dir_path, info)
    return PullResult(dest, rows, max(rows - previous, 0), device_key)


def set_clock_decision(dir_path, segment_key, decision, offset, source):
    """Save what the user chose for a clock segment."""
    info = read_device_info(dir_path)
    info.setdefault('clock_fixes', {})[segment_key] = {
        'offset': int(offset), 'source': source, 'decision': decision, 'set_at': int(time.time())}
    write_device_info(dir_path, info)


def clock_backup_dir(dir_path):
    return os.path.join(os.path.dirname(os.path.dirname(dir_path)), 'backups', os.path.basename(dir_path))


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(path):
    info = os.stat(path)
    return info.st_size, info.st_mtime


def _copy_synced(src, dest):
    """Copy and force the data out to the device before anything relies on it."""
    with open(src, 'rb') as fin, open(dest, 'wb') as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
        fout.flush()
        os.fsync(fout.fileno())


def _shift_segment(conn, segment, membership, rows, offset, now):
    """Move one segment inside an open database, or raise and change nothing."""
    before = conn.execute('SELECT count(*), sum(duration) FROM page_stat_data').fetchone()
    segments_before = len(classify_rows([r[1:] for r in rows], now)[0])
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.executemany(
            'UPDATE page_stat_data SET start_time = start_time + ? WHERE rowid = ?',
            [(offset, r[0]) for r, seg in zip(rows, membership) if seg is segment])
        # Books last opened during the bad stretch carry the same wrong date.
        books = conn.execute(
            'UPDATE book SET last_open = last_open + ? WHERE last_open BETWEEN ? AND ?',
            (offset, segment.min_t - DAY, segment.max_end + DAY)).rowcount
        after = conn.execute('SELECT count(*), sum(duration) FROM page_stat_data').fetchone()
        if after != before:
            raise StatsError('The update would have changed your reading totals, so it was stopped.')
        moved = conn.execute(
            'SELECT start_time, duration FROM page_stat_data ORDER BY rowid').fetchall()
        if len(classify_rows(moved, now)[0]) >= segments_before:
            raise StatsError(
                'Those dates would still look wrong, because they overlap earlier reading or run into '
                'the future. Choose a different start.')
        if conn.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
            raise StatsError('The updated database failed its integrity check, so it was not used.')
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    return books


def apply_clock_fix_to_device(device_db, segment_key, offset, backup_dir, now=None):
    """Move a clock segment in KOReader's own database, as one all-or-nothing step.

    The live file is never edited. The work happens on a copy, which is checked,
    written beside the original, read back, and only then swapped in with a single
    rename, so an interruption at any point leaves the original or the finished file.
    """
    now = time.time() if now is None else now
    for suffix in ('-wal', '-journal'):
        side = device_db + suffix
        if os.path.exists(side) and os.path.getsize(side) > 0:
            raise StatsError(
                'KOReader may still be using its database, or did not close cleanly. Open and close '
                'KOReader on your device, then connect it again.')
    fingerprint = _fingerprint(device_db)
    temp = device_db + '.calibre-tmp'
    with tempfile.TemporaryDirectory(prefix='koreader_stats_') as tmp:
        work = os.path.join(tmp, DB_NAME)
        shutil.copyfile(device_db, work)
        conn = sqlite3.connect(work, isolation_level=None)
        try:
            rows = conn.execute(
                'SELECT rowid, start_time, duration FROM page_stat_data ORDER BY rowid').fetchall()
            segments, membership = classify_rows([r[1:] for r in rows], now)
            segment = next((s for s in segments if s.key == segment_key), None)
            if segment is None:
                raise StatsError('These records are no longer on the device with the wrong dates.')

            # Before anything changes, and exactly what is on the device now.
            os.makedirs(backup_dir, exist_ok=True)
            backup = os.path.join(
                backup_dir, 'statistics-before-device-change-%s.sqlite3' % time.strftime('%Y%m%d-%H%M%S'))
            shutil.copyfile(device_db, backup + '.tmp')
            os.replace(backup + '.tmp', backup)
            if _fingerprint(device_db) != fingerprint or _digest(backup) != _digest(device_db):
                raise StatsError('The backup could not be verified, so your device was not changed.')

            books = _shift_segment(conn, segment, membership, rows, offset, now)
        except sqlite3.IntegrityError:
            raise StatsError('Those dates clash with records already there. Choose a different start.')
        finally:
            conn.close()

        if _fingerprint(device_db) != fingerprint:
            raise StatsError('The database on your device changed during the update, so it was not replaced.')
        expected = _digest(work)
        try:
            _copy_synced(work, temp)
            if _digest(temp) != expected:
                raise StatsError('The new database did not copy to your device correctly, so it was not used.')
            os.replace(temp, device_db)
        finally:
            if os.path.exists(temp):
                os.remove(temp)
    if _digest(device_db) != expected:
        raise StatsError(
            'The database on your device does not match after the update. Restore it from the backup: %s' % backup)
    for suffix in ('-wal', '-shm'):
        if os.path.exists(device_db + suffix):
            os.remove(device_db + suffix)
    return DeviceFixResult(segment.rows, books, backup)


def ensure_clock_backup(dir_path, segment_key):
    """Back up the stored statistics once per clock problem; return the path.

    Kept beside the devices folder, not in it, so the viewer doesn't read it as
    more history. Raises OSError if the copy can't be made.
    """
    folder = clock_backup_dir(dir_path)
    info = read_device_info(dir_path)
    name = info.get('clock_backups', {}).get(segment_key)
    if name and os.path.exists(os.path.join(folder, name)):
        return os.path.join(folder, name)
    os.makedirs(folder, exist_ok=True)
    name = 'statistics-before-clock-review-%s.sqlite3' % time.strftime('%Y%m%d-%H%M%S')
    path = os.path.join(folder, name)
    shutil.copyfile(os.path.join(dir_path, DB_NAME), path + '.tmp')
    os.replace(path + '.tmp', path)
    info.setdefault('clock_backups', {})[segment_key] = name
    write_device_info(dir_path, info)
    return path


# --------------------------------------------------------------- loading

class Book:
    def __init__(self, key, title, authors):
        self.key = key
        self.title = title
        self.authors = authors
        self.seconds = 0
        self.first_ts = None
        self.last_ts = None
        self.total_pages = 0
        self.pages = set()
        self.slot = None  # Index into the chart's categorical colours.

    def touch(self, t):
        self.first_ts = t if self.first_ts is None else min(self.first_ts, t)
        self.last_ts = t if self.last_ts is None else max(self.last_ts, t)


class DayEntry:
    def __init__(self):
        self.seconds = 0
        self.pages = set()
        self.first_ts = None
        self.last_ts = None


class SegmentInfo:
    """A clock segment and how it has been dealt with, for display."""

    def __init__(self, dir_path, device_name, segment, offset, source, decision):
        self.dir_path = dir_path
        self.device_name = device_name
        self.key = segment.key
        self.min_t = segment.min_t
        self.max_end = segment.max_end
        self.seconds = segment.seconds
        self.rows = segment.rows
        self.offset = offset
        self.source = source
        self.decision = decision  # None until the user has chosen.
        self.ongoing = segment.next_good_start is None
        self.prev_good_end = segment.prev_good_end
        self.imported = os.path.basename(dir_path) == 'imported'

    @property
    def needs_review(self):
        """Unanswered, or moved only in the plugin while the device still has it wrong."""
        return self.decision is None or (self.decision == DECISION_MOVE and not self.imported)


class Dataset:
    def __init__(self):
        self.books = {}
        self.days = {}  # date -> {book key: DayEntry}
        self.segments = []
        self.devices = []
        self.last_pull = None

    @property
    def empty(self):
        return not self.days

    def years(self):
        return sorted({d.year for d in self.days})


def _normalise(text):
    return re.sub(r'[^\w]+', ' ', (text or '').casefold()).strip()


def book_key(title, authors, md5):
    """Identify a book across devices, and across re-downloaded copies."""
    first_author = (authors or '').split('\n')[0]
    key = '%s|%s' % (_normalise(title), _normalise(first_author))
    return key if key != '|' else 'md5:%s' % md5


def clean_authors(authors):
    parts = [a.strip() for a in (authors or '').split('\n') if a.strip()]
    return ', '.join(a for a in parts if a != 'N/A')


def _load_db(path, dir_path, info, ds, seen, seen_segments, now, day_start_hour):
    conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        book_rows = conn.execute(
            'SELECT id, title, authors, md5, pages FROM book').fetchall()
        rows = conn.execute(
            'SELECT id_book, page, start_time, duration, total_pages '
            'FROM page_stat_data ORDER BY rowid').fetchall()
    finally:
        conn.close()

    segments, membership = classify_rows([(r[2], r[3]) for r in rows], now)
    fixes = info.get('clock_fixes', {})
    offsets = {}
    for seg in segments:
        fix = fixes.get(seg.key)
        if fix:
            offset, source = int(fix['offset']), fix.get('source', SOURCE_MANUAL)
            decision = fix.get('decision')
        else:
            offset, source, decision = continuity_offset(seg, now), SOURCE_CONTINUITY, None
        offsets[id(seg)] = offset if decision == DECISION_MOVE else 0
        if (dir_path, seg.key) not in seen_segments:
            seen_segments.add((dir_path, seg.key))
            ds.segments.append(SegmentInfo(
                dir_path, info.get('name') or os.path.basename(dir_path), seg, offset, source, decision))

    meta = {}
    for id_book, title, authors, md5, pages in book_rows:
        key = book_key(title, authors, md5)
        book = ds.books.get(key)
        if book is None:
            book = ds.books[key] = Book(key, (title or '').strip() or '(untitled)', clean_authors(authors))
        book.total_pages = max(book.total_pages, pages or 0)
        meta[id_book] = book

    for row, seg in zip(rows, membership):
        id_book, page, raw, duration, total_pages = row
        book = meta.get(id_book)
        if book is None:
            continue
        t = raw + offsets[id(seg)] if seg is not None else raw
        # Compared after moving, so a copy from before the device was fixed still matches.
        marker = (book.key, page, t)
        if marker in seen:
            continue  # The same record, from an archived copy or second device.
        seen.add(marker)
        day = dt.date.fromtimestamp(t - day_start_hour * 3600)
        book.seconds += duration
        book.pages.add(page)
        book.touch(t)
        entry = ds.days.setdefault(day, {}).setdefault(book.key, DayEntry())
        entry.seconds += duration
        entry.pages.add(page)
        entry.first_ts = t if entry.first_ts is None else min(entry.first_ts, t)
        entry.last_ts = t if entry.last_ts is None else max(entry.last_ts, t)


def assign_colour_slots(ds, slots):
    """Give each book a colour for good, never shared within a calendar month.

    Far more books exist than distinguishable colours, but only a few are ever
    on screen at once, so books read in the same month get different colours.
    A book colliding with all of them stays None and is drawn neutral; its bar
    still carries its title.
    """
    months = {}
    for day, entries in ds.days.items():
        for key in entries:
            months.setdefault(key, set()).add((day.year, day.month))
    books = sorted((b for b in ds.books.values() if b.key in months), key=lambda b: b.first_ts)
    taken = {}  # month -> slots in use
    next_slot = 0
    for book in books:
        used = set()
        for month in months[book.key]:
            used |= taken.get(month, set())
        for i in range(slots):
            candidate = (next_slot + i) % slots
            if candidate not in used:
                book.slot = candidate
                next_slot = candidate + 1
                for month in months[book.key]:
                    taken.setdefault(month, set()).add(candidate)
                break


def hide_short_books(ds, min_seconds):
    """Leave out books only briefly opened, as if they were never read."""
    hidden = {k for k, b in ds.books.items() if b.seconds < min_seconds}
    for key in hidden:
        del ds.books[key]
    for day in list(ds.days):
        for key in hidden & ds.days[day].keys():
            del ds.days[day][key]
        if not ds.days[day]:
            del ds.days[day]


def load_dataset(store_root, now=None, slots=8, day_start_hour=0, min_book_seconds=0):
    """Merge every stored database into one Dataset.

    day_start_hour counts reading before that hour towards the previous day.
    """
    now = time.time() if now is None else now
    ds = Dataset()
    seen, seen_segments = set(), set()
    devices_root = os.path.join(store_root, 'devices')
    try:
        names = sorted(os.listdir(devices_root))
    except OSError:
        names = []
    for name in names:
        dir_path = os.path.join(devices_root, name)
        if not os.path.isdir(dir_path):
            continue
        info = read_device_info(dir_path)
        files = sorted(f for f in os.listdir(dir_path) if f.endswith('.sqlite3'))
        # The current copy first, so its records win over archived ones.
        files.sort(key=lambda f: f != DB_NAME)
        loaded = False
        for fname in files:
            try:
                _load_db(os.path.join(dir_path, fname), dir_path, info, ds, seen,
                         seen_segments, now, day_start_hour)
                loaded = True
            except sqlite3.Error:
                continue  # Skip one unreadable copy rather than show nothing.
        if loaded:
            ds.devices.append(info.get('name') or name)
            if info.get('last_pull'):
                ds.last_pull = max(ds.last_pull or 0, info['last_pull'])
    ds.books = {k: b for k, b in ds.books.items() if b.first_ts is not None}
    if min_book_seconds:
        hide_short_books(ds, min_book_seconds)
    assign_colour_slots(ds, slots)
    return ds


# --------------------------------------------------------------- summaries

def fmt_duration(seconds):
    minutes = int(round(seconds / 60))
    if seconds > 0 and minutes == 0:
        return '<1 min'
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return '%d h %d min' % (hours, minutes)
    if hours:
        return '%d h' % hours
    return '%d min' % minutes


def longest_streak(dates):
    best = run = 0
    previous = None
    for day in sorted(dates):
        run = run + 1 if previous and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    return best
