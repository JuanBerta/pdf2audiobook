"""PDF text extraction, chunking and Piper TTS synthesis."""
from .i18n import tr
import os
import re
import wave

try:  # PyMuPDF >= 1.24 exposes the modern namespace
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older versions
    import fitz  # type: ignore[no-redef]


class CancelledError(Exception):
    """Raised when the conversion queue is cancelled mid-job."""


class PiperTTS:
    """Thin wrapper around piper-tts, tolerant of API differences."""

    def __init__(self, model_path, config_path=None):
        self.model_path = model_path
        self.config_path = config_path
        self.voice = None

    @property
    def ready(self):
        return self.voice is not None

    def load(self):
        if self.voice is not None:
            return
        try:
            from piper import PiperVoice
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(tr("piper_not_installed")) from exc
        if not os.path.exists(self.model_path):
            raise RuntimeError(tr("model_not_found", path=self.model_path))
        try:
            self.voice = PiperVoice.load(
                self.model_path, config_path=self.config_path
            )
        except TypeError:  # older piper-tts API
            self.voice = PiperVoice(self.model_path, config_path=self.config_path)

    def synthesize_to_file(self, text, out_path, length_scale=1.0):
        self.load()
        kwargs = {"length_scale": length_scale}
        if hasattr(self.voice, "synthesize_wav"):
            # piper-tts >= 1.3 expects a wave.Wave_write object
            import wave

            with wave.open(out_path, "wb") as w:
                self.voice.synthesize_wav(text, w, **kwargs)
        else:  # older piper-tts API writes bytes to a binary file
            with open(out_path, "wb") as f:
                self.voice.synthesize(text, f, **kwargs)
        return out_path


def extract_pages(pdf_path):
    """Return a list of raw text strings, one per PDF page."""
    doc = fitz.open(pdf_path)
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


def first_page_cover(pdf_path, out_dir):
    """Render the first page as a small PNG cover, or None on failure."""
    try:
        doc = fitz.open(pdf_path)
        try:
            if doc.page_count == 0:
                return None
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
        finally:
            doc.close()
        cover_path = os.path.join(out_dir, "cover.png")
        pix.save(cover_path)
        return cover_path
    except Exception:  # noqa: BLE001 - cover is a best-effort nicety
        return None


def clean_text(text):
    """Basic cleanup: drop standalone page numbers, normalize spacing."""
    lines = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            lines.append("")
        elif re.fullmatch(r"\d{1,4}", ln):
            continue  # page number
        elif re.fullmatch(r"[-–—_=*#~]{3,}", ln):
            continue  # divider
        else:
            lines.append(ln)
    return "\n".join(lines)


def chunk_text(pages, max_chars=2500):
    """Split cleaned text into chunks, preferring paragraph boundaries.

    PDF text extraction usually yields soft line breaks instead of blank
    lines, so consecutive non-empty lines are re-joined into paragraphs
    before chunking. Returns a list of strings suitable for TTS.
    """
    paragraphs: list[str] = []
    for page in pages:
        lines = clean_text(page).splitlines()
        buffer: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                if buffer:
                    paragraphs.append(" ".join(buffer))
                    buffer = []
            else:
                buffer.append(line)
        if buffer:
            paragraphs.append(" ".join(buffer))

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 <= max_chars:
            current = f"{current}\n\n{para}"
        else:
            if current:
                chunks.append(current)
            while len(para) > max_chars:  # hard-split oversized paragraph
                chunks.append(para[:max_chars])
                para = para[max_chars:]
            current = para
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------
# Read-along: sentence splitting + PDF text/bbox locating.
# ---------------------------------------------------------------------
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？…])\s+")


def split_sentences(text):
    """Split cleaned paragraph text into sentences, keeping the terminator.

    A tiny guard merges back very short trailing pieces that are usually
    abbreviations (e.g. "Mr.", "e.g.") instead of sentence boundaries.
    """
    parts = _SENT_SPLIT_RE.split(text.strip())
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if out and len(part) <= 3 and not part[0].isupper():
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def _norm_text(text):
    """Remove all whitespace so layout artifacts do not break matching."""
    return re.sub(r"\s+", "", text)


def _build_page_spans(page):
    """Return (haystack, spans) for locating sentences on one PDF page.

    ``haystack`` concatenates every text span with whitespace stripped;
    ``spans`` maps each span's character range back to its bounding box as
    ``(start, end, (x0, y0, x1, y1))``.
    """
    hay_parts = []
    spans = []
    pos = 0
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text:
                    continue
                hay_parts.append(text)
                spans.append((pos, pos + len(text), tuple(span["bbox"])))
                pos += len(text)
    return "".join(hay_parts), spans


