from app.textmodel import PdfTextModel, join_lines, normalize_with_map, split_sentences


def _sentences(model):
    return [s for sec in model.sections for p in sec.paragraphs for s in split_sentences(p.text)]


def test_every_sentence_is_located(book_pdf, sample_pdf):
    # Regression: the old locator compared a space-free needle with a haystack
    # that still had spaces, so 0% of sentences were ever found.
    for pdf in (book_pdf, sample_pdf):
        model = PdfTextModel(pdf, default_title="Book")
        sents = _sentences(model)
        assert sents
        cursor = 0
        for s in sents:
            geo, cursor = model.geometry(s, cursor)
            assert geo is not None, s
            assert geo["rects"] and geo["words"]


def test_running_headers_and_page_numbers_removed(book_pdf):
    model = PdfTextModel(book_pdf)
    text = " ".join(p.text for sec in model.sections for p in sec.paragraphs)
    assert "Running Header" not in text
    assert not any(t.strip().isdigit() for t in text.split("  "))


def test_toc_sections_and_dehyphenation(book_pdf):
    model = PdfTextModel(book_pdf)
    assert [s.title for s in model.sections] == [f"Chapter {i}: The Beginning" for i in (1, 2, 3)]
    first = model.sections[0].paragraphs
    assert first[0].heading
    assert "example" in first[1].text and "exam-" not in first[1].text


def test_heading_fallback_without_toc(tmp_path):
    from tests.conftest import make_book_pdf

    pdf = make_book_pdf(str(tmp_path / "notoc.pdf"), with_toc=False)
    model = PdfTextModel(pdf)
    assert len(model.sections) == 3
    assert model.sections[1].title.startswith("Chapter 2")


def test_rects_are_in_pdf_space(book_pdf):
    model = PdfTextModel(book_pdf)
    geo, _ = model.geometry(model.sections[0].paragraphs[0].text)
    page, x0, y0, x1, y1 = geo["rects"][0]
    assert page == 0
    # heading is drawn near the top: PDF space has y growing upwards
    assert y0 > 700 and x1 > x0


def test_split_sentences():
    assert split_sentences("Mr. Smith arrived. He sat down! Did he? Yes.") == [
        "Mr. Smith arrived.", "He sat down!", "Did he?", "Yes."]
    assert split_sentences("你好。这是第二句！第三句？好的") == ["你好。", "这是第二句！", "第三句？", "好的"]
    assert split_sentences("See fig. 3 for details. Next one.") == ["See fig. 3 for details.", "Next one."]
    assert split_sentences("J. R. R. Tolkien wrote it. Then...") == ["J. R. R. Tolkien wrote it.", "Then..."]
    long = "word, " * 200
    assert all(len(p) <= 450 for p in split_sentences(long))


def test_join_lines():
    assert join_lines("exam-", "ple text") == "example text"
    assert join_lines("well-", "Known") == "well- Known"
    assert join_lines("你好", "世界") == "你好世界"
    assert join_lines("hello", "world") == "hello world"


def test_normalize_with_map():
    norm, idx = normalize_with_map("A ﬁne-day")
    assert norm == "afineday"
    assert idx[1] == idx[2] == 2  # ligature expands to two chars of one source char
