"""Persisted app settings: ffmpeg/ffprobe paths and last output folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def default_config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / "DiscRipper" / "config.json"


@dataclass
class Config:
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    last_output_dir: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or default_config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            ffmpeg_path=data.get("ffmpeg_path", ""),
            ffprobe_path=data.get("ffprobe_path", ""),
            last_output_dir=data.get("last_output_dir", ""),
        )

    def save(self, path: Path | None = None) -> None:
        path = path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def infer_ffprobe_path(self) -> str:
        """Best guess at ffprobe's path given ffmpeg's, when not set explicitly."""
        if self.ffprobe_path:
            return self.ffprobe_path
        if not self.ffmpeg_path:
            return ""
        ffmpeg_path = Path(self.ffmpeg_path)
        candidate = ffmpeg_path.with_name(
            ffmpeg_path.name.replace("ffmpeg", "ffprobe")
        )
        return str(candidate)
