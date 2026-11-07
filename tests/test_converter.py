import os
import threading

import pytest

from app.converter import (
    CancelledError, build_chapter_markers, convert_pdf, needs_realign, plan_tracks, realign_book,
)
from app.database import AudiobookDB
from app.textmodel import PdfTextModel


def test_convert_creates_chapters_and_exact_alignment(tmp_path, book_pdf, fake_tts):
    db = AudiobookDB(str(tmp_path / "lib.db"))
    bid = convert_pdf(book_pdf, str(tmp_path), db, fake_tts, title="T", voice_model="en_US-x")
    book = db.get_book(bid)
    chapters = db.get_chapters(bid)
    assert book["total_chapters"] == len(chapters) == 3
    assert [c["title"] for c in chapters][0] == "Chapter 1: The Beginning"
    assert all(c["audio_path"].endswith(".mp3") and os.path.exists(c["audio_path"]) for c in chapters)
    rows = db.get_sentence_alignments(bid)
    assert rows and all(r["page_no"] > 0 for r in rows)
    assert not needs_realign(rows)
    # timestamps are monotonic and lie inside the book
    starts = [r["audio_start"] for r in rows]
    assert starts == sorted(starts)
    assert rows[-1]["audio_end"] <= book["total_duration"] + 1e-6


def test_cancel_then_resume_keeps_finished_tracks(tmp_path, book_pdf, fake_tts):
    db = AudiobookDB(str(tmp_path / "lib.db"))
    cancel = threading.Event()
    calls = {"n": 0}

    def progress(_p, _m):
        calls["n"] += 1
        if calls["n"] == 35:
            cancel.set()

    with pytest.raises(CancelledError):
        convert_pdf(book_pdf, str(tmp_path), db, fake_tts, title="T", voice_model="v",
                    progress_cb=progress, cancel_event=cancel)
    kept = len(db.get_chapters(1))
    assert kept >= 1
    fake_tts.calls = 0
    bid = convert_pdf(book_pdf, str(tmp_path), db, fake_tts, title="T", voice_model="v")
    assert bid == 1 and len(db.list_books()) == 1
    total_sentences = len(db.get_sentence_alignments(bid))
    assert fake_tts.calls < total_sentences  # only the missing tracks were synthesized


def test_different_settings_do_not_mix_into_unfinished_book(tmp_path, book_pdf, fake_tts):
    db = AudiobookDB(str(tmp_path / "lib.db"))
    cancel = threading.Event()

    def progress(pct, _m):
        if pct >= 30:  # stop after the first track is written
            cancel.set()

    with pytest.raises(CancelledError):
        convert_pdf(book_pdf, str(tmp_path), db, fake_tts, title="T", voice_model="v1",
                    progress_cb=progress, cancel_event=cancel)
    assert db.get_book(1)["total_chapters"] == 0
    bid = convert_pdf(book_pdf, str(tmp_path), db, fake_tts, title="T", voice_model="v2")
    assert db.get_book(bid)["voice_model"] == "v2"
    assert bid != 1  # a new book; the unfinished v1 book is left untouched
    assert db.get_book(1)["voice_model"] == "v1"


def test_length_scale_is_respected(tmp_path, book_pdf, fake_tts):
    db = AudiobookDB(str(tmp_path / "lib.db"))
    a = convert_pdf(book_pdf, str(tmp_path / "a"), db, fake_tts, title="A", voice_model="v", length_scale=1.0)
    b = convert_pdf(book_pdf, str(tmp_path / "b"), db, fake_tts, title="B", voice_model="v", length_scale=0.5)
    assert db.get_book(b)["total_duration"] < db.get_book(a)["total_duration"]


def test_realign_old_book(tmp_path, book_pdf, fake_tts):
    db = AudiobookDB(str(tmp_path / "lib.db"))
    bid = convert_pdf(book_pdf, str(tmp_path), db, fake_tts, title="T", voice_model="v")
    rows = db.get_sentence_alignments(bid)
    db.update_alignment_geometry([(r["id"], 0, "null") for r in rows])
    assert needs_realign(db.get_sentence_alignments(bid))
    located, total = realign_book(db, bid)
    assert located == total == len(rows)


def test_plan_tracks_splits_long_sections(book_pdf):
    model = PdfTextModel(book_pdf)
    tracks = plan_tracks(model, max_chars=500)
    assert len(tracks) > 3
    assert any(t.title.endswith("(1/2)") or "(1/" in t.title for t in tracks)
    assert all(t.chars <= 500 or len(t.items) == 1 for t in tracks)


def test_chapter_markers_merge_tracks_of_a_section():
    chapters = [
        {"duration": 10, "section_idx": 0, "section_title": "A", "title": "A (1/2)"},
        {"duration": 5, "section_idx": 0, "section_title": "A", "title": "A (2/2)"},
        {"duration": 7, "section_idx": 1, "section_title": "B", "title": "B"},
    ]
    assert build_chapter_markers(chapters) == [(0.0, 15.0, "A"), (15.0, 22.0, "B")]
