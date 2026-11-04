# SPDX-License-Identifier: GPL-3.0-only
"""The settings form, shown from the plugin's menu and calibre's plugin preferences."""

from qt.core import (
    QCheckBox, QColor, QComboBox, QFormLayout, QGroupBox, QIcon, QLabel, QPainter,
    QPixmap, QRectF, QSize, Qt, QVBoxLayout, QWidget,
)

from .heatmap import HEAT_RAMPS, HEAT_SCALES, is_dark
from .prefs import SYNC_ASK, SYNC_AUTO, SYNC_MANUAL, prefs


def ramp_icon(name):
    chip, gap = 12, 2
    pixmap = QPixmap(5 * chip + 4 * gap, chip)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    ramp = HEAT_RAMPS[name][1]
    for i, colour in enumerate(reversed(ramp) if is_dark() else ramp):
        p.setBrush(QColor(colour))
        p.drawRoundedRect(QRectF(i * (chip + gap), 0, chip, chip), 2, 2)
    p.end()
    return QIcon(pixmap)


def hour_label(hour):
    return 'Midnight' if hour == 0 else '%d am' % hour


class ConfigWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        sync = self._group(layout, 'Syncing')
        self.sync_mode = self._combo(sync, 'When a device connects:', 'sync_mode', (
            ('Copy statistics automatically', SYNC_AUTO),
            ('Ask each time', SYNC_ASK),
            ('Do nothing (manual only)', SYNC_MANUAL),
        ))
        self.sync_messages = QCheckBox('Show a status bar message after copying', self)
        self.sync_messages.setChecked(prefs['sync_messages'])
        sync.addRow(self.sync_messages)

        look = self._group(layout, 'Heat map')
        self.heat_colour = self._combo(look, 'Colour:', 'heat_colour', [
            (label, name) for name, (label, _ramp) in HEAT_RAMPS.items()])
        self.heat_colour.setIconSize(QSize(68, 12))
        for i in range(self.heat_colour.count()):
            self.heat_colour.setItemIcon(i, ramp_icon(self.heat_colour.itemData(i)))
        self.heat_scale = self._combo(look, 'Darkest shade means:', 'heat_scale', [
            ('%d hour%s or more in a day' % (m // 60, '' if m == 60 else 's'), m) for m in HEAT_SCALES])

        cal = self._group(layout, 'Calendar')
        self.start_view = self._combo(cal, 'Open in:', 'start_view', (
            ('Year view', 'year'), ('Month view', 'month')))
        self.week_start = self._combo(cal, 'Weeks start on:', 'week_start', (
            ('System default', 'locale'), ('Monday', 0), ('Sunday', 6)))
        self.day_start_hour = self._combo(cal, 'A new day starts at:', 'day_start_hour', [
            (hour_label(h), h) for h in range(0, 7)])
        self.day_start_hour.setToolTip('Late-night reading before this time counts towards the previous day.')
        self.min_book_minutes = self._combo(cal, 'Hide books read for under:', 'min_book_minutes', (
            ('Show every book', 0), ('5 minutes', 5), ('15 minutes', 15), ('30 minutes', 30)))
        self.min_book_minutes.setToolTip('Leaves out books you only opened briefly. Nothing is deleted.')

        layout.addStretch(1)

        self.keys = {
            'sync_mode': self.sync_mode, 'heat_colour': self.heat_colour, 'heat_scale': self.heat_scale,
            'start_view': self.start_view, 'week_start': self.week_start,
            'day_start_hour': self.day_start_hour, 'min_book_minutes': self.min_book_minutes,
        }

    def _group(self, layout, title):
        box = QGroupBox(title, self)
        form = QFormLayout(box)
        layout.addWidget(box)
        return form

    def _combo(self, form, label, key, options):
        combo = QComboBox(self)
        for text, value in options:
            combo.addItem(text, value)
        index = combo.findData(prefs[key])
        combo.setCurrentIndex(max(index, 0))
        form.addRow(label, combo)
        return combo

    def save_settings(self):
        for key, combo in self.keys.items():
            prefs[key] = combo.currentData()
        prefs['sync_messages'] = self.sync_messages.isChecked()
