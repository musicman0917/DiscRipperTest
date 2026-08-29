"""PySide6 main window: drive picker, scan, title/track selection, rip."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import Config
from .disc import DiscScanError, FfmpegBuildError, list_optical_drives, scan_disc
from .models import Disc, Title, Track
from .ripper import RipCancelled, RipError, RipProgress, Ripper


class ScanWorker(QObject):
    finished = Signal(object)  # Disc
    failed = Signal(str)
    progress = Signal(object)  # (current: int, total: int | None, label: str)

    def __init__(self, drive: str, ffprobe_path: str):
        super().__init__()
        self.drive = drive
        self.ffprobe_path = ffprobe_path

    def run(self) -> None:
        try:
            disc = scan_disc(
                self.drive,
                self.ffprobe_path,
                on_progress=lambda cur, total, label: self.progress.emit((cur, total, label)),
            )
        except (DiscScanError, FfmpegBuildError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # unexpected - still surface it, don't hang the GUI
            self.failed.emit(f"Unexpected error scanning disc: {exc}")
        else:
            self.finished.emit(disc)


class RipWorker(QObject):
    progress = Signal(object)  # RipProgress
    log = Signal(str)
    finished = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        ripper: Ripper,
        disc: Disc,
        title: Title,
        tracks: list[Track],
        output_path: Path,
    ):
        super().__init__()
        self.ripper = ripper
        self.disc = disc
        self.title = title
        self.tracks = tracks
        self.output_path = output_path

    def run(self) -> None:
        try:
            self.ripper.rip(
                self.disc,
                self.title,
                self.tracks,
                self.output_path,
                on_progress=self.progress.emit,
                on_log=self.log.emit,
            )
        except RipCancelled:
            self.log.emit("Rip cancelled.")
            self.cancelled.emit()
        except RipError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Unexpected error during rip: {exc}")
        else:
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiscRipper")
        self.resize(820, 640)

        self.config = Config.load()
        self.disc: Disc | None = None
        self.ripper: Ripper | None = None

        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._rip_thread: QThread | None = None
        self._rip_worker: RipWorker | None = None

        # Batch rip queue: rip multiple checked titles one after another.
        self._rip_queue: list[Title] = []
        self._rip_queue_pos = 0
        self._rip_results: list[tuple[Title, bool, str]] = []
        self._batch_cancelled = False

        self._build_ui()
        self._refresh_drives()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addLayout(self._build_ffmpeg_row())
        layout.addLayout(self._build_drive_row())
        layout.addLayout(self._build_title_section())

        layout.addWidget(QLabel("Tracks (of the highlighted title above):"))
        self.track_tree = QTreeWidget()
        self.track_tree.setHeaderLabels(["Track", "Rip?"])
        self.track_tree.setColumnWidth(0, 500)
        layout.addWidget(self.track_tree)

        layout.addLayout(self._build_output_row())
        layout.addLayout(self._build_rip_row())

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self._reset_progress_bar()
        layout.addWidget(self.progress_bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(160)
        layout.addWidget(self.log_view)

    def _build_ffmpeg_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("ffmpeg.exe:"))
        self.ffmpeg_path_edit = QLineEdit(self.config.ffmpeg_path)
        self.ffmpeg_path_edit.setPlaceholderText(r"C:\ffmpeg\bin\ffmpeg.exe")
        self.ffmpeg_path_edit.editingFinished.connect(self._on_ffmpeg_path_changed)
        row.addWidget(self.ffmpeg_path_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_ffmpeg)
        row.addWidget(browse_btn)
        return row

    def _build_drive_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        row.addWidget(self.drive_combo, 1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_drives)
        row.addWidget(refresh_btn)
        self.scan_btn = QPushButton("Scan Disc")
        self.scan_btn.clicked.connect(self._scan_disc)
        row.addWidget(self.scan_btn)
        return row

    def _build_title_section(self) -> QVBoxLayout:
        section = QVBoxLayout()
        header = QHBoxLayout()
        header.addWidget(QLabel("Titles (check the ones to rip):"))
        header.addStretch(1)
        header.addWidget(QLabel("Hide shorter than:"))
        self.min_duration_spin = QSpinBox()
        self.min_duration_spin.setRange(0, 999)
        self.min_duration_spin.setValue(2)
        self.min_duration_spin.setSuffix(" min")
        self.min_duration_spin.valueChanged.connect(self._apply_title_filter)
        header.addWidget(self.min_duration_spin)
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self._set_all_titles_checked(True))
        header.addWidget(select_all_btn)
        select_none_btn = QPushButton("Select None")
        select_none_btn.clicked.connect(lambda: self._set_all_titles_checked(False))
        header.addWidget(select_none_btn)
        section.addLayout(header)

        self.title_tree = QTreeWidget()
        self.title_tree.setHeaderLabels(["Title", "Rip?"])
        self.title_tree.setColumnWidth(0, 500)
        self.title_tree.setMaximumHeight(160)
        # Highlighting (not checking) a row previews/edits its own track
        # selection below - the checkbox is what actually queues it for rip.
        self.title_tree.currentItemChanged.connect(self._on_title_focused)
        section.addWidget(self.title_tree)
        return section

    def _build_output_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Output folder:"))
        self.output_dir_edit = QLineEdit(self.config.last_output_dir)
        row.addWidget(self.output_dir_edit, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_output_dir)
        row.addWidget(browse_btn)
        return row

    def _build_rip_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.rip_btn = QPushButton("Rip Selected")
        self.rip_btn.clicked.connect(self._start_rip)
        self.rip_btn.setEnabled(False)
        row.addWidget(self.rip_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_rip)
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        return row

    # -- ffmpeg path ---------------------------------------------------

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate ffmpeg.exe")
        if path:
            self.ffmpeg_path_edit.setText(path)
            self._on_ffmpeg_path_changed()

    def _on_ffmpeg_path_changed(self) -> None:
        self.config.ffmpeg_path = self.ffmpeg_path_edit.text().strip()
        self.config.save()

    def _ffprobe_path(self) -> str:
        self.config.ffmpeg_path = self.ffmpeg_path_edit.text().strip()
        return self.config.infer_ffprobe_path()

    # -- drive / scan ----------------------------------------------------

    def _refresh_drives(self) -> None:
        self.drive_combo.clear()
        drives = list_optical_drives()
        if not drives:
            self.drive_combo.addItem("(no optical drives detected)")
            self.drive_combo.setEnabled(False)
        else:
            self.drive_combo.setEnabled(True)
            self.drive_combo.addItems(drives)

    def _log(self, message: str) -> None:
        self.log_view.append(message)

    # -- progress bar / status label -----------------------------------

    def _reset_progress_bar(self) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)

    def _set_progress_indeterminate(self) -> None:
        # Qt convention: range (0, 0) makes a QProgressBar show a "busy"
        # animation instead of a fraction - used whenever we don't know a
        # total ahead of time (DVD title probing, the Blu-ray brute-force
        # fallback) so the bar still visibly moves rather than sitting at 0.
        self.progress_bar.setRange(0, 0)

    def _scan_disc(self) -> None:
        if not self.drive_combo.isEnabled():
            QMessageBox.warning(self, "No drive", "No optical drive detected.")
            return
        ffmpeg_path = self.ffmpeg_path_edit.text().strip()
        ffprobe_path = self._ffprobe_path()
        if not ffmpeg_path or not ffprobe_path:
            QMessageBox.warning(
                self, "ffmpeg not configured", "Set the path to ffmpeg.exe first."
            )
            return

        drive = self.drive_combo.currentText()
        self.scan_btn.setEnabled(False)
        self.rip_btn.setEnabled(False)
        self._set_progress_indeterminate()
        self.status_label.setText(f"Scanning {drive} ...")
        self._log(f"Scanning {drive} ...")

        self._scan_thread = QThread()
        self._scan_worker = ScanWorker(drive, ffprobe_path)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.failed.connect(self._scan_thread.quit)
        self._scan_thread.start()

    def _on_scan_progress(self, progress: tuple) -> None:
        current, total, label = progress
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        else:
            self._set_progress_indeterminate()
        self.status_label.setText(label)

    def _on_scan_finished(self, disc: Disc) -> None:
        self.scan_btn.setEnabled(True)
        self.disc = disc
        self._reset_progress_bar()
        self.status_label.setText(f"Found {len(disc.titles)} title(s).")
        self._log(f"Found {len(disc.titles)} title(s) on {disc.drive_letter}.")
        self._populate_titles(disc)

    def _on_scan_failed(self, message: str) -> None:
        self.scan_btn.setEnabled(True)
        self._reset_progress_bar()
        self.status_label.setText("Scan failed.")
        self._log(f"Scan failed: {message}")
        QMessageBox.critical(self, "Scan failed", message)

    def _populate_titles(self, disc: Disc) -> None:
        self.title_tree.clear()
        self.track_tree.clear()
        main_feature = disc.main_feature
        main_feature_item = None
        for title in disc.titles:
            item = QTreeWidgetItem([title.label])
            item.setData(0, Qt.ItemDataRole.UserRole, title)
            checkbox = QCheckBox()
            is_main_feature = main_feature is not None and title.index == main_feature.index
            checkbox.setChecked(is_main_feature)
            self.title_tree.addTopLevelItem(item)
            self.title_tree.setItemWidget(item, 1, checkbox)
            if is_main_feature:
                main_feature_item = item

        self._apply_title_filter()

        focus_item = (
            main_feature_item
            if main_feature_item is not None and not main_feature_item.isHidden()
            else self._first_visible_title_item()
        )
        if focus_item is not None:
            self.title_tree.setCurrentItem(focus_item)
            self._on_title_focused(focus_item, None)

        self.rip_btn.setEnabled(self.title_tree.topLevelItemCount() > 0)

    def _first_visible_title_item(self) -> QTreeWidgetItem | None:
        for i in range(self.title_tree.topLevelItemCount()):
            item = self.title_tree.topLevelItem(i)
            if not item.isHidden():
                return item
        return None

    def _apply_title_filter(self) -> None:
        min_seconds = self.min_duration_spin.value() * 60
        for i in range(self.title_tree.topLevelItemCount()):
            item = self.title_tree.topLevelItem(i)
            title: Title | None = item.data(0, Qt.ItemDataRole.UserRole)
            if title is None:
                continue
            item.setHidden(title.duration_seconds < min_seconds)

        # If the filter just hid whichever title was being previewed below,
        # fall back to the next visible one instead of showing stale tracks.
        current = self.title_tree.currentItem()
        if current is not None and current.isHidden():
            fallback = self._first_visible_title_item()
            if fallback is not None:
                self.title_tree.setCurrentItem(fallback)
                self._on_title_focused(fallback, None)
            else:
                self.track_tree.clear()

    def _on_title_focused(self, current: QTreeWidgetItem | None, _previous) -> None:
        self.track_tree.clear()
        if current is None:
            return
        title: Title | None = current.data(0, Qt.ItemDataRole.UserRole)
        if title is None:
            return

        for track in title.tracks:
            item = QTreeWidgetItem([track.label])
            checkbox = QCheckBox()
            checkbox.setChecked(track.selected)
            checkbox.stateChanged.connect(
                lambda state, t=track: setattr(t, "selected", bool(state))
            )
            self.track_tree.addTopLevelItem(item)
            self.track_tree.setItemWidget(item, 1, checkbox)

    def _set_all_titles_checked(self, checked: bool) -> None:
        # Scoped to visible rows only, so "Select All" with a duration
        # filter active means "all real content," not "everything
        # including the menus/trailers I just asked to hide."
        for i in range(self.title_tree.topLevelItemCount()):
            item = self.title_tree.topLevelItem(i)
            if item.isHidden():
                continue
            checkbox = self.title_tree.itemWidget(item, 1)
            if checkbox is not None:
                checkbox.setChecked(checked)

    def _checked_titles(self) -> list[Title]:
        titles = []
        for i in range(self.title_tree.topLevelItemCount()):
            item = self.title_tree.topLevelItem(i)
            checkbox = self.title_tree.itemWidget(item, 1)
            if checkbox is not None and checkbox.isChecked():
                title = item.data(0, Qt.ItemDataRole.UserRole)
                if title is not None:
                    titles.append(title)
        return titles

    # -- output folder -----------------------------------------------------

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if path:
            self.output_dir_edit.setText(path)
            self.config.last_output_dir = path
            self.config.save()

    # -- rip (batch queue over checked titles) --------------------------

    def _selected_tracks(self, title: Title) -> list[Track]:
        return [t for t in title.tracks if t.selected]

    def _start_rip(self) -> None:
        if self.disc is None:
            return
        checked_titles = self._checked_titles()
        if not checked_titles:
            QMessageBox.warning(
                self, "No titles selected", "Check at least one title to rip."
            )
            return

        output_dir = self.output_dir_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No output folder", "Choose an output folder first.")
            return
        self.config.last_output_dir = output_dir
        self.config.save()

        queue = []
        skipped = []
        for title in checked_titles:
            if self._selected_tracks(title):
                queue.append(title)
            else:
                skipped.append(title.index)
        if skipped:
            self._log(
                "Skipping title(s) with no tracks selected: "
                + ", ".join(str(i) for i in skipped)
            )
        if not queue:
            QMessageBox.warning(
                self,
                "No tracks selected",
                "None of the checked titles have any tracks selected.",
            )
            return

        self._rip_queue = queue
        self._rip_queue_pos = 0
        self._rip_results = []
        self._batch_cancelled = False

        self.rip_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._reset_progress_bar()

        self._rip_next_in_queue()

    def _rip_next_in_queue(self) -> None:
        if self._batch_cancelled or self._rip_queue_pos >= len(self._rip_queue):
            self._finish_rip_batch()
            return

        title = self._rip_queue[self._rip_queue_pos]
        tracks = self._selected_tracks(title)
        output_dir = self.output_dir_edit.text().strip()
        output_path = Path(output_dir) / f"Title_{title.index:02d}.mkv"

        ffmpeg_path = self.ffmpeg_path_edit.text().strip()
        self.ripper = Ripper(ffmpeg_path)

        position = f"{self._rip_queue_pos + 1}/{len(self._rip_queue)}"
        self._reset_progress_bar()
        self.status_label.setText(f"Ripping title {title.index} ({position}) ...")
        self._log(f"Ripping title {title.index} ({position}) -> {output_path}")

        self._rip_thread = QThread()
        self._rip_worker = RipWorker(self.ripper, self.disc, title, tracks, output_path)
        self._rip_worker.moveToThread(self._rip_thread)
        self._rip_thread.started.connect(self._rip_worker.run)
        self._rip_worker.progress.connect(self._on_rip_progress)
        self._rip_worker.log.connect(self._log)
        self._rip_worker.finished.connect(self._on_rip_worker_finished)
        self._rip_worker.failed.connect(self._on_rip_worker_failed)
        self._rip_worker.cancelled.connect(self._on_rip_worker_cancelled)
        self._rip_worker.finished.connect(self._rip_thread.quit)
        self._rip_worker.failed.connect(self._rip_thread.quit)
        self._rip_worker.cancelled.connect(self._rip_thread.quit)
        self._rip_thread.start()

    def _on_rip_progress(self, progress: RipProgress) -> None:
        self.progress_bar.setValue(int(progress.fraction * 1000))
        pct = int(progress.fraction * 100)
        speed = f" ({progress.speed})" if progress.speed else ""
        position = f"{self._rip_queue_pos + 1}/{len(self._rip_queue)}"
        self.status_label.setText(f"Ripping title {position}... {pct}%{speed}")

    def _cleanup_rip_thread(self) -> None:
        # A QThread must not be dropped (and hence garbage-collected) while
        # its OS thread is still shutting down - the finished/failed/
        # cancelled signal here fires from a queued cross-thread connection,
        # which runs *before* the .quit() call queued after it, so at this
        # point the thread's own event loop may not have processed quit()
        # yet. Reassigning self._rip_thread/_rip_worker to the next title's
        # objects without waiting first was crashing the process outright
        # (PySide6 deleting a QThread that Qt still considers running).
        if self._rip_thread is not None:
            self._rip_thread.quit()
            self._rip_thread.wait()
            self._rip_thread = None
            self._rip_worker = None

    def _on_rip_worker_finished(self) -> None:
        title = self._rip_queue[self._rip_queue_pos]
        self._log(f"Title {title.index} finished.")
        self._rip_results.append((title, True, ""))
        self._cleanup_rip_thread()
        self._rip_queue_pos += 1
        self._rip_next_in_queue()

    def _on_rip_worker_failed(self, message: str) -> None:
        title = self._rip_queue[self._rip_queue_pos]
        self._log(f"Title {title.index} failed: {message}")
        self._rip_results.append((title, False, message))
        self._cleanup_rip_thread()
        self._rip_queue_pos += 1
        self._rip_next_in_queue()

    def _on_rip_worker_cancelled(self) -> None:
        title = self._rip_queue[self._rip_queue_pos]
        self._rip_results.append((title, False, "cancelled"))
        self._batch_cancelled = True
        self._cleanup_rip_thread()
        self._finish_rip_batch()

    def _finish_rip_batch(self) -> None:
        # Deliberately not resetting the progress bar here: after a
        # successful batch it should keep showing the last title's 100%
        # rather than snapping back to empty. _rip_next_in_queue() already
        # resets it per-title, and _start_rip() resets it for a new batch.
        total = len(self._rip_queue)
        successes = sum(1 for _, ok, _ in self._rip_results if ok)
        failed = [t for t, ok, _ in self._rip_results if not ok]

        if self._batch_cancelled:
            self.status_label.setText(f"Cancelled - {successes}/{total} title(s) ripped.")
            self._log(f"Batch cancelled after {successes}/{total} title(s).")
        else:
            self.status_label.setText(f"Done - {successes}/{total} title(s) ripped.")
            self._log(f"Batch finished: {successes}/{total} title(s) ripped.")
            if failed and successes == 0:
                QMessageBox.critical(
                    self, "Rip failed", f"All {total} title(s) failed. See log for details."
                )
            elif failed:
                QMessageBox.warning(
                    self,
                    "Some titles failed",
                    f"{len(failed)} of {total} title(s) failed. See log for details.",
                )

        self.rip_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _cancel_rip(self) -> None:
        if self.ripper is not None:
            self.ripper.cancel()
            self._log("Cancelling...")


def main() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.exec()
