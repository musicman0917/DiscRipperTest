from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src import gui as gui_module
from src.models import Disc, DiscType, Title, Track, TrackType


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """QMessageBox.exec() blocks waiting for a click that will never come
    under the offscreen QPA platform - stub both so a bug that triggers an
    unexpected dialog fails loudly instead of hanging the test suite."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        gui_module.QMessageBox, "critical", staticmethod(lambda *a, **k: calls.append(("critical", a)))
    )
    monkeypatch.setattr(
        gui_module.QMessageBox, "warning", staticmethod(lambda *a, **k: calls.append(("warning", a)))
    )
    return calls


def _pump(app, ms=200):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _sample_disc() -> Disc:
    return Disc(
        drive_letter="G:",
        disc_type=DiscType.DVD,
        titles=[
            Title(
                index=1,
                duration_seconds=300,
                tracks=[Track(stream_index=0, track_type=TrackType.VIDEO, codec="h264")],
            ),
            Title(
                index=2,
                duration_seconds=5400,
                tracks=[
                    Track(stream_index=0, track_type=TrackType.VIDEO, codec="h264"),
                    Track(
                        stream_index=1,
                        track_type=TrackType.AUDIO,
                        codec="ac3",
                        language="eng",
                        channels=6,
                    ),
                ],
            ),
        ],
    )


def test_window_constructs_with_no_drives(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: [])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    window = gui_module.MainWindow()
    try:
        assert not window.drive_combo.isEnabled()
        assert not window.rip_btn.isEnabled()
    finally:
        window.close()


def test_scan_populates_titles_and_selects_main_feature(app, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe: disc)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        assert window._scan_thread is not None
        window._scan_thread.wait(2000)
        _pump(app)

        assert window.title_combo.count() == 2
        # main_feature is the 5400s title (index 2) - should be auto-selected.
        selected_title: Title = window.title_combo.itemData(window.title_combo.currentIndex())
        assert selected_title.index == 2
        assert window.track_tree.topLevelItemCount() == 2
        assert window.rip_btn.isEnabled()
    finally:
        window.close()


def test_unchecking_track_updates_model_selection(app, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe: disc)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        title: Title = window.title_combo.itemData(window.title_combo.currentIndex())
        audio_track = title.audio_tracks[0]
        assert audio_track.selected is True

        item = window.track_tree.topLevelItem(1)
        checkbox = window.track_tree.itemWidget(item, 1)
        checkbox.setChecked(False)
        assert audio_track.selected is False
    finally:
        window.close()


def test_rip_flow_updates_progress_and_reenables_buttons(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe: disc)

    class FakeRipper:
        def __init__(self, ffmpeg_path):
            self.ffmpeg_path = ffmpeg_path

        def rip(self, disc, title, tracks, output_path, on_progress=None, on_log=None):
            from src.ripper import RipProgress

            if on_log:
                on_log("Running: fake ffmpeg command")
            if on_progress:
                on_progress(
                    RipProgress(seconds_done=0, duration_seconds=title.duration_seconds)
                )
                on_progress(
                    RipProgress(
                        seconds_done=title.duration_seconds,
                        duration_seconds=title.duration_seconds,
                        finished=True,
                    )
                )
            output_path.write_bytes(b"fake mkv")

        def cancel(self):
            pass

    monkeypatch.setattr(gui_module, "Ripper", FakeRipper)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        window.output_dir_edit.setText(str(tmp_path))
        window._start_rip()
        assert window._rip_thread is not None
        window._rip_thread.wait(2000)
        _pump(app)

        assert window.progress_bar.value() == 1000
        assert window.rip_btn.isEnabled()
        assert not window.cancel_btn.isEnabled()
        assert (tmp_path / "Title_02.mkv").exists()
        assert "Running: fake ffmpeg command" in window.log_view.toPlainText()
    finally:
        window.close()


def test_rip_requires_tracks_selected(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = Disc(
        drive_letter="G:",
        disc_type=DiscType.DVD,
        titles=[
            Title(
                index=1,
                duration_seconds=100,
                tracks=[
                    Track(
                        stream_index=0,
                        track_type=TrackType.VIDEO,
                        codec="h264",
                        selected=False,
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe: disc)
    monkeypatch.setattr(gui_module.QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        window.output_dir_edit.setText(str(tmp_path))
        window._start_rip()
        # No tracks selected -> should bail out without starting a rip thread.
        assert window._rip_thread is None
    finally:
        window.close()
