# SPDX-License-Identifier: GPL-3.0-only
"""Toolbar and menu action: copies stats when a device connects, opens the window."""

import os
import threading
import time

from calibre.constants import config_dir
from calibre.gui2 import choose_files, error_dialog, gprefs, open_local_file, question_dialog
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.device import device_signals
from qt.core import QIcon, QMenu, QPixmap, QTimer, pyqtSignal

from . import stats
from .prefs import SYNC_ASK, SYNC_MANUAL, prefs

PLUGIN_ACTION_NAME = 'KOReader Reading Stats'
STORE_ROOT = os.path.join(config_dir, 'plugins', 'koreader_stats_data')

# calibre has separate layouts for when a device is and isn't connected.
BAR_LAYOUTS = (
    'action-layout-toolbar', 'action-layout-toolbar-device',
    'action-layout-menubar', 'action-layout-menubar-device',
)


class KOReaderStatsAction(InterfaceAction):
    name = PLUGIN_ACTION_NAME
    action_spec = ('KOReader Stats', None, 'View your KOReader reading history as a calendar', None)
    action_type = 'current'
    dont_add_to = frozenset(['context-menu-device'])

    pull_finished = pyqtSignal(object, object, object)  # (PullResult or None, error text or None, clock segments)

    def genesis(self):
        self.window = None
        self.pulled_this_connection = False
        self.pull_running = False
        self.connected_at = None
        self._place_in_bars()

        icons = self.load_resources(['images/icon.png'])
        pixmap = QPixmap()
        pixmap.loadFromData(icons['images/icon.png'])
        self.qaction.setIcon(QIcon(pixmap))
        self.qaction.triggered.connect(self.show_window)

        menu = QMenu(self.gui)
        menu.addAction('Open reading stats').triggered.connect(self.show_window)
        menu.addSeparator()
        self.pull_action = menu.addAction('Pull stats from device now')
        self.pull_action.triggered.connect(lambda: self.pull_from_device(manual=True))
        menu.addAction('Import a statistics file…').triggered.connect(self.import_file)
        menu.addSeparator()
        menu.addAction('Review device clock…').triggered.connect(self.review_clock)
        menu.addAction('Settings…').triggered.connect(self.show_settings)
        menu.addAction('Open data folder').triggered.connect(self.open_data_folder)
        self.qaction.setMenu(menu)

        self.pull_finished.connect(self._on_pull_finished)
        # This fires once calibre has read the device's ID and book list. It can
        # fire again in a session, so pulled_this_connection limits it to once.
        device_signals.device_metadata_available.connect(self._on_metadata_available)
        device_signals.device_connection_changed.connect(self._on_connection_changed)

    def _place_in_bars(self):
        """Add the action to the toolbar and menu bar, once only.

        calibre doesn't do this for plugins installed from the command line.
        Doing it just once means a user who later removes it is left alone.
        """
        if prefs['placed_in_bars']:
            return
        for key in BAR_LAYOUTS:
            layout = gprefs.get(key)
            if layout is None or self.name in layout:
                continue
            layout = list(layout)
            if 'Preferences' in layout:
                layout.insert(layout.index('Preferences'), self.name)
            else:
                layout.append(self.name)
            gprefs[key] = tuple(layout) if 'menubar' in key else layout
        prefs['placed_in_bars'] = True

    # ------------------------------------------------------------ device events

    def _on_connection_changed(self, connected):
        if connected:
            self.connected_at = time.time()
        else:
            self.connected_at = None
            self.pulled_this_connection = False

    def _on_metadata_available(self):
        mode = prefs['sync_mode']
        if self.pulled_this_connection or mode == SYNC_MANUAL:
            return
        if mode == SYNC_ASK:
            self.pulled_this_connection = True  # Only ask once per connection.
            # Don't block calibre's handling of the connection behind a dialog.
            QTimer.singleShot(0, self._ask_to_pull)
        else:
            self.pull_from_device(manual=False)

    def _ask_to_pull(self):
        device = self._connected_device()
        if device is None or stats.find_stats_db(self._storage_roots(device)) is None:
            return
        if question_dialog(self.gui, 'Copy reading stats?',
                           'Copy KOReader reading statistics from %s?' % self._device_name(device)):
            self.pull_from_device(manual=True)

    def _connected_device(self):
        manager = getattr(self.gui, 'device_manager', None)
        if manager is None or not manager.is_device_present:
            return None
        return manager.connected_device

    def _storage_roots(self, device):
        # Only drives mounted as storage expose a path; MTP and wireless don't.
        roots = [getattr(device, attr, None) for attr in ('_main_prefix', '_card_a_prefix', '_card_b_prefix')]
        return [r for r in roots if r]

    def _device_name(self, device):
        try:
            return device.get_gui_name()
        except Exception:
            return device.__class__.__name__

    def pull_from_device(self, manual=False):
        """Copy the database on a worker thread so a slow device can't block the UI."""
        if self.pull_running:
            return
        device = self._connected_device()
        if device is None:
            if manual:
                self._show_status('KOReader stats: no device connected.')
            return
        roots = self._storage_roots(device)
        src = stats.find_stats_db(roots)
        self.pulled_this_connection = True
        if src is None:
            # An ordinary reader with no KOReader on it: stay quiet unless asked.
            if manual:
                self._show_status(
                    'KOReader stats: no KOReader statistics found on this device.')
            return
        name = self._device_name(device)
        key = stats.read_device_uuid(roots[0]) or name
        self.pull_running = True
        self.pull_action.setEnabled(False)
        threading.Thread(
            target=self._pull_worker, args=(src, key, name, self.connected_at),
            name='KOReaderStatsPull', daemon=True).start()

    def _pull_worker(self, src, key, name, connected_at):
        try:
            result = stats.pull(src, STORE_ROOT, key, name, connected_at=connected_at)
            segments = self._undecided_segments(key)
        except Exception as e:  # A device can vanish mid-copy; report, never crash.
            self.pull_finished.emit(None, str(e), [])
        else:
            self.pull_finished.emit(result, None, segments)

    def _undecided_segments(self, device_key):
        folder = stats.device_dir(STORE_ROOT, device_key)
        return [s for s in stats.load_dataset(STORE_ROOT).segments
                if s.dir_path == folder and s.needs_review]

    def _device_db_for(self, segment):
        """KOReader's database on the connected device, if `segment` came from it."""
        device = self._connected_device()
        if device is None or segment.imported:
            return None
        roots = self._storage_roots(device)
        if not roots:
            return None
        key = stats.read_device_uuid(roots[0]) or self._device_name(device)
        if stats.device_dir(STORE_ROOT, key) != segment.dir_path:
            return None
        return stats.find_stats_db(roots)

    def _ask_about_clock(self, segments):
        from .heatmap import review_clock_segments
        parent = self.window if self.window is not None and self.window.isVisible() else self.gui
        if review_clock_segments(segments, parent, self._device_db_for):
            self._reload_window()

    def _on_pull_finished(self, result, error, segments):
        self.pull_running = False
        self.pull_action.setEnabled(True)
        if error:
            self._show_status('KOReader stats: could not copy statistics: %s' % error)
            return
        if prefs['sync_messages']:
            if result.new_rows:
                self._show_status('KOReader stats: copied %d new reading records.' % result.new_rows)
            else:
                self._show_status('KOReader stats: already up to date.')
        self._reload_window()
        if segments:
            # Don't block calibre's handling of the connection behind a dialog.
            QTimer.singleShot(0, lambda: self._ask_about_clock(segments))

    # ------------------------------------------------------------ menu actions

    def _show_status(self, message):
        self.gui.status_bar.show_message(message, 5000)

    def _reload_window(self):
        if self.window is not None and self.window.isVisible():
            self.window.reload()

    def show_window(self):
        from .heatmap import StatsWindow
        if self.window is None:
            self.window = StatsWindow(STORE_ROOT, self.gui)
            self.window.pull_requested.connect(lambda: self.pull_from_device(manual=True))
            self.window.import_requested.connect(self.import_file)
            self.window.settings_requested.connect(self.show_settings)
            self.window.device_db_for = self._device_db_for
            self.window.apply_start_view()
        elif self.window.isVisible():
            self.window.reload()
        else:
            self.window.reload()
            self.window.apply_start_view()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def review_clock(self):
        segments = stats.load_dataset(STORE_ROOT).segments
        if segments:
            self._ask_about_clock(segments)
        else:
            self._show_status('KOReader stats: no records with wrong clock dates.')

    def show_settings(self):
        parent = self.window if self.window is not None and self.window.isVisible() else self.gui
        if self.interface_action_base_plugin.do_user_config(parent) and self.window is not None:
            self.window.apply_settings()

    def import_file(self):
        files = choose_files(
            self.gui, 'koreader-stats-import', 'Choose a KOReader statistics file',
            filters=[('SQLite database', ['sqlite3', 'sqlite', 'db'])], all_files=True, select_only_single_file=True)
        if not files:
            return
        try:
            result = stats.pull(files[0], STORE_ROOT, 'imported', 'Imported file', live_device=False)
        except stats.StatsError as e:
            error_dialog(self.gui, 'Could not import', str(e), show=True)
            return
        self._show_status('KOReader stats: imported %d reading records.' % result.rows)
        self.show_window()
        segments = self._undecided_segments('imported')
        if segments:
            self._ask_about_clock(segments)

    def open_data_folder(self):
        os.makedirs(STORE_ROOT, exist_ok=True)
        open_local_file(STORE_ROOT)
