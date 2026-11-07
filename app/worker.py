"""Background threads: the conversion queue and generic one-off tasks."""
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from .converter import CancelledError, PiperTTS, convert_pdf


class QueueWorker(QThread):
    """Converts a queue of PDFs in a background thread.

    Jobs can be added while the queue is running. Signals:
        progress(percent, message)     - progress of the current job (0-100)
        job_started(index, total, title)
        book_created(book_id)          - the current job's book row exists
        job_done(book_id, title)
        job_failed(message, title)     - the queue continues with the next job
        all_done(done, failed, cancelled)
        paused_changed(paused)
    """

    progress = pyqtSignal(int, str)
    job_started = pyqtSignal(int, int, str)
    book_created = pyqtSignal(int)
    job_done = pyqtSignal(int, str)
    job_failed = pyqtSignal(str, str)
    all_done = pyqtSignal(int, int, bool)
    paused_changed = pyqtSignal(bool)

    def __init__(self, books_dir, db_path, parent=None):
        super().__init__(parent)
        self._books_dir = books_dir
        self._db_path = db_path
        self._jobs = []
        self._started = 0
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._tts_cache = {}
        self.current_book_id = None

    # ------------------------------------------------------------ control
    def add_job(self, pdf_path, model_path, config_path, title, author,
                voice_model, length_scale=1.0, audio_format="mp3",
                options=None, book_id=None):
        with self._lock:
            self._jobs.append(
                {
                    "pdf_path": pdf_path,
                    "model_path": model_path,
                    "config_path": config_path,
                    "title": title,
                    "author": author,
                    "voice_model": voice_model,
                    "length_scale": length_scale,
                    "audio_format": audio_format,
                    "options": options or {},
                    "book_id": book_id,
                }
            )

    def queued_book_ids(self):
        with self._lock:
            return {j["book_id"] for j in self._jobs if j.get("book_id")}

    @property
    def total_jobs(self):
        with self._lock:
            return self._started + len(self._jobs)

    def cancel(self):
        with self._lock:
            self._jobs.clear()
        self._cancel_event.set()
        self._pause_event.clear()

    def set_paused(self, paused):
        if paused:
            self._pause_event.set()
        else:
            self._pause_event.clear()
        self.paused_changed.emit(paused)

    def _next_job(self):
        with self._lock:
            if not self._jobs:
                return None
            self._started += 1
            return self._jobs.pop(0)

    def _tts(self, model_path, config_path):
        key = (model_path, config_path)
        if key not in self._tts_cache:
            self._tts_cache[key] = PiperTTS(model_path, config_path)
        return self._tts_cache[key]

    # ------------------------------------------------------------ thread
    def run(self):
        from .database import AudiobookDB

        done = failed = 0
        cancelled = False
        try:
            db = AudiobookDB(self._db_path)
        except Exception as exc:  # noqa: BLE001 - report and stop
            while (job := self._next_job()) is not None:
                self.job_failed.emit(str(exc), job["title"])
                failed += 1
            self.all_done.emit(done, failed, False)
            return

        while not self._cancel_event.is_set():
            job = self._next_job()
            if job is None:
                break
            self.job_started.emit(self._started, self.total_jobs, job["title"])
            self.current_book_id = job.get("book_id")

            def on_book(book_id):
                self.current_book_id = book_id
                self.book_created.emit(book_id)

            try:
                tts = self._tts(job["model_path"], job["config_path"])
                book_id = convert_pdf(
                    job["pdf_path"],
                    self._books_dir,
                    db,
                    tts,
                    title=job["title"],
                    author=job["author"],
                    voice_model=job["voice_model"],
                    length_scale=job["length_scale"],
                    audio_format=job["audio_format"],
                    options=job["options"],
                    progress_cb=lambda p, m: self.progress.emit(p, m),
                    cancel_event=self._cancel_event,
                    pause_event=self._pause_event,
                    book_id=job.get("book_id"),
                    on_book=on_book,
                )
                done += 1
                self.job_done.emit(book_id, job["title"])
            except CancelledError:
                cancelled = True
                break
            except Exception as exc:  # noqa: BLE001 - report and continue
                failed += 1
                self.job_failed.emit(str(exc), job["title"])
            finally:
                self.current_book_id = None
        if self._cancel_event.is_set():
            cancelled = True
        for tts in self._tts_cache.values():
            tts.close()
        self.all_done.emit(done, failed, cancelled)


class TaskThread(QThread):
    """Run ``fn(progress_cb, cancel_event)`` off the UI thread.

    Signals: progress(percent, message), succeeded(result), failed(message),
    cancelled().
    """

    progress = pyqtSignal(int, str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = self._fn(lambda p, m="": self.progress.emit(int(p), m), self.cancel_event)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
