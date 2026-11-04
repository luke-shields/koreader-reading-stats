# MobileRead forum post (draft)

Post this as a new thread in the [calibre Plugins forum](https://www.mobileread.com/forums/forumdisplay.php?f=237).

**Title:** `[GUI Plugin] KOReader Reading Stats`

**Before posting:**

- Attach `KOReader Reading Stats.zip` to the first post, and only that one zip.
- Attach or link the screenshots from `docs/screenshots`.
- Add a second post containing just "Reserved for test versions."

**First post** (BBCode, ready to paste):

```
[B]KOReader Reading Stats[/B] shows when you read each book. It copies KOReader's reading statistics when your device connects, and shows them as a calendar inside calibre.

[B]Main features[/B]
[LIST]
[*]Year view: a heat map of reading time, or a colour for each book that overlaps on days you read several
[*]Month view: a calendar with a bar for each book and the time read each day
[*]Filter by book, or click a day to see what you read, for how long, and how many pages
[*]Copy statistics automatically when a device connects, ask each time, or only on request
[*]Settings for heat map colour and scale, opening view, week start, when a new day starts, and hiding books you only opened briefly
[*]Finds records written while your device's clock was wrong, and offers to correct them
[*]Merges statistics from more than one device, and imports a statistics file by hand
[/LIST]

[B]Device support[/B]
Automatic copying needs a device that mounts as a drive.
[LIST]
[*]Kobo: tested on a Libra H2O with macOS
[*]Kindle, PocketBook, Cervantes, reMarkable: should work, but not yet tried on a real device. Reports are very welcome.
[*]Android (MTP) and wireless: not automatic. Copy statistics.sqlite3 to your computer and use Import a statistics file.
[/LIST]

[B]Please read: changing records on your device[/B]
If your device's clock reset, for example after a flat battery, reading after that point gets the wrong dates, often in 2012. The plugin shows you how many records are affected and where they would move to. If you choose [I]Change records on device[/I], it corrects KOReader's own statistics database.

This is the only time the plugin writes to your device, and it never happens without that click. A backup is saved to your computer first, and the new database is checked and read back before being swapped in. This feature is new and has only run for real on one Kobo, so please make your own copy of statistics.sqlite3 first if your reading history matters to you.

[B]Known limitation[/B]
Clock problems are detected from records being far out of order. KOReader's option to sync statistics between devices could look like one. Nothing changes unless you confirm it, so choose [I]Keep recorded dates[/I] if the pop-up looks wrong, and please report it.

[B]Privacy[/B]
The plugin makes no network connections. Your statistics stay on your device and in calibre's settings folder.

[B]Installation[/B]
Requires calibre 6 or later. Install from [I]Preferences > Plugins > Get new plugins[/I] once listed, or download the zip below and use [I]Load plugin from file[/I]. Restart calibre, then connect your device. [I]KOReader Stats[/I] appears in the toolbar and menu bar.

[B]Source code and bug reports[/B]
[URL="https://github.com/luke-shields/koreader-reading-stats"]github.com/luke-shields/koreader-reading-stats[/URL]
Licensed under GPL v3.

[B]Version History[/B]
[SPOILER]
[B]1.4.0[/B] - 4 November 2026
First public release.
[/SPOILER]
```

# Message to a moderator (draft)

Send this as a private message once the thread is up. The moderators are listed on the [forum page](https://www.mobileread.com/forums/forumdisplay.php?f=237). Replace the link with your thread's address.

**Subject:** Please add KOReader Reading Stats to the plugin index

```
Hi,

I've posted a new GUI plugin, KOReader Reading Stats, and would like it added to the Index of plugins, please:

[thread link]

Internal plugin name: KOReader Reading Stats
Type: User interface action
Summary: Copies KOReader's reading statistics when a device connects, and shows when you read each book as a calendar heat map.

The zip is attached to the first post, and the version history is in a spoiler there. It's also worth knowing that the plugin can write to KOReader's statistics database on a device, but only if the user clicks to confirm in a pop-up, and after saving a backup. The first post explains this.

Thanks!
```
