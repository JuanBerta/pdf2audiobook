"""PDF2Audiobook - PyQt6 desktop app.

Main window provides:
  - a gallery of converted audiobooks (SQLite-backed)
  - PDF -> audiobook conversion with progress feedback
  - chapter-based playback with resume support
"""
import logging
import os
import sys

from PyQt6.QtCore import QLockFile, QRect, QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .database import AudiobookDB
from .i18n import LANGUAGE_NAMES, SUPPORTED_LANGUAGES, get_language, set_language, tr
from .player import AudioPlayer
from .readalong import ReadAlongPanel
from .theme import apply_theme
from .updater import check_for_updates
from .worker import QueueWorker

APP_VERSION = "1.3.0"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PDF2Audiobook"
)
DB_PATH = os.path.join(DATA_DIR, "library.db")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
MODELS_DIR = os.path.join(DATA_DIR, "models")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOCK_PATH = os.path.join(DATA_DIR, "app.lock")


def resource_path(rel):
    """Resolve asset paths when frozen by PyInstaller."""
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)


# Piper voice names use "<lang>_<region>-<voice>[-<quality>]".
LANG_LABELS = {
    "en_US": "English (US)",
    "en_GB": "English (UK)",
    "zh_CN": "Chinese (Mandarin)",
    "es_ES": "Spanish (Spain)",
    "es_MX": "Spanish (Mexico)",
    "fr_FR": "French (France)",
    "de_DE": "German (Germany)",
    "it_IT": "Italian (Italy)",
    "pt_PT": "Portuguese (Portugal)",
    "pt_BR": "Portuguese (Brazil)",
    "ja_JP": "Japanese",
    "ko_KR": "Korean",
    "ru_RU": "Russian",
}


def voice_display_name(model_name):
    """Turn 'en_US-lessac-medium' into 'English (US) — lessac medium'."""
    stem = os.path.splitext(model_name)[0]
    parts = stem.split("-", 1)
    lang = parts[0] if parts else stem
    rest = parts[1].replace("-", " ") if len(parts) > 1 else ""
    label = LANG_LABELS.get(lang, lang.replace("_", " "))
    return f"{label} — {rest}".strip(" —")


def list_voice_models(models_dir):
    """Return [(model_path, config_path, display_name), ...] sorted by name."""
    if not os.path.isdir(models_dir):
        return []
    result = []
    for name in sorted(os.listdir(models_dir)):
        if not name.endswith(".onnx"):
            continue
        model = os.path.join(models_dir, name)
        config = model + ".json"
        if not os.path.exists(config):
            config = None
        result.append((model, config, voice_display_name(name)))
    return result


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


