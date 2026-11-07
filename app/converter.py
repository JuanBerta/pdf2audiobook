"""PDF -> audiobook pipeline: planning, Piper synthesis, alignment, export."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TimeoutError

import numpy as np
import soundfile as sf

from .i18n import tr
from .textmodel import (
    PdfTextModel,
    fitz,
    is_speakable,
    ocr_available,
    ocr_language,
    split_sentences,
)

AUDIO_FORMATS = ("mp3", "wav")
TRACK_MAX_CHARS = 4000          # ~4-5 minutes of narration per track
PAUSE_SENTENCE = 0.25           # seconds of silence after a sentence
PAUSE_PARAGRAPH = 0.6
PAUSE_HEADING = 1.0
SYNTH_TIMEOUT = 300             # per sentence; far beyond any real call


class CancelledError(Exception):
    """Raised when the conversion queue is cancelled mid-job."""


# ------------------------------------------------------------------ TTS
class PiperTTS:
    """Thin wrapper around piper-tts returning int16 sample arrays.

    Works with piper-tts 1.2 (``synthesize_stream_raw``) and 1.3+
    (``synthesize`` yielding AudioChunk objects). Every call runs on a
    dedicated worker thread guarded by a watchdog, so a wedged native call
    turns into a clear error instead of a frozen conversion.
    """

    def __init__(self, model_path, config_path=None):
        self.model_path = model_path
        self.config_path = config_path
        self.voice = None
        self.sample_rate = 22050
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")
        self._load_lock = threading.Lock()

    @property
    def ready(self):
        return self.voice is not None

    def load(self):
        with self._load_lock:
            if self.voice is not None:
                return
            try:
                from piper import PiperVoice
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(tr("piper_not_installed")) from exc
            if not os.path.exists(self.model_path):
                raise RuntimeError(tr("model_not_found", path=self.model_path))
            try:
                voice = PiperVoice.load(self.model_path, config_path=self.config_path)
            except TypeError:  # very old piper-tts API
                voice = PiperVoice(self.model_path, config_path=self.config_path)
            cfg = getattr(voice, "config", None)
            self.sample_rate = int(getattr(cfg, "sample_rate", 22050) or 22050)
            self.voice = voice

    def _synth(self, text, length_scale):
        voice = self.voice
        parts = []
        if hasattr(voice, "synthesize_stream_raw"):  # piper-tts 1.2.x
            for raw in voice.synthesize_stream_raw(
                text, length_scale=length_scale, sentence_silence=0.0
            ):
                parts.append(np.frombuffer(raw, dtype=np.int16))
        else:
            try:
                from piper import SynthesisConfig
            except ImportError:
                from piper.config import SynthesisConfig
            cfg = SynthesisConfig(length_scale=length_scale)
            for chunk in voice.synthesize(text, syn_config=cfg):
                arr = getattr(chunk, "audio_int16_array", None)
                if arr is None:
                    arr = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
                parts.append(np.asarray(arr, dtype=np.int16).reshape(-1))
        if not parts:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(parts)

    def synthesize(self, text, length_scale=1.0, cancel_event=None):
        """Synthesize ``text`` and return mono int16 samples."""
        self.load()
        fut = self._executor.submit(self._synth, text, length_scale)
        waited = 0.0
        while True:
            try:
                return fut.result(timeout=0.25)
            except _TimeoutError:
                waited += 0.25
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError() from None
                if waited >= SYNTH_TIMEOUT:
                    # Abandon the wedged thread and start clean next time.
                    self._executor.shutdown(wait=False)
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")
                    self.voice = None
                    raise RuntimeError(tr("synth_timeout")) from None

    def close(self):
        self._executor.shutdown(wait=False)


# ------------------------------------------------------------ planning
class Track:
    """One playable audio file: a list of (sentence, pause_seconds)."""

    def __init__(self, section_idx, section_title):
        self.section_idx = section_idx
        self.section_title = section_title
        self.title = section_title
        self.items = []

    @property
    def chars(self):
        return sum(len(s) for s, _ in self.items)


def plan_tracks(model, max_chars=TRACK_MAX_CHARS, part_label="Part {n}"):
    """Group the model's sections/paragraphs into tracks of <= max_chars."""
    tracks = []
    single_untitled = len(model.sections) == 1
    for s_idx, section in enumerate(model.sections):
        sec_tracks = []
        cur = Track(s_idx, section.title)
        for para in section.paragraphs:
            sents = [s for s in split_sentences(para.text) if is_speakable(s)]
            for j, sent in enumerate(sents):
                last = j == len(sents) - 1
                pause = (
                    PAUSE_HEADING if para.heading
                    else PAUSE_PARAGRAPH if last
                    else PAUSE_SENTENCE
                )
                if cur.items and cur.chars + len(sent) > max_chars:
                    sec_tracks.append(cur)
                    cur = Track(s_idx, section.title)
                cur.items.append((sent, pause))
        if cur.items:
            sec_tracks.append(cur)
        n = len(sec_tracks)
        for k, t in enumerate(sec_tracks, start=1):
            if single_untitled:
                t.title = part_label.format(n=k)
            elif n > 1:
                t.title = f"{section.title} ({k}/{n})"
        tracks.extend(sec_tracks)
    return tracks


