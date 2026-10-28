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

    tts.load()
    total = len(chunks)
    total_duration = 0.0
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
        total_duration += dur
        db.add_chapter(book_id, i, f"Chunk {i + 1}", out_path, dur)

    db.finalize_book(book_id, total, total_duration)
    if progress_cb:
        progress_cb(100, tr("done"))
    return book_id
