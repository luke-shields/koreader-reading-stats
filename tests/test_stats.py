# SPDX-License-Identifier: GPL-3.0-only
"""Run with: python3 tests/test_stats.py  (standard library only)."""
import datetime as dt
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    'stats', os.path.join(HERE, '..', 'koreader_stats', 'stats.py'))
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)

DAY = 86400
NOW = int(dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc).timestamp())


def ts(*args):
    return int(dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp())


def make_db(path, rows, books=((1, 'Book One', 'Ann Author', 'aa'),)):
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE book (id integer PRIMARY KEY, title text, authors text, notes integer,
            last_open integer, highlights integer, pages integer, series text, language text,
            md5 text, total_read_time integer, total_read_pages integer);
        CREATE TABLE page_stat_data (id_book integer, page integer, start_time integer,
            duration integer, total_pages integer);
    ''')
    for id_, title, authors, md5 in books:
        conn.execute('INSERT INTO book (id, title, authors, md5, pages) VALUES (?,?,?,?,100)',
                     (id_, title, authors, md5))
    conn.executemany('INSERT INTO page_stat_data VALUES (1,1,?,?,100)', rows)
    conn.commit()
    conn.close()


class Classify(unittest.TestCase):
    def test_clean_history_has_no_segments(self):
        rows = [(ts(2025, 1, 1) + i * DAY, 60) for i in range(10)]
        segments, membership = stats.classify_rows(rows, NOW)
        self.assertEqual(segments, [])
        self.assertEqual(membership, [None] * 10)

    def test_long_reading_break_is_not_a_reset(self):
        rows = [(ts(2025, 1, 1), 60), (ts(2026, 1, 1), 60)]
        self.assertEqual(stats.classify_rows(rows, NOW)[0], [])

    def test_small_shuffle_is_not_a_reset(self):
        # A book left open for days can flush rows slightly out of order.
        rows = [(ts(2025, 3, 10), 60), (ts(2025, 3, 2), 60), (ts(2025, 3, 11), 60)]
        self.assertEqual(stats.classify_rows(rows, NOW)[0], [])

    def test_reset_in_progress(self):
        rows = [(ts(2026, 3, 3), 60), (ts(2012, 1, 1), 60), (ts(2012, 8, 1), 60)]
        segments, membership = stats.classify_rows(rows, NOW)
        self.assertEqual(len(segments), 1)
        self.assertIsNone(membership[0])
        self.assertIs(membership[2], segments[0])
        self.assertEqual(segments[0].prev_good_end, ts(2026, 3, 3) + 60)
        self.assertIsNone(segments[0].next_good_start)

    def test_reset_then_clock_fixed(self):
        rows = [(ts(2026, 3, 3), 60), (ts(2012, 1, 1), 60), (ts(2026, 4, 1), 60)]
        segments, membership = stats.classify_rows(rows, NOW)
        self.assertEqual(segments[0].next_good_start, ts(2026, 4, 1))
        self.assertIsNone(membership[2])

    def test_two_resets(self):
        rows = [(ts(2026, 1, 1), 60), (ts(2012, 1, 1), 60), (ts(2012, 6, 1), 60), (ts(2012, 1, 1, 8), 60)]
        self.assertEqual(len(stats.classify_rows(rows, NOW)[0]), 2)

    def test_history_starting_with_bad_clock(self):
        rows = [(ts(2012, 1, 1), 60), (ts(2025, 1, 1), 60)]
        segments, membership = stats.classify_rows(rows, NOW)
        self.assertIs(membership[0], segments[0])
        self.assertIsNone(membership[1])
        self.assertIsNone(segments[0].prev_good_end)
        # Nothing before it, so it lands just before the good row that follows.
        self.assertEqual(ts(2012, 1, 1) + 60 + stats.continuity_offset(segments[0], NOW), ts(2025, 1, 1))

    def test_future_clock_is_bad(self):
        rows = [(ts(2025, 1, 1), 60), (ts(2099, 1, 1), 60)]
        self.assertEqual(len(stats.classify_rows(rows, NOW)[0]), 1)


def move_all(store, now):
    for seg in stats.load_dataset(store, now).segments:
        stats.set_clock_decision(seg.dir_path, seg.key, stats.DECISION_MOVE, seg.offset, seg.source)
    return stats.load_dataset(store, now)


class Pull(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.settings = os.path.join(self.tmp, 'dev', '.adds', 'koreader', 'settings')
        os.makedirs(self.settings)
        self.src = os.path.join(self.settings, stats.DB_NAME)
        self.store = os.path.join(self.tmp, 'store')

    def test_finds_kobo_path(self):
        make_db(self.src, [(ts(2025, 1, 1), 60)])
        self.assertEqual(stats.find_stats_db([None, os.path.join(self.tmp, 'dev')]), self.src)
        self.assertIsNone(stats.find_stats_db([self.store]))

    def test_rejects_other_sqlite(self):
        conn = sqlite3.connect(self.src)
        conn.execute('CREATE TABLE x (y)')
        conn.close()
        with self.assertRaises(stats.StatsError):
            stats.pull(self.src, self.store, 'k', now=NOW)
        self.assertFalse(os.path.exists(os.path.join(stats.device_dir(self.store, 'k'), stats.DB_NAME)))

    def test_rejects_garbage_keeping_existing_copy(self):
        make_db(self.src, [(ts(2025, 1, 1), 60)])
        stats.pull(self.src, self.store, 'k', now=NOW)
        os.remove(self.src)
        with open(self.src, 'wb') as f:
            f.write(b'not a database' * 100)
        with self.assertRaises(stats.StatsError):
            stats.pull(self.src, self.store, 'k', now=NOW)
        self.assertEqual(len(stats.load_dataset(self.store, NOW).days), 1)

    def test_does_not_change_source(self):
        make_db(self.src, [(ts(2025, 1, 1), 60)])
        with open(self.src, 'rb') as f:
            before = f.read()
        mtime = os.path.getmtime(self.src)
        stats.pull(self.src, self.store, 'k', now=NOW)
        with open(self.src, 'rb') as f:
            self.assertEqual(before, f.read())
        self.assertEqual(mtime, os.path.getmtime(self.src))
        self.assertEqual(sorted(os.listdir(self.settings)), [stats.DB_NAME])

    def test_wal_source_is_read_in_full_and_stored_as_one_file(self):
        make_db(self.src, [(ts(2025, 1, 1), 60)])
        writer = sqlite3.connect(self.src)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL').fetchone()
        writer.execute('PRAGMA wal_autocheckpoint=0').fetchone()
        writer.execute('INSERT INTO page_stat_data VALUES (1,2,?,60,100)', (ts(2025, 1, 2),))
        writer.commit()  # This row exists only in the -wal file.
        self.assertTrue(os.path.exists(self.src + '-wal'))
        self.assertEqual(stats.pull(self.src, self.store, 'k', now=NOW).rows, 2)
        self.assertEqual(len(stats.load_dataset(self.store, NOW).days), 2)
        stored = stats.device_dir(self.store, 'k')
        self.assertEqual(sorted(os.listdir(stored)), ['device.json', stats.DB_NAME])

    def test_shrunken_db_archives_and_keeps_history(self):
        make_db(self.src, [(ts(2025, 1, 1) + i * DAY, 60) for i in range(5)])
        stats.pull(self.src, self.store, 'k', now=NOW)
        os.remove(self.src)
        make_db(self.src, [(ts(2026, 1, 1), 60)])
        result = stats.pull(self.src, self.store, 'k', now=NOW)
        self.assertEqual(result.rows, 1)
        self.assertEqual(len(stats.load_dataset(self.store, NOW).days), 6)

    def _broken_clock_device(self):
        make_db(self.src, [(ts(2026, 3, 3, 22), 60), (ts(2012, 1, 1, 14), 60), (ts(2012, 8, 26, 13, 47), 60)])
        # The 2 h skew between file time and row time is the zone calibration.
        clock = ts(2012, 8, 26, 15, 48)
        os.utime(self.src, (clock, clock))
        os.utime(self.settings, (clock, clock))
        lua = os.path.join(os.path.dirname(self.settings), 'settings.reader.lua')
        open(lua, 'w').close()
        connected = ts(2012, 8, 27, 3)
        os.utime(lua, (connected, connected))

    def test_device_clock_offset_is_measured_and_kept(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        ds = stats.load_dataset(self.store, NOW)
        self.assertEqual(ds.segments[0].source, stats.SOURCE_DEVICE_CLOCK)
        ds = move_all(self.store, NOW)
        # Connected at 03:00 skewed (01:00 true device time), really 09:00 Nov 4,
        # so the last 2012 row (13:47 on Aug 26) was really 21:47 on Nov 3.
        last = max(b.last_ts for b in ds.books.values())
        self.assertEqual(dt.datetime.fromtimestamp(last, dt.timezone.utc),
                         dt.datetime(2026, 11, 3, 21, 47, tzinfo=dt.timezone.utc))
        # The clock later gets fixed: the measured offset must not change.
        offset = ds.segments[0].offset
        stats.pull(self.src, self.store, 'k', now=NOW + 30 * DAY, live_device=False)
        self.assertEqual(stats.load_dataset(self.store, NOW + 30 * DAY).segments[0].offset, offset)

    def test_late_pull_measures_from_connection_time(self):
        self._broken_clock_device()
        # Connected at NOW, but pulled by hand six hours later.
        stats.pull(self.src, self.store, 'k', now=NOW + 6 * 3600, connected_at=NOW)
        ds = move_all(self.store, NOW + 6 * 3600)
        last = max(b.last_ts for b in ds.books.values())
        self.assertEqual(dt.datetime.fromtimestamp(last, dt.timezone.utc),
                         dt.datetime(2026, 11, 3, 21, 47, tzinfo=dt.timezone.utc))

    def test_falls_back_to_continuity_without_clock_files(self):
        make_db(self.src, [(ts(2026, 3, 3, 22), 60), (ts(2012, 1, 1, 14), 60)])
        stats.pull(self.src, self.store, 'k', now=NOW, live_device=False)
        ds = stats.load_dataset(self.store, NOW)
        self.assertEqual(ds.segments[0].source, stats.SOURCE_CONTINUITY)
        ds = move_all(self.store, NOW)
        self.assertEqual(sorted(ds.days)[-1], dt.date.fromtimestamp(ts(2026, 3, 3, 22, 2)))

    def test_nothing_moves_until_decided(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        ds = stats.load_dataset(self.store, NOW)
        seg = ds.segments[0]
        self.assertIsNone(seg.decision)
        self.assertTrue(seg.ongoing)
        self.assertEqual(seg.rows, 2)
        self.assertTrue(any(d.year == 2012 for d in ds.days))
        self.assertGreater(seg.min_t + seg.offset, ts(2026, 1, 1))  # The proposal.

    def test_decisions_are_kept_and_can_change(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        seg = stats.load_dataset(self.store, NOW).segments[0]
        stats.set_clock_decision(seg.dir_path, seg.key, stats.DECISION_KEEP, seg.offset, seg.source)
        # A later pull neither asks again nor forgets the proposal.
        stats.pull(self.src, self.store, 'k', now=NOW + DAY)
        ds = stats.load_dataset(self.store, NOW + DAY)
        self.assertEqual(ds.segments[0].decision, stats.DECISION_KEEP)
        self.assertEqual(ds.segments[0].offset, seg.offset)
        self.assertTrue(any(d.year == 2012 for d in ds.days))
        stats.set_clock_decision(seg.dir_path, seg.key, stats.DECISION_MOVE, ts(2026, 6, 1) - seg.min_t, stats.SOURCE_MANUAL)
        ds = stats.load_dataset(self.store, NOW + DAY)
        self.assertFalse(any(d.year == 2012 for d in ds.days))
        self.assertIn(dt.date.fromtimestamp(ts(2026, 6, 1)), ds.days)

    def test_backup_is_made_once_and_not_read_as_history(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        before = stats.load_dataset(self.store, NOW)
        seg = before.segments[0]
        path = stats.ensure_clock_backup(seg.dir_path, seg.key)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute('SELECT count(*) FROM page_stat_data').fetchone()[0], 3)
        conn.close()
        self.assertEqual(stats.ensure_clock_backup(seg.dir_path, seg.key), path)
        self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])
        after = stats.load_dataset(self.store, NOW)
        self.assertEqual(sum(b.seconds for b in after.books.values()),
                         sum(b.seconds for b in before.books.values()))
        # Later pulls keep the original backup, from before any decision.
        stats.pull(self.src, self.store, 'k', now=NOW + DAY)
        self.assertEqual(stats.ensure_clock_backup(seg.dir_path, seg.key), path)

    def test_making_a_decision_leaves_every_database_unchanged(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        seg = stats.load_dataset(self.store, NOW).segments[0]
        stored = os.path.join(seg.dir_path, stats.DB_NAME)
        def contents():
            return [open(p, 'rb').read() for p in (self.src, stored)]
        before = contents()
        stats.set_clock_decision(seg.dir_path, seg.key, stats.DECISION_MOVE, seg.offset, seg.source)
        stats.load_dataset(self.store, NOW)
        self.assertEqual(contents(), before)

    def test_backup_failure_raises(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        seg = stats.load_dataset(self.store, NOW).segments[0]
        os.remove(os.path.join(seg.dir_path, stats.DB_NAME))
        with self.assertRaises(OSError):
            stats.ensure_clock_backup(seg.dir_path, seg.key)

    def test_fix_saved_by_older_version_needs_a_decision(self):
        self._broken_clock_device()
        stats.pull(self.src, self.store, 'k', now=NOW)
        info = stats.read_device_info(stats.device_dir(self.store, 'k'))
        fix = next(iter(info['clock_fixes'].values()))
        self.assertNotIn('decision', fix)  # The same shape as 1.0 to 1.2 saved.
        seg = stats.load_dataset(self.store, NOW).segments[0]
        self.assertIsNone(seg.decision)
        self.assertEqual(seg.offset, fix['offset'])


class DeviceWrite(unittest.TestCase):
    OFFSET = ts(2026, 3, 10, 22) - ts(2012, 1, 1, 14)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, stats.DB_NAME)
        self.backups = os.path.join(self.tmp, 'backups')
        make_db(self.db, [(ts(2026, 3, 3, 22), 60), (ts(2012, 1, 1, 14), 60), (ts(2012, 8, 1, 9), 120)])
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE book SET last_open = ?', (ts(2012, 8, 1, 9),))
        conn.commit()
        self.assertEqual(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        conn.close()
        self.key = stats.classify_rows(self.rows(), NOW)[0][0].key

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute('SELECT start_time, duration FROM page_stat_data ORDER BY rowid').fetchall()
        finally:
            conn.close()

    def raw(self):
        with open(self.db, 'rb') as f:
            return f.read()

    def apply(self, offset=None):
        return stats.apply_clock_fix_to_device(self.db, self.key, offset or self.OFFSET, self.backups, now=NOW)

    def test_moves_only_the_bad_rows(self):
        result = self.apply()
        self.assertEqual(result.rows, 2)
        self.assertEqual(result.books, 1)
        self.assertEqual(self.rows(), [
            (ts(2026, 3, 3, 22), 60), (ts(2012, 1, 1, 14) + self.OFFSET, 60), (ts(2012, 8, 1, 9) + self.OFFSET, 120)])
        self.assertEqual(stats.classify_rows(self.rows(), NOW)[0], [])
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT last_open FROM book').fetchone()[0], ts(2012, 8, 1, 9) + self.OFFSET)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')  # As KOReader had it.
        conn.close()

    def test_backup_is_the_untouched_original(self):
        before = self.raw()
        result = self.apply()
        with open(result.backup_path, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertNotEqual(self.raw(), before)

    def test_leaves_nothing_extra_on_the_device(self):
        self.apply()
        self.assertEqual(sorted(f for f in os.listdir(self.tmp) if f != 'backups'), [stats.DB_NAME])

    def test_second_apply_refuses_and_changes_nothing(self):
        self.apply()
        before = self.raw()
        with self.assertRaises(stats.StatsError):
            self.apply()
        self.assertEqual(self.raw(), before)

    def assert_refused_unchanged(self, offset=None):
        before = self.raw()
        with self.assertRaises(stats.StatsError):
            self.apply(offset)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.db + '.calibre-tmp'))

    def test_refuses_dates_in_the_future(self):
        self.assert_refused_unchanged(self.OFFSET + 400 * DAY)

    def test_refuses_dates_overlapping_earlier_reading(self):
        self.assert_refused_unchanged(self.OFFSET - 200 * DAY)

    def test_refuses_while_database_may_be_in_use(self):
        with open(self.db + '-wal', 'wb') as f:
            f.write(b'unfinished')
        self.assert_refused_unchanged()

    def test_refuses_clashing_records(self):
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE UNIQUE INDEX u ON page_stat_data (id_book, page, start_time)')
        conn.execute('DELETE FROM page_stat_data WHERE start_time = ?', (ts(2026, 3, 3, 22),))
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO page_stat_data VALUES (1,1,?,60,100)', (ts(2012, 1, 1, 14) + self.OFFSET,))
        conn.commit()
        conn.close()
        self.key = stats.classify_rows(self.rows(), NOW)[0][0].key
        self.assert_refused_unchanged()

    def test_interrupted_swap_keeps_the_original(self):
        before = self.raw()
        real = stats.os.replace
        def fail(src, dst):
            if dst == self.db:
                raise OSError('device removed')
            return real(src, dst)
        stats.os.replace = fail
        self.addCleanup(setattr, stats.os, 'replace', real)
        with self.assertRaises(OSError):
            self.apply()
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.db + '.calibre-tmp'))

    def test_failed_copy_to_device_keeps_the_original(self):
        before = self.raw()
        real = stats._copy_synced
        def corrupt(src, dest):
            real(src, dest)
            with open(dest, 'r+b') as f:
                f.seek(200)
                f.write(b'x')
        stats._copy_synced = corrupt
        self.addCleanup(setattr, stats, '_copy_synced', real)
        with self.assertRaises(stats.StatsError):
            self.apply()
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.db + '.calibre-tmp'))

    def test_no_double_count_from_a_copy_made_before_the_fix(self):
        store = os.path.join(self.tmp, 'store')
        stats.pull(self.db, store, 'k', now=NOW, live_device=False)
        folder = stats.device_dir(store, 'k')
        shutil.copyfile(os.path.join(folder, stats.DB_NAME), os.path.join(folder, 'archive-old.sqlite3'))
        total = sum(b.seconds for b in stats.load_dataset(store, NOW).books.values())
        stats.set_clock_decision(folder, self.key, stats.DECISION_MOVE, self.OFFSET, stats.SOURCE_MANUAL)
        self.apply()
        stats.pull(self.db, store, 'k', now=NOW, live_device=False)
        ds = stats.load_dataset(store, NOW)
        self.assertEqual(sum(b.seconds for b in ds.books.values()), total)
        self.assertFalse(any(d.year == 2012 for d in ds.days))


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _pull(self, name, rows, books):
        path = os.path.join(self.tmp, name)
        make_db(path, rows, books)
        stats.pull(path, self.tmp, name, now=NOW, live_device=False)

    def test_same_book_on_two_devices_merges(self):
        t = ts(2025, 1, 1, 12)
        self._pull('a', [(t, 60)], [(1, 'The Book', 'Ann', 'x')])
        self._pull('b', [(t + 3600, 30)], [(1, 'the book', 'ANN', 'y')])
        ds = stats.load_dataset(self.tmp, NOW)
        self.assertEqual(len(ds.books), 1)
        self.assertEqual(list(ds.books.values())[0].seconds, 90)

    def test_colours_never_collide_within_a_month(self):
        t = ts(2025, 1, 10)
        books = [(i, 'Book %d' % i, 'A', str(i)) for i in range(1, 9)]
        conn_rows = ()
        path = os.path.join(self.tmp, 'c')
        make_db(path, conn_rows, books)
        conn = sqlite3.connect(path)
        for i in range(1, 9):
            conn.execute('INSERT INTO page_stat_data VALUES (?,1,?,60,100)', (i, t + i))
        conn.commit()
        conn.close()
        stats.pull(path, self.tmp, 'c', now=NOW, live_device=False)
        ds = stats.load_dataset(self.tmp, NOW, slots=8)
        self.assertEqual(len({b.slot for b in ds.books.values()}), 8)
        ds = stats.load_dataset(self.tmp, NOW, slots=3)
        # Too many books for the colours: extras stay neutral, none share one.
        filled = [b.slot for b in ds.books.values() if b.slot is not None]
        self.assertEqual(len(filled), 3)
        self.assertEqual(len(set(filled)), 3)


class Views(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        path = os.path.join(self.tmp, 'src')
        make_db(path, [], ((1, 'Long', 'A', 'a'), (2, 'Glance', 'A', 'b')))
        self.late = int(dt.datetime(2025, 3, 2, 1, 30).timestamp())  # Local time.
        conn = sqlite3.connect(path)
        conn.execute('INSERT INTO page_stat_data VALUES (1,1,?,3600,100)', (self.late,))
        conn.execute('INSERT INTO page_stat_data VALUES (2,1,?,120,100)', (self.late + 7200,))
        conn.commit()
        conn.close()
        stats.pull(path, self.tmp, 'k', now=NOW, live_device=False)

    def test_day_start(self):
        self.assertEqual(list(stats.load_dataset(self.tmp, NOW).days), [dt.date(2025, 3, 2)])
        late = stats.load_dataset(self.tmp, NOW, day_start_hour=3)
        self.assertEqual(sorted(late.days), [dt.date(2025, 3, 1), dt.date(2025, 3, 2)])

    def test_hide_short_books(self):
        ds = stats.load_dataset(self.tmp, NOW, min_book_seconds=300)
        self.assertEqual([b.title for b in ds.books.values()], ['Long'])
        self.assertEqual(list(ds.days[dt.date(2025, 3, 2)]), ['long|a'])
        # Nothing is lost: it comes back when the setting is off.
        self.assertEqual(len(stats.load_dataset(self.tmp, NOW).books), 2)

    def test_hiding_removes_days_left_empty(self):
        self.assertEqual(stats.load_dataset(self.tmp, NOW, min_book_seconds=99999).days, {})


class Helpers(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(stats.fmt_duration(10), '<1 min')
        self.assertEqual(stats.fmt_duration(0), '0 min')
        self.assertEqual(stats.fmt_duration(3600), '1 h')
        self.assertEqual(stats.fmt_duration(4320), '1 h 12 min')

    def test_streak(self):
        d = dt.date
        self.assertEqual(stats.longest_streak([d(2025, 1, 1), d(2025, 1, 2), d(2025, 1, 5)]), 2)
        self.assertEqual(stats.longest_streak([]), 0)


if __name__ == '__main__':
    unittest.main()
