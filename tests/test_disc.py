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


def test_dvd_device_path_uses_raw_device_syntax_on_windows(monkeypatch):
    # Confirmed on real hardware: "G:\\" fails with "libdvdnav: Unable to
    # open device file" even though Explorer and filesystem-level reads
    # work fine on that path - CSS's authentication handshake needs raw
    # block-device access, which Windows only grants via "\\.\G:", not a
    # normal directory open. "\\.\G:" opened and read the disc
    # successfully in the same test.
    monkeypatch.setattr(disc.sys, "platform", "win32")
    assert disc.dvd_device_path("G:") == r"\\.\G:"
    assert disc.dvd_device_path("G:\\") == r"\\.\G:"
    assert disc.dvd_device_path("G:/") == r"\\.\G:"


def test_dvd_device_path_falls_back_off_windows(tmp_path):
    # No raw-device syntax exists to test off Windows - falls back to the
    # plain filesystem path so this stays callable in dev/test.
    assert disc.dvd_device_path(str(tmp_path)) == disc.drive_root(str(tmp_path))


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


def test_read_bluray_disc_name_from_metadata(tmp_path):
    meta_dir = tmp_path / "BDMV" / "META" / "DL"
    meta_dir.mkdir(parents=True)
    (meta_dir / "bdmt_eng.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<disclib xmlns:di="urn:BDA:bdmv;discinfo" xmlns="urn:BDA:bdmv;discinfo:local">\n'
        "  <di:discinfo>\n"
        "    <di:title>\n"
        "      <di:name>Yu Yu Hakusho: Season 4, Disc 1</di:name>\n"
        "    </di:title>\n"
        "  </di:discinfo>\n"
        "</disclib>\n",
        encoding="utf-8",
    )
    assert disc._read_bluray_disc_name(tmp_path) == "Yu Yu Hakusho: Season 4, Disc 1"


def test_read_bluray_disc_name_missing_returns_none(tmp_path):
    assert disc._read_bluray_disc_name(tmp_path) is None


def test_read_bluray_disc_name_malformed_xml_returns_none(tmp_path):
    meta_dir = tmp_path / "BDMV" / "META" / "DL"
    meta_dir.mkdir(parents=True)
    (meta_dir / "bdmt_eng.xml").write_text("<not valid xml", encoding="utf-8")
    assert disc._read_bluray_disc_name(tmp_path) is None


def test_disc_label_off_windows_is_none(tmp_path):
    # _read_volume_label is Windows-only (uses ctypes.windll); off-Windows
    # a DVD with no metadata source at all should just come back None.
    assert disc.disc_label(str(tmp_path), DiscType.DVD) is None


def test_disc_label_bluray_prefers_metadata_over_volume_label(tmp_path, monkeypatch):
    meta_dir = tmp_path / "BDMV" / "META" / "DL"
    meta_dir.mkdir(parents=True)
    (meta_dir / "bdmt_eng.xml").write_text(
        '<disclib xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo>'
        "<di:title><di:name>Yu Yu Hakusho: Season 4, Disc 1</di:name></di:title>"
        "</di:discinfo></disclib>",
        encoding="utf-8",
    )
    monkeypatch.setattr(disc, "_read_volume_label", lambda drive: "YUYUHAKUSHO_S4D1")
    assert (
        disc.disc_label(str(tmp_path), DiscType.BLURAY)
        == "Yu Yu Hakusho: Season 4, Disc 1"
    )


def test_disc_label_bluray_falls_back_to_volume_label(tmp_path, monkeypatch):
    # No BDMV/META/DL at all - should fall back rather than returning None.
    monkeypatch.setattr(disc, "_read_volume_label", lambda drive: "YUYUHAKUSHO_S4D1")
    assert disc.disc_label(str(tmp_path), DiscType.BLURAY) == "YUYUHAKUSHO_S4D1"


def test_scan_bluray_populates_disc_label(tmp_path, fake_ffprobe_env):
    (tmp_path / "BDMV").mkdir()
    playlist_dir = tmp_path / "BDMV" / "PLAYLIST"
    playlist_dir.mkdir()
    (playlist_dir / "00000.mpls").touch()
    meta_dir = tmp_path / "BDMV" / "META" / "DL"
    meta_dir.mkdir(parents=True)
    (meta_dir / "bdmt_eng.xml").write_text(
        '<disclib xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo>'
        "<di:title><di:name>Yu Yu Hakusho: Season 4, Disc 1</di:name></di:title>"
        "</di:discinfo></disclib>",
        encoding="utf-8",
    )

    ffprobe = fake_ffprobe_env({"playlist:0": {"exit": 0, "stdout": _ffprobe_json(2700)}})
    result = disc.scan_disc(str(tmp_path), ffprobe)
    assert result.label == "Yu Yu Hakusho: Season 4, Disc 1"


class _FakeKernel32:
    def __init__(self):
        self.calls: list[tuple] = []

    def GetVolumeInformationW(self, root, buf, size, *rest):
        self.calls.append((root, buf, size))
        buf.value = "TESTLABEL"
        return 1


class _FakeWindll:
    def __init__(self):
        self.kernel32 = _FakeKernel32()


def test_read_volume_label_passes_character_count_not_byte_size(monkeypatch):
    # Regression test for a real heap buffer overflow: GetVolumeInformationW's
    # size argument must be the buffer's *character* capacity (261), but the
    # original code passed ctypes.sizeof(buf) - the buffer's *byte* size
    # (522, since Windows wide chars are 2 bytes) - telling the Win32 API
    # the buffer was twice as large as it actually was. Confirmed on real
    # hardware: the same physical disc's volume label came back different
    # (a dropped character) across separate scans, consistent with heap
    # corruption from writing past the buffer.
    import ctypes as ctypes_module

    monkeypatch.setattr(disc.sys, "platform", "win32")
    fake_windll = _FakeWindll()
    monkeypatch.setattr(ctypes_module, "windll", fake_windll, raising=False)

    label = disc._read_volume_label("G:")

    assert label == "TESTLABEL"
    _, buf, size = fake_windll.kernel32.calls[0]
    assert size == len(buf) == 261
