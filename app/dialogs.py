"""Dialogs: conversion options, voice manager, bookmarks, book details."""
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .i18n import tr
from .textmodel import ocr_available
from .voices import delete_voice, download_voice, fetch_catalog, list_voice_models
from .worker import TaskThread

SYNTH_SPEEDS = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5]


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def ask(parent, title, text, icon=QMessageBox.Icon.Question):
    """Yes/No question with translated buttons. Returns True for yes."""
    box = QMessageBox(icon, title, text, QMessageBox.StandardButton.NoButton, parent)
    yes = box.addButton(tr("yes"), QMessageBox.ButtonRole.YesRole)
    no = box.addButton(tr("no"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


def _settings_bool(settings, key, default):
    val = settings.value(key, default)
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes")
    return bool(val)


# ------------------------------------------------------------- convert
class ConvertDialog(QDialog):
    """All conversion options in one place (replaces four stacked pop-ups)."""

    def __init__(self, parent, pdf_paths, models_dir, settings,
                 preview_cb=None, manage_voices_cb=None, can_preview=True):
        super().__init__(parent)
        self.setWindowTitle(tr("convert_dlg_title"))
        self.resize(640, 520)
        self._models_dir = models_dir
        self._settings = settings
        self._preview_cb = preview_cb
        self._manage_cb = manage_voices_cb

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("files_label")))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([tr("col_file"), tr("col_title")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        btn_add = QPushButton(tr("add_files"))
        btn_add.clicked.connect(self._add_files)
        btn_rm = QPushButton(tr("remove_selected"))
        btn_rm.clicked.connect(self._remove_selected)
        row.addWidget(btn_add)
        row.addWidget(btn_rm)
        row.addStretch(1)
        layout.addLayout(row)

        form = QFormLayout()
        self.author = QLineEdit()
        self.author.setPlaceholderText(tr("optional"))
        form.addRow(tr("author_label"), self.author)

        voice_row = QHBoxLayout()
        self.voice = QComboBox()
        self.voice.setMinimumWidth(260)
        self.btn_preview = QPushButton(tr("preview_voice"))
        self.btn_preview.clicked.connect(self._preview)
        self.btn_preview.setEnabled(can_preview and preview_cb is not None)
        if not can_preview:
            self.btn_preview.setToolTip(tr("preview_busy"))
        btn_voices = QPushButton(tr("manage_voices"))
        btn_voices.clicked.connect(self._manage_voices)
        voice_row.addWidget(self.voice, 1)
        voice_row.addWidget(self.btn_preview)
        voice_row.addWidget(btn_voices)
        form.addRow(tr("voice_model_label"), voice_row)

        self.speed = QComboBox()
        for s in SYNTH_SPEEDS:
            self.speed.addItem(f"{s:g}x", s)
        last_speed = float(settings.value("convert/speed", 1.0) or 1.0)
        self.speed.setCurrentIndex(
            min(range(len(SYNTH_SPEEDS)), key=lambda i: abs(SYNTH_SPEEDS[i] - last_speed))
        )
        self.speed.setToolTip(tr("speed_tip"))
        form.addRow(tr("speed_dlg"), self.speed)

        self.fmt = QComboBox()
        self.fmt.addItem(tr("format_mp3"), "mp3")
        self.fmt.addItem(tr("format_wav"), "wav")
        self.fmt.setCurrentIndex(0 if settings.value("convert/format", "mp3") == "mp3" else 1)
        form.addRow(tr("format_label"), self.fmt)
        layout.addLayout(form)

        self.chk_chapters = QCheckBox(tr("opt_chapters"))
        self.chk_chapters.setChecked(_settings_bool(settings, "convert/chapters", True))
        self.chk_furniture = QCheckBox(tr("opt_furniture"))
        self.chk_furniture.setChecked(_settings_bool(settings, "convert/furniture", True))
        self.chk_ocr = QCheckBox(tr("opt_ocr"))
        has_ocr = ocr_available()
        self.chk_ocr.setEnabled(has_ocr)
        self.chk_ocr.setChecked(has_ocr and _settings_bool(settings, "convert/ocr", True))
        if not has_ocr:
            self.chk_ocr.setToolTip(tr("ocr_missing_tip"))
        for chk in (self.chk_chapters, self.chk_furniture, self.chk_ocr):
            layout.addWidget(chk)

        buttons = QDialogButtonBox()
        self.btn_ok = buttons.addButton(tr("start_conversion"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_ok.setObjectName("primaryButton")
        buttons.addButton(tr("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload_voices(settings.value("convert/voice", ""))
        for p in pdf_paths:
            self._append_file(p)

    # ------------------------------------------------------------ files
    def _append_file(self, path):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == path:
                return
        r = self.table.rowCount()
        self.table.insertRow(r)
        f = QTableWidgetItem(os.path.basename(path))
        f.setData(Qt.ItemDataRole.UserRole, path)
        f.setToolTip(path)
        f.setFlags(f.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(r, 0, f)
        self.table.setItem(r, 1, QTableWidgetItem(os.path.splitext(os.path.basename(path))[0]))

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("choose_pdfs"), "", f"{tr('pdf_files')} (*.pdf)")
        for p in paths:
            self._append_file(p)

    def _remove_selected(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    # ----------------------------------------------------------- voices
    def _reload_voices(self, select=""):
        self.voice.clear()
        for model, config, name in list_voice_models(self._models_dir):
            key = os.path.splitext(os.path.basename(model))[0]
            self.voice.addItem(name, (model, config, key))
            if key == select:
                self.voice.setCurrentIndex(self.voice.count() - 1)
        self.btn_ok.setEnabled(self.voice.count() > 0)

    def _manage_voices(self):
        current = self.voice.currentData()
        if self._manage_cb:
            self._manage_cb()
        self._reload_voices(current[2] if current else "")
        self.btn_ok.setEnabled(self.voice.count() > 0)

    def _preview(self):
        data = self.voice.currentData()
        if data and self._preview_cb:
            self._preview_cb(data[0], data[1], data[2], self.speed.currentData())

    # ----------------------------------------------------------- result
    def _accept(self):
        if self.table.rowCount() == 0:
            QMessageBox.information(self, tr("convert_dlg_title"), tr("no_files"))
            return
        if self.voice.currentData() is None:
            QMessageBox.information(self, tr("convert_dlg_title"), tr("model_missing_short"))
            return
        s = self._settings
        s.setValue("convert/voice", self.voice.currentData()[2])
        s.setValue("convert/speed", self.speed.currentData())
        s.setValue("convert/format", self.fmt.currentData())
        s.setValue("convert/chapters", self.chk_chapters.isChecked())
        s.setValue("convert/furniture", self.chk_furniture.isChecked())
        s.setValue("convert/ocr", self.chk_ocr.isChecked())
        self.accept()

    def jobs(self):
        model, config, key = self.voice.currentData()
        options = {
            "detect_chapters": self.chk_chapters.isChecked(),
            "remove_furniture": self.chk_furniture.isChecked(),
            "ocr": self.chk_ocr.isChecked(),
        }
        out = []
        for r in range(self.table.rowCount()):
            path = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            title = (self.table.item(r, 1).text() or "").strip() or os.path.splitext(os.path.basename(path))[0]
            out.append({
                "pdf_path": path,
                "title": title,
                "author": self.author.text().strip(),
                "model_path": model,
                "config_path": config,
                "voice_model": key,
                # Piper's length_scale is a *duration* factor: 1.2x faster
                # speech means 1/1.2 of the duration.
                "length_scale": round(1.0 / float(self.speed.currentData()), 4),
                "audio_format": self.fmt.currentData(),
                "options": options,
            })
        return out


# ------------------------------------------------------- voice manager
def _fmt_size(n):
    return f"{n / 1e6:.0f} MB" if n else ""


class VoiceManagerDialog(QDialog):
    """Browse the Piper voice catalog, download and delete voices."""

    def __init__(self, parent, models_dir, cache_path):
        super().__init__(parent)
        self.setWindowTitle(tr("voices_title"))
        self.resize(760, 520)
        self._models_dir = models_dir
        self._cache = cache_path
        self._catalog = []
        self._task = None

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("voices_filter"))
        self.filter.textChanged.connect(self._populate)
        self.chk_installed = QCheckBox(tr("voices_installed_only"))
        self.chk_installed.toggled.connect(self._populate)
        top.addWidget(self.filter, 1)
        top.addWidget(self.chk_installed)
        layout.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [tr("col_language"), tr("col_voice"), tr("col_quality"), tr("col_size"), tr("col_status")]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self._download())
        layout.addWidget(self.table, 1)

        self.status = QLabel(tr("voices_loading"))
        self.status.setObjectName("mutedLabel")
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)

        row = QHBoxLayout()
        self.btn_download = QPushButton(tr("download"))
        self.btn_download.setObjectName("primaryButton")
        self.btn_download.clicked.connect(self._download)
        self.btn_delete = QPushButton(tr("delete"))
        self.btn_delete.clicked.connect(self._delete)
        btn_folder = QPushButton(tr("open_models_dir"))
        btn_folder.clicked.connect(self._open_folder)
        self.btn_close = QPushButton(tr("close"))
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_download)
        row.addWidget(self.btn_delete)
        row.addWidget(btn_folder)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)

        self._populate()
        self._task = TaskThread(lambda _p, _c: fetch_catalog(self._cache), self)
        self._task.succeeded.connect(self._on_catalog)
        self._task.failed.connect(self._on_catalog_failed)
        self._task.start()

    def _installed(self):
        return {os.path.splitext(os.path.basename(m))[0]: m for m, _, _ in list_voice_models(self._models_dir)}

    def _on_catalog(self, voices):
        self._catalog = voices
        self.status.setText(tr("voices_count", count=len(voices)))
        self._populate()

    def _on_catalog_failed(self, msg):
        self.status.setText(tr("voices_offline", error=msg))
        self._populate()

    def _populate(self):
        installed = self._installed()
        q = self.filter.text().strip().lower()
        rows = list(self._catalog)
        known = {v["key"] for v in rows}
        for key in installed:
            if key not in known:
                rows.append({"key": key, "language": key.split("-")[0], "native": "",
                             "name": key, "quality": "", "size": 0, "files": []})
        self.table.setRowCount(0)
        for v in rows:
            is_inst = v["key"] in installed
            if self.chk_installed.isChecked() and not is_inst:
                continue
            hay = f"{v['key']} {v['language']} {v['native']} {v['name']}".lower()
            if q and q not in hay:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            lang = v["language"] + (f" · {v['native']}" if v.get("native") else "")
            vals = [lang, v["name"], v["quality"], _fmt_size(v["size"]),
                    tr("installed") if is_inst else ""]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                it.setData(Qt.ItemDataRole.UserRole, v)
                self.table.setItem(r, c, it)
        self._update_buttons()

    def _selected(self):
        items = self.table.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _update_buttons(self):
        v = self._selected()
        busy = self._task is not None and self._task.isRunning()
        installed = v is not None and v["key"] in self._installed()
        self.btn_download.setEnabled(bool(v) and not installed and bool(v.get("files")) and not busy)
        self.btn_delete.setEnabled(installed and not busy)

    def _download(self):
        v = self._selected()
        if not v or not v.get("files") or v["key"] in self._installed():
            return
        if self._task is not None and self._task.isRunning():
            return
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status.setText(tr("downloading", name=v["key"]))
        self._task = TaskThread(lambda p, c: download_voice(v, self._models_dir, p, c), self)
        self._task.progress.connect(lambda pct, _m: self.progress.setValue(pct))
        self._task.succeeded.connect(lambda _r: self._download_finished(tr("download_done", name=v["key"])))
        self._task.failed.connect(lambda m: self._download_finished(tr("download_failed", error=m)))
        self._task.cancelled.connect(lambda: self._download_finished(""))
        self._task.start()
        self._update_buttons()

    def _download_finished(self, msg):
        self.progress.setVisible(False)
        self.status.setText(msg)
        self._populate()

    def _delete(self):
        v = self._selected()
        installed = self._installed()
        if not v or v["key"] not in installed:
            return
        if not ask(self, tr("voices_title"), tr("delete_voice_msg", name=v["key"])):
            return
        try:
            delete_voice(installed[v["key"]])
        except OSError as exc:
            QMessageBox.warning(self, tr("voices_title"), str(exc))
        self._populate()

    def _open_folder(self):
        os.makedirs(self._models_dir, exist_ok=True)
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(self._models_dir))

    def reject(self):
        if self._task is not None and self._task.isRunning():
            self._task.cancel()
            self._task.wait(3000)
        super().reject()


# ------------------------------------------------------------ bookmarks
class BookmarkDialog(QDialog):
    """List bookmarks; double-click / Go to jump, Delete to remove."""

    def __init__(self, marks, db, chapters, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("bookmarks_menu"))
        self.resize(460, 340)
        self.chosen = None
        self._marks = list(marks)
        self._db = db
        titles = {c["idx"]: c["title"] for c in chapters}

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for i, mark in enumerate(self._marks):
            label = titles.get(mark["chapter_idx"], f"#{mark['chapter_idx'] + 1}")
            item = QListWidgetItem(f"{label}  @ {fmt_duration(mark['position'])}    {mark['created_at']}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.list_widget.addItem(item)
        if self._marks:
            self.list_widget.setCurrentRow(0)
        self.list_widget.itemDoubleClicked.connect(lambda _i: self._jump())
        layout.addWidget(self.list_widget)

        row = QHBoxLayout()
        btn_go = QPushButton(tr("go_to_bookmark"))
        btn_go.setObjectName("primaryButton")
        btn_go.clicked.connect(self._jump)
        btn_del = QPushButton(tr("delete_bookmark"))
        btn_del.clicked.connect(self._delete)
        btn_close = QPushButton(tr("close"))
        btn_close.clicked.connect(self.reject)
        row.addWidget(btn_go)
        row.addWidget(btn_del)
        row.addStretch(1)
        row.addWidget(btn_close)
        layout.addLayout(row)

    def keyPressEvent(self, event):  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Delete:
            self._delete()
        else:
            super().keyPressEvent(event)

    def _jump(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        mark = self._marks[item.data(Qt.ItemDataRole.UserRole)]
        self.chosen = (mark["chapter_idx"], mark["position"])
        self.accept()

    def _delete(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        mark = self._marks[item.data(Qt.ItemDataRole.UserRole)]
        self._db.delete_bookmark(mark["id"])
        self.list_widget.takeItem(self.list_widget.row(item))


# ---------------------------------------------------------- book details
class EditBookDialog(QDialog):
    def __init__(self, book, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("edit_details"))
        self.resize(420, 140)
        form = QFormLayout(self)
        self.title = QLineEdit(book.get("title", ""))
        self.author = QLineEdit(book.get("author", ""))
        form.addRow(tr("title_label"), self.title)
        form.addRow(tr("author_label"), self.author)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.title.text().strip(), self.author.text().strip()