def plan_signature(tracks, voice_model, length_scale, audio_format):
    h = hashlib.sha1()
    h.update(json.dumps([voice_model, round(length_scale, 4), audio_format]).encode())
    for t in tracks:
        h.update(t.title.encode("utf-8", "replace"))
        for s, _ in t.items:
            h.update(b"\x00" + s.encode("utf-8", "replace"))
    return h.hexdigest()


# ------------------------------------------------------------ helpers
def sanitize(name):
    cleaned = re.sub(r"[^\w\- ]+", "_", name).strip()
    return cleaned[:60] or "book"


def audio_duration(path):
    """Duration in seconds of an audio file (0.0 when unreadable)."""
    try:
        return float(sf.info(path).duration)
    except Exception:  # noqa: BLE001
        return 0.0


def write_audio(path, samples, sample_rate, audio_format):
    """Atomically write mono int16 samples as MP3 or WAV."""
    tmp = f"{path}.part"
    if audio_format == "mp3":
        # VBR ~40 kbps: transparent enough for mono 22 kHz speech, ~8x
        # smaller than WAV.
        try:
            sf.write(tmp, samples, sample_rate, format="MP3", subtype="MPEG_LAYER_III",
                     bitrate_mode="VARIABLE", compression_level=0.8)
        except TypeError:  # soundfile < 0.12 has no bitrate options
            sf.write(tmp, samples, sample_rate, format="MP3", subtype="MPEG_LAYER_III")
    else:
        sf.write(tmp, samples, sample_rate, format="WAV", subtype="PCM_16")
    os.replace(tmp, path)


def first_page_cover(pdf_path, out_dir, width=480):
    """Render the first page as a PNG cover (~480 px wide), or None."""
    try:
        doc = fitz.open(pdf_path)
        try:
            if doc.page_count == 0:
                return None
            page = doc[0]
            zoom = width / max(1.0, page.rect.width)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        finally:
            doc.close()
        cover_path = os.path.join(out_dir, "cover.png")
        pix.save(cover_path)
        return cover_path
    except Exception:  # noqa: BLE001 - cover is a best-effort nicety
        return None


def voice_lang(voice_model):
    return (voice_model or "en").split("-", 1)[0]


def build_model(pdf_path, title, options, progress_cb=None, cancel_check=None):
    """Extract a PdfTextModel honoring the conversion options."""
    return PdfTextModel(
        pdf_path,
        remove_furniture=options.get("remove_furniture", True),
        detect_sections=options.get("detect_chapters", True),
        ocr="auto" if options.get("ocr", True) else False,
        ocr_lang=options.get("ocr_lang", "eng"),
        default_title=title,
        front_title=tr("front_matter"),
        progress_cb=progress_cb,
        cancel_check=cancel_check,
    )


