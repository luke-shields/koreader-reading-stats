# SPDX-License-Identifier: GPL-3.0-only
"""Draw the plugin icon. Run with: calibre-debug tools/make_icon.py"""
import os, sys
from qt.core import QApplication, QColor, QGuiApplication, QImage, QPainter, QRectF, Qt
app = QGuiApplication.instance() or QApplication(['-platform', 'offscreen'])
SHADES = ('#b7d3f6', '#3987e5', '#0d366b', '#86b6ef', '#0d366b', '#1c5cab', '#3987e5', '#b7d3f6', '#1c5cab')
img = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setPen(Qt.PenStyle.NoPen)
size, gap = 36, 10
for i, shade in enumerate(SHADES):
    p.setBrush(QColor(shade))
    p.drawRoundedRect(QRectF(gap / 2 + (i % 3) * (size + gap - 1), gap / 2 + (i // 3) * (size + gap - 1), size, size), 7, 7)
p.end()
path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), '..', 'koreader_stats', 'images', 'icon.png')
img.save(path)
print('wrote', os.path.normpath(path))
