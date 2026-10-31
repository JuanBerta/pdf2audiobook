"""PDF text extraction, chunking and Piper TTS synthesis."""
from .i18n import tr
import os
import re
import time
import wave
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TimeoutError

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

    def synthesize_to_file(self, text, out_path, length_scale=1.0, cancel_event=None):
        self.load()
        # A hung synthesis (e.g. model/CPU stall) used to freeze the UI at
        # 8% forever because the worker only reports after the call
        # returns. Wrap the actual synthesis in a watchdog: on timeout we
        # raise a clear error (so the UI reports a failure instead of
        # appearing stuck) and drop the voice so the next job reloads it.
        def _run():
            if hasattr(self.voice, "synthesize_wav"):
                # piper-tts >= 1.3 expects a wave.Wave_write object and
                # carries voice parameters in a SynthesisConfig; older
                # builds accepted length_scale as a plain keyword argument
                # instead.
                import wave

                try:
                    from piper.config import SynthesisConfig
                except Exception:  # pragma: no cover - very old piper
                    SynthesisConfig = None  # type: ignore[assignment]
                with wave.open(out_path, "wb") as w:
                    if SynthesisConfig is not None:
                        try:
                            self.voice.synthesize_wav(
                                text,
                                w,
                                syn_config=SynthesisConfig(
                                    length_scale=length_scale
                                ),
                            )
                            return out_path
                        except TypeError:
                            pass  # fall through to the legacy keyword API
                    self.voice.synthesize_wav(
                        text, w, length_scale=length_scale
                    )
            else:  # older piper-tts API writes bytes to a binary file
                with open(out_path, "wb") as f:
                    self.voice.synthesize(
                        text, f, length_scale=length_scale
                    )
            return out_path

        # Run in a worker thread so an unresponsive native call cannot
        # freeze the pipeline forever; 10 minutes is far beyond any real
        # chunk but still gives us a way out of a deadlock. Poll in small
        # increments so an explicit cancel is honored while a chunk is
        # still wedged inside the native synthesis call.
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            fut = executor.submit(_run)
            deadline = time.monotonic() + 600
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # The worker thread is still alive but wedged; drop the
                    # voice so a retry starts from a clean model load.
                    self.voice = None
                    raise RuntimeError(tr("synth_timeout"))
                try:
                    return fut.result(timeout=min(0.5, remaining))
                except _TimeoutError:
                    if cancel_event is not None and cancel_event.is_set():
                        self.voice = None
                        raise CancelledError() from None
                    continue
        finally:
            # Never leak the watchdog thread; the runner stops once the
            # call finishes (or is abandoned).
            executor.shutdown(wait=False)
            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                # A timed-out/aborted synthesis can leave a zero-byte WAV
                # behind. Remove it so a resume pass does not mistake it
                # for a finished chunk.
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except OSError:
                    pass


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
    # Guard against a non-positive limit: a zero/negative max_chars would
    # make the hard-split loop below spin forever on non-empty paragraphs.
    max_chars = max(1, int(max_chars))
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
    # A monotonic start_page cursor is a bad idea: sentences inside one
    # chunk can legitimately live on an earlier page than the previous
    # chunk's last sentence (page-number-only pages, boxed sidebars,
    # extraction reordering). Searching forward from start_page only would
    # silently miss them, so if the forward pass fails we fall back to a
    # full scan from the first page.
    for page_idx in list(range(start_page, len(spans_cache))) + list(
        range(0, min(start_page, len(spans_cache)))
    ):
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

    # The extraction / alignment-preparation stages used to run with no
    # progress callback at all, leaving the UI stuck on "Extrayendo texto
    # del PDF..." at 0% until the first chunk is synthesized. Emit a few
    # early steps so the bar visibly moves even for large PDFs and slow
    # model loads.
    def _emit(percent, message):
        if progress_cb:
            progress_cb(percent, message)

    _emit(1, tr("extracting"))
    pages = extract_pages(pdf_path)
    _emit(3, tr("extracting"))
    chunks = chunk_text(pages)
    if not chunks:
        raise RuntimeError(tr("no_text_error"))
    _emit(4, tr("extracting"))

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
    _emit(5, tr("extracting"))

    # Per-page span cache used to locate each sentence (page + bbox).
    doc = fitz.open(pdf_path)
    try:
        spans_cache = [_build_page_spans(page) for page in doc]
    finally:
        doc.close()
    _emit(6, tr("extracting"))

    # Model loading can take several seconds (and can fail outright when
    # piper is broken/missing inside a packaged build); report it as its
    # own step so a stall is visible instead of being hidden behind the
    # generic "extracting" message at 8%.
    _emit(7, tr("loading_model"))
    tts.load()
    _emit(8, tr("extracting"))
    total = len(chunks)
    total_duration = 0.0
    chapter_offset = 0.0
    last_page = 0
    for i, chunk in enumerate(chunks):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        if pause_event is not None:
            # Pause/resume uses a threading.Event as a latch: set() while
            # paused, clear() on resume. A bare wait() would block forever
            # while *unpaused* (Event.wait blocks until the flag is set),
            # which froze the worker at the first chunk (progress stuck at
            # 8%) and made Cancel unresponsive. Poll instead so both pause
            # and cancel are honored between chunks.
            while pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError()
                pause_event.wait(timeout=0.2)
        if progress_cb:
            # 8% accounts for extraction/alignment prep above; synthesis
            # then fills the remaining 92% monotonically up to 100%.
            progress_cb(
                int(8 + (i / total) * 92),
                tr("synth_chunk", current=i + 1, total=total),
            )
        out_path = os.path.join(book_dir, f"ch_{i + 1:04d}.wav")
        if os.path.exists(out_path):
            dur = audio_duration(out_path)
            if dur <= 0:
                # Zero-byte / truncated WAV left by an aborted synthesis:
                # treat it as missing and re-synthesize instead of letting
                # a corrupt chunk silently enter the finished book.
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                tts.synthesize_to_file(
                    chunk, out_path, length_scale=length_scale,
                    cancel_event=cancel_event,
                )
                dur = audio_duration(out_path)
        else:
            tts.synthesize_to_file(
                chunk, out_path, length_scale=length_scale,
                cancel_event=cancel_event,
            )
            dur = audio_duration(out_path)

        # Collect per-sentence alignment for this chunk (read-along).
        sents = split_sentences(chunk)
        aligned = []
        for sent in sents:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
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
