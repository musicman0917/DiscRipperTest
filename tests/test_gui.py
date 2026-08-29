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
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        assert window._scan_thread is not None
        window._scan_thread.wait(2000)
        _pump(app)

        assert window.title_tree.topLevelItemCount() == 2
        # main_feature is the 5400s title (index 2) - should be highlighted
        # (for track preview) and its checkbox pre-checked (for batch rip),
        # while the other title starts unchecked.
        current = window.title_tree.currentItem()
        from PySide6.QtCore import Qt

        selected_title: Title = current.data(0, Qt.ItemDataRole.UserRole)
        assert selected_title.index == 2
        checked = {t.index for t in window._checked_titles()}
        assert checked == {2}
        assert window.track_tree.topLevelItemCount() == 2
        assert window.rip_btn.isEnabled()
    finally:
        window.close()


def test_duration_filter_hides_short_titles_and_scopes_select_all(app, monkeypatch):
    from PySide6.QtCore import Qt

    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = Disc(
        drive_letter="G:",
        disc_type=DiscType.BLURAY,
        titles=[
            Title(
                index=0,
                duration_seconds=45,  # menu/trailer - under the 2 min default
                tracks=[Track(stream_index=0, track_type=TrackType.VIDEO, codec="h264")],
            ),
            Title(
                index=1,
                duration_seconds=2700,  # real content
                tracks=[Track(stream_index=0, track_type=TrackType.VIDEO, codec="h264")],
            ),
            Title(
                index=2,
                duration_seconds=2600,  # also real content
                tracks=[Track(stream_index=0, track_type=TrackType.VIDEO, codec="h264")],
            ),
        ],
    )
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        # Default filter (2 min) hides the 45s title but not the others.
        assert window.min_duration_spin.value() == 2
        items = [window.title_tree.topLevelItem(i) for i in range(3)]
        hidden_by_index = {
            item.data(0, Qt.ItemDataRole.UserRole).index: item.isHidden() for item in items
        }
        assert hidden_by_index == {0: True, 1: False, 2: False}

        # Select All must only check what's actually visible.
        window._set_all_titles_checked(True)
        assert {t.index for t in window._checked_titles()} == {1, 2}

        # Raising the filter past everything hides all three...
        window.min_duration_spin.setValue(60)
        assert all(item.isHidden() for item in items)

        # ...and dropping it back to 0 reveals them again.
        window.min_duration_spin.setValue(0)
        assert not any(item.isHidden() for item in items)
    finally:
        window.close()


