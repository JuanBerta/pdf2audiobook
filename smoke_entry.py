"""Console smoke test for the packaged environment.

Verifies that PyMuPDF extraction and Piper TTS really work inside a
PyInstaller bundle. Run the built exe and check it prints SMOKE OK.

    dist\\PDF2AudiobookSmoke\\PDF2AudiobookSmoke.exe
"""
import os
import sys
import tempfile
import wave

import pymupdf
from piper import PiperVoice

MODELS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "PDF2Audiobook",
    "models",
)
PDF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.pdf")


def main():
    doc = pymupdf.open(PDF)
    text = doc[0].get_text("text").strip()
    assert len(text) > 20, "extraction too short"
    print(f"[1] extracted {len(text)} chars from page 1")

    model = os.path.join(MODELS_DIR, "en_US-lessac-medium.onnx")
    if not os.path.exists(model):
        model = os.path.join(MODELS_DIR, "zh-CN-huayan-medium.onnx")
    assert os.path.exists(model), f"no voice model in {MODELS_DIR}"

    voice = PiperVoice.load(model)
    out = os.path.join(tempfile.gettempdir(), "smoke_out.wav")
    with wave.open(out, "wb") as w:
        voice.synthesize_wav(text[:200], w)
    size = os.path.getsize(out)
    print(f"[2] synthesized {size} bytes -> {out}")
    assert size > 1000, "audio output too small"
    print("SMOKE OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        sys.exit(1)
