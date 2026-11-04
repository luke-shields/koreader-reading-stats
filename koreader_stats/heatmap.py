# SPDX-License-Identifier: GPL-3.0-only
"""The reading statistics window: a year heat map and a month calendar."""

import calendar
import datetime as dt
import html
import os
import sqlite3

from qt.core import (
    QAbstractItemView, QApplication, QButtonGroup, QColor, QDateTime, QDateTimeEdit,
    QDialog, QDialogButtonBox, QFont, QFontMetrics, QFormLayout, QFrame, QHBoxLayout,
    QIcon, QLabel, QLineEdit, QListWidget, QListWidgetItem, QLocale, QPainter, QPen,
    QPixmap, QPushButton, QRectF, QScrollArea, QSize, QSizePolicy, QStackedWidget, Qt,
    QToolButton, QToolTip, QUrl, QVBoxLayout, QWidget, pyqtSignal,
)

from calibre.gui2 import gprefs, info_dialog

from .prefs import prefs
from .stats import (
    DECISION_KEEP, DECISION_MOVE, SOURCE_CONTINUITY, SOURCE_DEVICE_CLOCK, SOURCE_MANUAL,
    StatsError, apply_clock_fix_to_device, clock_backup_dir, ensure_clock_backup, fmt_duration,
    load_dataset, longest_streak, pull, set_clock_decision,
)

# One hue each, light to dark. Dark themes run them the other way, so "more
# reading" always stands further from the surface. Red and yellow are left out:
# they read as warnings rather than as more of something.
HEAT_RAMPS = {
    'blue': ('Blue', ('#b7d3f6', '#86b6ef', '#3987e5', '#1c5cab', '#0d366b')),
    'green': ('Green', ('#b7e4c0', '#7fcf94', '#3fa862', '#1f7a40', '#0e4f28')),
    'teal': ('Teal', ('#b3e6df', '#72cfc3', '#22a596', '#127568', '#094a42')),
    'purple': ('Purple', ('#d9d2f7', '#b1a5ee', '#7f6ee0', '#5342b5', '#312577')),
    'pink': ('Pink', ('#f8cfe0', '#ee9dbf', '#d9568d', '#a5316a', '#681d43')),
    'orange': ('Orange', ('#fbd3b6', '#f5a673', '#e8742f', '#b3501a', '#74300c')),
}
EMPTY_LIGHT, EMPTY_DARK = '#e1e0d9', '#2c2c2a'
MUTED = '#898781'

# Fixed order, as validated for colour-blind separation. A book keeps its colour
# for good; see stats.assign_colour_slots.
SERIES_LIGHT = ('#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948')
SERIES_DARK = ('#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767')
NEUTRAL_LIGHT, NEUTRAL_DARK = '#c3c2b7', '#52514e'
YELLOW_SLOT = 3

# Fixed in minutes, so a shade means the same in every year and for every book.
# Keyed by the minutes that earn the darkest shade.
HEAT_SCALES = {
    60: (5, 15, 30, 60),
    120: (15, 30, 60, 120),
    240: (30, 60, 120, 240),
}


COLOUR_MODE_PREF = 'koreader_stats_year_colouring'
COLOUR_HEAT, COLOUR_BOOKS = 'heat', 'books'
# Beyond this the squares are too small to read; the tooltip still lists them all.
MAX_BOOK_LAYERS = 4


def book_layers(x, y, cell, count):
    """Squares for `count` books overlapping within one cell, back layer first.

    They step diagonally from the top left to the bottom right, so each layer
    behind the front one stays visible as an L-shaped edge.
    """
    if count <= 1:
        return [QRectF(x, y, cell, cell)]
    size = cell * (0.72 if count == 2 else 0.62 if count == 3 else 0.55)
    step = (cell - size) / (count - 1)
    return [QRectF(x + i * step, y + i * step, size, size) for i in range(count)]


def is_dark():
    return QApplication.instance().palette().window().color().lightness() < 128


def heat_limits():
    return HEAT_SCALES.get(prefs['heat_scale'], HEAT_SCALES[120])


def heat_ramp():
    ramp = HEAT_RAMPS.get(prefs['heat_colour'], HEAT_RAMPS['blue'])[1]
    return tuple(reversed(ramp)) if is_dark() else ramp


def level_labels(limits):
    labels = ['Under %d min' % limits[0]]
    for low, high in zip(limits, limits[1:]):
        if high <= 60:
            labels.append('%d–%d min' % (low, high))
        else:
            labels.append('%g–%g h' % (low / 60, high / 60))
    labels.append('%g h or more' % (limits[-1] / 60))
    return labels


def heat_level(seconds):
    minutes = seconds / 60
    limits = heat_limits()
    for i, limit in enumerate(limits):
        if minutes < limit:
            return i
    return len(limits)


def heat_colour(seconds):
    return QColor(heat_ramp()[heat_level(seconds)])


def empty_colour():
    return QColor(EMPTY_DARK if is_dark() else EMPTY_LIGHT)


def book_colour(book):
    if book is None or book.slot is None:
        return QColor(NEUTRAL_DARK if is_dark() else NEUTRAL_LIGHT)
    return QColor((SERIES_DARK if is_dark() else SERIES_LIGHT)[book.slot])


