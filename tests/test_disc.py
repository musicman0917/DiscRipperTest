from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import disc
from src.models import DiscType

FAKE_FFPROBE = str(Path(__file__).resolve().parent / "fake_ffprobe.py")


def _ffprobe_json(duration: float) -> str:
    return json.dumps(
        {
            "programs": [],
            "streams": [
                {
                    "index": 0,
                    "codec_name": "h264",
                    "codec_type": "video",
                    "disposition": {"default": 1, "forced": 0},
                    "tags": {},
                },
                {
                    "index": 1,
                    "codec_name": "ac3",
                    "codec_type": "audio",
                    "channels": 6,
                    "disposition": {"default": 1, "forced": 0},
                    "tags": {"language": "eng"},
                },
            ],
            "chapters": [{"id": 0}, {"id": 1}],
            "format": {"duration": f"{duration:.6f}"},
        }
    )


def _write_plan(tmp_path: Path, plan: dict) -> Path:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


@pytest.fixture
def fake_ffprobe_env(monkeypatch, tmp_path):
    def _set(plan: dict) -> str:
        plan_path = _write_plan(tmp_path, plan)
        monkeypatch.setenv("FAKE_FFPROBE_PLAN", str(plan_path))
        return FAKE_FFPROBE

    return _set


def test_list_optical_drives_returns_empty_off_windows():
    assert disc.list_optical_drives() == []


def test_detect_disc_type_dvd(tmp_path):
    (tmp_path / "VIDEO_TS").mkdir()
    assert disc.detect_disc_type(str(tmp_path)) is DiscType.DVD


def test_detect_disc_type_bluray(tmp_path):
    (tmp_path / "BDMV").mkdir()
    assert disc.detect_disc_type(str(tmp_path)) is DiscType.BLURAY


def test_detect_disc_type_none(tmp_path):
    assert disc.detect_disc_type(str(tmp_path)) is None


def test_scan_dvd_stops_at_first_gap(tmp_path, fake_ffprobe_env):
    (tmp_path / "VIDEO_TS").mkdir()
    ffprobe = fake_ffprobe_env(
        {
            "title:1": {"exit": 0, "stdout": _ffprobe_json(600)},
            "title:2": {"exit": 0, "stdout": _ffprobe_json(5400)},
            "title:3": {"exit": 0, "stdout": _ffprobe_json(120)},
            "default": {"exit": 1, "stdout": "", "stderr": "Title 4 not found\n"},
        }
    )
    result = disc.scan_disc(str(tmp_path), ffprobe)
    assert result.disc_type is DiscType.DVD
    assert [t.index for t in result.titles] == [1, 2, 3]
    assert result.main_feature.index == 2
    assert result.titles[0].chapters == 2
    assert result.titles[1].audio_tracks[0].language == "eng"


def test_scan_dvd_missing_disc_raises_scan_error(tmp_path, fake_ffprobe_env):
    (tmp_path / "VIDEO_TS").mkdir()
    ffprobe = fake_ffprobe_env({"default": {"exit": 1, "stdout": "", "stderr": "I/O error\n"}})
    with pytest.raises(disc.DiscScanError):
        disc.scan_disc(str(tmp_path), ffprobe)


def test_scan_dvd_missing_demuxer_raises_build_error(tmp_path, fake_ffprobe_env):
    (tmp_path / "VIDEO_TS").mkdir()
    ffprobe = fake_ffprobe_env(
        {
            "default": {
                "exit": 1,
                "stdout": "",
                "stderr": "Unknown input format: dvdvideo\n",
            }
        }
    )
    with pytest.raises(disc.FfmpegBuildError):
        disc.scan_disc(str(tmp_path), ffprobe)


def test_scan_bluray_uses_playlist_directory_listing(tmp_path, fake_ffprobe_env):
    (tmp_path / "BDMV").mkdir()
    playlist_dir = tmp_path / "BDMV" / "PLAYLIST"
    playlist_dir.mkdir()
    for name in ("00000.mpls", "00001.mpls", "00800.mpls"):
        (playlist_dir / name).touch()

    ffprobe = fake_ffprobe_env(
        {
            "playlist:0": {"exit": 0, "stdout": _ffprobe_json(30)},
            "playlist:1": {"exit": 0, "stdout": _ffprobe_json(7200)},
            "playlist:800": {"exit": 0, "stdout": _ffprobe_json(45)},
            # Deliberately no "default" entry: if the code fell back to
            # brute-force probing instead of using the directory listing,
            # any other playlist number would hit this and fail the test.
        }
    )
    result = disc.scan_disc(str(tmp_path), ffprobe)
    assert result.disc_type is DiscType.BLURAY
    assert [t.index for t in result.titles] == [0, 1, 800]
    assert result.main_feature.index == 1


def test_scan_bluray_falls_back_to_brute_force_without_playlist_dir(
    tmp_path, fake_ffprobe_env
):
    (tmp_path / "BDMV").mkdir()
    ffprobe = fake_ffprobe_env(
        {
            "playlist:0": {"exit": 0, "stdout": _ffprobe_json(30)},
            "playlist:1": {"exit": 0, "stdout": _ffprobe_json(6000)},
            "default": {"exit": 1, "stdout": "", "stderr": "no playlist\n"},
        }
    )
    result = disc.scan_disc(str(tmp_path), ffprobe)
    assert [t.index for t in result.titles] == [0, 1]


def test_scan_no_disc_structure_raises(tmp_path, fake_ffprobe_env):
    ffprobe = fake_ffprobe_env({})
    with pytest.raises(disc.DiscScanError):
        disc.scan_disc(str(tmp_path), ffprobe)


def test_scan_bluray_error_surfaces_real_ffprobe_stderr(tmp_path, fake_ffprobe_env):
    # Regression test: a real disc (40 valid .mpls files, every probe
    # failing with an AACS error) used to be reported as just "playlist 39"
    # with no hint of *why* - the actual ffprobe stderr must reach the user.
    (tmp_path / "BDMV").mkdir()
    playlist_dir = tmp_path / "BDMV" / "PLAYLIST"
    playlist_dir.mkdir()
    (playlist_dir / "00000.mpls").touch()

    ffprobe = fake_ffprobe_env(
        {
            "playlist:0": {
                "exit": 1,
                "stdout": "",
                "stderr": "aacs_open: AACS: Media key not found\n",
            }
        }
    )
    with pytest.raises(disc.DiscScanError, match="Media key not found"):
        disc.scan_disc(str(tmp_path), ffprobe)
