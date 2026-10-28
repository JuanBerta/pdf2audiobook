"""Background conversion worker supporting a queue of PDF jobs."""
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from .converter import CancelledError, PiperTTS, convert_pdf


class QueueWorker(QThread):
    """Converts a queue of PDFs in a background thread.

    Signals:
        progress(percent, message)   - global progress across the queue
        job_done(book_id, title)     - one book finished
        job_failed(message, title)   - one book failed (queue continues)
        all_done()                   - queue finished (or cancelled)
        paused_changed(paused)       - pause state toggled
    """

    progress = pyqtSignal(int, str)
    job_done = pyqtSignal(int, str)
    job_failed = pyqtSignal(str, str)
    all_done = pyqtSignal()
    paused_changed = pyqtSignal(bool)

    def __init__(self, books_dir, db_path, parent=None):
        super().__init__(parent)
        self._books_dir = books_dir
        self._db_path = db_path
        self._jobs = []
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()

    # ------------------------------------------------------------ control
    def add_job(
        self,
        pdf_path,
        model_path,
        config_path,
        title,
        author,
        voice_model,
        length_scale=1.0,
    ):
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
                }
            )

    def cancel(self):
        self._cancel_event.set()

    def set_paused(self, paused):
        if paused:
            self._pause_event.set()
        else:
            self._pause_event.clear()
        self.paused_changed.emit(paused)

    # ------------------------------------------------------------ thread
    def run(self):
        from .database import AudiobookDB

        with self._lock:
            jobs = list(self._jobs)
        total = len(jobs)
        db = AudiobookDB(self._db_path)

        for i, job in enumerate(jobs):
            if self._cancel_event.is_set():
                break
            tts = PiperTTS(job["model_path"], job["config_path"])
            base = (i / total) * 100 if total else 0.0

            def cb(percent, message, _base=base, _total=total):
                self.progress.emit(
                    int(_base + (percent / _total)), message
                )

            try:
                book_id = convert_pdf(
                    job["pdf_path"],
                    self._books_dir,
                    db,
                    tts,
                    title=job["title"],
                    author=job["author"],
                    voice_model=job["voice_model"],
                    length_scale=job["length_scale"],
                    progress_cb=cb,
                    cancel_event=self._cancel_event,
                    pause_event=self._pause_event,
                )
                self.job_done.emit(book_id, job["title"])
            except CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - report and continue
                self.job_failed.emit(str(exc), job["title"])
        self.all_done.emit()
