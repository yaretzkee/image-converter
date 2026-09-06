#!/usr/bin/env python3
"""
Image Format Converter - PySide6 GUI

Converts PNG/JPG/JPEG/TIFF/BMP -> PNG/WEBP/AVIF.
Optional resize with aspect-ratio preservation.
Follows system light/dark theme automatically.

Two conversion backends:
  - ImageMagick (external 'magick' CLI) - preferred if found on PATH.
  - Pillow (pure Python fallback) - used if ImageMagick is not installed.

Install dependencies:
    pip install PySide6 Pillow pillow-avif-plugin

pillow-avif-plugin is required only if you use the Pillow backend to export AVIF.
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPalette, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QListWidget, QCheckBox, QRadioButton, QButtonGroup, QPushButton,
    QLineEdit, QFileDialog, QLabel, QProgressBar, QMessageBox,
    QAbstractItemView, QSpinBox, QStyleFactory
)

INPUT_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
OUTPUT_FORMATS = ["png", "webp", "avif"]


try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

try:
    import pillow_avif  # noqa: F401  registers AVIF plugin with Pillow
    PILLOW_AVIF_AVAILABLE = True
except ImportError:
    PILLOW_AVIF_AVAILABLE = False


def find_imagemagick() -> str | None:
    """Return path to the 'magick' executable if found, else None."""
    return shutil.which("magick")


def apply_system_theme(app: QApplication):
    """
    Detect OS dark/light mode and apply a matching Fusion palette.
    Works cross-platform (Windows, macOS, Linux) using Qt's own
    color-scheme detection where available, with a heuristic fallback.
    """
    app.setStyle(QStyleFactory.create("Fusion"))

    is_dark = False
    try:
        hint = app.styleHints()
        scheme = hint.colorScheme()
        is_dark = scheme == Qt.ColorScheme.Dark
    except Exception:
        pal = app.palette()
        bg = pal.color(QPalette.Window)
        is_dark = bg.lightness() < 128

    if is_dark:
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(37, 37, 38))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(30, 30, 30))
        palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
        palette.setColor(QPalette.ToolTipBase, Qt.white)
        palette.setColor(QPalette.ToolTipText, Qt.white)
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(45, 45, 45))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(100, 160, 255))
        palette.setColor(QPalette.Highlight, QColor(0, 122, 204))
        palette.setColor(QPalette.HighlightedText, Qt.black)
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
        app.setPalette(palette)
        return True
    else:
        app.setPalette(app.style().standardPalette())
        return False


class DropListWidget(QListWidget):
    """A QListWidget that accepts dragged-and-dropped files."""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setStyleSheet(
            "QListWidget { border: 2px dashed #888; border-radius: 8px; font-size: 13px; }"
        )

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                p = Path(local)
                if p.is_file() and p.suffix.lower() in INPUT_EXTS:
                    paths.append(str(p))
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            for item in self.selectedItems():
                self.takeItem(self.row(item))
        else:
            super().keyPressEvent(event)


class ConversionWorker(QThread):
    progress = Signal(int, int)
    file_done = Signal(str, bool, str)
    finished_all = Signal()

    def __init__(self, files, formats, output_dir, use_source_path,
                 use_imagemagick, resize_enabled, resize_w, resize_h):
        super().__init__()
        self.files = files
        self.formats = formats
        self.output_dir = output_dir
        self.use_source_path = use_source_path
        self.use_imagemagick = use_imagemagick
        self.resize_enabled = resize_enabled
        self.resize_w = resize_w
        self.resize_h = resize_h

    def run(self):
        total = len(self.files) * len(self.formats)
        count = 0

        for f in self.files:
            src = Path(f)
            target_dir = src.parent if self.use_source_path else Path(self.output_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

            for fmt in self.formats:
                count += 1
                out_path = target_dir / f"{src.stem}.{fmt}"
                try:
                    if self.use_imagemagick:
                        self._convert_imagemagick(src, out_path, fmt)
                    else:
                        self._convert_pillow(src, out_path, fmt)
                    self.file_done.emit(str(out_path), True, "OK")
                except Exception as e:
                    self.file_done.emit(str(out_path), False, str(e))
                self.progress.emit(count, total)

        self.finished_all.emit()

    def _resize_geometry_arg(self) -> str:
        # ImageMagick: WxH with no '!' preserves aspect ratio (fits within box)
        return f"{self.resize_w}x{self.resize_h}"

    def _convert_imagemagick(self, src: Path, out_path: Path, fmt: str):
        base_cmd = ["magick", str(src)]
        if self.resize_enabled:
            base_cmd += ["-resize", self._resize_geometry_arg()]

        if fmt == "avif":
            cmd = base_cmd + ["-define", "heic:speed=2", str(out_path)]
        elif fmt == "webp":
            cmd = base_cmd + ["-quality", "50", str(out_path)]
        else:  # png
            cmd = base_cmd + [str(out_path)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ImageMagick conversion failed")

    def _convert_pillow(self, src: Path, out_path: Path, fmt: str):
        if not PILLOW_AVAILABLE:
            raise RuntimeError("Pillow is not installed (pip install Pillow)")
        if fmt == "avif" and not PILLOW_AVIF_AVAILABLE:
            raise RuntimeError("pillow-avif-plugin not installed (pip install pillow-avif-plugin)")

        img = Image.open(src)

        if self.resize_enabled:
            img.thumbnail((self.resize_w, self.resize_h), Image.LANCZOS)

        if fmt == "png":
            img.save(out_path, format="PNG")
        elif fmt == "webp":
            img.save(out_path, format="WEBP", quality=50)
        elif fmt == "avif":
            img.save(out_path, format="AVIF")


class ImageConverterApp(QWidget):
    __PROGRAM_PATH = Path(__file__).parent
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image Format Converter")
        self.resize(560, 700)
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Window)
                
        # Set window icon
        icon_path = os.path.join(self.__PROGRAM_PATH, 'img', 'image-converter-logo.ico')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            
        self.imagemagick_path = find_imagemagick()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        method_box = QGroupBox("Conversion method")
        method_layout = QHBoxLayout()
        self.rb_imagemagick = QRadioButton("ImageMagick")
        self.rb_pillow = QRadioButton("Own method (Pillow)")
        self.method_group = QButtonGroup(self)
        self.method_group.addButton(self.rb_imagemagick)
        self.method_group.addButton(self.rb_pillow)

        if self.imagemagick_path:
            self.rb_imagemagick.setChecked(True)
        else:
            self.rb_imagemagick.setEnabled(False)
            self.rb_imagemagick.setToolTip("ImageMagick not found on PATH")
            self.rb_pillow.setChecked(True)

        method_layout.addWidget(self.rb_imagemagick)
        method_layout.addWidget(self.rb_pillow)
        method_box.setLayout(method_layout)
        layout.addWidget(method_box)

        status_text = (
            f"ImageMagick detected: {self.imagemagick_path}"
            if self.imagemagick_path
            else "ImageMagick NOT found — using Pillow fallback"
        )
        self.status_label = QLabel(status_text)
        self.status_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel("Drag & drop image files here (PNG, JPG, JPEG, TIFF, BMP):"))
        self.drop_list = DropListWidget()
        self.drop_list.files_dropped.connect(self._add_files)
        self.drop_list.setMinimumHeight(160)
        layout.addWidget(self.drop_list)

        browse_row = QHBoxLayout()
        self.btn_browse = QPushButton("Browse files...")
        self.btn_browse.clicked.connect(self._browse_files)
        self.btn_clear = QPushButton("Clear list")
        self.btn_clear.clicked.connect(self.drop_list.clear)
        browse_row.addWidget(self.btn_browse)
        browse_row.addWidget(self.btn_clear)
        layout.addLayout(browse_row)

        fmt_box = QGroupBox("Target formats")
        fmt_layout = QHBoxLayout()
        self.format_checks = {}
        for fmt in OUTPUT_FORMATS:
            cb = QCheckBox(fmt.upper())
            cb.setChecked(fmt == "avif")
            self.format_checks[fmt] = cb
            fmt_layout.addWidget(cb)
        fmt_box.setLayout(fmt_layout)
        layout.addWidget(fmt_box)

        resize_box = QGroupBox("Resize")
        resize_layout = QHBoxLayout()
        self.cb_resize = QCheckBox("Keep aspect ratio / resize")
        self.cb_resize.setChecked(False)
        self.cb_resize.stateChanged.connect(self._toggle_resize)
        resize_layout.addWidget(self.cb_resize)

        resize_layout.addWidget(QLabel("Max width:"))
        self.spin_w = QSpinBox()
        self.spin_w.setRange(1, 20000)
        self.spin_w.setValue(1920)
        self.spin_w.setEnabled(False)
        resize_layout.addWidget(self.spin_w)

        resize_layout.addWidget(QLabel("Max height:"))
        self.spin_h = QSpinBox()
        self.spin_h.setRange(1, 20000)
        self.spin_h.setValue(1080)
        self.spin_h.setEnabled(False)
        resize_layout.addWidget(self.spin_h)

        resize_box.setLayout(resize_layout)
        layout.addWidget(resize_box)
        resize_hint = QLabel("Image is scaled to fit within max width/height; aspect ratio is always preserved.")
        resize_hint.setStyleSheet("color: #888; font-size: 10px;")
        resize_hint.setWordWrap(True)
        layout.addWidget(resize_hint)

        out_box = QGroupBox("Output location")
        out_layout = QVBoxLayout()
        self.cb_same_path = QCheckBox("Use same path as source")
        self.cb_same_path.setChecked(True)
        self.cb_same_path.stateChanged.connect(self._toggle_output_folder)
        out_layout.addWidget(self.cb_same_path)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setEnabled(False)
        self.btn_folder = QPushButton("Select folder...")
        self.btn_folder.setEnabled(False)
        self.btn_folder.clicked.connect(self._select_folder)
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(self.btn_folder)
        out_layout.addLayout(folder_row)
        out_box.setLayout(out_layout)
        layout.addWidget(out_box)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log_label = QLabel("")
        self.log_label.setStyleSheet("font-size: 11px;")
        self.log_label.setWordWrap(True)
        layout.addWidget(self.log_label)

        self.btn_convert = QPushButton("Convert")
        self.btn_convert.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 8px; background: #2e7d32; color: white; border-radius: 4px; }"
            "QPushButton:disabled { background: #666; }"
        )
        self.btn_convert.clicked.connect(self._start_conversion)
        layout.addWidget(self.btn_convert)

    def _add_files(self, paths):
        existing = {self.drop_list.item(i).text() for i in range(self.drop_list.count())}
        for p in paths:
            if p not in existing:
                self.drop_list.addItem(p)

    def _browse_files(self):
        filt = "Images (*.png *.jpg *.jpeg *.tiff *.tif *.bmp)"
        files, _ = QFileDialog.getOpenFileNames(self, "Select images", "", filt)
        if files:
            self._add_files(files)

    def _toggle_output_folder(self, state):
        enabled = not self.cb_same_path.isChecked()
        self.folder_edit.setEnabled(enabled)
        self.btn_folder.setEnabled(enabled)

    def _toggle_resize(self, state):
        enabled = self.cb_resize.isChecked()
        self.spin_w.setEnabled(enabled)
        self.spin_h.setEnabled(enabled)

    def _select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.folder_edit.setText(folder)

    def _start_conversion(self):
        files = [self.drop_list.item(i).text() for i in range(self.drop_list.count())]
        if not files:
            QMessageBox.warning(self, "No files", "Please add at least one image file.")
            return

        formats = [fmt for fmt, cb in self.format_checks.items() if cb.isChecked()]
        if not formats:
            QMessageBox.warning(self, "No format selected", "Please select at least one target format.")
            return

        use_source_path = self.cb_same_path.isChecked()
        output_dir = self.folder_edit.text().strip()
        if not use_source_path and not output_dir:
            QMessageBox.warning(self, "No output folder", "Please select an output folder.")
            return

        use_imagemagick = self.rb_imagemagick.isChecked()
        if use_imagemagick and not self.imagemagick_path:
            QMessageBox.warning(self, "ImageMagick unavailable", "ImageMagick was not found on PATH.")
            return
        if not use_imagemagick and not PILLOW_AVAILABLE:
            QMessageBox.critical(self, "Pillow missing", "Pillow is not installed. Run: pip install Pillow")
            return

        resize_enabled = self.cb_resize.isChecked()
        resize_w = self.spin_w.value()
        resize_h = self.spin_h.value()

        self.btn_convert.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_label.setText("Converting...")

        self.worker = ConversionWorker(
            files, formats, output_dir, use_source_path, use_imagemagick,
            resize_enabled, resize_w, resize_h
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.finished_all.connect(self._on_finished)
        self.errors = []
        self.worker.start()

    def _on_progress(self, current, total):
        pct = int(current / total * 100) if total else 0
        self.progress_bar.setValue(pct)

    def _on_file_done(self, out_path, success, message):
        if not success:
            self.errors.append(f"{out_path}: {message}")

    def _on_finished(self):
        self.btn_convert.setEnabled(True)
        if self.errors:
            self.log_label.setText(f"Done with {len(self.errors)} error(s):\n" + "\n".join(self.errors[:5]))
        else:
            self.log_label.setText("All conversions completed successfully.")


def main():
    app = QApplication(sys.argv)
    apply_system_theme(app)
    window = ImageConverterApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
