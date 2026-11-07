"""Shared fixtures: generated test PDFs, a fake TTS and a fake audio device."""
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------- audio device
class _CallbackStop(Exception):
    pass


class FakeStream:
    """Stand-in for sounddevice.OutputStream that pulls audio from a thread."""

    def __init__(self, samplerate, channels, dtype, callback, latency):
        self.cb, self.sr, self.ch = callback, samplerate, channels
        self.active = False

    def start(self):
        self.active = True

        def run():
            buf = np.zeros((512, self.ch), np.float32)
            while self.active:
                try:
                    self.cb(buf, 512, None, None)
                except _CallbackStop:
                    self.active = False
                    break
                time.sleep(512 / self.sr / 4)  # 4x faster than real time

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self.active = False

    abort = stop

    def close(self):
        pass


sys.modules.setdefault(
    "sounddevice", types.SimpleNamespace(OutputStream=FakeStream, CallbackStop=_CallbackStop)
)


# -------------------------------------------------------------------- TTS
class FakeTTS:
    """Deterministic 'speech': a tone whose length depends on the text."""

    sample_rate = 22050

    def __init__(self):
        self.calls = 0

    def load(self):
        pass

    def synthesize(self, text, length_scale=1.0, cancel_event=None):
        from app.converter import CancelledError

        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        self.calls += 1
        n = int(len(text) * 300 * length_scale)
        return (np.sin(np.arange(n) / 5) * 8000).astype(np.int16)


@pytest.fixture
def fake_tts():
    return FakeTTS()


# -------------------------------------------------------------------- PDFs
WORDS = ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
         "tempor incididunt ut labore et dolore magna aliqua").split()


def make_book_pdf(path, chapters=3, pages_per_chapter=2, with_toc=True):
    import pymupdf as fitz

    doc = fitz.open()
    toc = []
    for ch in range(1, chapters + 1):
        for pg in range(pages_per_chapter):
            page = doc.new_page()
            page.insert_text((72, 40), "My Great Book - Running Header", fontsize=9)
            page.insert_text((300, 800), str(len(doc)), fontsize=9)
            y = 90
            if pg == 0:
                page.insert_text((72, y), f"Chapter {ch}: The Beginning", fontsize=20)
                y += 40
                toc.append([1, f"Chapter {ch}: The Beginning", len(doc)])
            for para in range(3):
                text = " ".join(WORDS * 2) + (
                    f". Second sentence number {para} is here! Is this the third? Mr. Smith agreed."
                )
                lines, cur = [], ""
                for w in text.split():
                    if len(cur) + len(w) > 60:
                        lines.append(cur)
                        cur = w
                    else:
                        cur = (cur + " " + w).strip()
                lines.append(cur)
                lines[0] += " exam-"
                lines[1] = "ple " + lines[1]
                for line in lines:
                    page.insert_text((72, y), line, fontsize=11)
                    y += 15
                y += 12
    if with_toc:
        doc.set_toc(toc)
    doc.save(path)
    return path


@pytest.fixture
def book_pdf(tmp_path):
    return make_book_pdf(str(tmp_path / "book.pdf"))


@pytest.fixture
def sample_pdf():
    return os.path.join(ROOT, "sample.pdf")
