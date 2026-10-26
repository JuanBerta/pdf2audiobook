"""Background conversion worker (QThread)."""
import os

from PyQt6.QtCore import QThread, pyqtSignal

from .converter import PiperTTS, convert_pdf


class ConvertWorker(QThread):
    progress = pyqtSignal(int, str)
    succeeded = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(
        self,
        pdf_path,
        out_dir,
        db_path,
        model_path,
        config_path,
        title,
        author,
        voice_model="",
        parent=None,
    ):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.out_dir = out_dir
        self.db_path = db_path
        self.model_path = model_path
        self.config_path = config_path
        self.title = title
        self.author = author
        self.voice_model = voice_model

    def run(self):
        try:
            from .database import AudiobookDB

            db = AudiobookDB(self.db_path)
            tts = PiperTTS(self.model_path, self.config_path)
            self.progress.emit(0, "Extracting PDF text...")
            book_id = convert_pdf(
                self.pdf_path,
                self.out_dir,
                db,
                tts,
                title=self.title,
                author=self.author,
                voice_model=self.voice_model,
                progress_cb=lambda p, m: self.progress.emit(p, m),
            )
            self.succeeded.emit(book_id)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.failed.emit(str(exc))