def ink_on(book):
    """Label colour for a bar filled with this book's colour."""
    if book is None or book.slot is None:
        return QColor('#f0efec' if is_dark() else '#0b0b0b')
    return QColor('#0b0b0b' if book.slot == YELLOW_SLOT and not is_dark() else '#ffffff')


def week_start():
    """The locale's first weekday, as 0 = Monday ... 6 = Sunday."""
    setting = prefs['week_start']
    return setting if setting in (0, 6) else QLocale().firstDayOfWeek().value - 1


def fmt_day(day):
    return '%s %d %s %d' % (day.strftime('%a'), day.day, day.strftime('%b'), day.year)


def fmt_date(day):
    # Written out because strftime's '%-d' doesn't exist on Windows.
    return '%d %s %d' % (day.day, day.strftime('%b'), day.year)


def fmt_clock(ts):
    return dt.datetime.fromtimestamp(ts).strftime('%H:%M')


class View:
    """What the charts are showing, shared between them."""

    def __init__(self, dataset):
        self.ds = dataset
        self.book_key = None
        self.selected = None

    def entries(self, day):
        entries = self.ds.days.get(day)
        if not entries:
            return {}
        if self.book_key is None:
            return entries
        return {self.book_key: entries[self.book_key]} if self.book_key in entries else {}

    def seconds(self, day):
        return sum(e.seconds for e in self.entries(day).values())

    def tooltip(self, day):
        entries = self.entries(day)
        if not entries:
            return '%s\nNo reading' % fmt_day(day)
        lines = ['%s · %s' % (fmt_day(day), fmt_duration(sum(e.seconds for e in entries.values())))]
        for key, e in sorted(entries.items(), key=lambda kv: -kv[1].seconds):
            lines.append('%s: %s' % (self.ds.books[key].title, fmt_duration(e.seconds)))
        return '\n'.join(lines)


class YearHeatMap(QWidget):
    """One square per day, in weeks left to right, as the year runs."""

    day_clicked = pyqtSignal(object)
    day_activated = pyqtSignal(object)

    LEFT = 32
    TOP = 20
    GAP = 3

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.year = dt.date.today().year
        self.colour_mode = COLOUR_HEAT
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(self.TOP + 7 * 14)

    def set_year(self, year):
        self.year = year
        self.update()

    def set_colour_mode(self, mode):
        self.colour_mode = mode
        self.update()

    def _paint_books(self, p, rect, entries):
        """Overlap a square per book, with the book read longest on top."""
        order = sorted(entries, key=lambda key: -entries[key].seconds)[:MAX_BOOK_LAYERS]
        layers = book_layers(rect.x(), rect.y(), rect.width(), len(order))
        # A surface-coloured ring keeps touching colours apart.
        ring = QPen(self.palette().window().color(), 1) if len(order) > 1 else Qt.PenStyle.NoPen
        radius = min(3.0, layers[0].width() / 4)
        p.setPen(ring)
        for key, layer in zip(reversed(order), layers):
            p.setBrush(book_colour(self.view.ds.books[key]))
            p.drawRoundedRect(layer, radius, radius)
        p.setPen(Qt.PenStyle.NoPen)

    def _start(self):
        first = dt.date(self.year, 1, 1)
        return first - dt.timedelta(days=(first.weekday() - week_start()) % 7)

    def _cell(self):
        weeks = 54
        return max(8.0, min(22.0, (self.width() - self.LEFT - 2) / weeks - self.GAP))

    def sizeHint(self):
        return QSize(820, self.heightForWidth(820))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        cell = max(8.0, min(22.0, (width - self.LEFT - 2) / 54 - self.GAP))
        return int(self.TOP + 7 * (cell + self.GAP) + 2)

    def resizeEvent(self, event):
        height = self.heightForWidth(self.width())
        if height != self.height():
            self.setFixedHeight(height)
        super().resizeEvent(event)

    def _rect(self, day):
        delta = (day - self._start()).days
        week, weekday = divmod(delta, 7)
        cell = self._cell()
        return QRectF(self.LEFT + week * (cell + self.GAP), self.TOP + weekday * (cell + self.GAP), cell, cell)

    def _day_at(self, pos):
        cell = self._cell()
        # The hit area is the whole cell plus its gap, bigger than the mark.
        week = int((pos.x() - self.LEFT) // (cell + self.GAP))
        weekday = int((pos.y() - self.TOP) // (cell + self.GAP))
        if week < 0 or not 0 <= weekday <= 6:
            return None
        day = self._start() + dt.timedelta(days=week * 7 + weekday)
        return day if day.year == self.year and day <= dt.date.today() else None

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 2))
        p.setFont(font)
        muted = QColor(MUTED)
        ink = self.palette().windowText().color()
        cell = self._cell()

        p.setPen(muted)
        for month in range(1, 13):
            rect = self._rect(dt.date(self.year, month, 1))
            p.drawText(QRectF(rect.x(), 0, 60, self.TOP - 3), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       calendar.month_abbr[month])
        for row in (0, 2, 4):
            name = calendar.day_abbr[(week_start() + row) % 7]
            p.drawText(QRectF(0, self.TOP + row * (cell + self.GAP), self.LEFT - 6, cell),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, name)

        today = dt.date.today()
        day = dt.date(self.year, 1, 1)
        one = dt.timedelta(days=1)
        radius = min(3.0, cell / 4)
        p.setPen(Qt.PenStyle.NoPen)
        while day.year == self.year and day <= today:
            rect = self._rect(day)
            entries = self.view.entries(day)
            if entries and self.colour_mode == COLOUR_BOOKS:
                if len(entries) > 1:
                    p.setBrush(empty_colour())
                    p.drawRoundedRect(rect, radius, radius)
                self._paint_books(p, rect, entries)
            else:
                seconds = sum(e.seconds for e in entries.values())
                p.setBrush(heat_colour(seconds) if seconds else empty_colour())
                p.drawRoundedRect(rect, radius, radius)
            if day == self.view.selected or day == today:
                ring = QPen(ink if day == self.view.selected else muted, 2 if day == self.view.selected else 1)
                p.setPen(ring)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius + 1, radius + 1)
                p.setPen(Qt.PenStyle.NoPen)
            day += one
        p.end()

    def mouseMoveEvent(self, event):
        day = self._day_at(event.position())
        if day is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), self.view.tooltip(day), self)

    def mousePressEvent(self, event):
        day = self._day_at(event.position())
        if day is not None:
            self.day_clicked.emit(day)

    def mouseDoubleClickEvent(self, event):
        day = self._day_at(event.position())
        if day is not None:
            self.day_activated.emit(day)


