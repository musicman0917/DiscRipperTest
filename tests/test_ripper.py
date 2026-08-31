from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import disc as disc_module
from src import ripper as ripper_module
from src.models import Disc, DiscType, Title, Track, TrackType
from src.ripper import (
    RipError,
    RipProgress,
    Ripper,
    build_rip_command,
    parse_progress_stream,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _dvd_disc() -> Disc:
    return Disc(drive_letter="G:", disc_type=DiscType.DVD)


def _bluray_disc() -> Disc:
    return Disc(drive_letter="G:", disc_type=DiscType.BLURAY)


def _tracks() -> list[Track]:
    return [
        Track(stream_index=0, track_type=TrackType.VIDEO, codec="h264"),
        Track(stream_index=1, track_type=TrackType.AUDIO, codec="ac3"),
    ]


def test_build_rip_command_dvd():
    title = Title(index=3, duration_seconds=5400)
    cmd = build_rip_command("ffmpeg", _dvd_disc(), title, _tracks(), Path("out.mkv"))
    assert cmd[0] == "ffmpeg"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "dvdvideo"
    assert "-title" in cmd and cmd[cmd.index("-title") + 1] == "3"
    assert "-i" in cmd
    input_arg = cmd[cmd.index("-i") + 1]
    assert input_arg == "G:" + os.sep
    assert "-map" in cmd
    assert cmd.count("-map") == 2
    assert "0:0" in cmd
    assert "0:1" in cmd
    assert "-map_chapters" in cmd
    assert cmd[-1] == "out.mkv"
    assert "-progress" in cmd and cmd[cmd.index("-progress") + 1] == "pipe:1"


def test_build_rip_command_includes_xerror():
    # Regression: confirmed on real hardware that without -xerror, ffmpeg
    # can exit 0 despite a fatal demux read failure (e.g. the drive never
    # opened), which Ripper.rip()'s exit-code check would silently treat
    # as a successful rip. -xerror makes ffmpeg's own exit code honest.
    title = Title(index=1, duration_seconds=100)
    cmd = build_rip_command("ffmpeg", _dvd_disc(), title, _tracks(), Path("out.mkv"))
    assert "-xerror" in cmd


def test_build_rip_command_dvd_uses_raw_device_path_on_windows(monkeypatch):
    # Regression: build_rip_command used to pass "G:\" (a plain directory
    # path) as -i, which failed on real hardware with "libdvdnav: Unable
    # to open device file" - CSS needs raw block-device access via
    # "\\.\G:", confirmed working on the same disc.
    monkeypatch.setattr(disc_module.sys, "platform", "win32")
    title = Title(index=1, duration_seconds=100)
    cmd = build_rip_command("ffmpeg", _dvd_disc(), title, _tracks(), Path("out.mkv"))
    input_arg = cmd[cmd.index("-i") + 1]
    assert input_arg == r"\\.\G:"


def test_build_rip_command_bluray():
    title = Title(index=800, duration_seconds=7200)
    cmd = build_rip_command("ffmpeg", _bluray_disc(), title, _tracks(), Path("out.mkv"))
    assert "-playlist" in cmd and cmd[cmd.index("-playlist") + 1] == "800"
    input_arg = cmd[cmd.index("-i") + 1]
    assert input_arg == "bluray:G:/"
    assert "-map_chapters" not in cmd  # DVD-only chapter mapping


def test_build_rip_command_requires_tracks():
    title = Title(index=1, duration_seconds=100)
    with pytest.raises(ValueError):
        build_rip_command("ffmpeg", _dvd_disc(), title, [], Path("out.mkv"))


def test_parse_progress_stream_real_ffmpeg_output():
    # Captured verbatim from a live `ffmpeg -progress pipe:1 -nostats` remux.
    lines = [
        "bitrate=  -0.0kbits/s\n",
        "total_size=0\n",
        "out_time_us=-57000\n",
        "out_time_ms=-57000\n",
        "out_time=-00:00:00.057000\n",
        "dup_frames=0\n",
        "drop_frames=0\n",
        "speed=N/A\n",
        "progress=continue\n",
        "bitrate= 117.8kbits/s\n",
        "total_size=44452\n",
        "out_time_us=3018000\n",
        "out_time_ms=3018000\n",
        "out_time=00:00:03.018000\n",
        "dup_frames=0\n",
        "drop_frames=0\n",
        "speed= 471x\n",
        "progress=end\n",
    ]
    updates: list[RipProgress] = []
    parse_progress_stream(lines, duration_seconds=3.023, on_progress=updates.append)

    assert len(updates) == 2
    first, last = updates
    assert first.seconds_done == 0.0  # negative out_time_us clamped to 0
    assert first.finished is False
    assert last.seconds_done == pytest.approx(3.018)
    assert last.finished is True
    assert last.speed == "471x"
    assert last.fraction == pytest.approx(3.018 / 3.023, rel=1e-3)


@pytest.mark.skipif(not HAS_FFMPEG, reason="requires a real ffmpeg binary")
def test_ripper_rip_runs_real_ffmpeg_subprocess(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=duration=1",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "aac", str(source),
        ],
        check=True,
    )

    def fake_build_command(ffmpeg_path, disc, title, tracks, output_path):
        return [
            ffmpeg_path, "-y", "-nostdin", "-loglevel", "error",
            "-i", str(source),
            "-map", "0:0", "-map", "0:1", "-c", "copy",
            "-progress", "pipe:1", "-nostats", str(output_path),
        ]

    monkeypatch.setattr(ripper_module, "build_rip_command", fake_build_command)

    output = tmp_path / "out.mkv"
    progress_updates: list[RipProgress] = []
    log_lines: list[str] = []

    r = Ripper("ffmpeg")
    r.rip(
        _dvd_disc(),
        Title(index=1, duration_seconds=1.0),
        _tracks(),
        output,
        on_progress=progress_updates.append,
        on_log=log_lines.append,
    )

    assert output.exists()
    assert output.stat().st_size > 0
    assert any(u.finished for u in progress_updates)
    assert any("Running:" in line for line in log_lines)


@pytest.mark.skipif(not HAS_FFMPEG, reason="requires a real ffmpeg binary")
def test_ripper_rip_raises_riperror_on_bad_command(tmp_path, monkeypatch):
    def fake_build_command(ffmpeg_path, disc, title, tracks, output_path):
        return [ffmpeg_path, "-i", "/no/such/file.mkv", str(output_path)]

    monkeypatch.setattr(ripper_module, "build_rip_command", fake_build_command)

    r = Ripper("ffmpeg")
    with pytest.raises(RipError):
        r.rip(_dvd_disc(), Title(index=1, duration_seconds=1.0), _tracks(), tmp_path / "out.mkv")
