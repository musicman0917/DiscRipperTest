from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Disc, DiscType, Title, Track, TrackType


def _track(idx, ttype, **kw) -> Track:
    return Track(stream_index=idx, track_type=ttype, codec="x", **kw)


def test_main_feature_picks_longest_title():
    disc = Disc(
        drive_letter="G:",
        disc_type=DiscType.DVD,
        titles=[
            Title(index=1, duration_seconds=120),
            Title(index=2, duration_seconds=5400),
            Title(index=3, duration_seconds=600),
        ],
    )
    assert disc.main_feature.index == 2


def test_main_feature_none_when_no_titles():
    disc = Disc(drive_letter="G:", disc_type=DiscType.DVD)
    assert disc.main_feature is None


def test_title_track_filters():
    title = Title(
        index=1,
        duration_seconds=100,
        tracks=[
            _track(0, TrackType.VIDEO),
            _track(1, TrackType.AUDIO, language="eng"),
            _track(2, TrackType.AUDIO, language="spa"),
            _track(3, TrackType.SUBTITLE, language="eng"),
        ],
    )
    assert [t.stream_index for t in title.video_tracks] == [0]
    assert [t.stream_index for t in title.audio_tracks] == [1, 2]
    assert [t.stream_index for t in title.subtitle_tracks] == [3]


def test_track_label_includes_language_and_channels():
    track = _track(1, TrackType.AUDIO, language="eng", channels=6)
    assert "eng" in track.label
    assert "6ch" in track.label


def test_duration_label_formats_hms():
    title = Title(index=1, duration_seconds=3725)
    assert title.duration_label == "1:02:05"
