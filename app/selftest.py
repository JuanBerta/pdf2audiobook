"""Headless self-test of the whole pipeline (works inside the packaged exe).

    PDF2Audiobook.exe --selftest [report.txt]

Builds a small PDF, converts it with the first installed voice into a
temporary library (MP3), checks the read-along alignment, exports an M4B
when ffmpeg is available and plays a short silent clip on the audio device.
The report goes to the given file (default: logs\\selftest.txt) and the
exit code is 0 on success.
"""
import os
import shutil
import tempfile
import time
import traceback


def _make_pdf(path):
    from .textmodel import fitz

    doc = fitz.open()
    toc = []
    for ch in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 90), f"Chapter {ch}", fontsize=20)
        toc.append([1, f"Chapter {ch}", ch])
        y = 130
        for line in ("This is a short self test of the audiobook pipeline. It has two",
                     "sentences per paragraph, and a hyphen-", "ated word. Is it working?"):
            page.insert_text((72, y), line, fontsize=11)
            y += 15
    doc.set_toc(toc)
    doc.save(path)


def run(report_path, models_dir):
    lines = []

    def log(msg):
        lines.append(msg)

    ok = False
    work = tempfile.mkdtemp(prefix="p2a_selftest_")
    try:
        import numpy as np

        from .converter import PiperTTS, convert_pdf, export_m4b, find_ffmpeg
        from .database import AudiobookDB
        from .textmodel import ocr_available
        from .voices import list_voice_models

        models = list_voice_models(models_dir)
        if not models:
            raise RuntimeError(f"no voice models in {models_dir}")
        en = [m for m in models if os.path.basename(m[0]).startswith("en")]
        model, config, name = (en or models)[0]
        log(f"voice: {name}")
        log(f"ocr available: {ocr_available()} | ffmpeg: {find_ffmpeg()}")
        pdf = os.path.join(work, "selftest.pdf")
        _make_pdf(pdf)
        db = AudiobookDB(os.path.join(work, "lib.db"))
        tts = PiperTTS(model, config)
        t0 = time.time()
        bid = convert_pdf(pdf, work, db, tts, title="Self test",
                          voice_model=os.path.splitext(os.path.basename(model))[0])
        book = db.get_book(bid)
        chapters = db.get_chapters(bid)
        rows = db.get_sentence_alignments(bid)
        log(f"converted in {time.time() - t0:.1f}s: {len(chapters)} tracks, "
            f"{book['total_duration']:.1f}s audio, {[c['title'] for c in chapters]}")
        located = sum(1 for r in rows if r["page_no"])
        log(f"read-along: {located}/{len(rows)} sentences located")
        assert chapters and all(os.path.getsize(c["audio_path"]) > 1000 for c in chapters)
        assert located == len(rows) > 0
        if find_ffmpeg():
            out = export_m4b(db, bid, os.path.join(work, "selftest.m4b"))
            log(f"m4b export: {os.path.getsize(out)} bytes")
        from .audio_backend import SoundDevicePlayer

        p = SoundDevicePlayer()
        p.play_array(np.zeros(int(22050 * 0.4), np.float32), 22050)
        time.sleep(1.0)
        log(f"audio device: finished={p.is_finished}")
        p.stop()
        ok = True
        log("SELFTEST OK")
    except Exception:  # noqa: BLE001
        log("SELFTEST FAILED")
        log(traceback.format_exc())
    finally:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0 if ok else 1
