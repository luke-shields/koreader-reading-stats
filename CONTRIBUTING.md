# Contributing

Bug reports, device reports and pull requests are all welcome.

## Never share real statistics

A statistics database lists every book you've read and when. Don't attach one to an issue unless you're happy for it to be public. `.gitignore` keeps `*.sqlite3` files out of commits, and test data is always made up.

## Layout

| Path | What it does |
|---|---|
| `koreader_stats/stats.py` | Finding, copying and parsing statistics, clock repair and device writes. Standard library only |
| `koreader_stats/heatmap.py` | The stats window and clock pop-up |
| `koreader_stats/action.py` | calibre integration: menus and device events |
| `koreader_stats/config.py`, `prefs.py` | Settings |
| `tests/` | Tests. CI runs them on Linux, macOS and Windows |
| `tools/` | Build, release check, icon and screenshot scripts |

## Tests

`tests/test_stats.py` runs under ordinary Python. `tests/test_window.py` needs calibre, and always uses a temporary calibre profile.

**Use `check()`, not `assert`, in anything run by `calibre-debug`.** calibre's bundled Python strips `assert` statements, so an `assert` there silently checks nothing.

Anything that writes to a device must be tested on temporary files, including its failure cases. A change to `apply_clock_fix_to_device` should come with a test showing that a failure leaves the original file unchanged.

## Releasing

1. Update `version` in `koreader_stats/__init__.py`, and add an entry at the top of `CHANGELOG.md`.
2. Merge to `main`, and check that CI passes.
3. Tag it, for example `git tag v1.4.1 && git push origin v1.4.1`. This builds the zip and creates a draft GitHub release.
4. Test the zip from the draft, then publish it.
5. Replace the zip on the first post of the MobileRead thread, and update the version history there.