class MonthCalendar(QWidget):
    """A month grid with a bar for each book, like KOReader's calendar."""

    day_clicked = pyqtSignal(object)

    HEAD = 22
    BAR = 18
    BAR_GAP = 2
    DATE_ROW = 20

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        today = dt.date.today()
        self.year, self.month = today.year, today.month
        self.colour_mode = COLOUR_HEAT
        self.setMouseTracking(True)
        self.setMinimumHeight(420)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_month(self, year, month):
        self.year, self.month = year, month
        self.update()

    def set_colour_mode(self, mode):
        self.colour_mode = mode
        self.update()

    def _weeks(self):
        first = dt.date(self.year, self.month, 1)
        start = first - dt.timedelta(days=(first.weekday() - week_start()) % 7)
        last = dt.date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])
        weeks = []
        while start <= last:
            weeks.append([start + dt.timedelta(days=i) for i in range(7)])
            start += dt.timedelta(days=7)
        return weeks

    def _metrics(self):
        weeks = self._weeks()
        cell_w = (self.width() - 1) / 7
        cell_h = (self.height() - self.HEAD - 1) / len(weeks)
        return weeks, cell_w, cell_h

    def _layout_week(self, week):
        """Place each book on one row of the week; busiest books first."""
        totals = {}
        for day in week:
            if day.month != self.month:
                continue
            for key, entry in self.view.entries(day).items():
                totals[key] = totals.get(key, 0) + entry.seconds
        return sorted(totals, key=lambda k: -totals[k])

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        small = QFont(self.font())
        small.setPointSizeF(max(9.0, small.pointSizeF() - 2))
        p.setFont(small)
        ink = self.palette().windowText().color()
        muted = QColor(MUTED)
        grid = QColor(EMPTY_DARK if is_dark() else EMPTY_LIGHT)
        weeks, cell_w, cell_h = self._metrics()
        metrics = QFontMetrics(small)
        today = dt.date.today()

        p.setPen(muted)
        for i in range(7):
            p.drawText(QRectF(i * cell_w, 0, cell_w, self.HEAD), Qt.AlignmentFlag.AlignCenter,
                       calendar.day_abbr[(week_start() + i) % 7])

        for row, week in enumerate(weeks):
            top = self.HEAD + row * cell_h
            for col, day in enumerate(week):
                rect = QRectF(col * cell_w, top, cell_w, cell_h)
                p.setPen(QPen(grid, 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
                in_month = day.month == self.month
                if day == self.view.selected:
                    p.setPen(QPen(ink, 2))
                    p.drawRect(rect.adjusted(1, 1, -1, -1))
                if not in_month:
                    continue
                seconds = self.view.seconds(day)
                if seconds:
                    # The heat chip ties this view to the year view's scale. In
                    # books view the bars carry the colour, so the time stands alone.
                    right_margin = 6
                    if self.colour_mode == COLOUR_HEAT:
                        right_margin = 18
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(heat_colour(seconds))
                        p.drawRoundedRect(QRectF(rect.right() - 13, rect.top() + 6, 9, 9), 2, 2)
                    p.setPen(muted)
                    p.drawText(QRectF(rect.x() + 28, rect.y() + 2, rect.width() - 28 - right_margin, self.DATE_ROW - 2),
                               Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                               fmt_duration(seconds).replace(' min', 'm').replace(' h', 'h'))
                date_font = QFont(small)
                date_font.setBold(day == today)
                p.setFont(date_font)
                p.setPen(ink)
                p.drawText(QRectF(rect.x() + 6, rect.y() + 2, 26, self.DATE_ROW - 2),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(day.day))
                p.setFont(small)
            self._paint_week_bars(p, week, top, cell_w, cell_h, metrics)
        p.end()

    def _paint_week_bars(self, p, week, top, cell_w, cell_h, metrics):
        order = self._layout_week(week)
        room = int((cell_h - self.DATE_ROW - 3) // (self.BAR + self.BAR_GAP))
        shown = order if len(order) <= room else order[:max(room - 1, 0)]
        for slot, key in enumerate(shown):
            book = self.view.ds.books[key]
            y = top + self.DATE_ROW + slot * (self.BAR + self.BAR_GAP)
            col = 0
            while col < 7:
                if not self._read_on(week[col], key):
                    col += 1
                    continue
                end = col
                while end + 1 < 7 and self._read_on(week[end + 1], key):
                    end += 1
                bar = QRectF(col * cell_w + 3, y, (end - col + 1) * cell_w - 6, self.BAR)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(book_colour(book))
                p.drawRoundedRect(bar, 4, 4)
                p.setPen(ink_on(book))
                label = metrics.elidedText(book.title, Qt.TextElideMode.ElideRight, int(bar.width()) - 10)
                p.drawText(bar.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
                col = end + 1
        hidden = [key for key in order if key not in shown]
        if hidden:
            for col, day in enumerate(week):
                count = sum(1 for key in hidden if self._read_on(day, key))
                if count:
                    y = top + self.DATE_ROW + len(shown) * (self.BAR + self.BAR_GAP)
                    p.setPen(QColor(MUTED))
                    p.drawText(QRectF(col * cell_w + 6, y, cell_w - 8, self.BAR),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, '+%d more' % count)

    def _read_on(self, day, key):
        return day.month == self.month and key in self.view.entries(day)

    def _day_at(self, pos):
        weeks, cell_w, cell_h = self._metrics()
        if pos.y() < self.HEAD:
            return None
        row = int((pos.y() - self.HEAD) // cell_h)
        col = int(pos.x() // cell_w)
        if 0 <= row < len(weeks) and 0 <= col < 7:
            day = weeks[row][col]
            return day if day.month == self.month else None
        return None

    def mouseMoveEvent(self, event):
        day = self._day_at(event.position())
        if day is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), self.view.tooltip(day), self)

    def mousePressEvent(self, event):
        day = self._day_at(event.position())
        if day is not None:
            self.day_clicked.emit(day)


class Legend(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.rebuild()

    def rebuild(self):
        layout = self.layout()
        while layout.count():
            widget = layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        layout.addStretch(1)
        layout.addWidget(muted_label('Time read per day:'))
        for colour, text in zip(heat_ramp(), level_labels(heat_limits())):
            chip = QLabel(self)
            chip.setPixmap(swatch(QColor(colour), 12))
            layout.addSpacing(6)
            layout.addWidget(chip)
            layout.addWidget(muted_label(text))


def muted_label(text):
    label = QLabel(text)
    font = label.font()
    font.setPointSizeF(max(9.0, font.pointSizeF() - 2))
    label.setFont(font)
    label.setStyleSheet('color: %s' % MUTED)
    return label


def swatch(colour, size):
    ratio = QApplication.instance().devicePixelRatio()
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawRoundedRect(QRectF(0, 0, size, size), 3, 3)
    p.end()
    return pixmap


class StatTile(QWidget):
    def __init__(self, caption, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.value = QLabel('–', self)
        font = self.value.font()
        font.setPointSizeF(font.pointSizeF() + 7)
        font.setWeight(QFont.Weight.DemiBold)
        self.value.setFont(font)
        # Bold figures can render slightly wider than Qt measures them.
        self.value.setContentsMargins(0, 0, 8, 0)
        layout.addWidget(self.value)
        layout.addWidget(muted_label(caption))


class ClockFixDialog(QDialog):
    """Ask whether to move records written while a device clock was wrong.

    device_db is KOReader's database on the connected device. Without it a device's
    records can't be moved from here. Imported files are only changed in this plugin.
    """

    def __init__(self, seg, parent=None, device_db=None):
        super().__init__(parent)
        self.seg = seg
        self.device_db = device_db
        self.result_message = None
        self.setWindowTitle("Device clock was wrong")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        if seg.imported:
            headline = "The device clock was wrong when these records were written."
        else:
            headline = "The clock on your %s was wrong while you were reading." % html.escape(seg.device_name)
        title = QLabel("<b>%s</b>" % headline, self)
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(QLabel("{:,} reading records ({}) have the wrong dates.".format(
            seg.rows, fmt_duration(seg.seconds)), self))

        form = QFormLayout()
        form.addRow("Recorded as:", QLabel("%s – %s" % (
            fmt_date(dt.date.fromtimestamp(seg.min_t)), fmt_date(dt.date.fromtimestamp(seg.max_end))), self))
        self.start_edit = QDateTimeEdit(self)
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("d MMM yyyy HH:mm")
        self.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(seg.min_t + seg.offset)))
        self.start_edit.dateTimeChanged.connect(self._update_end)
        self.end_label = QLabel(self)
        row = QHBoxLayout()
        row.addWidget(self.start_edit)
        row.addWidget(self.end_label)
        row.addStretch(1)
        moved_in_plugin = seg.imported and seg.decision == DECISION_MOVE
        form.addRow("Moved to:" if moved_in_plugin else "Would move to:", row)
        layout.addLayout(form)
        self._update_end()

        layout.addWidget(self._note(self._basis()))
        self.change_label = self._message(self._change_text())
        layout.addWidget(self.change_label)
        self.backup_label = self._message(self._backup_text())
        layout.addWidget(self.backup_label)
        if seg.ongoing and not seg.imported:
            layout.addWidget(self._note(
                "If the device clock is still wrong, new reading will be dated wrongly too, and you will "
                "be asked again. Connecting the device to Wi-Fi usually sets the clock."))
        self.error_label = self._message("")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(self)
        if seg.imported:
            move_text = "Save new dates" if moved_in_plugin else "Move in plugin only"
        else:
            move_text = "Change records on device"
        self.move_button = buttons.addButton(move_text, QDialogButtonBox.ButtonRole.AcceptRole)
        self.keep_button = buttons.addButton(
            "Put back as recorded" if moved_in_plugin else "Keep recorded dates",
            QDialogButtonBox.ButtonRole.DestructiveRole)
        self.later_button = buttons.addButton(
            "Close" if seg.decision == DECISION_KEEP or moved_in_plugin else "Decide later",
            QDialogButtonBox.ButtonRole.RejectRole)
        self.move_button.setEnabled(seg.imported or device_db is not None)
        self.move_button.setDefault(self.move_button.isEnabled())
        self.move_button.clicked.connect(lambda: self.choose(DECISION_MOVE))
        self.keep_button.clicked.connect(lambda: self.choose(DECISION_KEEP))
        self.later_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _note(self, text):
        label = muted_label(text)
        label.setWordWrap(True)
        return label

    def _message(self, text):
        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        return label

    def _basis(self):
        seg = self.seg
        if seg.source == SOURCE_DEVICE_CLOCK:
            return "The new dates come from comparing the device's clock with the real time when it connected, so they are usually right to within an hour."
        if seg.source == SOURCE_MANUAL:
            return "The new dates use the start you entered. The length of the stretch stays the same."
        if seg.prev_good_end is not None:
            return (
                "The new dates assume you carried on straight after your last correctly dated record on %s. "
                "The real dates could be later, so change the start if you remember it." %
                fmt_date(dt.date.fromtimestamp(seg.prev_good_end)))
        return "The new dates are a best guess, so change the start if you remember it."

    def _change_text(self):
        if self.seg.imported:
            return (
                "<b>This only changes what this plugin shows.</b> The file you imported is not touched, "
                "whichever you choose.")
        if self.device_db is None:
            return (
                "<b>Connect your %s to change these records.</b> This changes KOReader's statistics "
                "database on the device, so the device needs to be connected." % html.escape(self.seg.device_name))
        return (
            "<b>This changes KOReader's statistics database on your device.</b> KOReader and this plugin "
            "will both show the new dates. Reading times and page counts stay the same. If you keep the "
            "recorded dates, nothing on your device changes.")

    def _backup_text(self):
        if self.seg.imported:
            own_copy = "keep the original file safe"
        else:
            own_copy = "also make your own copy of statistics.sqlite3 from KOReader's settings folder on your device"
        try:
            path = ensure_clock_backup(self.seg.dir_path, self.seg.key)
            saved = dt.datetime.fromtimestamp(os.path.getmtime(path))
        except OSError as e:
            return (
                "<b>Backup failed:</b> the automatic backup could not be saved (%s). If you're concerned, "
                "you should %s before choosing." % (html.escape(str(e)), own_copy))
        folder = QUrl.fromLocalFile(os.path.dirname(path)).toString()
        text = (
            "<b>Backup made:</b> a copy of your statistics was saved automatically on this computer on "
            "%s at %s (<a href=\"%s\">show backup</a>)." % (
                fmt_date(saved.date()), saved.strftime("%H:%M"), html.escape(folder, quote=True)))
        if self.device_db is not None:
            text += " Another is saved straight from your device just before it is changed."
        return text + " If you're concerned, you should %s." % own_copy

    def _chosen_offset(self):
        return self.start_edit.dateTime().toSecsSinceEpoch() - self.seg.min_t

    def _update_end(self):
        end = self.seg.max_end + self._chosen_offset()
        self.end_label.setText("to  %s" % fmt_date(dt.date.fromtimestamp(end)))

    def _show_error(self, error):
        self.error_label.setText(
            "<b>Your device was not changed.</b> %s" % html.escape(str(error)))
        self.error_label.show()

    def _write_to_device(self, offset):
        """Change the device, then refresh the plugin's copy. Returns False on failure."""
        seg = self.seg
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = apply_clock_fix_to_device(self.device_db, seg.key, offset, clock_backup_dir(seg.dir_path))
        except (StatsError, OSError, sqlite3.Error) as e:
            QApplication.restoreOverrideCursor()
            self._show_error(e)
            return False
        try:
            store_root = os.path.dirname(os.path.dirname(seg.dir_path))
            pull(self.device_db, store_root, os.path.basename(seg.dir_path), seg.device_name, live_device=False)
        except (StatsError, OSError):
            pass  # The device is right; the saved decision covers this copy until the next pull.
        finally:
            QApplication.restoreOverrideCursor()
        self.result_message = (
            "{:,} reading records were moved in KOReader's database on your {}.\n\n"
            "A backup of the database from just before the change is saved at:\n{}\n\n"
            "Eject the device as usual before unplugging it.".format(
                result.rows, seg.device_name, result.backup_path))
        return True

    def choose(self, decision):
        seg = self.seg
        offset, source = seg.offset, seg.source
        # The editor shows whole minutes, so ignore differences below that.
        if decision == DECISION_MOVE and abs(self._chosen_offset() - offset) >= 60:
            offset, source = self._chosen_offset(), SOURCE_MANUAL
        if decision == DECISION_MOVE and not seg.imported and not self._write_to_device(offset):
            return
        set_clock_decision(seg.dir_path, seg.key, decision, offset, source)
        self.accept()


def review_clock_segments(segments, parent=None, device_db_for=None):
    """Show the question for each segment. Returns True if anything was decided.

    device_db_for(segment) gives the connected device's database, if it is there.
    """
    changed = False
    for seg in segments:
        dialog = ClockFixDialog(seg, parent, device_db_for(seg) if device_db_for else None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            changed = True
            if dialog.result_message:
                info_dialog(parent, "Device records changed", dialog.result_message, show=True)
    return changed


class StatsWindow(QDialog):
    pull_requested = pyqtSignal()
    import_requested = pyqtSignal()
    settings_requested = pyqtSignal()

    def __init__(self, store_root, parent=None):
        super().__init__(parent)
        self.store_root = store_root
        self.device_db_for = None  # Set by the action, which knows what is connected.
        self.setWindowTitle('KOReader Reading Stats')
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(1200, 800)
        self.view = View(self._load())
        self.year = None
        self.month = None
        self._build()
        self.reload()

    def _load(self):
        return load_dataset(
            self.store_root, day_start_hour=prefs['day_start_hour'],
            min_book_seconds=prefs['min_book_minutes'] * 60)

    # ------------------------------------------------------------ construction

    def _build(self):
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Filter books')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_books)
        left.addWidget(self.search)
        self.book_list = QListWidget(self)
        self.book_list.setIconSize(QSize(12, 12))
        self.book_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.book_list.setWordWrap(True)
        self.book_list.setUniformItemSizes(False)
        self.book_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.book_list.currentItemChanged.connect(self._book_changed)
        left.addWidget(self.book_list, 1)
        side = QWidget(self)
        side.setLayout(left)
        side.setFixedWidth(280)
        left.setContentsMargins(0, 0, 0, 0)
        root.addWidget(side)

        right = QVBoxLayout()
        right.setSpacing(10)
        root.addLayout(right, 1)

        nav = QHBoxLayout()
        self.prev_button = QToolButton(self)
        self.prev_button.setText('‹')
        self.prev_button.setAccessibleName('Previous')
        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.next_button = QToolButton(self)
        self.next_button.setText('›')
        self.next_button.setAccessibleName('Next')
        self.next_button.clicked.connect(lambda: self._step(1))
        self.period = QLabel(self)
        font = self.period.font()
        font.setPointSizeF(font.pointSizeF() + 4)
        font.setWeight(QFont.Weight.DemiBold)
        self.period.setFont(font)
        nav.addWidget(self.prev_button)
        nav.addWidget(self.period)
        nav.addWidget(self.next_button)
        nav.addStretch(1)
        self.heat_button = QToolButton(self)
        self.heat_button.setText('Heat map')
        self.books_button = QToolButton(self)
        self.books_button.setText('Books')
        colour_group = QButtonGroup(self)
        saved_mode = gprefs.get(COLOUR_MODE_PREF, COLOUR_HEAT)
        for button, mode in ((self.heat_button, COLOUR_HEAT), (self.books_button, COLOUR_BOOKS)):
            button.setCheckable(True)
            button.setChecked(mode == saved_mode)
            colour_group.addButton(button)
            button.clicked.connect(lambda _checked=False, m=mode: self._set_colour_mode(m))
            nav.addWidget(button)
        nav.addSpacing(16)
        self.year_button = QToolButton(self)
        self.year_button.setText('Year')
        self.month_button = QToolButton(self)
        self.month_button.setText('Month')
        group = QButtonGroup(self)
        for i, button in enumerate((self.year_button, self.month_button)):
            button.setCheckable(True)
            group.addButton(button, i)
            nav.addWidget(button)
        self.year_button.setChecked(True)
        self.year_button.clicked.connect(lambda: self._set_mode(0))
        self.month_button.clicked.connect(lambda: self._set_mode(1))
        right.addLayout(nav)

        tiles = QHBoxLayout()
        tiles.setSpacing(36)
        self.tile_time = StatTile('Time read', self)
        self.tile_days = StatTile('Days read', self)
        self.tile_books = StatTile('Books opened', self)
        self.tile_streak = StatTile('Longest streak', self)
        for tile in (self.tile_time, self.tile_days, self.tile_books, self.tile_streak):
            tiles.addWidget(tile)
        tiles.addStretch(1)
        right.addLayout(tiles)

        self.stack = QStackedWidget(self)
        year_page = QWidget(self)
        year_layout = QVBoxLayout(year_page)
        year_layout.setContentsMargins(0, 0, 0, 0)
        self.heat = YearHeatMap(self.view, year_page)
        self.heat.day_clicked.connect(self._select_day)
        self.heat.day_activated.connect(self._open_month)
        year_layout.addWidget(self.heat)
        self.heat_legend = Legend(year_page)
        year_layout.addWidget(self.heat_legend)
        self.books_legend = muted_label(
            'Each colour is a book; see the list on the left. Overlapping squares mean more than one '
            'book that day, with the longest read on top. Colours can repeat in other months, so hover '
            'over a day for its titles.')
        self.books_legend.setWordWrap(True)
        year_layout.addWidget(self.books_legend)
        year_layout.addWidget(self._hline())
        self.detail = QLabel(year_page)
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea(year_page)
        scroll.setWidget(self.detail)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        year_layout.addWidget(scroll, 1)
        self.stack.addWidget(year_page)
        self.month_view = MonthCalendar(self.view, self)
        self.month_view.day_clicked.connect(self._select_day)
        self.stack.addWidget(self.month_view)
        self.empty_page = self._build_empty_page()
        self.stack.addWidget(self.empty_page)
        right.addWidget(self.stack, 1)
        self._apply_colour_mode(saved_mode)

        foot = QHBoxLayout()
        self.footer = muted_label('')
        foot.addWidget(self.footer, 1)
        self.clock_button = QPushButton(self)
        self.clock_button.clicked.connect(self._review_clock)
        foot.addWidget(self.clock_button)
        self.refresh_button = QPushButton('Pull from device', self)
        self.refresh_button.clicked.connect(self.pull_requested)
        foot.addWidget(self.refresh_button)
        self.settings_button = QPushButton('Settings…', self)
        self.settings_button.clicked.connect(self.settings_requested)
        foot.addWidget(self.settings_button)
        right.addLayout(foot)

    def _hline(self):
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        return line

    def _build_empty_page(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        text = QLabel(
            '<h3>No reading statistics yet</h3>'
            'Connect a device running KOReader and they will be copied '
            'automatically.<br>You can also import a '
            '<code>statistics.sqlite3</code> file you already have.', page)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(text)
        button = QPushButton('Import a file…', page)
        button.clicked.connect(self.import_requested)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ data

    def reload(self):
        """Read the store again, keeping the current book and date if possible."""
        ds = self._load()
        self.view.ds = ds
        if self.view.book_key not in ds.books:
            self.view.book_key = None
        self._populate_books()
        segments = ds.segments
        self.clock_button.setVisible(bool(segments))
        undecided = any(s.needs_review for s in segments)
        self.clock_button.setText('Review device clock…' if undecided else 'Device clock…')

        latest = max(ds.days) if ds.days else dt.date.today()
        years = ds.years()
        if self.year not in years:
            self.year = latest.year
            self.month = latest.month
        if self.month is None:
            self.month = latest.month
        if ds.empty:
            self.stack.setCurrentWidget(self.empty_page)
        elif self.stack.currentWidget() is self.empty_page:
            self.stack.setCurrentIndex(0)
        self._set_nav_enabled(not ds.empty)
        if ds.last_pull:
            pulled = dt.datetime.fromtimestamp(ds.last_pull)
            self.footer.setText('%s · last copied %s, %s' % (
                ', '.join(ds.devices), fmt_date(pulled.date()), pulled.strftime('%H:%M')))
        else:
            self.footer.setText('')
        self._refresh()

    def _set_nav_enabled(self, enabled):
        for widget in (self.prev_button, self.next_button, self.year_button, self.month_button,
                       self.heat_button, self.books_button):
            widget.setEnabled(enabled)

    def _populate_books(self):
        self.book_list.blockSignals(True)
        self.book_list.clear()
        all_item = QListWidgetItem('All books')
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.book_list.addItem(all_item)
        current = all_item
        books = sorted(self.view.ds.books.values(), key=lambda b: -b.last_ts)
        for book in books:
            subtitle = '%s · %s' % (
                fmt_duration(book.seconds), dt.date.fromtimestamp(book.last_ts).strftime('%b %Y'))
            item = QListWidgetItem('%s\n%s' % (book.title, subtitle))
            item.setIcon(QIcon(swatch(book_colour(book), 12)))
            item.setData(Qt.ItemDataRole.UserRole, book.key)
            tip = book.title
            if book.authors:
                tip += '\n' + book.authors
            item.setToolTip(tip)
            self.book_list.addItem(item)
            if book.key == self.view.book_key:
                current = item
        self.book_list.setCurrentItem(current)
        self.book_list.blockSignals(False)
        self._filter_books(self.search.text())

    def _filter_books(self, text):
        needle = text.casefold().strip()
        for i in range(1, self.book_list.count()):
            item = self.book_list.item(i)
            book = self.view.ds.books.get(item.data(Qt.ItemDataRole.UserRole))
            haystack = '%s %s' % (book.title, book.authors) if book else ''
            item.setHidden(bool(needle) and needle not in haystack.casefold())

    # ------------------------------------------------------------ interaction

    def _book_changed(self, item, _previous=None):
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self.view.book_key = key
        self.view.selected = None
        book = self.view.ds.books.get(key)
        if book is not None:
            last = dt.date.fromtimestamp(book.last_ts)
            self.year, self.month = last.year, last.month
        self._refresh()

    def _select_day(self, day):
        self.view.selected = day
        self._refresh()

    def _open_month(self, day):
        self.view.selected = day
        self.year, self.month = day.year, day.month
        self.month_button.setChecked(True)
        self._set_mode(1)

    def _set_mode(self, index):
        selected = self.view.selected
        # Follow the day picked in the year on screen, not one left in another year.
        if index == 1 and not self._in_month_mode() and selected is not None and selected.year == self.year:
            self.month = selected.month
        if not self.view.ds.empty:
            self.stack.setCurrentIndex(index)
        self._refresh()

    def apply_start_view(self):
        """Show the view chosen in settings, for when the window opens."""
        month = prefs['start_view'] == 'month'
        (self.month_button if month else self.year_button).setChecked(True)
        self._set_mode(1 if month else 0)

    def apply_settings(self):
        """Redraw after settings change."""
        self.heat_legend.rebuild()
        self.reload()

    def _apply_colour_mode(self, mode):
        self.heat.set_colour_mode(mode)
        self.month_view.set_colour_mode(mode)
        self.heat_legend.setVisible(mode == COLOUR_HEAT)
        self.books_legend.setVisible(mode == COLOUR_BOOKS)

    def _set_colour_mode(self, mode):
        gprefs[COLOUR_MODE_PREF] = mode
        self._apply_colour_mode(mode)

    def _in_month_mode(self):
        return self.stack.currentWidget() is self.month_view

    def _step(self, direction):
        years = self.view.ds.years()
        if not years:
            return
        if self._in_month_mode():
            total = self.year * 12 + self.month - 1 + direction
            year, month = divmod(total, 12)
            month += 1
            if years[0] <= year <= dt.date.today().year:
                self.year, self.month = year, month
        else:
            options = [y for y in years if (y > self.year if direction > 0 else y < self.year)]
            if options:
                self.year = min(options) if direction > 0 else max(options)
                self.month = 12 if direction < 0 else 1
                last_in_year = [d for d in self.view.ds.days if d.year == self.year]
                if last_in_year:
                    self.month = max(last_in_year).month
        self._refresh()

    def _review_clock(self):
        if review_clock_segments(self.view.ds.segments, self, self.device_db_for):
            self.reload()

    # ------------------------------------------------------------ rendering

    def _refresh(self):
        month_mode = self._in_month_mode()
        if self.year is None:
            self.year = dt.date.today().year
        if self.month is None:
            self.month = dt.date.today().month
        if month_mode:
            self.period.setText('%s %d' % (calendar.month_name[self.month], self.year))
            days = [d for d in self.view.ds.days if d.year == self.year and d.month == self.month]
        else:
            self.period.setText(str(self.year))
            days = [d for d in self.view.ds.days if d.year == self.year]
        self.heat.set_year(self.year)
        self.month_view.set_month(self.year, self.month)

        years = self.view.ds.years()
        if years:
            if month_mode:
                self.prev_button.setEnabled((self.year, self.month) > (years[0], 1))
                today = dt.date.today()
                self.next_button.setEnabled((self.year, self.month) < (today.year, today.month))
            else:
                self.prev_button.setEnabled(self.year > years[0])
                self.next_button.setEnabled(self.year < years[-1])

        active = [d for d in days if self.view.entries(d)]
        seconds = sum(self.view.seconds(d) for d in active)
        books = {k for d in active for k in self.view.entries(d)}
        self.tile_time.value.setText(fmt_duration(seconds))
        self.tile_days.value.setText(str(len(active)))
        self.tile_books.value.setText(str(len(books)))
        streak = longest_streak(active)
        self.tile_streak.value.setText('%d day%s' % (streak, '' if streak == 1 else 's'))
        self._render_detail()
        self.heat.update()
        self.month_view.update()

    def _render_detail(self):
        ink_muted = MUTED
        day = self.view.selected
        if day is not None:
            entries = self.view.entries(day)
            parts = ['<h3 style="margin:0 0 6px 0">%s</h3>' % fmt_day(day)]
            if not entries:
                parts.append('No reading on this day.')
            else:
                parts.append('<table cellspacing="0" cellpadding="3">')
                for key, entry in sorted(entries.items(), key=lambda kv: -kv[1].seconds):
                    book = self.view.ds.books[key]
                    parts.append(
                        '<tr><td><span style="color:%s">&#9632;</span></td><td><b>%s</b></td>'
                        '<td>&nbsp;&nbsp;%s</td><td style="color:%s">&nbsp;&nbsp;%d page%s, %s–%s</td></tr>' % (
                            book_colour(book).name(), html.escape(book.title), fmt_duration(entry.seconds),
                            ink_muted, len(entry.pages), '' if len(entry.pages) == 1 else 's',
                            fmt_clock(entry.first_ts), fmt_clock(entry.last_ts)))
                parts.append('</table>')
            self.detail.setText(''.join(parts))
            return
        book = self.view.ds.books.get(self.view.book_key)
        if book is None:
            self.detail.setText(
                '<span style="color:%s">Select a day to see what you read, or a book on the left to see '
                'when you read it. Double-click a day to open its month.</span>' % ink_muted)
            return
        days = sorted(d for d, entries in self.view.ds.days.items() if book.key in entries)
        by = ('By %s' % html.escape(book.authors)) if book.authors else ''
        pages = '%d' % len(book.pages)
        if book.total_pages:
            pages += ' of %d' % book.total_pages
        self.detail.setText(
            '<h3 style="margin:0 0 6px 0">%s</h3><span style="color:%s">%s</span><br><br>'
            'Read for <b>%s</b> over <b>%d day%s</b>, from %s to %s.<br>%s pages seen.' % (
                html.escape(book.title), ink_muted, by,
                fmt_duration(book.seconds), len(days), '' if len(days) == 1 else 's',
                fmt_day(days[0]), fmt_day(days[-1]), pages))
