"""PDF2Audiobook - PyQt6 desktop app.

Main window provides:
  - a gallery of converted audiobooks (SQLite-backed)
  - a PDF -> audiobook conversion queue with progress, pause and resume
  - chapter-based playback with resume, speed, bookmarks and sleep timer
  - a browser read-along view, M4B export and an in-app voice manager
"""
import collections
import json
import logging
import os
import shutil
import sys
import webbrowser

from PyQt6.QtCore import QLockFile, QRect, QSettings, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .browser_pdf import ReadAlongServer
from .converter import PiperTTS, export_m4b, find_ffmpeg, needs_realign, realign_book, sanitize
from .database import AudiobookDB
from .dialogs import (
    BookmarkDialog,
    ConvertDialog,
    EditBookDialog,
    VoiceManagerDialog,
    ask,
    fmt_duration,
)
from .i18n import LANGUAGE_NAMES, SUPPORTED_LANGUAGES, get_language, set_language, tr
from .mediakeys import GlobalMediaKeys
from .player import AudioPlayer
from .theme import apply_theme
from .updater import RELEASES_PAGE, check_for_updates, is_newer
from .voices import list_voice_models, preview_text, voice_display_name
from .worker import QueueWorker, TaskThread

APP_VERSION = __version__

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PDF2Audiobook")
DB_PATH = os.path.join(DATA_DIR, "library.db")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
MODELS_DIR = os.path.join(DATA_DIR, "models")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOCK_PATH = os.path.join(DATA_DIR, "app.lock")
CATALOG_CACHE = os.path.join(DATA_DIR, "voices_catalog.json")

PLAYBACK_SPEEDS = [0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
JUMP_SECONDS = 10

REPEAT_OFF, REPEAT_ALL, REPEAT_ONE = 0, 1, 2
REPEAT_ICON_FILES = {REPEAT_OFF: "repeat_off.png", REPEAT_ALL: "repeat_all.png", REPEAT_ONE: "repeat_one.png"}
REPEAT_LABEL_KEYS = {REPEAT_OFF: "repeat_off", REPEAT_ALL: "repeat_all", REPEAT_ONE: "repeat_one"}
SLEEP_END_OF_TRACK = -1


def resource_path(rel):
    """Resolve asset paths when frozen by PyInstaller."""
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), rel)


def asset_icon(name):
    return QIcon(resource_path(os.path.join("assets", name)))


