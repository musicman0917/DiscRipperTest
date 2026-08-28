"""Data model for discs, titles, and tracks.

These are plain dataclasses shared between disc.py (scanning), ripper.py
(command building), and gui.py (display/selection state).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DiscType(Enum):
    DVD = "dvd"
    BLURAY = "bluray"


class TrackType(Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"


@dataclass
class Track:
    # Absolute ffprobe/ffmpeg stream index for this title, used as -map 0:<stream_index>.
    stream_index: int
    track_type: TrackType
    codec: str
    language: str | None = None
    channels: int | None = None
    title: str | None = None
    default: bool = False
    forced: bool = False
    selected: bool = True

    @property
    def label(self) -> str:
        parts = [self.track_type.value.capitalize(), self.codec]
        if self.language:
            parts.append(self.language)
        if self.channels:
            parts.append(f"{self.channels}ch")
        if self.title:
            parts.append(self.title)
        if self.forced:
            parts.append("forced")
        elif self.default:
            parts.append("default")
        return " - ".join(parts)


@dataclass
class Title:
    # DVD: value passed as -title N. Blu-ray: value passed as -playlist N.
    index: int
    duration_seconds: float
    chapters: int = 0
    tracks: list[Track] = field(default_factory=list)

    @property
    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.track_type is TrackType.VIDEO]

    @property
    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.track_type is TrackType.AUDIO]

    @property
    def subtitle_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.track_type is TrackType.SUBTITLE]

    @property
    def duration_label(self) -> str:
        total = int(self.duration_seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}"

    @property
    def label(self) -> str:
        return f"Title {self.index} ({self.duration_label}, {len(self.tracks)} tracks)"


@dataclass
class Disc:
    drive_letter: str
    disc_type: DiscType
    label: str | None = None
    titles: list[Title] = field(default_factory=list)

    @property
    def main_feature(self) -> Title | None:
        """Longest title on the disc - the same heuristic MakeMKV uses to guess
        the main movie/episode as opposed to menus, trailers, or extras."""
        if not self.titles:
            return None
        return max(self.titles, key=lambda t: t.duration_seconds)
