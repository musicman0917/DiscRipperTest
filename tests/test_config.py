from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config


def test_round_trip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(ffmpeg_path="C:\\ffmpeg\\ffmpeg.exe", last_output_dir="D:\\Rips")
    cfg.save(path)

    loaded = Config.load(path)
    assert loaded.ffmpeg_path == cfg.ffmpeg_path
    assert loaded.last_output_dir == cfg.last_output_dir


def test_load_missing_file_returns_defaults(tmp_path):
    loaded = Config.load(tmp_path / "does-not-exist.json")
    assert loaded == Config()


def test_load_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("not json", encoding="utf-8")
    assert Config.load(path) == Config()


def test_infer_ffprobe_path_from_ffmpeg_path():
    cfg = Config(ffmpeg_path="C:\\tools\\ffmpeg.exe")
    assert cfg.infer_ffprobe_path() == "C:\\tools\\ffprobe.exe"


def test_infer_ffprobe_path_prefers_explicit_value():
    cfg = Config(ffmpeg_path="C:\\tools\\ffmpeg.exe", ffprobe_path="D:\\other\\ffprobe.exe")
    assert cfg.infer_ffprobe_path() == "D:\\other\\ffprobe.exe"


def test_infer_ffprobe_path_empty_when_unset():
    assert Config().infer_ffprobe_path() == ""