class ClickSlider(QSlider):
    """Slider that jumps to the clicked position instead of page-stepping."""

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self
            )
            pt = event.position().toPoint()
            if not handle.contains(pt):
                span = max(1, self.width() - handle.width())
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), pt.x() - handle.width() // 2, span
                )
                self.setValue(value)
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF2Audiobook")
        self.resize(980, 640)
        self.setAcceptDrops(True)

        self.db = AudiobookDB(DB_PATH)
        self.player = AudioPlayer()
        self.preview_player = AudioPlayer()
        self.worker = None
        self._tasks = set()
        self._conv_failures = []
        self._job_prefix = ""
        self._conv_paused = False

        self.current_book = None
        self.current_chapters = []
        self.current_chapter_idx = -1
        self._chapter_offsets = []
        self._resume_pos = 0.0
        self._slider_dragging = False
        self.repeat_mode = REPEAT_OFF
        self._book_items = {}
        self._cover_cache = {}
        self._read_server = None
        # Commands from the browser reader arrive on HTTP threads; they are
        # queued and applied on the UI tick (main thread, serialized).
        self._commands = collections.deque()
        self._media_keys = None
        self._tts_preview_cache = {}

        self.settings = QSettings("PDF2Audiobook", "PDF2Audiobook")
        self._theme_mode = str(self.settings.value("ui/theme", "dark") or "dark")
        self._sleep_remaining_min = 0
        self._sleep_eot = False  # "stop at end of current track"
        self._sleep_timer = QTimer(self)
        self._sleep_timer.timeout.connect(self._on_sleep_tick)

        try:
            rate = float(self.settings.value("player/rate", 1.0) or 1.0)
            self.player.set_rate(rate)
        except (TypeError, ValueError):
            pass

        self._build_ui()
        self._build_menu()
        self._build_shortcuts()
        self._apply_ui_texts()
        self._reload_books()

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(250)
        self._ui_timer.timeout.connect(self._tick)
        self._ui_timer.start()

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(self._save_progress)

        if self._settings_bool("player/global_media_keys", False):
            QTimer.singleShot(0, lambda: self._set_global_media_keys(True))

    # ============================================================ helpers
    def _settings_bool(self, key, default):
        val = self.settings.value(key, default)
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes")
        return bool(val)

    def _run_task(self, fn, on_success=None, on_fail=None, progress_dialog_title=None,
                  on_cancel=None):
        """Run fn(progress_cb, cancel_event) in a TaskThread (optionally with a dialog)."""
        task = TaskThread(fn, self)
        dlg = None
        if progress_dialog_title:
            dlg = QProgressDialog(progress_dialog_title, tr("cancel"), 0, 100, self)
            dlg.setWindowTitle("PDF2Audiobook")
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            dlg.setMinimumDuration(300)
            dlg.setAutoClose(False)
            dlg.setAutoReset(False)
            dlg.canceled.connect(task.cancel)

            def prog(p, m):
                dlg.setValue(p)
                if m:
                    dlg.setLabelText(m)

            task.progress.connect(prog)

        def finish():
            self._tasks.discard(task)
            if dlg is not None:
                dlg.close()

        task.finished.connect(finish)
        if on_success:
            task.succeeded.connect(on_success)
        task.failed.connect(on_fail or (lambda m: QMessageBox.warning(self, "PDF2Audiobook", m)))
        if on_cancel:
            task.cancelled.connect(on_cancel)
        self._tasks.add(task)
        task.start()
        return task

    def _is_converting(self):
        return self.worker is not None and self.worker.isRunning()

    def _converting_ids(self):
        if not self._is_converting():
            return set()
        ids = set(self.worker.queued_book_ids())
        if self.worker.current_book_id:
            ids.add(self.worker.current_book_id)
        return ids

    # ================================================================ UI
    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.btn_convert = QPushButton()
        self.btn_convert.setObjectName("primaryButton")
        self.btn_convert.clicked.connect(lambda: self._start_conversion())
        self.btn_voices = QPushButton()
        self.btn_voices.clicked.connect(self._manage_voices)
        self.btn_open_dir = QPushButton()
        self.btn_open_dir.clicked.connect(self._open_library_dir)
        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(220)
        self.search_box.textChanged.connect(self._apply_book_filter)
        top.addWidget(self.btn_convert)
        top.addWidget(self.btn_voices)
        top.addWidget(self.btn_open_dir)
        top.addStretch(1)
        top.addWidget(self.search_box)
        root.addLayout(top)

        self.progress_bar = QProgressBar()
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("mutedLabel")
        self.btn_pause = QPushButton()
        self.btn_pause.clicked.connect(self._toggle_conv_pause)
        self.btn_cancel = QPushButton()
        self.btn_cancel.clicked.connect(self._cancel_conversion)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.btn_pause)
        progress_row.addWidget(self.btn_cancel)
        root.addLayout(progress_row)
        root.addWidget(self.progress_label)
        self._set_progress_visible(False)

        middle = QHBoxLayout()
        self.book_list = QListWidget()
        self.book_list.setViewMode(QListView.ViewMode.IconMode)
        self.book_list.setIconSize(QSize(110, 150))
        self.book_list.setGridSize(QSize(170, 240))
        self.book_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.book_list.setMovement(QListView.Movement.Static)
        self.book_list.setWordWrap(True)
        self.book_list.setSpacing(8)
        self.book_list.setUniformItemSizes(True)
        self.book_list.itemDoubleClicked.connect(lambda item: self._open_book_item(item))
        self.book_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.book_list.customContextMenuRequested.connect(self._book_menu)
        self.chapter_list = QListWidget()
        self.chapter_list.itemDoubleClicked.connect(self._play_chapter_item)
        self.chapter_list.setMinimumWidth(300)
        middle.addWidget(self.book_list, 3)
        middle.addWidget(self.chapter_list, 2)
        root.addLayout(middle, 1)

        now_row = QHBoxLayout()
        self.now_label = QLabel()
        self.now_label.setObjectName("nowPlayingLabel")
        self.btn_readalong = QPushButton()
        self.btn_readalong.clicked.connect(self._open_read_pdf)
        now_row.addWidget(self.now_label, 1)
        now_row.addWidget(self.btn_readalong)
        root.addLayout(now_row)

        bar = QHBoxLayout()
        self.btn_prev = QPushButton(asset_icon("prev.png"), "")
        self.btn_prev.clicked.connect(lambda: self._skip(-1))
        self.btn_play = QPushButton(asset_icon("play.png"), "")
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_stop = QPushButton(asset_icon("stop.png"), "")
        self.btn_stop.clicked.connect(lambda: self._stop_playback())
        self.btn_next = QPushButton(asset_icon("next.png"), "")
        self.btn_next.clicked.connect(lambda: self._skip(1))
        self.btn_repeat = QPushButton(asset_icon("repeat_off.png"), "")
        self.btn_repeat.clicked.connect(self._toggle_repeat)
        self.btn_speed = QPushButton()
        self.btn_speed.setFixedWidth(64)
        self.speed_menu = QMenu(self)
        self.btn_speed.setMenu(self.speed_menu)
        for b in (self.btn_prev, self.btn_play, self.btn_stop, self.btn_next, self.btn_repeat):
            b.setIconSize(QSize(22, 22))
        self.slider = ClickSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(self._slider_pressed)
        self.slider.sliderMoved.connect(self._slider_moved)
        self.slider.sliderReleased.connect(self._slider_released)
        self.time_label = QLabel("00:00 / 00:00")
        for w in (self.btn_prev, self.btn_play, self.btn_stop, self.btn_next, self.btn_repeat, self.btn_speed):
            bar.addWidget(w)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.time_label)
        root.addLayout(bar)

        self.book_progress_label = QLabel("")
        self.statusBar().addPermanentWidget(self.book_progress_label)
        self.setCentralWidget(central)
        self._update_speed_ui()

    def _build_menu(self):
        mb = self.menuBar()
        mb.clear()

        m_file = mb.addMenu(tr("menu_file"))
        act = m_file.addAction(tr("convert_btn"), lambda: self._start_conversion())
        act.setShortcut(QKeySequence("Ctrl+O"))
        m_file.addAction(tr("manage_voices"), self._manage_voices)
        m_file.addAction(tr("open_dir_btn"), self._open_library_dir)
        m_file.addSeparator()
        act = m_file.addAction(tr("quit"), self.close)
        act.setShortcut(QKeySequence("Ctrl+Q"))

        m_play = mb.addMenu(tr("menu_playback"))
        self._add_action(m_play, tr("play_pause"), self._toggle_play)
        self._add_action(m_play, tr("stop"), lambda: self._stop_playback())
        m_play.addSeparator()
        self._add_action(m_play, tr("prev"), lambda: self._skip(-1), "Ctrl+Left")
        self._add_action(m_play, tr("next"), lambda: self._skip(1), "Ctrl+Right")
        self._add_action(m_play, tr("jump_back", seconds=JUMP_SECONDS), lambda: self._jump(-JUMP_SECONDS), "Alt+Left")
        self._add_action(m_play, tr("jump_forward", seconds=JUMP_SECONDS), lambda: self._jump(JUMP_SECONDS), "Alt+Right")
        m_play.addSeparator()
        self._add_action(m_play, tr("speed_up"), lambda: self._step_speed(1), "Ctrl+Up")
        self._add_action(m_play, tr("speed_down"), lambda: self._step_speed(-1), "Ctrl+Down")
        self._add_action(m_play, tr("toggle_repeat"), self._toggle_repeat, "R")
        m_play.addSeparator()
        self._add_action(m_play, tr("add_bookmark"), self._add_bookmark, "Ctrl+B")
        self._add_action(m_play, tr("show_bookmarks"), self._show_bookmarks, "Ctrl+Shift+B")
        m_play.addSeparator()

        self.sleep_menu = m_play.addMenu(tr("menu_sleep_timer"))
        group = QActionGroup(self)
        self._sleep_actions = {}
        for minutes in (0, SLEEP_END_OF_TRACK, 15, 30, 45, 60, 90, 120):
            if minutes == 0:
                label = tr("sleep_off")
            elif minutes == SLEEP_END_OF_TRACK:
                label = tr("sleep_end_of_track")
            else:
                label = tr("sleep_minutes", minutes=minutes)
            a = QAction(label, self, checkable=True)
            a.setActionGroup(group)
            a.triggered.connect(lambda _c, m=minutes: self._set_sleep_timer(m))
            self.sleep_menu.addAction(a)
            self._sleep_actions[minutes] = a
        current = SLEEP_END_OF_TRACK if getattr(self, "_sleep_eot", False) else self._sleep_remaining_min
        self._sleep_actions.get(current, self._sleep_actions[0]).setChecked(True)

        if GlobalMediaKeys.supported():
            m_play.addSeparator()
            self._act_media = QAction(tr("global_media_keys"), self, checkable=True)
            self._act_media.setChecked(self._media_keys is not None)
            self._act_media.toggled.connect(self._set_global_media_keys)
            m_play.addAction(self._act_media)

        m_view = mb.addMenu(tr("menu_view"))
        m_lang = m_view.addMenu(tr("menu_language"))
        group = QActionGroup(self)
        for code in SUPPORTED_LANGUAGES:
            a = QAction(LANGUAGE_NAMES[code], self, checkable=True)
            a.setActionGroup(group)
            a.setChecked(code == get_language())
            a.triggered.connect(lambda _c, c=code: self._switch_language(c))
            m_lang.addAction(a)
        m_theme = m_view.addMenu(tr("menu_theme"))
        group = QActionGroup(self)
        for mode, key in (("dark", "theme_dark"), ("light", "theme_light")):
            a = QAction(tr(key), self, checkable=True)
            a.setActionGroup(group)
            a.setChecked(mode == self._theme_mode)
            a.triggered.connect(lambda _c, m=mode: self._set_theme(m))
            m_theme.addAction(a)

        m_help = mb.addMenu(tr("menu_help"))
        m_help.addAction(tr("check_updates"), lambda: self._check_updates(silent=False))
        m_help.addAction(tr("open_logs"), self._open_logs)
        m_help.addAction(tr("about"), self._about)

    def _add_action(self, menu, text, slot, shortcut=None):
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        act.triggered.connect(slot)
        menu.addAction(act)
        return act

    def _build_shortcuts(self):
        # Space is a QShortcut (not a menu shortcut) so it is not shown on the
        # menu and lists keep their own keyboard behaviour elsewhere.
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_play)
        for key, cmd in (
            (Qt.Key.Key_MediaTogglePlayPause, "toggle"),
            (Qt.Key.Key_MediaPlay, "toggle"),
            (Qt.Key.Key_MediaPause, "toggle"),
            (Qt.Key.Key_MediaNext, "next"),
            (Qt.Key.Key_MediaPrevious, "prev"),
            (Qt.Key.Key_MediaStop, "stop"),
        ):
            QShortcut(QKeySequence(key), self, activated=lambda c=cmd: self._media_command(c))

    def _apply_ui_texts(self):
        self.search_box.setPlaceholderText(tr("search_placeholder"))
        self.btn_convert.setText(tr("convert_btn"))
        self.btn_voices.setText(tr("manage_voices"))
        self.btn_open_dir.setText(tr("open_dir_btn"))
        self.btn_play.setToolTip(tr("play_pause") + " (Space)")
        self.btn_stop.setToolTip(tr("stop"))
        self.btn_prev.setToolTip(tr("prev") + " (Ctrl+Left)")
        self.btn_next.setToolTip(tr("next") + " (Ctrl+Right)")
        self.btn_speed.setToolTip(tr("speed_tooltip"))
        self.btn_cancel.setText(tr("cancel"))
        self.btn_pause.setText(tr("resume_conv") if self._conv_paused else tr("pause_conv"))
        self.btn_readalong.setText(tr("readalong_toggle"))
        self.btn_readalong.setToolTip(tr("readalong_tip"))
        self._update_repeat_button()
        self._update_speed_ui()
        self._update_now_label()
        self._update_statusbar()

    def _set_progress_visible(self, visible):
        for w in (self.progress_bar, self.progress_label, self.btn_pause, self.btn_cancel):
            w.setVisible(visible)

    # ------------------------------------------------------------ settings
    def _set_theme(self, mode):
        self._theme_mode = mode
        self.settings.setValue("ui/theme", mode)
        apply_theme(QApplication.instance(), mode)
        self._update_repeat_button()

    def _switch_language(self, code):
        if code == get_language():
            return
        set_language(code)
        self.settings.setValue("ui/language", code)
        self._build_menu()
        self._apply_ui_texts()
        self._reload_books()
        if self.current_book is not None:
            self._fill_chapter_list()

    def _set_sleep_timer(self, minutes):
        self._sleep_eot = minutes == SLEEP_END_OF_TRACK
        self._sleep_remaining_min = max(0, minutes)
        self._sleep_timer.stop()
        if minutes > 0:
            self._sleep_timer.start(60 * 1000)
        if minutes in self._sleep_actions:
            self._sleep_actions[minutes].setChecked(True)
        self._update_statusbar()

    def _on_sleep_tick(self):
        if not self.player.is_playing:
            return  # the countdown only runs while something is playing
        self._sleep_remaining_min -= 1
        if self._sleep_remaining_min <= 0:
            self._set_sleep_timer(0)
            self._pause_playback()
        self._update_statusbar()

    def _set_global_media_keys(self, enabled):
        if enabled and self._media_keys is None:
            keys = GlobalMediaKeys(int(self.winId()), self._media_command)
            if keys.register():
                QApplication.instance().installNativeEventFilter(keys)
                self._media_keys = keys
            else:
                QMessageBox.information(self, "PDF2Audiobook", tr("media_keys_failed"))
                enabled = False
        elif not enabled and self._media_keys is not None:
            self._media_keys.unregister()
            QApplication.instance().removeNativeEventFilter(self._media_keys)
            self._media_keys = None
        self.settings.setValue("player/global_media_keys", bool(enabled))
        if hasattr(self, "_act_media"):
            self._act_media.blockSignals(True)
            self._act_media.setChecked(bool(enabled))
            self._act_media.blockSignals(False)

    def _media_command(self, cmd):
        if cmd == "toggle":
            self._toggle_play()
        elif cmd == "next":
            self._skip(1)
        elif cmd == "prev":
            self._skip(-1)
        elif cmd == "stop":
            self._stop_playback()

    # ============================================================= library
    def _book_dir(self, book):
        d = book.get("book_dir") or ""
        if not d:
            chapters = self.db.get_chapters(book["id"])
            if chapters:
                d = os.path.dirname(chapters[0]["audio_path"])
            elif book.get("cover_path"):
                d = os.path.dirname(book["cover_path"])
        return d

    def _book_text(self, book):
        author = book["author"] or tr("unknown_author")
        lines = [book["title"], author]
        converting = book["id"] in self._converting_ids()
        if converting:
            lines.append(tr("state_converting", count=book["chapter_count"]))
        elif book["total_chapters"] == 0:
            lines.append(tr("state_incomplete", count=book["chapter_count"]))
        else:
            total = book["total_duration"] or book.get("audio_duration") or 0
            listened = self._listened_seconds(book)
            pct = int(100 * listened / total) if total else 0
            lines.append(tr("listened_progress", pos=fmt_duration(listened), total=fmt_duration(total), pct=pct))
        return "\n".join(lines)

    def _listened_seconds(self, book):
        if self.current_book and self.current_book["id"] == book["id"] and self._chapter_offsets:
            idx = min(book["current_chapter"], len(self._chapter_offsets) - 1)
            return self._chapter_offsets[max(0, idx)] + book["position"]
        # approximate from saved chapter index and an even split
        n = book.get("chapter_count") or 0
        total = book.get("audio_duration") or book.get("total_duration") or 0
        if not n:
            return 0.0
        return min(total, total * book["current_chapter"] / n + book["position"])

    def _book_tooltip(self, book):
        parts = [book["title"], book["author"] or tr("unknown_author")]
        if book.get("voice_model"):
            parts.append(tr("voice_label", voice=voice_display_name(book["voice_model"])))
        parts.append(tr("chunks", count=book["chapter_count"]))
        if book.get("source_pdf"):
            parts.append(book["source_pdf"])
        return "\n".join(parts)

    def _reload_books(self):
        selected = self._selected_book_id()
        self.book_list.clear()
        self._book_items = {}
        for book in self.db.list_books():
            item = QListWidgetItem(self._book_icon(book), self._book_text(book))
            item.setData(Qt.ItemDataRole.UserRole, book["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, book)
            item.setToolTip(self._book_tooltip(book))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.book_list.addItem(item)
            self._book_items[book["id"]] = item
            if book["id"] == selected:
                self.book_list.setCurrentItem(item)
        self._apply_book_filter(self.search_box.text())
        self._highlight_active_book()
        self._update_statusbar()

    def _refresh_book_item(self, book_id):
        """Update one gallery entry in place (no full rebuild)."""
        item = self._book_items.get(book_id)
        book = self.db.get_book(book_id)
        if item is None or book is None:
            self._reload_books()
            return
        item.setData(Qt.ItemDataRole.UserRole + 1, book)
        item.setText(self._book_text(book))
        item.setToolTip(self._book_tooltip(book))
        item.setIcon(self._book_icon(book))
        if self.current_book and self.current_book["id"] == book_id:
            self.current_book = book

    def _selected_book_id(self):
        item = self.book_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _book_icon(self, book):
        cover = book.get("cover_path")
        key = (book["id"], cover, book["title"])
        if cover and os.path.exists(cover):
            key = key + (os.path.getmtime(cover),)
        if key in self._cover_cache:
            return self._cover_cache[key]
        pm = None
        if cover and os.path.exists(cover):
            pm = QPixmap(cover)
            if pm.isNull():
                pm = None
            else:
                pm = pm.scaled(220, 300, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        if pm is None:
            pm = self._placeholder_cover(book)
        icon = QIcon(pm)
        self._cover_cache[key] = icon
        return icon

    @staticmethod
    def _cover_font(point_size):
        font = QFont("Segoe UI")
        font.setPixelSize(max(8, int(point_size * 1.33)))  # pixel size never warns
        return font

    def _placeholder_cover(self, book):
        palette = [
            ("#2e86ab", "#1b5570"), ("#b5651d", "#7a4214"), ("#6c5ce7", "#4a3f9e"),
            ("#00b894", "#007a5e"), ("#d63031", "#8f1f1f"), ("#e17055", "#9c4a38"),
            ("#0984e3", "#065e9e"), ("#8e44ad", "#5e2d73"),
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
        font = self._cover_font(28)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRect(6, 44, 98, 64), Qt.AlignmentFlag.AlignCenter, title[:2] or "?")
        painter.setFont(self._cover_font(9))
        painter.drawText(QRect(6, 106, 98, 40),
                         Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
                         title[:90])
        painter.end()
        return pm

    def _apply_book_filter(self, text):
        query = text.strip().lower()
        for item in self._book_items.values():
            book = item.data(Qt.ItemDataRole.UserRole + 1)
            hay = f"{book['title']} {book['author'] or ''} {book.get('voice_model') or ''}".lower()
            item.setHidden(bool(query) and query not in hay)

    def _highlight_active_book(self):
        active = self.current_book["id"] if self.current_book else None
        for bid, item in self._book_items.items():
            item.setBackground(
                QBrush(QColor(46, 204, 113, 70)) if bid == active else QBrush(Qt.GlobalColor.transparent)
            )

    def _update_statusbar(self):
        parts = []
        if self._book_items:
            parts.append(tr("stat_books", count=len(self._book_items)))
        if self.repeat_mode != REPEAT_OFF:
            parts.append(tr(REPEAT_LABEL_KEYS[self.repeat_mode]))
        if getattr(self, "_sleep_eot", False):
            parts.append(tr("sleep_end_of_track"))
        elif self._sleep_remaining_min > 0:
            parts.append(tr("sleep_left", minutes=self._sleep_remaining_min))
        self.statusBar().showMessage("  |  ".join(parts))

    def _update_book_progress_label(self):
        if self.current_book is None or self.current_chapter_idx < 0 or not self._chapter_offsets:
            self.book_progress_label.setText("")
            return
        total = sum(c["duration"] for c in self.current_chapters)
        done = self._chapter_offsets[self.current_chapter_idx] + self.player.position
        left = max(0.0, total - done) / max(0.1, self.player.rate)
        self.book_progress_label.setText(
            tr("book_progress", pos=fmt_duration(done), total=fmt_duration(total),
               left=fmt_duration(left), pct=int(100 * done / total) if total else 0)
        )

    # ---------------------------------------------------------- book menu
    def _book_menu(self, pos):
        item = self.book_list.itemAt(pos)
        if item is None:
            return
        book = self.db.get_book(item.data(Qt.ItemDataRole.UserRole))
        if book is None:
            return
        converting = book["id"] in self._converting_ids()
        menu = QMenu(self)
        a_play = menu.addAction(tr("play"))
        a_play.setEnabled(book["chapter_count"] > 0)
        a_resume = None
        if book["total_chapters"] == 0 and not converting:
            a_resume = menu.addAction(tr("resume_conversion"))
        a_read = menu.addAction(tr("readalong_toggle"))
        a_export = menu.addAction(tr("export_m4b"))
        a_export.setEnabled(book["total_chapters"] > 0)
        a_realign = menu.addAction(tr("rebuild_readalong"))
        a_realign.setEnabled(book["chapter_count"] > 0 and not converting)
        menu.addSeparator()
        a_edit = menu.addAction(tr("edit_details"))
        a_folder = menu.addAction(tr("open_book_folder"))
        menu.addSeparator()
        a_delete = menu.addAction(tr("delete_from_library"))
        a_delete.setEnabled(not converting)
        chosen = menu.exec(self.book_list.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is a_play:
            self._open_book(book)
        elif a_resume is not None and chosen is a_resume:
            self._resume_book(book)
        elif chosen is a_read:
            if not self.current_book or self.current_book["id"] != book["id"]:
                self._open_book(book, autoplay=False)
            self._open_read_pdf()
        elif chosen is a_export:
            self._export_book(book)
        elif chosen is a_realign:
            self._realign(book, then=None)
        elif chosen is a_edit:
            self._edit_book(book)
        elif chosen is a_folder:
            d = self._book_dir(book)
            if d and os.path.isdir(d):
                QDesktopServices.openUrl(QUrl.fromLocalFile(d))
        elif chosen is a_delete:
            self._delete_book(book)

    def _edit_book(self, book):
        dlg = EditBookDialog(book, self)
        if dlg.exec():
            title, author = dlg.values()
            self.db.update_book(book["id"], title=title or book["title"], author=author)
            self._refresh_book_item(book["id"])
            self._update_now_label()

    def _delete_book(self, book):
        if book["id"] in self._converting_ids():
            QMessageBox.information(self, tr("delete_book_title"), tr("delete_busy"))
            return
        if not ask(self, tr("delete_book_title"), tr("delete_book_msg", title=book["title"]),
                   QMessageBox.Icon.Warning):
            return
        if self.current_book and self.current_book["id"] == book["id"]:
            self._stop_playback(save=False)
            self.current_book = None
            self.current_chapters = []
            self.current_chapter_idx = -1
            self.chapter_list.clear()
            self._update_now_label()
        book_dir = self._book_dir(book)
        self.db.delete_book(book["id"])
        if book_dir and os.path.isdir(book_dir):
            try:
                inside = os.path.commonpath([os.path.abspath(book_dir), os.path.abspath(BOOKS_DIR)]) \
                    == os.path.abspath(BOOKS_DIR)
            except ValueError:
                inside = False
            if inside:
                shutil.rmtree(book_dir, ignore_errors=True)
        self._reload_books()

    # =========================================================== conversion
    def _ensure_voices(self):
        if list_voice_models(MODELS_DIR):
            return True
        if ask(self, tr("model_missing_title"), tr("model_missing_ask")):
            self._manage_voices()
        return bool(list_voice_models(MODELS_DIR))

    def _manage_voices(self):
        VoiceManagerDialog(self, MODELS_DIR, CATALOG_CACHE).exec()

    def _start_conversion(self, pdf_paths=None):
        if not self._ensure_voices():
            return
        if not pdf_paths:
            last_dir = str(self.settings.value("convert/last_dir", "") or "")
            pdf_paths, _ = QFileDialog.getOpenFileNames(
                self, tr("choose_pdfs"), last_dir, f"{tr('pdf_files')} (*.pdf)"
            )
            if not pdf_paths:
                return
            self.settings.setValue("convert/last_dir", os.path.dirname(pdf_paths[0]))
        dlg = ConvertDialog(
            self, pdf_paths, MODELS_DIR, self.settings,
            preview_cb=self._preview_voice,
            manage_voices_cb=self._manage_voices,
            can_preview=not self._is_converting(),
        )
        if not dlg.exec():
            self.preview_player.stop()
            return
        self.preview_player.stop()
        self._enqueue(dlg.jobs())

    def _enqueue(self, jobs):
        new_worker = not self._is_converting()
        if new_worker:
            self.worker = QueueWorker(BOOKS_DIR, DB_PATH, self)
            self.worker.progress.connect(self._on_progress)
            self.worker.job_started.connect(self._on_job_started)
            self.worker.book_created.connect(self._on_book_created)
            self.worker.job_done.connect(self._on_convert_done)
            self.worker.job_failed.connect(self._on_convert_failed)
            self.worker.all_done.connect(self._on_convert_all_done)
            self.worker.paused_changed.connect(self._on_conv_paused_changed)
            self._conv_failures = []
        for job in jobs:
            self.worker.add_job(
                job["pdf_path"], job["model_path"], job["config_path"], job["title"],
                job["author"], job["voice_model"], job["length_scale"],
                job.get("audio_format", "mp3"), job.get("options"), job.get("book_id"),
            )
        if new_worker:
            self._conv_paused = False
            self.btn_pause.setText(tr("pause_conv"))
            self.btn_cancel.setEnabled(True)
            self.progress_bar.setValue(0)
            self.progress_label.setText(tr("extracting"))
            self._set_progress_visible(True)
            self.worker.start()
        else:
            self.statusBar().showMessage(tr("queued_jobs", count=len(jobs)), 4000)

    def _resume_book(self, book):
        model_path = os.path.join(MODELS_DIR, f"{book['voice_model']}.onnx")
        if not os.path.exists(model_path):
            QMessageBox.warning(self, tr("failed_title"), tr("model_not_found", path=model_path))
            return
        if not os.path.exists(book.get("source_pdf") or ""):
            QMessageBox.warning(self, tr("failed_title"), tr("pdf_not_found", path=book.get("source_pdf")))
            return
        try:
            options = json.loads(book.get("options") or "{}") or {}
        except ValueError:
            options = {}
        config = model_path + ".json"
        self._enqueue([{
            "pdf_path": book["source_pdf"], "model_path": model_path,
            "config_path": config if os.path.exists(config) else None,
            "title": book["title"], "author": book["author"], "voice_model": book["voice_model"],
            "length_scale": book.get("rate") or 1.0,
            "audio_format": book.get("audio_format") or "wav",
            "options": options, "book_id": book["id"],
        }])

    def _on_job_started(self, index, total, title):
        self._job_prefix = tr("queue_progress", current=index, total=total, title=title)
        self.progress_label.setText(self._job_prefix)

    def _on_progress(self, percent, message):
        self.progress_bar.setValue(percent)
        self.progress_label.setText(f"{self._job_prefix}  —  {message}" if self._job_prefix else message)

    def _on_book_created(self, book_id):
        if book_id in self._book_items:
            self._refresh_book_item(book_id)
        else:
            self._reload_books()

    def _on_convert_done(self, book_id, title):
        self._refresh_book_item(book_id)
        if self.current_book and self.current_book["id"] == book_id:
            self._load_chapters(self.db.get_book(book_id))

    def _on_convert_failed(self, message, title):
        logging.warning("Conversion failed for %s: %s", title, message)
        self._conv_failures.append((title, message))

    def _on_convert_all_done(self, done, failed, cancelled):
        self._set_progress_visible(False)
        self.worker = None
        self._job_prefix = ""
        self._reload_books()
        if cancelled:
            msg = tr("summary_cancelled", done=done)
        elif failed:
            msg = tr("summary_failed", done=done, failed=failed)
        else:
            msg = tr("summary_done", done=done)
        if self._conv_failures:
            msg += "\n\n" + "\n".join(f"• {t}: {m}" for t, m in self._conv_failures)
        icon = QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information
        QMessageBox(icon, tr("done_title"), msg, QMessageBox.StandardButton.Ok, self).exec()

    def _toggle_conv_pause(self):
        if self.worker is not None:
            self.worker.set_paused(not self._conv_paused)

    def _on_conv_paused_changed(self, paused):
        self._conv_paused = paused
        self.btn_pause.setText(tr("resume_conv") if paused else tr("pause_conv"))

    def _cancel_conversion(self):
        if self.worker is None:
            return
        self.worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.progress_label.setText(tr("cancelling"))

    def _preview_voice(self, model_path, config_path, voice_model, speed):
        if self._is_converting():
            return
        if self.player.is_playing:
            self._pause_playback()
        tts = self._tts_preview_cache.get(model_path)
        if tts is None:
            tts = PiperTTS(model_path, config_path)
            self._tts_preview_cache = {model_path: tts}
        text = preview_text(voice_model)

        def job(_p, cancel):
            samples = tts.synthesize(text, length_scale=1.0 / float(speed or 1.0), cancel_event=cancel)
            return samples, tts.sample_rate

        self.statusBar().showMessage(tr("preview_loading"), 5000)
        self._run_task(
            job,
            on_success=lambda r: self.preview_player.play_array(r[0].astype("float32") / 32768.0, r[1]),
        )

    # ============================================================ playback
    def _open_book_item(self, item):
        book = self.db.get_book(item.data(Qt.ItemDataRole.UserRole))
        if book:
            self._open_book(book)

    def _load_chapters(self, book):
        self.current_book = book
        self.current_chapters = self.db.get_chapters(book["id"])
        self._chapter_offsets = []
        acc = 0.0
        for ch in self.current_chapters:
            self._chapter_offsets.append(acc)
            acc += ch["duration"]
        self._fill_chapter_list()
        if self._read_server is not None:
            self._push_book_to_reader()

    def _fill_chapter_list(self):
        self.chapter_list.clear()
        for ch in self.current_chapters:
            item = QListWidgetItem(f"{ch['idx'] + 1:02d}  {ch['title']}  ({fmt_duration(ch['duration'])})")
            item.setData(Qt.ItemDataRole.UserRole, ch["idx"])
            item.setToolTip(ch["title"])
            self.chapter_list.addItem(item)
        if 0 <= self.current_chapter_idx < self.chapter_list.count():
            self.chapter_list.setCurrentRow(self.current_chapter_idx)

    def _open_book(self, book, autoplay=True):
        same_book = self.current_book is not None and self.current_book["id"] == book["id"]
        if same_book and self.player.is_loaded:
            # Re-opening the book that is already loaded: just keep listening.
            if autoplay and self.player.is_paused:
                self.player.resume()
                self._save_timer.start()
                self._update_now_label()
            return
        if self.current_book and not same_book:
            self._save_progress()
            self._stop_playback(save=False)
        self._load_chapters(book)
        if not self.current_chapters:
            QMessageBox.information(self, "PDF2Audiobook", tr("no_audio_yet"))
            return
        idx = book["current_chapter"] if book["current_chapter"] < len(self.current_chapters) else 0
        pos = book["position"] if idx == book["current_chapter"] else 0.0
        self.current_chapter_idx = idx
        self._resume_pos = pos
        self._highlight_active_book()
        if autoplay:
            self._play_chapter(idx, pos)
        else:
            self.chapter_list.setCurrentRow(idx)
            self._update_now_label()

    def _play_chapter_item(self, item):
        if item is not None:
            self._play_chapter(item.data(Qt.ItemDataRole.UserRole), 0.0)

    def _row_for_idx(self, idx):
        for i, ch in enumerate(self.current_chapters):
            if ch["idx"] == idx:
                return i
        return -1

    def _play_chapter(self, row, position=0.0):
        if self.current_book is None or not (0 <= row < len(self.current_chapters)):
            return
        ch = self.current_chapters[row]
        same = (
            row == self.current_chapter_idx
            and self.player.is_loaded
            and self.player.current_path == ch["audio_path"]
        )
        try:
            if same:
                self.player.seek(position)
                if self.player.is_paused:
                    self.player.resume()
            else:
                self.player.play(ch["audio_path"], position=position)
        except Exception as exc:  # noqa: BLE001
            logging.exception("playback failed")
            QMessageBox.warning(self, tr("playback_error_title"), f"{exc}")
            return
        self.current_chapter_idx = row
        self.chapter_list.setCurrentRow(row)
        item = self.chapter_list.item(row)
        if item is not None:
            self.chapter_list.scrollToItem(item)
        self._save_timer.start()
        self._highlight_active_book()
        self._update_now_label()

    def _toggle_play(self):
        if self.current_book is None:
            item = self.book_list.currentItem()
            if item is not None:
                self._open_book_item(item)
            return
        if self.player.is_playing:
            self._pause_playback()
        elif self.player.is_paused:
            self.player.resume()
            self._save_timer.start()
        elif self.current_chapter_idx >= 0:
            self._play_chapter(self.current_chapter_idx, self._resume_pos)
        self._update_now_label()

    def _pause_playback(self):
        self.player.pause()
        self._save_progress()
        self._update_now_label()

    def _stop_playback(self, save=True):
        if save:
            self._save_progress()
        if self.player.is_loaded:
            self._resume_pos = self.player.position
        self.player.stop()
        self._save_timer.stop()
        self.slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self._update_now_label()

    def _skip(self, delta):
        if self.current_book is None:
            return
        if delta < 0 and self.player.is_loaded and self.player.position > 3.0:
            self._play_chapter(self.current_chapter_idx, 0.0)  # restart current track first
            return
        nxt = self.current_chapter_idx + delta
        if 0 <= nxt < len(self.current_chapters):
            self._play_chapter(nxt, 0.0)
        elif delta < 0 and self.current_chapter_idx == 0:
            self._play_chapter(0, 0.0)

    def _jump(self, seconds):
        if self.current_book is None or self.current_chapter_idx < 0:
            return
        row = self.current_chapter_idx
        pos = (self.player.position if self.player.is_loaded else self._resume_pos) + seconds
        if pos < 0 and row > 0:
            prev = self.current_chapters[row - 1]
            self._play_chapter(row - 1, max(0.0, prev["duration"] + pos))
        elif pos >= self.current_chapters[row]["duration"] and row < len(self.current_chapters) - 1:
            self._play_chapter(row + 1, pos - self.current_chapters[row]["duration"])
        else:
            self._play_chapter(row, min(max(0.0, pos), self.current_chapters[row]["duration"] - 0.5))

    def _toggle_repeat(self):
        self.repeat_mode = (self.repeat_mode + 1) % 3
        self._update_repeat_button()
        self._update_statusbar()

    def _update_repeat_button(self):
        name = REPEAT_ICON_FILES[self.repeat_mode]
        if self.repeat_mode == REPEAT_ONE and self._theme_mode == "light":
            name = "repeat_one_light.png"
        self.btn_repeat.setIcon(asset_icon(name))
        self.btn_repeat.setToolTip(tr(REPEAT_LABEL_KEYS[self.repeat_mode]) + " (R)")

    # ------------------------------------------------------------- speed
    def _update_speed_ui(self):
        rate = self.player.rate
        self.btn_speed.setText(f"{rate:g}x")
        self.speed_menu.clear()
        group = QActionGroup(self.speed_menu)
        for s in PLAYBACK_SPEEDS:
            a = QAction(f"{s:g}x", self.speed_menu, checkable=True)
            a.setActionGroup(group)
            a.setChecked(abs(s - rate) < 1e-6)
            a.triggered.connect(lambda _c, v=s: self._set_speed(v))
            self.speed_menu.addAction(a)

    def _set_speed(self, rate):
        try:
            self.player.set_rate(rate)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, tr("playback_error_title"), str(exc))
            return
        self.settings.setValue("player/rate", rate)
        self._update_speed_ui()
        self.statusBar().showMessage(tr("speed_now", rate=f"{rate:g}"), 2000)

    def _step_speed(self, delta):
        rate = self.player.rate
        idx = min(range(len(PLAYBACK_SPEEDS)), key=lambda i: abs(PLAYBACK_SPEEDS[i] - rate))
        idx = max(0, min(len(PLAYBACK_SPEEDS) - 1, idx + delta))
        self._set_speed(PLAYBACK_SPEEDS[idx])

    # ------------------------------------------------------------ slider
    def _slider_pressed(self):
        self._slider_dragging = True

    def _slider_moved(self, value):
        if 0 <= self.current_chapter_idx < len(self.current_chapters):
            dur = self.current_chapters[self.current_chapter_idx]["duration"]
            self.time_label.setText(f"{fmt_duration(value / 1000 * dur)} / {fmt_duration(dur)}")

    def _slider_released(self):
        self._slider_dragging = False
        if not (0 <= self.current_chapter_idx < len(self.current_chapters)):
            return
        dur = self.current_chapters[self.current_chapter_idx]["duration"]
        if dur > 0:
            self._play_chapter(self.current_chapter_idx, self.slider.value() / 1000.0 * dur)

    # ------------------------------------------------------------- ticker
    def _tick(self):
        self._drain_commands()
        if self.current_book is None or not (0 <= self.current_chapter_idx < len(self.current_chapters)):
            return
        ch = self.current_chapters[self.current_chapter_idx]
        pos = self.player.position if self.player.is_loaded else self._resume_pos
        if ch["duration"] > 0 and not self._slider_dragging:
            self.slider.blockSignals(True)
            self.slider.setValue(int(min(1.0, pos / ch["duration"]) * 1000))
            self.slider.blockSignals(False)
        if not self._slider_dragging:
            self.time_label.setText(f"{fmt_duration(pos)} / {fmt_duration(ch['duration'])}")
        self._sync_readalong()
        self._update_book_progress_label()
        if self.player.is_finished:
            self._on_track_finished()

    def _on_track_finished(self):
        if getattr(self, "_sleep_eot", False):
            self._set_sleep_timer(0)
            self._resume_pos = 0.0
            nxt = self.current_chapter_idx + 1
            self._stop_playback(save=False)
            if nxt < len(self.current_chapters):
                self.current_chapter_idx = nxt
            self._save_progress(position=0.0)
            return
        if self.repeat_mode == REPEAT_ONE:
            self._play_chapter(self.current_chapter_idx, 0.0)
        elif self.current_chapter_idx < len(self.current_chapters) - 1:
            self._play_chapter(self.current_chapter_idx + 1, 0.0)
        elif self.repeat_mode == REPEAT_ALL:
            self._play_chapter(0, 0.0)
        else:  # book finished: next time start from the beginning
            self.player.stop()
            self._save_timer.stop()
            self.current_chapter_idx = 0
            self._resume_pos = 0.0
            self.db.update_progress(self.current_book["id"], 0, 0.0)
            self._refresh_book_item(self.current_book["id"])
            self._update_now_label()
            self.statusBar().showMessage(tr("book_finished", title=self.current_book["title"]), 8000)

    def _save_progress(self, position=None):
        if self.current_book is None or self.current_chapter_idx < 0:
            return
        if position is None:
            position = self.player.position if self.player.is_loaded else self._resume_pos
        self.db.update_progress(self.current_book["id"], self.current_chapter_idx, position)
        self._refresh_book_item(self.current_book["id"])

    def _update_now_label(self):
        playing = self.player.is_playing
        self.btn_play.setIcon(asset_icon("pause.png" if playing else "play.png"))
        if self.current_book is None or not (0 <= self.current_chapter_idx < len(self.current_chapters)):
            self.now_label.setText(tr("nothing_playing"))
            return
        ch = self.current_chapters[self.current_chapter_idx]
        state = tr("state_playing") if playing else tr("state_paused")
        self.now_label.setText(tr("now_playing", title=self.current_book["title"], chapter=ch["title"], state=state))

    # ---------------------------------------------------------- bookmarks
    def _add_bookmark(self):
        if self.current_book is None or self.current_chapter_idx < 0:
            return
        ch = self.current_chapters[self.current_chapter_idx]
        pos = self.player.position if self.player.is_loaded else self._resume_pos
        self.db.add_bookmark(self.current_book["id"], ch["idx"], pos, ch["title"])
        self.statusBar().showMessage(tr("bookmark_added_at", title=ch["title"], pos=fmt_duration(pos)), 4000)

    def _show_bookmarks(self):
        if self.current_book is None:
            QMessageBox.information(self, tr("bookmarks_menu"), tr("open_book_first"))
            return
        marks = self.db.list_bookmarks(self.current_book["id"])
        if not marks:
            QMessageBox.information(self, tr("bookmarks_menu"), tr("no_bookmarks"))
            return
        dlg = BookmarkDialog(marks, self.db, self.current_chapters, self)
        if dlg.exec() and dlg.chosen is not None:
            idx, pos = dlg.chosen
            row = self._row_for_idx(idx)
            if row >= 0:
                self._play_chapter(row, pos)

    # ======================================================= read along
    def _open_read_pdf(self):
        book = self.current_book
        if book is None:
            item = self.book_list.currentItem()
            if item is None:
                QMessageBox.information(self, tr("readalong_toggle"), tr("open_book_first"))
                return
            book = self.db.get_book(item.data(Qt.ItemDataRole.UserRole))
            self._open_book(book, autoplay=False)
            book = self.current_book
            if book is None:
                return
        pdf = book.get("source_pdf") or ""
        if not os.path.exists(pdf):
            if not ask(self, tr("readalong_toggle"), tr("pdf_missing_locate", path=pdf or "?")):
                return
            new_pdf, _ = QFileDialog.getOpenFileName(self, tr("choose_pdfs"), "", f"{tr('pdf_files')} (*.pdf)")
            if not new_pdf:
                return
            self.db.update_book(book["id"], source_pdf=new_pdf)
            self.current_book = book = self.db.get_book(book["id"])
        alignments = self.db.get_sentence_alignments(book["id"])
        if needs_realign(alignments):
            self._realign(book, then=self._open_read_pdf)
            return
        if self._read_server is None:
            server = ReadAlongServer()
            server.set_seek_callback(lambda t, idx: self._commands.append(("seek", t, idx)))
            server.set_control_callback(lambda a: self._commands.append(("control", a, None)))
            server.start()
            self._read_server = server
        self._push_book_to_reader(alignments)
        if self._read_server.url:
            webbrowser.open(self._read_server.url)
        self._sync_readalong()

    def _push_book_to_reader(self, alignments=None):
        if self._read_server is None or self.current_book is None:
            return
        if alignments is None:
            alignments = self.db.get_sentence_alignments(self.current_book["id"])
        self._read_server.set_book(
            self.current_book.get("source_pdf"), alignments, self.current_chapters,
            get_language(), self.current_book.get("title", ""),
        )

    def _realign(self, book, then=None):
        if not os.path.exists(book.get("source_pdf") or ""):
            QMessageBox.warning(self, tr("rebuild_readalong"), tr("pdf_not_found", path=book.get("source_pdf")))
            return

        def done(result):
            located, total = result
            self.statusBar().showMessage(tr("realign_done", located=located, total=total), 6000)
            if then is not None:
                then()

        self._run_task(
            lambda p, c: realign_book(self.db, book["id"], p, c),
            on_success=done,
            progress_dialog_title=tr("realigning"),
        )

    def _sync_readalong(self):
        if self._read_server is None:
            return
        if self.current_chapter_idx < 0 or self.current_book is None or not self._chapter_offsets:
            self._read_server.update_state(-1, 0.0, False, self.player.rate)
            return
        offset = self._chapter_offsets[self.current_chapter_idx]
        pos = self.player.position if self.player.is_loaded else self._resume_pos
        self._read_server.update_state(
            self.current_chapters[self.current_chapter_idx]["idx"], offset + pos,
            self.player.is_playing, self.player.rate,
        )

    def _drain_commands(self):
        last_seek = None
        while self._commands:
            cmd = self._commands.popleft()
            if cmd[0] == "seek":
                last_seek = cmd  # rapid clicks: only the last one wins
            elif cmd[0] == "control":
                action = cmd[1]
                if action in ("toggle", "next", "prev"):
                    self._media_command(action)
                elif action == "back":
                    self._jump(-JUMP_SECONDS)
                elif action == "forward":
                    self._jump(JUMP_SECONDS)
        if last_seek is None or self.current_book is None:
            return
        _, audio_start, chapter_idx = last_seek
        row = self._row_for_idx(chapter_idx) if chapter_idx is not None else self.current_chapter_idx
        if row < 0:
            return
        target = max(0.0, audio_start - self._chapter_offsets[row])
        self._play_chapter(row, target)

    # ============================================================== export
    def _export_book(self, book):
        if not find_ffmpeg():
            QMessageBox.warning(self, tr("export_m4b"), tr("ffmpeg_missing"))
            return
        last = str(self.settings.value("export/last_dir", "") or os.path.expanduser("~"))
        path, _ = QFileDialog.getSaveFileName(
            self, tr("export_m4b"), os.path.join(last, f"{sanitize(book['title'])}.m4b"),
            "M4B audiobook (*.m4b)",
        )
        if not path:
            return
        if not path.lower().endswith(".m4b"):
            path += ".m4b"
        self.settings.setValue("export/last_dir", os.path.dirname(path))

        def done(out):
            if ask(self, tr("export_m4b"), tr("export_done", path=out), QMessageBox.Icon.Information):
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out)))

        self._run_task(
            lambda p, c: export_m4b(self.db, book["id"], path, p, c),
            on_success=done,
            progress_dialog_title=tr("exporting"),
        )

    # ============================================================ drag/drop
    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 - Qt API
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile().lower().endswith(".pdf")]
        if paths:
            QTimer.singleShot(0, lambda: self._start_conversion(paths))

    # ================================================================ misc
    def closeEvent(self, event):  # noqa: N802 - Qt API
        if self._is_converting():
            if not ask(self, tr("quit"), tr("quit_while_converting")):
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(15000)
        self._save_progress()
        self.player.stop()
        self.preview_player.stop()
        for task in list(self._tasks):
            task.cancel()
            task.wait(3000)
        if self._read_server is not None:
            self._read_server.shutdown()
        if self._media_keys is not None:
            self._media_keys.unregister()
        super().closeEvent(event)

    def _open_library_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(DATA_DIR))

    def _open_logs(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_DIR))

    def _about(self):
        QMessageBox.about(self, tr("about"), tr("about_text", version=APP_VERSION, url=RELEASES_PAGE))

    def _check_updates(self, silent=True):
        def ok(latest):
            if latest and is_newer(latest, APP_VERSION):
                if ask(self, tr("update_available_title"), tr("update_available_msg", version=latest)):
                    webbrowser.open(RELEASES_PAGE)
            elif not silent:
                QMessageBox.information(self, tr("check_updates"), tr("update_ok", version=APP_VERSION))

        def fail(msg):
            logging.info("update check failed: %s", msg)
            if not silent:
                QMessageBox.warning(self, tr("check_updates"), tr("update_failed", error=msg))

        self._run_task(lambda _p, _c: check_for_updates(), on_success=ok, on_fail=fail)


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    if "--selftest" in sys.argv:
        from .selftest import run

        i = sys.argv.index("--selftest")
        report = sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.join(LOG_DIR, "selftest.txt")
        return run(report, MODELS_DIR)
    logging.basicConfig(
        filename=os.path.join(LOG_DIR, "app.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("PDF2Audiobook %s starting", APP_VERSION)

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        try:
            QMessageBox.critical(None, "PDF2Audiobook", f"{tr('unexpected_error')}\n{exc_value}")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setApplicationName("PDF2Audiobook")
    app.setApplicationDisplayName("PDF2Audiobook")
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(resource_path(os.path.join("assets", "icon.png"))))

    settings = QSettings("PDF2Audiobook", "PDF2Audiobook")
    stored = settings.value("ui/language", "en")
    if stored in SUPPORTED_LANGUAGES:
        set_language(stored)

    # Single-instance lock: protects the SQLite library from concurrent writes.
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = QLockFile(LOCK_PATH)
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, tr("single_instance_title"), tr("single_instance_msg"))
        return 1

    apply_theme(app, str(settings.value("ui/theme", "dark") or "dark"))
    win = MainWindow()
    win.show()

    if not list_voice_models(MODELS_DIR):
        QTimer.singleShot(800, win._ensure_voices)  # noqa: SLF001
    QTimer.singleShot(3000, lambda: win._check_updates(silent=True))  # noqa: SLF001

    rc = app.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
