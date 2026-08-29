"""PySide6 main window: drive picker, scan, title/track selection, rip."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
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

        self._build_ui()
        self._refresh_drives()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addLayout(self._build_ffmpeg_row())
        layout.addLayout(self._build_drive_row())
        layout.addLayout(self._build_title_row())

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

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Title:"))
        self.title_combo = QComboBox()
        self.title_combo.currentIndexChanged.connect(self._on_title_selected)
        row.addWidget(self.title_combo, 1)
        return row

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
        self.rip_btn = QPushButton("Rip")
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
        self.title_combo.blockSignals(True)
        self.title_combo.clear()
        main_feature = disc.main_feature
        selected_row = 0
        for row, title in enumerate(disc.titles):
            self.title_combo.addItem(title.label, userData=title)
            if main_feature is not None and title.index == main_feature.index:
                selected_row = row
        self.title_combo.blockSignals(False)
        if disc.titles:
            self.title_combo.setCurrentIndex(selected_row)
            self._on_title_selected(selected_row)

    def _on_title_selected(self, row: int) -> None:
        self.track_tree.clear()
        if row < 0 or self.disc is None:
            self.rip_btn.setEnabled(False)
            return
        title: Title | None = self.title_combo.itemData(row)
        if title is None:
            self.rip_btn.setEnabled(False)
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

        self.rip_btn.setEnabled(True)

    # -- output folder -----------------------------------------------------

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if path:
            self.output_dir_edit.setText(path)
            self.config.last_output_dir = path
            self.config.save()

    # -- rip -----------------------------------------------------------

    def _selected_title(self) -> Title | None:
        row = self.title_combo.currentIndex()
        if row < 0:
            return None
        return self.title_combo.itemData(row)

    def _selected_tracks(self, title: Title) -> list[Track]:
        return [t for t in title.tracks if t.selected]

    def _start_rip(self) -> None:
        if self.disc is None:
            return
        title = self._selected_title()
        if title is None:
            return
        tracks = self._selected_tracks(title)
        if not tracks:
            QMessageBox.warning(self, "No tracks selected", "Select at least one track.")
            return

        output_dir = self.output_dir_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No output folder", "Choose an output folder first.")
            return
        self.config.last_output_dir = output_dir
        self.config.save()

        output_path = Path(output_dir) / f"Title_{title.index:02d}.mkv"

        ffmpeg_path = self.ffmpeg_path_edit.text().strip()
        self.ripper = Ripper(ffmpeg_path)

        self.rip_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._reset_progress_bar()
        self.status_label.setText(f"Ripping title {title.index} ...")
        self._log(f"Ripping title {title.index} -> {output_path}")

        self._rip_thread = QThread()
        self._rip_worker = RipWorker(self.ripper, self.disc, title, tracks, output_path)
        self._rip_worker.moveToThread(self._rip_thread)
        self._rip_thread.started.connect(self._rip_worker.run)
        self._rip_worker.progress.connect(self._on_rip_progress)
        self._rip_worker.log.connect(self._log)
        self._rip_worker.finished.connect(self._on_rip_finished)
        self._rip_worker.failed.connect(self._on_rip_failed)
        self._rip_worker.finished.connect(self._rip_thread.quit)
        self._rip_worker.failed.connect(self._rip_thread.quit)
        self._rip_thread.start()

    def _on_rip_progress(self, progress: RipProgress) -> None:
        self.progress_bar.setValue(int(progress.fraction * 1000))
        pct = int(progress.fraction * 100)
        speed = f" ({progress.speed})" if progress.speed else ""
        self.status_label.setText(f"Ripping... {pct}%{speed}")

    def _on_rip_finished(self) -> None:
        self._log("Rip finished.")
        self.status_label.setText("Rip finished.")
        self._rip_done()

    def _on_rip_failed(self, message: str) -> None:
        self._log(f"Rip failed: {message}")
        self.status_label.setText("Rip failed.")
        QMessageBox.critical(self, "Rip failed", message)
        self._rip_done()

    def _rip_done(self) -> None:
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