# ------------------------------------------------------------ convert
def convert_pdf(
    pdf_path,
    out_dir,
    db,
    tts,
    title=None,
    author="",
    voice_model="",
    length_scale=1.0,
    audio_format="mp3",
    options=None,
    progress_cb=None,
    cancel_event=None,
    pause_event=None,
    book_id=None,
    on_book=None,
):
    """Convert one PDF into an audiobook registered in ``db``.

    Each sentence is synthesized on its own, so its exact start/end time in
    the audio is known (precise read-along). Conversions are resumable:
    finished tracks are kept and an interrupted job continues where it
    stopped as long as the text, voice, speed and format are unchanged.

    ``book_id`` forces resuming/rebuilding that specific book. Returns the
    book id.
    """
    options = dict(options or {})
    if title is None:
        title = os.path.splitext(os.path.basename(pdf_path))[0]
    if audio_format not in AUDIO_FORMATS:
        audio_format = "mp3"
    options.setdefault("ocr_lang", ocr_language(voice_lang(voice_model)))

    def emit(percent, message):
        if progress_cb:
            progress_cb(int(percent), message)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()

    def wait_if_paused():
        if pause_event is None:
            return
        while pause_event.is_set():
            check_cancel()
            pause_event.wait(timeout=0.2)

    if not os.path.exists(pdf_path):
        raise RuntimeError(tr("pdf_not_found", path=pdf_path))

    def page_progress(pno, total):
        emit(1 + 7 * pno / max(1, total), tr("reading_page", current=pno + 1, total=total))

    emit(0, tr("extracting"))
    model = build_model(pdf_path, title, options, page_progress, check_cancel)
    tracks = plan_tracks(model, part_label=tr("part_n"))
    if not tracks:
        key = "no_text_error" if ocr_available() else "no_text_error_ocr"
        raise RuntimeError(tr(key))
    signature = plan_signature(tracks, voice_model, length_scale, audio_format)
    book_options = {
        k: options.get(k) for k in ("remove_furniture", "detect_chapters", "ocr")
    }

    # --------------------------------------------------- book record
    resume_from = 0
    if book_id:
        book = db.get_book(book_id)
    else:
        book = db.find_incomplete_book(pdf_path, signature)
    if book is not None:
        book_id = book["id"]
        if book.get("signature") == signature:
            resume_from = db.valid_chapter_prefix(book_id)
        for stale in db.get_chapters(book_id)[resume_from:]:
            try:
                os.remove(stale["audio_path"])
            except OSError:
                pass
        db.truncate_book(book_id, resume_from)
        db.update_book(
            book_id,
            title=title,
            author=author,
            voice_model=voice_model,
            rate=length_scale,
            audio_format=audio_format,
            signature=signature,
            options=json.dumps(book_options),
            total_chapters=0,
            total_duration=0.0,
        )
    else:
        book_id = db.add_book(
            title,
            author=author,
            source_pdf=pdf_path,
            voice_model=voice_model,
            rate=length_scale,
            audio_format=audio_format,
            signature=signature,
            options=json.dumps(book_options),
        )
    book = db.get_book(book_id)
    book_dir = book.get("book_dir") or os.path.join(out_dir, f"{book_id:04d}_{sanitize(title)}")
    os.makedirs(book_dir, exist_ok=True)
    db.update_book(book_id, book_dir=book_dir)
    if on_book:
        on_book(book_id)

    cover = first_page_cover(pdf_path, book_dir)
    if cover:
        db.update_book(book_id, cover_path=cover)

    emit(8, tr("loading_model"))
    check_cancel()
    tts.load()
    sr = tts.sample_rate

    total_chars = sum(t.chars for t in tracks) or 1
    done_chars = sum(t.chars for t in tracks[:resume_from])
    offset = sum(c["duration"] for c in db.get_chapters(book_id)[:resume_from])
    total_duration = offset
    cursor = 0
    for t in tracks[:resume_from]:  # move the locate cursor past finished tracks
        for sent, _ in t.items:
            _, cursor = model.geometry(sent, cursor)

    ext = "mp3" if audio_format == "mp3" else "wav"
    n_tracks = len(tracks)
    for t_idx in range(resume_from, n_tracks):
        track = tracks[t_idx]
        pieces = []
        aligned = []
        pos = 0  # samples into this track
        for sent, pause in track.items:
            wait_if_paused()
            check_cancel()
            emit(
                10 + 90 * done_chars / total_chars,
                tr("synth_progress", current=t_idx + 1, total=n_tracks, title=track.title),
            )
            audio = tts.synthesize(sent, length_scale=length_scale, cancel_event=cancel_event)
            start = offset + pos / sr
            end = start + len(audio) / sr
            pieces.append(audio)
            pos += len(audio)
            gap = int(pause * sr)
            if gap:
                pieces.append(np.zeros(gap, dtype=np.int16))
                pos += gap
            geo, cursor = model.geometry(sent, cursor)
            page_no = geo["rects"][0][0] + 1 if geo and geo["rects"] else 0
            aligned.append(
                {"page_no": page_no, "bbox": geo, "text": sent,
                 "audio_start": start, "audio_end": end}
            )
            done_chars += len(sent)
        samples = np.concatenate(pieces) if pieces else np.zeros(sr, dtype=np.int16)
        out_path = os.path.join(book_dir, f"ch_{t_idx + 1:04d}.{ext}")
        write_audio(out_path, samples, sr, audio_format)
        dur = len(samples) / sr
        db.add_chapter_with_alignments(
            book_id, t_idx, track.title, out_path, dur,
            track.section_idx, track.section_title, aligned,
        )
        offset += dur
        total_duration += dur

    db.update_book(book_id, total_chapters=n_tracks, total_duration=total_duration)
    emit(100, tr("done"))
    return book_id


# ------------------------------------------------------------- realign
def needs_realign(alignments):
    """True when a book's read-along rows carry no usable geometry."""
    if not alignments:
        return False
    for row in alignments:
        bbox = row.get("bbox")
        if isinstance(bbox, str):
            try:
                bbox = json.loads(bbox)
            except ValueError:
                bbox = None
        if isinstance(bbox, dict) and bbox.get("rects"):
            return False
    return True


