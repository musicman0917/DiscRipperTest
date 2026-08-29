"""Optical drive detection and title/playlist enumeration.

All decryption (CSS for DVD, AACS for Blu-ray) happens inside ffmpeg's own
libdvdread/libdvdcss/libbluray/libaacs stack - this module only shells out
to ffprobe to ask "what titles/playlists exist and what's in them" and never
touches disc content itself.

ffmpeg CLI details below (demuxer/protocol option names, error strings) were
verified against ffmpeg's own source (libavformat/dvdvideodec.c and
bluray.c) and a live ffmpeg 6.1.1 build, not guessed - see README.md for
notes on what still needs validation against a real disc.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from .models import Disc, DiscType, Title, Track, TrackType

DRIVE_CDROM = 5  # Windows GetDriveType() return value for optical drives.

# Combined ffprobe query used for every title/playlist probe. Verified
# against a real ffprobe 6.1.1 to produce the exact JSON shape
# _parse_json_to_title() expects.
_SHOW_ENTRIES = (
    "format=duration:"
    "stream=index,codec_type,codec_name,channels:"
    "stream_tags=language,title:"
    "stream_disposition=default,forced"
)


class FfmpegBuildError(RuntimeError):
    """The configured ffmpeg/ffprobe build is missing something required
    (e.g. compiled without libdvdread, so the 'dvdvideo' demuxer doesn't
    exist). Not a disc problem - a build problem."""


class DiscScanError(RuntimeError):
    """The disc itself couldn't be read (no disc, tray open, bad drive,
    I/O error) as opposed to simply running out of titles/playlists."""


def list_optical_drives() -> list[str]:
    """Return drive letters (e.g. ["D:", "G:"]) for attached optical drives.

    Windows-only (uses GetDriveType via ctypes); returns [] elsewhere so the
    rest of the app can be developed/tested off Windows.
    """
    if sys.platform != "win32":
        return []

    import ctypes

    drives: list[str] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = chr(ord("A") + i)
        root = f"{letter}:\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(root)
        )
        if drive_type == DRIVE_CDROM:
            drives.append(f"{letter}:")
    return drives


def drive_root(drive: str) -> str:
    """Normalize a drive spec ("G:", "G:\\") into its filesystem root,
    *as a string with a trailing separator*.

    The trailing separator matters on Windows: "G:" alone means "the
    current directory on drive G", while "G:\\" means the drive's root -
    dropping it would silently point ffmpeg somewhere unpredictable. Kept
    as a plain str (not run through pathlib) because Path collapses
    trailing separators back off on POSIX, which would defeat the point
    when this runs under non-Windows tests.

    Uses os.sep rather than a hardcoded backslash so this (and the disc
    scanning built on it) stays testable on non-Windows dev machines: on
    Windows it's still "G:\\" as ffmpeg/libdvdread/libbluray expect.
    """
    return drive.rstrip("\\/") + os.sep


def detect_disc_type(drive: str) -> DiscType | None:
    """Filesystem-only check - both VIDEO_TS and BDMV are plain directories
    on the disc's filesystem (UDF/ISO9660), readable without any ffmpeg
    involvement or decryption."""
    root = Path(drive_root(drive))
    if (root / "BDMV").is_dir():
        return DiscType.BLURAY
    if (root / "VIDEO_TS").is_dir():
        return DiscType.DVD
    return None


class ProbeResult(NamedTuple):
    data: dict | None
    stderr: str  # empty on success; the real ffprobe stderr on failure


def _run_ffprobe_json(ffprobe_path: str, extra_args: list[str]) -> ProbeResult:
    """Run ffprobe with _SHOW_ENTRIES plus extra_args, parsed as JSON.

    data is None for anything that should be treated as "nothing here"
    (used by callers to know when to stop enumerating titles/playlists);
    stderr carries ffprobe's actual error text in that case so callers can
    put something diagnosable in front of the user instead of just "it
    failed" - e.g. distinguishing "no such playlist" from an AACS/CSS
    decryption failure.
    Raises FfmpegBuildError if the failure is clearly a missing
    demuxer/protocol rather than a normal probe failure.
    """
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", _SHOW_ENTRIES,
        "-show_chapters",
        "-of", "json",
        *extra_args,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise FfmpegBuildError(f"ffprobe not found at {ffprobe_path!r}") from exc
    except subprocess.TimeoutExpired:
        return ProbeResult(None, "ffprobe timed out (disc may still be spinning up)")

    stderr = result.stderr.strip()
    if result.returncode != 0:
        # Confirmed verbatim on a build without libdvdread:
        #   "Unknown input format: dvdvideo"
        #   "Failed to set value 'dvdvideo' for option 'f': Invalid argument"
        if "Unknown input format" in stderr or "Requested output format" in stderr:
            raise FfmpegBuildError(
                "This ffmpeg/ffprobe build doesn't support the demuxer/protocol "
                f"this disc needs (ffprobe said: {stderr!r}). You need a build "
                "compiled with libdvdread (DVD) and libbluray (Blu-ray) - e.g. "
                "the 'full' build from gyan.dev, not a minimal/'essentials' one."
            )
        return ProbeResult(None, stderr or f"ffprobe exited {result.returncode} with no output")
    if not result.stdout.strip():
        return ProbeResult(None, "ffprobe produced no output")
    try:
        return ProbeResult(json.loads(result.stdout), "")
    except json.JSONDecodeError:
        return ProbeResult(None, "ffprobe produced unparseable output")


def _parse_json_to_title(index: int, data: dict) -> Title:
    fmt = data.get("format", {}) or {}
    try:
        duration = float(fmt.get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0

    tracks: list[Track] = []
    for stream in data.get("streams", []) or []:
        codec_type = stream.get("codec_type")
        if codec_type not in ("video", "audio", "subtitle"):
            continue
        tags = stream.get("tags", {}) or {}
        disposition = stream.get("disposition", {}) or {}
        tracks.append(
            Track(
                stream_index=stream["index"],
                track_type=TrackType(codec_type),
                codec=stream.get("codec_name", "unknown"),
                language=tags.get("language"),
                channels=stream.get("channels"),
                title=tags.get("title"),
                default=bool(disposition.get("default")),
                forced=bool(disposition.get("forced")),
            )
        )
    chapters = len(data.get("chapters", []) or [])
    return Title(index=index, duration_seconds=duration, chapters=chapters, tracks=tracks)


def _scan_dvd(drive: str, ffprobe_path: str) -> list[Title]:
    # The dvdvideo demuxer's own range check is `title > tt_srpt->nr_of_srpts`
    # (source: libavformat/dvdvideodec.c), so valid titles are a contiguous
    # 1..N run - one failure means "past the end", no need to keep probing.
    root = drive_root(drive)
    titles: list[Title] = []
    for title_num in range(1, 100):
        data, stderr = _run_ffprobe_json(
            ffprobe_path, ["-f", "dvdvideo", "-title", str(title_num), root]
        )
        if data is None:
            if title_num == 1:
                raise DiscScanError(
                    f"Could not read any titles from {drive} as a DVD "
                    f"(ffprobe said: {stderr!r}). Check that a disc is "
                    "inserted, the tray is fully closed, and the drive has "
                    "finished spinning up, then scan again."
                )
            break
        title = _parse_json_to_title(title_num, data)
        if not title.tracks:
            break
        titles.append(title)
    return titles


def _scan_bluray(drive: str, ffprobe_path: str) -> list[Title]:
    root = drive_root(drive)
    target = "bluray:" + root.replace("\\", "/")
    playlist_dir = Path(root) / "BDMV" / "PLAYLIST"

    # Preferred: BDMV/PLAYLIST/*.mpls is a plain, unencrypted directory
    # listing on the disc's filesystem - reading it directly is far more
    # reliable than brute-force probing playlist numbers via ffprobe.
    playlist_numbers: list[int] = []
    if playlist_dir.is_dir():
        playlist_numbers = sorted(
            {int(p.stem) for p in playlist_dir.glob("*.mpls") if p.stem.isdigit()}
        )

    titles: list[Title] = []
    if playlist_numbers:
        last_error = ""
        for num in playlist_numbers:
            data, stderr = _run_ffprobe_json(ffprobe_path, ["-playlist", str(num), target])
            if data is None:
                last_error = stderr
                continue
            title = _parse_json_to_title(num, data)
            if title.tracks:
                titles.append(title)
        if not titles:
            raise DiscScanError(
                f"Found {len(playlist_numbers)} playlist file(s) under "
                f"BDMV/PLAYLIST on {drive} but ffprobe couldn't read any of "
                f"them (ffprobe said: {last_error!r}). If that mentions AACS "
                "or a decryption/key error, this disc needs a KEYDB.cfg "
                "covering it; otherwise check the disc and your libbluray "
                "setup."
            )
        return titles

    # Fallback for the unusual case where BDMV/PLAYLIST isn't directly
    # listable: brute-force probe playlist numbers, stopping after a run of
    # consecutive failures (original, untested-on-real-hardware heuristic).
    consecutive_failures = 0
    last_error = ""
    for num in range(0, 100):
        data, stderr = _run_ffprobe_json(ffprobe_path, ["-playlist", str(num), target])
        if data is None:
            last_error = stderr
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break
            continue
        consecutive_failures = 0
        title = _parse_json_to_title(num, data)
        if title.tracks:
            titles.append(title)

    if not titles:
        raise DiscScanError(
            f"Could not read any playlists from {drive} as a Blu-ray "
            "(neither BDMV/PLAYLIST listing nor brute-force probing found "
            f"anything; last ffprobe error: {last_error!r}). Check that the "
            "disc is inserted and readable."
        )
    return titles


def scan_disc(drive: str, ffprobe_path: str) -> Disc:
    disc_type = detect_disc_type(drive)
    if disc_type is None:
        raise DiscScanError(
            f"No VIDEO_TS (DVD) or BDMV (Blu-ray) folder found at the root "
            f"of {drive}. Is a disc inserted?"
        )
    if disc_type is DiscType.DVD:
        titles = _scan_dvd(drive, ffprobe_path)
    else:
        titles = _scan_bluray(drive, ffprobe_path)
    return Disc(drive_letter=drive, disc_type=disc_type, titles=titles)