def _locate_sentence(spans_cache, sentence, start_page=0):
    """Locate a sentence inside the cached per-page span maps.

    Returns ``(page_index, (x0, y0, x1, y1))`` or ``(None, None)`` when the
    sentence cannot be matched (e.g. layout differences from cleaning).
    """
    needle = _norm_text(sentence)
    if not needle:
        return None, None
    for page_idx in range(start_page, len(spans_cache)):
        haystack, spans = spans_cache[page_idx]
        idx = haystack.find(needle)
        if idx < 0:
            continue
        end = idx + len(needle)
        covering = [sp for sp in spans if sp[0] < end and sp[1] > idx]
        if not covering:
            continue
        x0 = min(sp[2][0] for sp in covering)
        y0 = min(sp[2][1] for sp in covering)
        x1 = max(sp[2][2] for sp in covering)
        y1 = max(sp[2][3] for sp in covering)
        return page_idx, (x0, y0, x1, y1)
    return None, None


def audio_duration(path):
    """Duration in seconds of a WAV file."""
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def sanitize(name):
    cleaned = re.sub(r"[^\w\- ]+", "_", name).strip()
    return cleaned or "book"


def convert_pdf(
    pdf_path,
    out_dir,
    db,
    tts,
    title=None,
    author="",
    voice_model="",
    length_scale=1.0,
    progress_cb=None,
    cancel_event=None,
    pause_event=None,
):
    """Extract text, synthesize each chunk and register the result in the library.

    Supports variable speech speed (length_scale), cooperative cancel/pause
    events, and resuming an unfinished book that was left behind by an
    earlier cancelled or crashed conversion of the same PDF.

    Returns the new (or resumed) book id.
    """
    if title is None:
        title = os.path.splitext(os.path.basename(pdf_path))[0]

    pages = extract_pages(pdf_path)
    chunks = chunk_text(pages)
    if not chunks:
        raise RuntimeError(tr("no_text_error"))

    unfinished = db.find_incomplete_book(pdf_path, title)
    if unfinished is not None:
        book_id = unfinished["id"]
        db.clear_chapters(book_id)
        db.clear_sentence_alignments(book_id)
    else:
        book_id = db.add_book(
            title,
            author=author,
            source_pdf=pdf_path,
            voice_model=voice_model,
            rate=length_scale,
        )
    book_dir = os.path.join(out_dir, f"{book_id:04d}_{sanitize(title)}")
    os.makedirs(book_dir, exist_ok=True)

    cover = first_page_cover(pdf_path, book_dir)
    if cover:
        db.set_cover(book_id, cover)

    # Per-page span cache used to locate each sentence (page + bbox).
    doc = fitz.open(pdf_path)
    try:
        spans_cache = [_build_page_spans(page) for page in doc]
    finally:
        doc.close()

    tts.load()
    total = len(chunks)
    total_duration = 0.0
    chapter_offset = 0.0
    last_page = 0
    for i, chunk in enumerate(chunks):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        if pause_event is not None:
            pause_event.wait()
        if progress_cb:
            progress_cb(
                int((i / total) * 100),
                tr("synth_chunk", current=i + 1, total=total),
            )
        out_path = os.path.join(book_dir, f"ch_{i + 1:04d}.wav")
        if os.path.exists(out_path):
            dur = audio_duration(out_path)
        else:
            tts.synthesize_to_file(chunk, out_path, length_scale=length_scale)
            dur = audio_duration(out_path)

        # Collect per-sentence alignment for this chunk (read-along).
        sents = split_sentences(chunk)
        aligned = []
        for sent in sents:
            page_idx, bbox = _locate_sentence(
                spans_cache, sent, start_page=last_page
            )
            if page_idx is not None:
                last_page = page_idx
            aligned.append(
                {
                    "page_no": (page_idx + 1) if page_idx is not None else 0,
                    "bbox": bbox,
                    "text": sent,
                }
            )
        # Distribute the chapter's audio by sentence text length (no
        # word-level timestamps from Piper); timestamps use the global
        # audio timeline so variable playback rate stays aligned.
        total_chars = sum(len(a["text"]) for a in aligned) or 1
        cursor = chapter_offset
        for a in aligned:
            seg = dur * len(a["text"]) / total_chars
            a["audio_start"] = cursor
            a["audio_end"] = cursor + seg
            cursor += seg
        db.add_sentence_alignments(book_id, i, aligned)
        chapter_offset += dur

        total_duration += dur
        db.add_chapter(book_id, i, f"Chunk {i + 1}", out_path, dur)

    db.finalize_book(book_id, total, total_duration)
    if progress_cb:
        progress_cb(100, tr("done"))
    return book_id