def realign_book(db, book_id, progress_cb=None, cancel_event=None):
    """Rebuild highlight geometry for a book made by an older version."""
    book = db.get_book(book_id)
    pdf = book.get("source_pdf") if book else None
    if not pdf or not os.path.exists(pdf):
        raise RuntimeError(tr("pdf_not_found", path=pdf or "?"))

    def cancel_check():
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()

    def page_progress(pno, total):
        if progress_cb:
            progress_cb(int(50 * pno / max(1, total)),
                        tr("reading_page", current=pno + 1, total=total))

    model = PdfTextModel(pdf, remove_furniture=False, detect_sections=False,
                         ocr=False, progress_cb=page_progress, cancel_check=cancel_check)
    model.include_all_lines()
    rows = db.get_sentence_alignments(book_id)
    updates = []
    cursor = 0
    for i, row in enumerate(rows):
        cancel_check()
        geo, cursor = model.geometry(row.get("text", ""), cursor)
        page_no = geo["rects"][0][0] + 1 if geo and geo["rects"] else 0
        updates.append((row["id"], page_no, json.dumps(geo)))
        if progress_cb and i % 50 == 0:
            progress_cb(50 + int(50 * i / max(1, len(rows))), tr("realigning"))
    db.update_alignment_geometry(updates)
    located = sum(1 for _, p, _ in updates if p)
    return located, len(rows)


# --------------------------------------------------------------- export
def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        pass
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin", "ffmpeg.exe"),
    ):
        if os.path.exists(cand):
            return cand
    return None


def _meta_escape(text):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(text))


def build_chapter_markers(chapters):
    """Merge consecutive tracks of one section into (start, end, title)."""
    marks = []
    t = 0.0
    for ch in chapters:
        sec = ch.get("section_idx", -1)
        if sec is None:
            sec = -1
        title = (ch.get("section_title") if sec >= 0 else "") or ch.get("title") or ""
        if marks and sec >= 0 and marks[-1][3] == sec:
            marks[-1][1] = t + ch["duration"]
        else:
            marks.append([t, t + ch["duration"], title, sec])
        t += ch["duration"]
    return [(a, b, title) for a, b, title, _ in marks]


def export_m4b(db, book_id, out_path, progress_cb=None, cancel_event=None, bitrate="64k"):
    """Export a book as a single M4B with chapter markers and cover art."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(tr("ffmpeg_missing"))
    book = db.get_book(book_id)
    chapters = db.get_chapters(book_id)
    if not chapters:
        raise RuntimeError(tr("export_no_audio"))
    missing = [c["audio_path"] for c in chapters if not os.path.exists(c["audio_path"])]
    if missing:
        raise RuntimeError(tr("export_missing_files", path=missing[0]))
    total = sum(c["duration"] for c in chapters) or 1.0

    work = tempfile.mkdtemp(prefix="p2a_export_")
    tmp_out = out_path + ".part.m4b"
    try:
        list_path = os.path.join(work, "list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for c in chapters:
                p = os.path.abspath(c["audio_path"]).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{p}'\n")
        meta_path = os.path.join(work, "meta.txt")
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            f.write(f"title={_meta_escape(book['title'])}\n")
            f.write(f"album={_meta_escape(book['title'])}\n")
            if book.get("author"):
                f.write(f"artist={_meta_escape(book['author'])}\n")
                f.write(f"album_artist={_meta_escape(book['author'])}\n")
            f.write("genre=Audiobook\n")
            for start, end, title in build_chapter_markers(chapters):
                f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                f.write(f"START={int(start * 1000)}\nEND={int(end * 1000)}\n")
                f.write(f"title={_meta_escape(title)}\n")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", list_path, "-i", meta_path]
        cover = book.get("cover_path")
        has_cover = bool(cover) and os.path.exists(cover)
        if has_cover:
            cmd += ["-i", cover]
        cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if has_cover:
            cmd += ["-map", "2:v", "-c:v", "mjpeg", "-vf", "scale='min(600,iw)':-2",
                    "-disposition:v:0", "attached_pic"]
        cmd += ["-c:a", "aac", "-b:a", bitrate, "-ac", "1",
                "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
                "-f", "mp4", tmp_out]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=flags,
        )
        err_lines = []
        t = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
        t.start()
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise CancelledError()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if progress_cb:
                    progress_cb(min(99, int(100 * us / 1e6 / total)), tr("exporting"))
        proc.wait()
        t.join(timeout=2)
        if proc.returncode != 0:
            raise RuntimeError(tr("export_failed", error="".join(err_lines)[-800:]))
        os.replace(tmp_out, out_path)
        if progress_cb:
            progress_cb(100, tr("done"))
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass
