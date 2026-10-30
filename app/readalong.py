"""Read-along panel: renders the source PDF and highlights the sentence/word
currently being read, scrolling so the active sentence stays visible.

The panel is a passive widget: the main window feeds it the global audio
position while playing (`set_playback_time`) and the user can click a
sentence (either in the sentence list or directly on the rendered page) to
jump the audio there (emitted via `seek_requested`).
"""
import json
import os
import re

import pymupdf as fitz
from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr

_WORD_RE = re.compile(r"[\w\u4e00-\u9fff']+")


class _PdfLabel(QLabel):
    """QLabel that reports clicks mapped to page coordinates."""

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def mousePressEvent(self, event):
        self._panel._on_pdf_click(event)
        super().mousePressEvent(event)


class ReadAlongPanel(QWidget):
    """Side/bottom panel showing the book's PDF with read-along sync."""

    seek_requested = pyqtSignal(float)  # global audio seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pdf_path = None
        self._doc = None
        self._zoom = 2.0
        self._chapter_offsets = []
        self._align = {}  # chapter_idx -> [sentence dicts]
        self._chapter_idx = -1
        self._sents = []
        self._active_sent_idx = -1
        self._word_rect = None  # (char_start, char_end) inside active text
        self._current_page = -1
        self._cached_pixmap = None
        self._cached_page = -1

        self._page_label = QLabel("")
        self._page_label.setStyleSheet("font-weight: bold; padding: 2px 4px;")
        self._pdf_label = _PdfLabel(self)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setWidget(self._pdf_label)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setStyleSheet("QScrollArea { border: 1px solid #888; }")

        self._sent_list = QListWidget()
        self._sent_list.setFixedWidth(260)
        self._sent_list.setStyleSheet(
            "QListWidget::item { padding: 3px 4px; }"
        )
        self._sent_list.itemClicked.connect(self._on_sent_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._page_label)
        row = QHBoxLayout()
        row.setSpacing(2)
        row.addWidget(self._scroll, 1)
        row.addWidget(self._sent_list, 0)
        layout.addLayout(row)

    # ---------------------------------------------------------- public API
    def set_book(self, pdf_path, alignments, chapter_offsets):
        """Load a book's source PDF and its alignment rows.

        ``alignments`` are raw rows from AudiobookDB.get_sentence_alignments;
        bbox strings are decoded here. ``chapter_offsets`` maps each chapter
        index to its start time on the global audio timeline.
        """
        self._pdf_path = pdf_path
        self._chapter_offsets = list(chapter_offsets or [])
        self._align = {}
        for row in alignments or []:
            item = dict(row)
            bbox = item.get("bbox")
            try:
                item["bbox"] = json.loads(bbox) if bbox else None
            except (TypeError, ValueError):
                item["bbox"] = None
            self._align.setdefault(item.get("chapter_idx", -1), []).append(item)
        if self._doc is not None:
            self._doc.close()
            self._doc = None
        if pdf_path and os.path.exists(pdf_path):
            try:
                self._doc = fitz.open(pdf_path)
            except Exception:
                self._doc = None
        self._active_sent_idx = -1
        self._word_rect = None
        self._cached_pixmap = None
        self._cached_page = -1
        self._current_page = -1
        self._chapter_idx = -1
        self._sent_list.clear()
        self._page_label.setText("")
        if self._doc is None or not self._align:
            self._page_label.setText(tr("readalong_no_data"))
            self._pdf_label.setPixmap(QPixmap())
            self._pdf_label.adjustSize()
            return
        first = min(self._align)
        self.set_chapter(first)

    def set_chapter(self, chapter_idx):
        """Switch the panel to a specific chapter's sentences."""
        if not self._align:
            return
        self._chapter_idx = chapter_idx
        self._sents = self._align.get(chapter_idx, [])
        self._active_sent_idx = -1
        self._word_rect = None
        self._cached_pixmap = None
        self._cached_page = -1
        self._rebuild_sent_list()
        self._render_current()

    def set_playback_time(self, global_time):
        """Called while audio plays; highlights the sentence/word at time."""
        if not self._doc or not self._sents or not self.isVisible():
            return
        sent_idx = self._sentence_at(global_time)
        if sent_idx != self._active_sent_idx:
            self._active_sent_idx = sent_idx
            self._sync_list_selection()
        self._highlight_word(global_time)
        self._render_current()

    def has_doc(self):
        return self._doc is not None and bool(self._align)

    def current_chapter(self):
        return self._chapter_idx

    # ------------------------------------------------------------ internals
    def _rebuild_sent_list(self):
        self._sent_list.clear()
        for i, sent in enumerate(self._sents):
            page = sent.get("page_no") or "?"
            text = (sent.get("text") or "").strip().replace("\n", " ")
            item = QListWidgetItem(f"p{page}  {text[:58]}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._sent_list.addItem(item)

    def _sentence_at(self, global_time):
        """Binary search: last sentence whose audio_start <= global_time."""
        if not self._sents:
            return -1
        if global_time < self._sents[0]["audio_start"]:
            return 0
        lo, hi = 0, len(self._sents) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._sents[mid]["audio_start"] <= global_time:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _highlight_word(self, global_time):
        if self._active_sent_idx < 0:
            self._word_rect = None
            return
        sent = self._sents[self._active_sent_idx]
        text = sent.get("text") or ""
        words = list(_WORD_RE.finditer(text))
        span = sent["audio_end"] - sent["audio_start"]
        if not words or span <= 0:
            self._word_rect = None
            return
        total_len = len(text) or 1
        rel = (global_time - sent["audio_start"]) / span
        target = max(0.0, min(1.0, rel)) * total_len
        chosen = words[0]
        for word in words:
            if word.start() <= target:
                chosen = word
            else:
                break
        self._word_rect = (chosen.start(), chosen.end())

    def _sync_list_selection(self):
        if self._active_sent_idx < 0:
            return
        if self._active_sent_idx < self._sent_list.count():
            item = self._sent_list.item(self._active_sent_idx)
            if item is not None:
                self._sent_list.setCurrentItem(item)
                self._sent_list.scrollToItem(item)

    def _render_current(self):
        if not self._doc or self._active_sent_idx < 0:
            return
        sent = self._sents[self._active_sent_idx]
        page_no = sent.get("page_no") or 0
        if page_no <= 0 or page_no > self._doc.page_count:
            return
        page_idx = page_no - 1
        if page_idx != self._cached_page or self._cached_pixmap is None:
            pix = self._doc[page_idx].get_pixmap(
                matrix=fitz.Matrix(self._zoom, self._zoom)
            )
            image = QImage.fromData(pix.tobytes("png"))
            self._cached_pixmap = QPixmap.fromImage(image)
            self._cached_page = page_idx
            self._current_page = page_idx

        pm = QPixmap(self._cached_pixmap)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bbox = sent.get("bbox")
        sent_rect = None
        if bbox:
            sent_rect = QRectF(
                bbox[0] * self._zoom,
                bbox[1] * self._zoom,
                (bbox[2] - bbox[0]) * self._zoom,
                (bbox[3] - bbox[1]) * self._zoom,
            )
            painter.fillRect(sent_rect, QColor(255, 235, 59, 70))
            if self._word_rect:
                text = sent.get("text") or ""
                total_len = len(text) or 1
                start, end = self._word_rect
                x0 = sent_rect.left() + sent_rect.width() * (start / total_len)
                x1 = sent_rect.left() + sent_rect.width() * (end / total_len)
                word_rect = QRectF(
                    x0, sent_rect.top(), max(x1 - x0, 4), sent_rect.height()
                )
                painter.fillRect(word_rect, QColor(255, 152, 0, 140))
        painter.end()
        self._pdf_label.setPixmap(pm)
        self._pdf_label.adjustSize()
        self._page_label.setText(tr("readalong_page", page=page_no))
        if sent_rect is not None:
            self._scroll.ensureVisible(
                int(sent_rect.center().x()),
                int(sent_rect.center().y()),
                60,
                120,
            )

    def _on_pdf_click(self, event):
        if not self._doc or not self._sents or self._current_page < 0:
            return
        pos = event.position()
        pix = self._pdf_label.pixmap()
        if pix is None or pix.isNull():
            return
        off_x = (self._pdf_label.width() - pix.width()) / 2
        off_y = (self._pdf_label.height() - pix.height()) / 2
        px = pos.x() - off_x
        py = pos.y() - off_y
        if not (0 <= px <= pix.width() and 0 <= py <= pix.height()):
            return
        page_x = px / self._zoom
        page_y = py / self._zoom
        page_no = self._current_page + 1
        for sent in self._sents:
            bbox = sent.get("bbox")
            if not bbox or sent.get("page_no") != page_no:
                continue
            x0, y0, x1, y1 = bbox
            if x0 <= page_x <= x1 and y0 <= page_y <= y1:
                self.seek_requested.emit(sent["audio_start"])
                return

    def _on_sent_clicked(self, item):
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is not None and 0 <= idx < len(self._sents):
            self.seek_requested.emit(self._sents[idx]["audio_start"])

    # ------------------------------------------------------------- teardown
    def closeEvent(self, event):
        if self._doc is not None:
            self._doc.close()
            self._doc = None
        super().closeEvent(event)