def test_scan_progress_updates_status_label_and_progress_bar(app, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()

    def fake_scan_disc(drive, ffprobe, on_progress=None):
        if on_progress:
            # Mirrors what disc.py sends for a Blu-ray with a known playlist
            # count: a real total, so the bar should go determinate.
            on_progress(1, 40, "Reading Blu-ray playlist 0 (1/40)...")
            on_progress(40, 40, "Reading Blu-ray playlist 800 (40/40)...")
        return disc

    monkeypatch.setattr(gui_module, "scan_disc", fake_scan_disc)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        # Final state after scan completes: bar reset, status reflects result.
        assert window.progress_bar.maximum() == 1000
        assert "Found 2 title(s)" in window.status_label.text()
    finally:
        window.close()


def test_scan_progress_with_known_total_sets_determinate_range(app, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        # Call the slot directly - deterministic, no thread timing involved.
        window._on_scan_progress((5, 40, "Reading Blu-ray playlist 5 (5/40)..."))
        assert window.progress_bar.minimum() == 0
        assert window.progress_bar.maximum() == 40
        assert window.progress_bar.value() == 5
        assert window.status_label.text() == "Reading Blu-ray playlist 5 (5/40)..."

        # Unknown total (DVD probing) -> busy/indeterminate mode (range 0,0).
        window._on_scan_progress((3, None, "Reading DVD title 3..."))
        assert window.progress_bar.minimum() == 0
        assert window.progress_bar.maximum() == 0
    finally:
        window.close()


def test_unchecking_track_updates_model_selection(app, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        from PySide6.QtCore import Qt

        title: Title = window.title_tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
        audio_track = title.audio_tracks[0]
        assert audio_track.selected is True

        item = window.track_tree.topLevelItem(1)
        checkbox = window.track_tree.itemWidget(item, 1)
        checkbox.setChecked(False)
        assert audio_track.selected is False
    finally:
        window.close()


def test_scan_with_disc_label_sets_window_title_and_filename_prefix(
    app, tmp_path, monkeypatch
):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    disc.label = "Yu Yu Hakusho: Season 4, Disc 1"
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

    captured_paths: list[str] = []

    class FakeRipper:
        def __init__(self, ffmpeg_path):
            self.ffmpeg_path = ffmpeg_path

        def rip(self, disc, title, tracks, output_path, on_progress=None, on_log=None):
            captured_paths.append(str(output_path))
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

        assert window.windowTitle() == "DiscRipper - Yu Yu Hakusho: Season 4, Disc 1"
        assert "Yu Yu Hakusho: Season 4, Disc 1" in window.status_label.text()

        window.output_dir_edit.setText(str(tmp_path))
        window._start_rip()
        assert window._rip_thread is not None
        window._rip_thread.wait(2000)
        _pump(app)

        assert captured_paths == [
            str(tmp_path / "Yu Yu Hakusho_ Season 4, Disc 1_Title_02.mkv")
        ]
    finally:
        window.close()


def test_rip_flow_updates_progress_and_reenables_buttons(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

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


def test_batch_rip_processes_all_checked_titles(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

    ripped_titles: list[int] = []

    class FakeRipper:
        def __init__(self, ffmpeg_path):
            self.ffmpeg_path = ffmpeg_path

        def rip(self, disc, title, tracks, output_path, on_progress=None, on_log=None):
            ripped_titles.append(title.index)
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

        # Only the main feature (title 2) is checked by default - this is
        # the "select all videos on the disc" opt-in the feature adds.
        assert {t.index for t in window._checked_titles()} == {2}
        window._set_all_titles_checked(True)
        assert {t.index for t in window._checked_titles()} == {1, 2}

        window.output_dir_edit.setText(str(tmp_path))
        window._start_rip()

        # Batch runs one title's QThread at a time, chained via signals -
        # poll until the queue drains (rip_btn re-enables) or give up.
        for _ in range(10):
            if window.rip_btn.isEnabled():
                break
            if window._rip_thread is not None:
                window._rip_thread.wait(2000)
            _pump(app, 200)

        assert window.rip_btn.isEnabled()
        assert not window.cancel_btn.isEnabled()
        assert sorted(ripped_titles) == [1, 2]
        assert (tmp_path / "Title_01.mkv").exists()
        assert (tmp_path / "Title_02.mkv").exists()
        assert "Batch finished: 2/2 title(s) ripped." in window.log_view.toPlainText()
    finally:
        window.close()


def test_cancel_stops_batch_and_reenables_buttons(app, tmp_path, monkeypatch):
    # Regression test: RipWorker used to have no `cancelled` signal, so a
    # RipCancelled from the ripper fired neither `finished` nor `failed` -
    # the UI stayed stuck with rip_btn disabled forever after a cancel.
    monkeypatch.setattr(gui_module, "list_optical_drives", lambda: ["G:"])
    monkeypatch.setattr(
        gui_module.Config, "load", staticmethod(lambda: gui_module.Config())
    )
    disc = _sample_disc()
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)

    attempted: list[int] = []

    class FakeRipper:
        def __init__(self, ffmpeg_path):
            self.ffmpeg_path = ffmpeg_path

        def rip(self, disc, title, tracks, output_path, on_progress=None, on_log=None):
            from src.ripper import RipCancelled

            attempted.append(title.index)
            raise RipCancelled(f"cancelled title {title.index}")

        def cancel(self):
            pass

    monkeypatch.setattr(gui_module, "Ripper", FakeRipper)

    window = gui_module.MainWindow()
    try:
        window.ffmpeg_path_edit.setText("/usr/bin/ffmpeg")
        window._scan_disc()
        window._scan_thread.wait(2000)
        _pump(app)

        window._set_all_titles_checked(True)
        window.output_dir_edit.setText(str(tmp_path))
        window._start_rip()
        assert window._rip_thread is not None
        window._rip_thread.wait(2000)
        _pump(app)

        # Only the first queued title should have been attempted - the
        # batch must stop on cancel, not roll on to the next title.
        assert attempted == [1]
        assert window.rip_btn.isEnabled()
        assert not window.cancel_btn.isEnabled()
        assert "cancelled" in window.log_view.toPlainText().lower()
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
    monkeypatch.setattr(gui_module, "scan_disc", lambda drive, ffprobe, on_progress=None: disc)
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