REPEAT_OFF = 0
REPEAT_ALL = 1
REPEAT_ONE = 2
REPEAT_ICON_FILES = {
    REPEAT_OFF: "repeat_off.png",
    REPEAT_ALL: "repeat_all.png",
    REPEAT_ONE: "repeat_one.png",
}
REPEAT_LABEL_KEYS = {
    REPEAT_OFF: "repeat_off",
    REPEAT_ALL: "repeat_all",
    REPEAT_ONE: "repeat_one",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF2Audiobook")
        self.resize(860, 560)

        self.db = AudiobookDB(DB_PATH)
        self.player = AudioPlayer()
        self.worker = None
        self._conv_paused = False

        self.current_book = None
        self.current_chapters = []
        self.current_chapter_idx = -1
        self._chapter_offsets = []
        self._slider_guard = False
        self.repeat_mode = REPEAT_OFF
        self._book_items = {}

        self.settings = QSettings("PDF2Audiobook", "PDF2Audiobook")
        self._theme_mode = str(self.settings.value("ui/theme", "dark") or "dark")
        self._sleep_remaining_min = 0  # 0 = sleep timer off
        self._sleep_timer = QTimer(self)
        self._sleep_timer.timeout.connect(self._on_sleep_tick)

        self._build_menu()
        self._build_ui()
        self._reload_books()

        # UI ticker: keep the progress slider in sync, auto-advance chapters.
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(500)
        self._ui_timer.timeout.connect(self._tick)
        self._ui_timer.start()

        # Keyboard shortcuts: Space toggles play/pause, Ctrl+Left/Right skip
        # chapters, R cycles the repeat mode, Ctrl+B adds a bookmark,
        # Ctrl+Up/Down change playback speed.
        QShortcut(QKeySequence("Space"), self, activated=self._toggle_play)
        QShortcut(
            QKeySequence("Ctrl+Left"), self, activated=lambda: self._skip(-1)
        )
        QShortcut(
            QKeySequence("Ctrl+Right"), self, activated=lambda: self._skip(1)
        )
        QShortcut(QKeySequence("R"), self, activated=self._toggle_repeat)
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self._add_bookmark)
        QShortcut(
            QKeySequence("Ctrl+Up"),
            self,
            activated=lambda: self._cycle_speed(1),
        )
        QShortcut(
            QKeySequence("Ctrl+Down"),
            self,
            activated=lambda: self._cycle_speed(-1),
        )

        # Persist listening progress every 5 seconds while playing.
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(self._save_progress)

    # ------------------------------------------------------------------ UI
    def _build_menu(self):
        menubar = self.menuBar()
        self.lang_menu = menubar.addMenu(tr("menu_language"))
        self._lang_actions = {}
        for code in SUPPORTED_LANGUAGES:
            act = QAction(LANGUAGE_NAMES[code], self, checkable=True)
            act.setChecked(code == get_language())
            act.triggered.connect(
                lambda checked, c=code: self._switch_language(c)
            )
            self.lang_menu.addAction(act)
            self._lang_actions[code] = act

        # Theme switcher
        self.theme_menu = menubar.addMenu(tr("menu_theme"))
        self._theme_actions = {}
        for mode, key in (("dark", "theme_dark"), ("light", "theme_light")):
            act = QAction(tr(key), self, checkable=True)
            act.setChecked(mode == self._theme_mode)
            act.triggered.connect(
                lambda checked, m=mode: self._set_theme(m)
            )
            self.theme_menu.addAction(act)
            self._theme_actions[mode] = act

        # Sleep timer
        self.sleep_menu = menubar.addMenu(tr("menu_sleep_timer"))
        self._sleep_actions = {}
        off = QAction(tr("sleep_off"), self, checkable=True)
        off.setChecked(True)
        off.triggered.connect(lambda: self._set_sleep_timer(0))
        self.sleep_menu.addAction(off)
        self._sleep_actions[0] = off
        for minutes in (15, 30, 45, 60, 90, 120):
            act = QAction(
                tr("sleep_minutes", minutes=minutes), self, checkable=True
            )
            act.triggered.connect(
                lambda checked, m=minutes: self._set_sleep_timer(m)
            )
            self.sleep_menu.addAction(act)
            self._sleep_actions[minutes] = act

        # Bookmarks
        self.bookmarks_menu = menubar.addMenu(tr("bookmarks_menu"))
        act_add = QAction(tr("add_bookmark"), self)
        act_add.setShortcut(QKeySequence("Ctrl+B"))
        act_add.triggered.connect(self._add_bookmark)
        self.bookmarks_menu.addAction(act_add)
        self.bookmarks_menu.addSeparator()
        self._bookmarks_list_action = self.bookmarks_menu.addAction(
            tr("no_bookmarks"), self._show_bookmarks
        )

        # Updates
        self.updates_menu = menubar.addMenu("Help")
        act_update = QAction(tr("update_ok"), self)
        act_update.triggered.connect(self._check_updates_now)
        self.updates_menu.addAction(act_update)

    def _set_theme(self, mode):
        self._theme_mode = mode
        self.settings.setValue("ui/theme", mode)
        for m, act in self._theme_actions.items():
            act.setChecked(m == mode)
        apply_theme(QApplication.instance(), mode)

    def _set_sleep_timer(self, minutes):
        for m, act in self._sleep_actions.items():
            act.setChecked(m == minutes)
        self._sleep_remaining_min = minutes
        self._sleep_timer.stop()
        if minutes > 0:
            self._sleep_timer.start(60 * 1000)
        self._update_statusbar()

    def _on_sleep_tick(self):
        if self._sleep_remaining_min <= 0:
            self._sleep_timer.stop()
            return
        self._sleep_remaining_min -= 1
        self._update_statusbar()
        if self._sleep_remaining_min <= 0:
            self._stop_playback()
            self._save_progress()

    def _switch_language(self, code):
        if code == get_language():
            return
        set_language(code)
        QSettings("PDF2Audiobook", "PDF2Audiobook").setValue(
            "ui/language", code
        )
        for c, act in self._lang_actions.items():
            act.setChecked(c == code)
        self._apply_ui_texts()
        self._reload_books()
        self._update_now_label()

    def _apply_ui_texts(self):
        self.lang_menu.setTitle(tr("menu_language"))
        self.theme_menu.setTitle(tr("menu_theme"))
        self.sleep_menu.setTitle(tr("menu_sleep_timer"))
        self.bookmarks_menu.setTitle(tr("bookmarks_menu"))
        for mode, key in (("dark", "theme_dark"), ("light", "theme_light")):
            self._theme_actions[mode].setText(tr(key))
        for minutes, act in self._sleep_actions.items():
            act.setText(
                tr("sleep_off") if minutes == 0 else tr("sleep_minutes", minutes=minutes)
            )
        self.search_box.setPlaceholderText(tr("search_placeholder"))
        self.btn_convert.setText(tr("convert_btn"))
        self.btn_open_dir.setText(tr("open_dir_btn"))
        self.btn_play.setText(tr("play"))
        self.btn_play.setToolTip(tr("play"))
        self.btn_stop.setToolTip(tr("stop"))
        self.btn_prev.setToolTip(tr("prev"))
        self.btn_next.setToolTip(tr("next"))
        self.btn_repeat.setToolTip(tr(REPEAT_LABEL_KEYS[self.repeat_mode]))
        self.btn_cancel.setText(tr("cancel"))
        self.btn_pause.setText(
            tr("resume_conv") if self._conv_paused else tr("pause_conv")
        )
        self.btn_readalong.setText(tr("readalong_toggle"))
        self.btn_readalong.setToolTip(tr("readalong_toggle"))
        if self.current_chapter_idx < 0:
            self.now_label.setText(tr("nothing_playing"))
        self._update_statusbar()

    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)

        # Top bar
        top = QHBoxLayout()
        self.btn_convert = QPushButton(tr("convert_btn"))
        self.btn_convert.clicked.connect(self._start_conversion)
        self.btn_open_dir = QPushButton(tr("open_dir_btn"))
        self.btn_open_dir.clicked.connect(self._open_library_dir)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(tr("search_placeholder"))
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(200)
        self.search_box.textChanged.connect(self._apply_book_filter)
        top.addWidget(self.btn_convert)
        top.addWidget(self.btn_open_dir)
        top.addStretch(1)
        top.addWidget(self.search_box)
        root.addLayout(top)

        # Progress row (hidden until a conversion runs)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        self.btn_pause = QPushButton(tr("pause_conv"))
        self.btn_pause.setVisible(False)
        self.btn_pause.clicked.connect(self._toggle_conv_pause)
        self.btn_cancel = QPushButton(tr("cancel"))
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel_conversion)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.btn_pause)
        progress_row.addWidget(self.btn_cancel)
        root.addLayout(progress_row)
        root.addWidget(self.progress_label)

        # Gallery + chapters
        middle = QHBoxLayout()
        self.book_list = QListWidget()
        self.book_list.setViewMode(QListView.ViewMode.IconMode)
        self.book_list.setIconSize(QSize(110, 150))
        self.book_list.setGridSize(QSize(160, 215))
        self.book_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.book_list.setMovement(QListView.Movement.Static)
        self.book_list.setWordWrap(True)
        self.book_list.setSpacing(8)
        self.book_list.itemDoubleClicked.connect(self._play_selected)
        self.book_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.book_list.customContextMenuRequested.connect(self._book_menu)
        self.chapter_list = QListWidget()
        self.chapter_list.itemDoubleClicked.connect(self._play_chapter_item)
        self.chapter_list.setFixedWidth(300)
        middle.addWidget(self.book_list, 3)
        middle.addWidget(self.chapter_list, 2)
        root.addLayout(middle, 1)

        # Now-playing label + read-along toggle
        now_row = QHBoxLayout()
        self.now_label = QLabel(tr("nothing_playing"))
        self.btn_readalong = QPushButton(tr("readalong_toggle"))
        self.btn_readalong.setCheckable(True)
        self.btn_readalong.setToolTip(tr("readalong_toggle"))
        self.btn_readalong.clicked.connect(self._toggle_readalong)
        now_row.addWidget(self.now_label, 1)
        now_row.addWidget(self.btn_readalong)
        root.addLayout(now_row)

        # Player bar
        bar = QHBoxLayout()

        def player_icon(name):
            return QIcon(resource_path(os.path.join("assets", name)))

        self.btn_prev = QPushButton(player_icon("prev.png"), "")
        self.btn_prev.setToolTip(tr("prev"))
        self.btn_prev.clicked.connect(lambda: self._skip(-1))
        self.btn_play = QPushButton(player_icon("play.png"), tr("play"))
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_stop = QPushButton(player_icon("stop.png"), "")
        self.btn_stop.setToolTip(tr("stop"))
        self.btn_stop.clicked.connect(self._stop_playback)
        self.btn_next = QPushButton(player_icon("next.png"), "")
        self.btn_next.setToolTip(tr("next"))
        self.btn_next.clicked.connect(lambda: self._skip(1))
        self.btn_repeat = QPushButton(player_icon("repeat_off.png"), "")
        self.btn_repeat.setToolTip(tr("repeat_off"))
        self.btn_repeat.clicked.connect(self._toggle_repeat)
        self.btn_speed = QPushButton("1.0x")
        self.btn_speed.setFixedWidth(56)
        self.btn_speed.setToolTip("Playback speed (Ctrl+Up/Down)")
        self.btn_speed.clicked.connect(lambda: self._cycle_speed(1))
        for b in (
            self.btn_prev,
            self.btn_play,
            self.btn_stop,
            self.btn_next,
            self.btn_repeat,
        ):
            b.setIconSize(QSize(22, 22))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderMoved.connect(self._seek)
        self.slider.sliderPressed.connect(self._slider_pressed)
        self.slider.sliderReleased.connect(self._slider_released)
        self.time_label = QLabel("00:00 / 00:00")
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.btn_stop)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.btn_repeat)
        bar.addWidget(self.btn_speed)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.time_label)
        root.addLayout(bar)

        # Read-along panel (hidden until the user toggles it)
        self.read_panel = ReadAlongPanel()
        self.read_panel.setVisible(False)
        self.read_panel.seek_requested.connect(self._readalong_seek)
        root.addWidget(self.read_panel)

        self.setCentralWidget(central)

    # ------------------------------------------------------------- library
    def _reload_books(self):
        self.book_list.clear()
        self._book_items = {}
        for book in self.db.list_books():
            pos = fmt_duration(book["position"])
            total = fmt_duration(book["total_duration"])
            author = book["author"] or tr("unknown_author")
            voice = (
                tr("voice_label", voice=voice_display_name(book["voice_model"]))
                if book.get("voice_model")
                else ""
            )
            text = (
                f"{book['title']}  ({author})\n"
                f"{tr('chunks', count=book['chapter_count'])}  |  "
                f"{tr('listened_progress', pos=pos, total=total)}"
                + (f"\n{voice}" if voice else "")
            )
            item = QListWidgetItem(QIcon(self._book_cover(book)), text)
            item.setData(Qt.ItemDataRole.UserRole, book["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, book)
            item.setToolTip(
                f"{book['title']}\n{author}\n{pos} / {total}"
            )
            self.book_list.addItem(item)
            self._book_items[book["id"]] = item
        self._apply_book_filter(self.search_box.text())
        self._update_statusbar()

    def _book_cover(self, book):
        """Cover pixmap: real cover image if present, else a colored spine."""
        cover = book.get("cover_path")
        if cover and os.path.exists(cover):
            try:
                return QPixmap(cover)
            except Exception:  # noqa: BLE001 - fall back to placeholder
                pass
        palette = [
            ("#2e86ab", "#1b5570"),
            ("#b5651d", "#7a4214"),
            ("#6c5ce7", "#4a3f9e"),
            ("#00b894", "#007a5e"),
            ("#d63031", "#8f1f1f"),
            ("#e17055", "#9c4a38"),
            ("#0984e3", "#065e9e"),
            ("#8e44ad", "#5e2d73"),
        ]
        color, edge = palette[book["id"] % len(palette)]
        pm = QPixmap(110, 150)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(edge))
        painter.drawRoundedRect(2, 2, 106, 146, 10, 10)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(2, 2, 106, 40, 10, 10)
        painter.drawRect(2, 36, 106, 6)
        title = book["title"] or "?"
        painter.setPen(QColor("#ffffff"))
        first = title[:2] or "?"
        font = QFont("Segoe UI", 30, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(
            QRect(6, 44, 98, 64),
            Qt.AlignmentFlag.AlignCenter,
            first,
        )
        font = QFont("Segoe UI", 9)
        painter.setFont(font)
        painter.drawText(
            QRect(6, 106, 98, 40),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            title[:90],
        )
        painter.end()
        return pm

    def _apply_book_filter(self, text):
        query = text.strip().lower()
        for book_id, item in self._book_items.items():
            book = item.data(Qt.ItemDataRole.UserRole + 1)
            haystack = (
                f"{book['title']} {book['author'] or ''} "
                f"{book.get('voice_model') or ''}"
            ).lower()
            item.setHidden(bool(query) and query not in haystack)

    def _update_statusbar(self):
        parts = []
        if self._book_items:
            parts.append(tr("stat_books", count=len(self._book_items)))
        if self.repeat_mode != REPEAT_OFF:
            parts.append(tr(REPEAT_LABEL_KEYS[self.repeat_mode]))
        if self._sleep_remaining_min > 0:
            parts.append(
                tr("sleep_minutes", minutes=self._sleep_remaining_min)
            )
        self.statusBar().showMessage("  |  ".join(parts))

    def _book_menu(self, pos):
        item = self.book_list.itemAt(pos)
        if item is None:
            return
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        action = menu.addAction(tr("delete_from_library"))
        chosen = menu.exec(self.book_list.mapToGlobal(pos))
        if chosen is action:
            book_id = item.data(Qt.ItemDataRole.UserRole)
            self._delete_book(book_id)

    def _delete_book(self, book_id):
        box = QMessageBox(
            QMessageBox.Icon.Question,
            tr("delete_book_title"),
            tr("delete_book_msg"),
            self,
        )
        yes_btn = box.addButton(tr("yes"), QMessageBox.ButtonRole.YesRole)
        box.addButton(tr("no"), QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() is not yes_btn:
            return
        if self.current_book and self.current_book["id"] == book_id:
            self._stop_playback()
        self.db.delete_book(book_id)
        self._reload_books()
        self.chapter_list.clear()

    def _load_chapters(self, book):
        self.current_book = book
        self.current_chapters = self.db.get_chapters(book["id"])
        self._chapter_offsets = []
        acc = 0.0
        for ch in self.current_chapters:
            self._chapter_offsets.append(acc)
            acc += ch["duration"]
        self.chapter_list.clear()
        for ch in self.current_chapters:
            item = QListWidgetItem(
                f"{ch['idx'] + 1:02d}  {ch['title']}  ({fmt_duration(ch['duration'])})"
            )
            item.setData(Qt.ItemDataRole.UserRole, ch["idx"])
            self.chapter_list.addItem(item)
        self._load_readalong()

    # --------------------------------------------------------- conversion
    def _start_conversion(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, tr("busy_title"), tr("busy_msg"))
            return

        models = list_voice_models(MODELS_DIR)
        if not models:
            QMessageBox.warning(
                self,
                tr("model_missing_title"),
                tr("model_missing_msg", dir=MODELS_DIR),
            )
            return

        pdf_paths, _ = QFileDialog.getOpenFileNames(
            self, tr("choose_pdfs"), "", f"{tr('pdf_files')} (*.pdf)"
        )
        if not pdf_paths:
            return

        base_title = os.path.splitext(os.path.basename(pdf_paths[0]))[0]
        title, ok = QInputDialog.getText(
            self,
            tr("book_title_dlg"),
            tr("title_label"),
            QLineEdit.EchoMode.Normal,
            base_title,
        )
        if not ok:
            return
        author, ok = QInputDialog.getText(
            self, tr("author_dlg"), tr("author_label"), QLineEdit.EchoMode.Normal, ""
        )
        if not ok:
            return

        names = [entry[2] for entry in models]
        choice, ok = QInputDialog.getItem(
            self, tr("choose_voice_dlg"), tr("voice_model_label"), names, 0, False
        )
        if not ok:
            return
        model_path, config_path, _ = next(e for e in models if e[2] == choice)
        voice_model = os.path.splitext(os.path.basename(model_path))[0]

        speeds = ["0.8x", "0.9x", "1.0x", "1.1x", "1.2x", "1.3x"]
        speed_choice, ok = QInputDialog.getItem(
            self, tr("speed_dlg"), tr("speed_dlg"), speeds, 2, False
        )
        if not ok:
            return
        length_scale = float(speed_choice[:-1])

        self.worker = QueueWorker(BOOKS_DIR, DB_PATH, self)
        clean_title = title.strip() or base_title
        for i, pdf in enumerate(pdf_paths):
            book_title = clean_title
            if len(pdf_paths) > 1 and i > 0:
                book_title = f"{clean_title} ({i + 1})"
            self.worker.add_job(
                pdf,
                model_path,
                config_path,
                book_title,
                author.strip(),
                voice_model,
                length_scale,
            )
        self.worker.progress.connect(self._on_progress)
        self.worker.job_done.connect(self._on_convert_done)
        self.worker.job_failed.connect(self._on_convert_failed)
        self.worker.all_done.connect(self._on_convert_all_done)
        self.worker.paused_changed.connect(self._on_conv_paused_changed)
        self._conv_paused = False
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.btn_pause.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText(tr("extracting"))
        self.worker.start()

    def _on_progress(self, percent, message):
        self.progress_bar.setValue(percent)
        self.progress_label.setText(message)

    def _on_convert_done(self, book_id, title):
        self._reload_books()

    def _on_convert_all_done(self):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.btn_pause.setVisible(False)
        self.btn_cancel.setVisible(False)
        self._reload_books()
        self.worker = None
        QMessageBox.information(self, tr("done_title"), tr("done"))

    def _on_convert_failed(self, message, title):
        self.progress_label.setText(f"{title}: {message}")

    def _toggle_conv_pause(self):
        if self.worker is None:
            return
        paused = not self._conv_paused
        self.worker.set_paused(paused)
        self.btn_pause.setText(
            tr("resume_conv") if paused else tr("pause_conv")
        )

    def _on_conv_paused_changed(self, paused):
        self._conv_paused = paused
        self.btn_pause.setText(
            tr("resume_conv") if paused else tr("pause_conv")
        )

    def _cancel_conversion(self):
        if self.worker is None:
            return
        self.worker.cancel()
        self.btn_cancel.setEnabled(False)

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
        item = self.chapter_list.item(idx)
        if item is not None:
            self.chapter_list.setCurrentRow(idx)
            self.chapter_list.scrollToItem(item)
        try:
            self.player.play(ch["audio_path"], position=position)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, tr("playback_error_title"), str(exc))
            return
        self.read_panel.set_chapter(idx)
        self._save_timer.start()
        self._highlight_active_book()
        self._update_now_label()

    def _highlight_active_book(self):
        if self.current_book is None:
            return
        for bid, item in self._book_items.items():
            item.setBackground(QBrush(Qt.GlobalColor.transparent))
        item = self._book_items.get(self.current_book["id"])
        if item is not None:
            item.setBackground(QBrush(QColor(46, 204, 113, 70)))

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

    # ------------------------------------------------------ read-along sync
    def _toggle_readalong(self):
        show = not self.read_panel.isVisible()
        self.read_panel.setVisible(show)
        self.btn_readalong.setChecked(show)
        if show:
            self._load_readalong()
            self._sync_readalong()

    def _load_readalong(self):
        if self.current_book is None:
            self.read_panel.set_book(None, [], self._chapter_offsets)
            return
        alignments = self.db.get_sentence_alignments(self.current_book["id"])
        self.read_panel.set_book(
            self.current_book.get("source_pdf"), alignments, self._chapter_offsets
        )

    def _sync_readalong(self):
        if not hasattr(self, "read_panel") or not self.read_panel.isVisible():
            return
        if self.current_chapter_idx < 0 or self.current_book is None:
            return
        if not self.player.is_playing:
            # Paused/stopped: never scroll or highlight while not playing.
            return
        offset = (
            self._chapter_offsets[self.current_chapter_idx]
            if self.current_chapter_idx < len(self._chapter_offsets)
            else 0.0
        )
        self.read_panel.set_playback_time(offset + self.player.position)

    def _readalong_seek(self, audio_start):
        """Jump audio to a sentence clicked in the read-along panel."""
        if self.current_chapter_idx < 0:
            return
        offset = (
            self._chapter_offsets[self.current_chapter_idx]
            if self.current_chapter_idx < len(self._chapter_offsets)
            else 0.0
        )
        target = max(0.0, audio_start - offset)
        self._play_chapter(self.current_chapter_idx, target)

    def _stop_playback(self):
        self.player.stop()
        self._save_timer.stop()
        self.now_label.setText(tr("nothing_playing"))
        self.slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")

    def _toggle_repeat(self):
        self.repeat_mode = (self.repeat_mode + 1) % 3
        self._update_repeat_button()
        self._update_statusbar()

    def _update_repeat_button(self):
        icon_name = REPEAT_ICON_FILES[self.repeat_mode]
        self.btn_repeat.setIcon(
            QIcon(resource_path(os.path.join("assets", icon_name)))
        )
        self.btn_repeat.setToolTip(tr(REPEAT_LABEL_KEYS[self.repeat_mode]))

    SPEEDS = [0.75, 1.0, 1.25, 1.5, 2.0]

    def _cycle_speed(self, delta):
        cur = 1
        try:
            cur = self.SPEEDS.index(round(self.player.rate, 2))
        except ValueError:
            pass
        nxt = (cur + delta) % len(self.SPEEDS)
        rate = self.SPEEDS[nxt]
        try:
            self.player.set_rate(rate)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, tr("playback_error_title"), str(exc))
            return
        self.btn_speed.setText(f"{rate:g}x")
        self._update_now_label()

    # ---------------------------------------------------------- bookmarks
    def _add_bookmark(self):
        if self.current_book is None or self.current_chapter_idx < 0:
            return
        ch = self.current_chapters[self.current_chapter_idx]
        pos = self.player.position
        self.db.add_bookmark(
            self.current_book["id"],
            self.current_chapter_idx,
            pos,
            f"{ch['title']} @ {fmt_duration(pos)}",
        )
        self._update_bookmarks_menu()
        QMessageBox.information(self, tr("bookmarks_menu"), tr("bookmark_added"))

    def _update_bookmarks_menu(self):
        marks = self.db.list_bookmarks(self.current_book["id"]) if self.current_book else []
        text = tr("no_bookmarks") if not marks else tr("bookmarks_menu")
        self._bookmarks_list_action.setText(text)

    def _show_bookmarks(self):
        if self.current_book is None:
            return
        marks = self.db.list_bookmarks(self.current_book["id"])
        if not marks:
            QMessageBox.information(self, tr("bookmarks_menu"), tr("no_bookmarks"))
            return
        dlg = BookmarkDialog(marks, self)
        if dlg.exec() and dlg.chosen is not None:
            idx, pos = dlg.chosen
            self._play_chapter(idx, pos)

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

    def _slider_pressed(self):
        # User grabbed the handle or clicked the track: pause ticker sync so
        # the 500 ms UI timer cannot overwrite the slider before release.
        self._slider_guard = True

    def _slider_released(self):
        # Fired for both dragging (after sliderMoved) and a plain track click.
        # On a plain click the slider jumps to the clicked value without any
        # sliderMoved emission, so seeking on release covers that case.
        self._slider_guard = False
        self._seek(self.slider.value())

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
        self._sync_readalong()
        # Auto-advance when a chapter finishes (never while paused).
        if (
            not self.player.is_playing
            and not self.player.is_paused
            and self.player.current_path is not None
        ):
            if self.repeat_mode == REPEAT_ONE:
                self._play_chapter(self.current_chapter_idx, 0.0)
            elif self.current_chapter_idx < len(self.current_chapters) - 1:
                self._play_chapter(self.current_chapter_idx + 1, 0.0)
            elif self.repeat_mode == REPEAT_ALL:
                self._play_chapter(0, 0.0)
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
        state = (
            tr("state_playing") if self.player.is_playing else tr("state_paused")
        )
        self.now_label.setText(
            tr(
                "now_playing",
                title=self.current_book["title"],
                chapter=ch["title"],
                state=state,
            )
        )
        playing = self.player.is_playing
        icon_name = "pause.png" if playing else "play.png"
        self.btn_play.setIcon(
            QIcon(resource_path(os.path.join("assets", icon_name)))
        )
        self.btn_play.setText(tr("pause") if playing else tr("play"))

    def closeEvent(self, event):
        self._save_progress()
        self.player.stop()
        super().closeEvent(event)

    # -------------------------------------------------------------- misc
    def _open_library_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.startfile(DATA_DIR)  # noqa: S606 - opening a folder is intended

    def _check_updates_now(self):
        import webbrowser

        try:
            latest = check_for_updates()
        except Exception:  # noqa: BLE001 - network failures are non-fatal
            QMessageBox.information(self, tr("update_ok"), tr("update_ok"))
            return
        if not latest or latest == APP_VERSION:
            QMessageBox.information(self, tr("update_ok"), tr("update_ok"))
            return
        box = QMessageBox(
            QMessageBox.Icon.Question,
            tr("update_available_title"),
            tr("update_available_msg", version=latest),
            parent=self,
        )
        open_btn = box.addButton(tr("yes"), QMessageBox.ButtonRole.YesRole)
        box.addButton(tr("no"), QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() is open_btn:
            webbrowser.open("https://github.com/JuanBerta/pdf2audiobook/releases/latest")


class BookmarkDialog(QDialog):
    """Modal dialog listing bookmarks; double-click to jump, Del to delete."""

    def __init__(self, marks, parent=None):
        super().__init__(parent)
        self.setWindowTitle(parent.tr("bookmarks_menu"))
        self.resize(420, 320)
        self.chosen = None
        self._marks = marks

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for i, mark in enumerate(marks):
            item = QListWidgetItem(
                f"{mark['chapter_idx'] + 1:02d}  {fmt_duration(mark['position'])}  {mark['note'] or ''}"
            )
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.list_widget.addItem(item)
        self.list_widget.itemDoubleClicked.connect(self._jump)
        self.list_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.list_widget.customContextMenuRequested.connect(self._context)
        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        btn_close = QPushButton(parent.tr("no"))
        btn_close.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _jump(self, item):
        self.chosen = (
            self._marks[item.data(Qt.ItemDataRole.UserRole)]["chapter_idx"],
            self._marks[item.data(Qt.ItemDataRole.UserRole)]["position"],
        )
        self.accept()

    def _context(self, pos):
        from PyQt6.QtWidgets import QMenu

        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        action = menu.addAction(self.parent().tr("delete_from_library"))
        if menu.exec(self.list_widget.mapToGlobal(pos)) is action:
            idx = item.data(Qt.ItemDataRole.UserRole)
            mark = self._marks.pop(idx)
            self.parent().db.delete_bookmark(mark["id"])
            self.list_widget.takeItem(self.list_widget.row(item))


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(LOG_DIR, "app.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.error(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        try:
            QMessageBox.critical(
                None, "PDF2Audiobook", f"Unexpected error:\n{exc_value}"
            )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setApplicationName("PDF2Audiobook")
    app.setApplicationDisplayName("PDF2Audiobook")

    # Single-instance lock: protects the SQLite library from concurrent writes.
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = QLockFile(LOCK_PATH)
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(
            None, tr("single_instance_title"), tr("single_instance_msg")
        )
        return 1

    settings = QSettings("PDF2Audiobook", "PDF2Audiobook")
    stored = settings.value("ui/language", "en")
    if stored in SUPPORTED_LANGUAGES:
        set_language(stored)
    theme_mode = str(settings.value("ui/theme", "dark") or "dark")
    apply_theme(app, theme_mode)

    win = MainWindow()
    win.show()

    # First-run guide: warn early when no Piper voices are installed.
    if not list_voice_models(MODELS_DIR):
        QTimer.singleShot(
            800,
            lambda: QMessageBox.information(
                win, tr("model_missing_title"), tr("model_missing_msg", dir=MODELS_DIR)
            ),
        )

    # Silent update check shortly after startup.
    def _silent_update_check():
        try:
            latest = check_for_updates()
        except Exception:  # noqa: BLE001
            return
        if latest and latest != APP_VERSION:
            box = QMessageBox(
                QMessageBox.Icon.Question,
                tr("update_available_title"),
                tr("update_available_msg", version=latest),
                parent=win,
            )
            open_btn = box.addButton(tr("yes"), QMessageBox.ButtonRole.YesRole)
            box.addButton(tr("no"), QMessageBox.ButtonRole.NoRole)
            box.exec()
            if box.clickedButton() is open_btn:
                import webbrowser

                webbrowser.open(
                    "https://github.com/JuanBerta/pdf2audiobook/releases/latest"
                )

    QTimer.singleShot(3000, _silent_update_check)

    rc = app.exec()
    lock.unlock()
    sys.exit(rc)


if __name__ == "__main__":
    main()
