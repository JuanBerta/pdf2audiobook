"""Generate a small sample PDF for testing the conversion pipeline.

Usage:
    python scripts/make_test_pdf.py [out.pdf]
"""
import os
import sys

try:  # PyMuPDF >= 1.24 exposes the modern namespace
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older versions
    import fitz  # type: ignore[no-redef]


SAMPLE_PARAGRAPHS = [
    "The quick brown fox jumps over the lazy dog. This is a short sentence "
    "used to verify that text extraction and speech synthesis work end to end. "
    "Every word in this paragraph should be read aloud by the text to speech "
    "engine, producing a natural sounding sentence that a listener can follow "
    "without any visual reference to the original document.",
    "An audiobook is simply a recording of a written work being read aloud. "
    "With modern speech synthesis, it is possible to generate such recordings "
    "automatically from any text file or document. The quality depends on the "
    "voice model and the clarity of the source text, so well formatted "
    "documents generally produce better results than noisy scans.",
    "This application extracts text from a PDF document, splits it into "
    "paragraph-sized chunks, and feeds each chunk to a local neural text to "
    "speech engine. The resulting audio files are stored in a library that "
    "remembers your listening position between sessions, so you can always "
    "return to exactly the sentence where you stopped listening last time.",
    "You can pause, resume, skip chapters, and come back later to continue "
    "exactly where you left off. The library is stored locally in a SQLite "
    "database, so no account or cloud service is required. Everything runs "
    "on your own computer, keeping your documents private and offline.",
    "Chapter based navigation is useful for long documents such as books and "
    "manuals. Each paragraph chunk becomes a separate chapter with its own "
    "duration, and the player shows both the elapsed time and the remaining "
    "time for the current chapter while it is being spoken aloud.",
    "Speech synthesis engines have improved dramatically in recent years. "
    "Neural voice models now produce recordings that are clear, expressive "
    "and pleasant to listen to for extended periods, making them a practical "
    "alternative to professional narration for personal use cases.",
    "If the source PDF contains tables, footnotes or unusual layouts, the "
    "extracted text may contain artifacts. The converter applies basic "
    "cleanup such as removing standalone page numbers and joining wrapped "
    "lines, which handles the majority of everyday documents.",
    "This project is open source and built from freely available components: "
    "PyMuPDF for document parsing, Piper for neural speech synthesis, "
    "pygame for audio playback and PyQt6 for the desktop interface. "
    "Contributions and new voice models are always welcome.",
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "sample.pdf"
    doc = fitz.open()
    for i, para in enumerate(SAMPLE_PARAGRAPHS):
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(72, 72, 540, 720),
            f"Sample PDF - page {i + 1}\n\n{para}\n\n",
            fontsize=12,
        )
    doc.save(out)
    doc.close()
    print(f"Wrote {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main()
