"""Builds and runs the `ffmpeg -c copy` rip command for a selected title.

ffmpeg does 100% of the decode/decrypt/remux work here; this module only
constructs the argv list and parses the -progress pipe:1 stream it emits.
No disc content is read or interpreted in Python.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .disc import drive_root
from .models import Disc, DiscType, Title, Track


@dataclass
class RipProgress:
    seconds_done: float
    duration_seconds: float
    speed: str | None = None
    finished: bool = False

    @property
    def fraction(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, self.seconds_done / self.duration_seconds))


class RipError(RuntimeError):
    """ffmpeg exited non-zero. Carries the tail of stderr for the log pane."""


class RipCancelled(RuntimeError):
    """Raised when a rip was stopped via Ripper.cancel()."""


def _disc_input_target(disc: Disc) -> str:
    root = drive_root(disc.drive_letter)
    if disc.disc_type is DiscType.BLURAY:
        return "bluray:" + root.replace("\\", "/")
    return root


def build_rip_command(
    ffmpeg_path: str,
    disc: Disc,
    title: Title,
    tracks: list[Track],
    output_path: Path,
) -> list[str]:
    if not tracks:
        raise ValueError("At least one track must be selected to rip")

    cmd = [ffmpeg_path, "-y", "-nostdin", "-loglevel", "error"]

    if disc.disc_type is DiscType.DVD:
        cmd += ["-f", "dvdvideo", "-title", str(title.index)]
    elif disc.disc_type is DiscType.BLURAY:
        cmd += ["-playlist", str(title.index)]
    else:
        raise ValueError(f"Unsupported disc type: {disc.disc_type}")

    cmd += ["-i", _disc_input_target(disc)]

    for track in tracks:
        cmd += ["-map", f"0:{track.stream_index}"]

    cmd += ["-c", "copy"]
    if disc.disc_type is DiscType.DVD:
        # dvdvideo exposes PTTs (chapters) that ffmpeg can carry into the
        # output automatically; nothing extra needed for -map_chapters 0
        # since input 0 is the only input.
        cmd += ["-map_chapters", "0"]

    cmd += ["-progress", "pipe:1", "-nostats", str(output_path)]
    return cmd


# ffmpeg -progress key=value line, e.g. "out_time_us=3018000".
_PROGRESS_LINE_RE = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")


def _seconds_from_fields(fields: dict[str, str]) -> float:
    # out_time_us/out_time_ms are both in microseconds despite the "_ms"
    # name (verified against a live ffmpeg 6.1.1: a 3.018s remux reported
    # out_time_ms=3018000, i.e. microseconds, not milliseconds).
    raw = fields.get("out_time_us") or fields.get("out_time_ms")
    if raw is not None:
        try:
            return max(0.0, int(raw) / 1_000_000)
        except ValueError:
            pass
    out_time = fields.get("out_time")
    if out_time:
        try:
            h, m, s = out_time.split(":")
            return max(0.0, int(h) * 3600 + int(m) * 60 + float(s))
        except ValueError:
            pass
    return 0.0


def parse_progress_stream(
    lines: object,
    duration_seconds: float,
    on_progress: Callable[[RipProgress], None],
) -> None:
    """Consume ffmpeg's -progress pipe:1 output (an iterable of lines) and
    call on_progress once per progress=... block."""
    fields: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = _PROGRESS_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        fields[key] = value
        if key == "progress":
            on_progress(
                RipProgress(
                    seconds_done=_seconds_from_fields(fields),
                    duration_seconds=duration_seconds,
                    speed=fields.get("speed"),
                    finished=(value == "end"),
                )
            )
            fields = {}


class Ripper:
    """Runs one rip at a time. Not thread-safe for concurrent rips by
    design - DiscRipper rips one title at a time, same as MakeMKV's basic
    flow."""

    def __init__(self, ffmpeg_path: str):
        self.ffmpeg_path = ffmpeg_path
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def rip(
        self,
        disc: Disc,
        title: Title,
        tracks: list[Track],
        output_path: Path,
        on_progress: Callable[[RipProgress], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        cmd = build_rip_command(self.ffmpeg_path, disc, title, tracks, output_path)
        if on_log:
            on_log("Running: " + " ".join(cmd))

        self._cancelled = False
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RipError(f"ffmpeg not found at {self.ffmpeg_path!r}") from exc
        self._process = process

        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line)
                if on_log:
                    on_log(line.rstrip())

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        assert process.stdout is not None
        parse_progress_stream(
            process.stdout,
            title.duration_seconds,
            on_progress or (lambda _p: None),
        )

        process.wait()
        stderr_thread.join(timeout=5)
        self._process = None

        if self._cancelled:
            raise RipCancelled(f"Rip of title {title.index} was cancelled")
        if process.returncode != 0:
            tail = "".join(stderr_lines[-40:])
            raise RipError(
                f"ffmpeg exited with code {process.returncode} for title "
                f"{title.index}:\n{tail}"
            )

    def cancel(self) -> None:
        self._cancelled = True
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
