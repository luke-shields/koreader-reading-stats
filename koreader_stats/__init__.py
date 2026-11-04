# SPDX-License-Identifier: GPL-3.0-only
from calibre.customize import InterfaceActionBase

__license__ = 'GPL v3'
__copyright__ = '2026, Luke Shields'


class KOReaderStatsPlugin(InterfaceActionBase):
    name = 'KOReader Reading Stats'
    description = (
        'Copies KOReader\'s reading statistics when your device connects, and shows '
        'when you read each book as a calendar heat map.')
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Luke Shields'
    version = (1, 4, 0)
    minimum_calibre_version = (6, 0, 0)  # Qt 6, and the qt.core import layer
    actual_plugin = 'calibre_plugins.koreader_stats.action:KOReaderStatsAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.koreader_stats.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
