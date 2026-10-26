"""PDF2Audiobook - PyQt6 desktop app.

Main window provides:
  - a gallery of converted audiobooks (SQLite-backed)
  - PDF -> audiobook conversion with progress feedback
  - chapter-based playback with resume support
"""
import os
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .database import AudiobookDB
from .player import AudioPlayer
from .worker import ConvertWorker

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PDF2Audiobook"
)
DB_PATH = os.path.join(DATA_DIR, "library.db")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
MODELS_DIR = os.path.join(DATA_DIR, "models")


def resource_path(rel):
    """Resolve asset paths when frozen by PyInstaller."""
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)


def find_voice_model(models_dir):
    """Return (model_path, config_path) for the first .onnx in models_dir."""
    if not os.path.isdir(models_dir):
        return None, None
    for name in sorted(os.listdir(models_dir)):
        if name.endswith(".onnx"):
            model = os.path.join(models_dir, name)
            config = model + ".json"
            if not os.path.exists(config):
                config = None
            return model, config
    return None, None


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF2Audiobook")
        self.resize(860, 560)

        self.db = AudiobookDB(DB_PATH)
        self.player = AudioPlayer()
        self.worker = None

        self.current_book = None
        self.current_chapters = []
        self.current_chapter_idx = -1
        self._slider_guard = False

        self._build_ui()
        self._reload_books()

        # UI ticker: keep the progress slider in sync, auto-advance chapters.
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(500)
        self._ui_timer.timeout.connect(self._tick)
        self._ui_timer.start()

        # Persist listening progress every 5 seconds while playing.
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(self._save_progress)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)

        # Top bar
        top = QHBoxLayout()
        self.btn_convert = QPushButton("Convert PDF to audiobook")
        self.btn_convert.clicked.connect(self._start_conversion)
        self.btn_open_dir = QPushButton("Open library folder")
        self.btn_open_dir.clicked.connect(self._open_library_dir)
        top.addWidget(self.btn_convert)
        top.addWidget(self.btn_open_dir)
        top.addStretch(1)
        root.addLayout(top)

        # Progress row (hidden until a conversion runs)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        root.addWidget(self.progress_bar)
        root.addWidget(self.progress_label)

        # Gallery + chapters
        middle = QHBoxLayout()
        self.book_list = QListWidget()
        self.book_list.itemDoubleClicked.connect(self._play_selected)
        self.book_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.book_list.customContextMenuRequested.connect(self._book_menu)
        self.chapter_list = QListWidget()
        self.chapter_list.itemDoubleClicked.connect(self._play_chapter_item)
        self.chapter_list.setFixedWidth(300)
        middle.addWidget(self.book_list, 3)
        middle.addWidget(self.chapter_list, 2)
        root.addLayout(middle, 1)

        # Now-playing label
        self.now_label = QLabel("Nothing playing")
        root.addWidget(self.now_label)

        # Player bar
        bar = QHBoxLayout()
        self.btn_prev = QPushButton("|<")
        self.btn_prev.clicked.connect(lambda: self._skip(-1))
        self.btn_play = QPushButton("Play")
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self._stop_playback)
        self.btn_next = QPushButton(">|")
        self.btn_next.clicked.connect(lambda: self._skip(1))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderMoved.connect(self._seek)
        self.time_label = QLabel("00:00 / 00:00")
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.btn_stop)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.time_label)
        root.addLayout(bar)

        self.setCentralWidget(central)

    # ------------------------------------------------------------- library
    def _reload_books(self):
        self.book_list.clear()
        for book in self.db.list_books():
            pos = fmt_duration(book["position"])
            total = fmt_duration(book["total_duration"])
            text = (
                f"{book['title']}  ({book['author'] or 'unknown author'})\n"
                f"{book['chapter_count']} chunks  |  listened {pos} / {total}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, book["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, book)
            self.book_list.addItem(item)

    def _book_menu(self, pos):
        item = self.book_list.itemAt(pos)
        if item is None:
            return
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        action = menu.addAction("Delete from library")
        chosen = menu.exec(self.book_list.mapToGlobal(pos))
        if chosen is action:
            book_id = item.data(Qt.ItemDataRole.UserRole)
            self._delete_book(book_id)

    def _delete_book(self, book_id):
        answer = QMessageBox.question(
            self,
            "Delete book",
            "Remove this audiobook from the library? (audio files are kept on disk)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.current_book and self.current_book["id"] == book_id:
            self._stop_playback()
        self.db.delete_book(book_id)
        self._reload_books()
        self.chapter_list.clear()

    def _load_chapters(self, book):
        self.current_book = book
        self.current_chapters = self.db.get_chapters(book["id"])
        self.chapter_list.clear()
        for ch in self.current_chapters:
            item = QListWidgetItem(
                f"{ch['idx'] + 1:02d}  {ch['title']}  ({fmt_duration(ch['duration'])})"
            )
            item.setData(Qt.ItemDataRole.UserRole, ch["idx"])
            self.chapter_list.addItem(item)

    # --------------------------------------------------------- conversion
    def _start_conversion(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "A conversion is already running.")
            return

        model_path, config_path = find_voice_model(MODELS_DIR)
        if not model_path:
            QMessageBox.warning(
                self,
                "Voice model missing",
                "No Piper voice model found in:\n"
                f"{MODELS_DIR}\n\n"
                "Run:  python scripts/download_models.py\n"
                "to download the default Chinese voice, then try again.",
            )
            return

        pdf_path, _ = QFileDialog.getOpenFileName(
            self, "Select PDF", "", "PDF files (*.pdf)"
        )
        if not pdf_path:
            return

        default_title = os.path.splitext(os.path.basename(pdf_path))[0]
        title, ok = QInputDialog.getText(
            self, "Book title", "Title:", QLineEdit.EchoMode.Normal, default_title
        )
        if not ok:
            return
        author, ok = QInputDialog.getText(
            self, "Author (optional)", "Author:", QLineEdit.EchoMode.Normal, ""
        )
        if not ok:
            return

        self.worker = ConvertWorker(
            pdf_path,
            BOOKS_DIR,
            DB_PATH,
            model_path,
            config_path,
            title.strip() or default_title,
            author.strip(),
            self,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_convert_done)
        self.worker.failed.connect(self._on_convert_failed)
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Extracting PDF text...")
        self.worker.start()

    def _on_progress(self, percent, message):
        self.progress_bar.setValue(percent)
        self.progress_label.setText(message)

    def _on_convert_done(self, book_id):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self._reload_books()
        QMessageBox.information(
            self, "Conversion finished", f"Audiobook saved to your library (id {book_id})."
        )

    def _on_convert_failed(self, message):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        QMessageBox.critical(self, "Conversion failed", message)

    # ---------------------------------------------------------- playback
    def _play_selected(self):
        item = self.book_list.currentItem()
        if item is None:
            return
        book = item.data(Qt.ItemDataRole.UserRole + 1)
        self._load_chapters(book)
        start_idx = book["current_chapter"]
        start_pos = book["position"] if start_idx < len(self.current_chapters) else 0.0
        self._play_chapter(start_idx, start_pos)

    def _play_chapter_item(self):
        item = self.chapter_list.currentItem()
        if item is None:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        self._play_chapter(idx, 0.0)

    def _play_chapter(self, idx, position=0.0):
        if self.current_book is None or not self.current_chapters:
            return
        if idx < 0 or idx >= len(self.current_chapters):
            return
        self.current_chapter_idx = idx
        ch = self.current_chapters[idx]
        try:
            self.player.play(ch["audio_path"], position=position)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Playback error", str(exc))
            return
        self._save_timer.start()
        self._update_now_label()

    def _toggle_play(self):
        if self.current_chapter_idx < 0:
            self._play_selected()
            return
        if self.player.is_playing:
            self.player.pause()
        elif self.player.current_path is not None:
            self.player.resume()
        else:
            self._play_chapter(self.current_chapter_idx)
        self._update_now_label()

    def _stop_playback(self):
        self.player.stop()
        self._save_timer.stop()
        self.now_label.setText("Nothing playing")
        self.slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")

    def _skip(self, delta):
        if self.current_book is None:
            return
        nxt = self.current_chapter_idx + delta
        if 0 <= nxt < len(self.current_chapters):
            self._play_chapter(nxt, 0.0)
        elif delta > 0 and self.current_chapter_idx == len(self.current_chapters) - 1:
            self._stop_playback()

    def _seek(self, value):
        if self.current_chapter_idx < 0:
            return
        ch = self.current_chapters[self.current_chapter_idx]
        if ch["duration"] <= 0:
            return
        target = (value / 1000.0) * ch["duration"]
        self._play_chapter(self.current_chapter_idx, target)

    def _tick(self):
        if self.current_chapter_idx < 0 or self.current_book is None:
            return
        ch = self.current_chapters[self.current_chapter_idx]
        pos = self.player.position
        if ch["duration"] > 0 and not self._slider_guard:
            self._slider_guard = True
            self.slider.setValue(int((pos / ch["duration"]) * 1000))
            self._slider_guard = False
        self.time_label.setText(f"{fmt_duration(pos)} / {fmt_duration(ch['duration'])}")
        # Auto-advance when a chapter finishes.
        if not self.player.is_playing and self.player.current_path is not None:
            if self.current_chapter_idx < len(self.current_chapters) - 1:
                self._play_chapter(self.current_chapter_idx + 1, 0.0)
            else:
                self._stop_playback()
                self._reload_books()

    def _save_progress(self):
        if self.current_book is None or self.current_chapter_idx < 0:
            return
        self.db.update_progress(
            self.current_book["id"], self.current_chapter_idx, self.player.position
        )
        self._reload_books()

    def _update_now_label(self):
        if self.current_chapter_idx < 0 or self.current_book is None:
            return
        ch = self.current_chapters[self.current_chapter_idx]
        state = "playing" if self.player.is_playing else "paused"
        self.now_label.setText(
            f"{self.current_book['title']} - {ch['title']} ({state})"
        )
        self.btn_play.setText("Pause" if self.player.is_playing else "Play")

    def closeEvent(self, event):
        self._save_progress()
        self.player.stop()
        super().closeEvent(event)

    # -------------------------------------------------------------- misc
    def _open_library_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.startfile(DATA_DIR)  # noqa: S606 - opening a folder is intended


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PDF2Audiobook")
    app.setApplicationDisplayName("PDF2Audiobook")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
