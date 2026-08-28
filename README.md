# DiscRipper

A small Windows desktop GUI for backing up DVDs and Blu-rays you own to
`.mkv` files. It's an orchestrator, nothing more: it asks `ffprobe` what
titles/tracks exist on a disc, lets you pick which ones to keep, and runs
`ffmpeg -c copy` to remux the selected streams into an `.mkv`. **All
decryption (CSS for DVD, AACS for Blu-ray) happens inside ffmpeg's own
libdvdread/libdvdcss/libbluray/libaacs stack** - this project contains no
decryption code of its own and never will. That's the same model VLC and
HandBrake use.

## Requirements

- Windows, with an optical drive.
- Python 3.11+.
- **A special ffmpeg build.** This is the part most people trip on:
  - Most prebuilt ffmpeg binaries (including the plain distro packages,
    e.g. Ubuntu/Debian's `ffmpeg` from apt) are **not** compiled with
    `libdvdread`, so DVD ripping won't work at all - `ffprobe`/`ffmpeg`
    will say `Unknown input format: dvdvideo`. That's a build problem,
    not a disc problem (the app detects this specific error and surfaces
    it distinctly rather than a generic failure).
  - You need the **"full" build from
    [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)** (not the
    "essentials" one) - it includes both `libdvdread` (DVD) and
    `libbluray` (Blu-ray).
  - `libdvdread` alone doesn't decrypt CSS; it can `dlopen` a
    `libdvdcss-2.dll` if one is present next to `ffmpeg.exe` or on PATH.
    You'll need to source that yourself (same as VLC does under the
    hood) - it isn't bundled with gyan.dev's build for licensing reasons.
  - For Blu-ray AACS, `libbluray` looks for `libaacs`/`libbdplus` and a
    `KEYDB.cfg` at the standard config path (`%APPDATA%\aacs\KEYDB.cfg`
    or similar depending on build). You supply your own key database -
    this app does not fetch or embed one.
  - Point the app at the resulting `ffmpeg.exe`; it infers `ffprobe.exe`
    from the same folder unless you set it separately.

## Setup

```
pip install -r requirements.txt
python -m src.main
```

## Architecture

- `src/models.py` - `Disc`/`Title`/`Track` dataclasses.
- `src/disc.py` - drive detection (`GetDriveType` via ctypes) and
  title/playlist enumeration by shelling out to `ffprobe`.
- `src/ripper.py` - builds an `ffmpeg -c copy` command for the selected
  title/tracks, runs it, and parses `-progress pipe:1` output for a
  progress bar.
- `src/gui.py` - PySide6 main window: drive picker, scan, title dropdown
  (auto-selects the longest title as "main feature" - the same heuristic
  MakeMKV uses), a checkable track list, output folder picker, and a rip
  button with a progress bar and log pane. Scanning and ripping run on
  background `QThread`s so the UI doesn't freeze.
- `src/config.py` - persists the ffmpeg path and last output folder to
  `%APPDATA%\DiscRipper\config.json`.
- `src/main.py` - entry point.

### DVD title enumeration

The `dvdvideo` demuxer (added in ffmpeg 6.1, powered by libdvdread) is
opened per-title via `-f dvdvideo -title N -i <drive root>`. Its own
range check (`title > tt_srpt->nr_of_srpts`, from ffmpeg's own source)
means valid titles are a contiguous `1..N` run, so `disc.py` probes
`-title 1`, `2`, `3`, ... and stops at the first failure - no
"consecutive failure" heuristic needed there. `-title 0` is *not* "auto
main feature" despite looking like it should be; ffmpeg's own source
defaults it to title 1 with a logged warning ("not always the main
feature, validation suggested"), so the app always passes an explicit
title number and does its own "longest title" heuristic instead.

### Blu-ray playlist enumeration

`BDMV/PLAYLIST/*.mpls` is a plain, unencrypted directory on the disc's
filesystem - `disc.py` lists it directly to get the exact set of valid
playlist numbers, rather than brute-force probing `-playlist 0..N` via
ffprobe (which is slower and can't distinguish "playlist doesn't exist"
from "disc unreadable"). If that directory somehow isn't listable, it
falls back to the original brute-force probe (stop after 3 consecutive
failures) as a safety net.

### Progress parsing

`ffmpeg -progress pipe:1 -nostats` emits `key=value` lines terminated by
`progress=continue`/`progress=end`. Verified against a live ffmpeg 6.1.1
run: `out_time_us` and `out_time_ms` both report **microseconds** despite
the `_ms` name (a long-standing ffmpeg naming quirk) - `ripper.py`
divides by 1,000,000, not 1,000.

## Testing

```
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v   # QT_QPA_PLATFORM=offscreen only needed off Windows/without a display
```

The suite exercises real subprocesses, not just mocks:

- `tests/test_disc.py` runs `disc.py` against a small stand-in `ffprobe`
  script (`tests/fake_ffprobe.py`) so the real subprocess + JSON-parsing
  code path is covered without needing a real disc or a libdvdread/
  libbluray-enabled ffmpeg build.
- `tests/test_ripper.py` builds a real short video with a real `ffmpeg`
  and remuxes it through `Ripper.rip()` end to end (skipped if no
  `ffmpeg` is on PATH).
- `tests/test_gui.py` runs the actual PySide6 widgets under the
  `offscreen` Qt platform plugin: scans, selects the main feature,
  toggles a track checkbox, and drives a full (faked) rip through the
  progress bar and log pane.

## What's still unverified against real hardware

This was built and tested without a Windows machine, an optical drive, or
a disc-capable ffmpeg build available in the dev environment - the CLI
details above (demuxer option names, error strings, the `-progress`
key format) were verified against ffmpeg's own source and a live
(distro, non-DVD/BD-capable) ffmpeg build, not guessed, but the following
still need a first real run:

1. **DVD title enumeration end-to-end** - the exact `-title` numbering
   and "main feature" selection against a real multi-title commercial DVD
   (menus, extras, multiple angles/episodes can complicate the "longest
   title" heuristic).
2. **CSS decryption** - depends entirely on you sourcing a working
   `libdvdcss-2.dll` for your ffmpeg build; the app has no fallback if
   it's missing beyond ffmpeg's own error output.
3. **Blu-ray `BDMV/PLAYLIST` listing** against a real BD filesystem -
   confirmed correct in isolation (real Windows UDF drives expose it as
   an ordinary readable directory) but not against actual BD authoring
   quirks (hidden/backup playlists, unusual numbering).
4. **AACS** - you supply your own `KEYDB.cfg`; behavior on discs your key
   database doesn't cover is whatever `libbluray`/`libaacs` report,
   surfaced as-is through the rip log pane.

If title/playlist enumeration or the rip itself fails on your first real
disc, the log pane's `ffmpeg`/`ffprobe` stderr output is the place to
look first - `disc.py` is written to distinguish "your ffmpeg build is
missing a demuxer" from "the disc couldn't be read" in its error
messages, which should narrow down which of the above it is.
