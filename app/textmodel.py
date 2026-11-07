"""PDF text model used for narration and read-along.

The whole pipeline (what gets spoken *and* where it is highlighted) is
built from one extraction pass, so every spoken sentence can be mapped back
to exact character boxes on the page:

* ``rawdict`` extraction gives every character with its bounding box.
* Repeated page furniture (running headers/footers, page numbers, divider
  lines) is detected and dropped.
* Lines are joined into paragraphs with de-hyphenation ("exam-" + "ple").
* Paragraphs are grouped into sections using the PDF outline (table of
  contents) or, when there is none, large-font headings.
* A whitespace-free, case-folded "haystack" of all kept characters maps each
  position back to (line, char) so sentences can be located precisely.
* Image-only pages can be OCR'd with Tesseract when it is installed.
"""
import bisect
import os
import re
import shutil
import unicodedata
from array import array
from collections import Counter

try:  # PyMuPDF >= 1.24 exposes the modern namespace
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older versions
    import fitz  # type: ignore[no-redef]


# ---------------------------------------------------------------- helpers
_DROP_CHARS = set(
    " \t\r\n\f\v\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u200b\u202f\u205f\u3000\ufeff"
    "-\u00ad\u2010\u2011\u2012\u2013\u2014\u2212"
)
_HYPHENS = "-\u00ad\u2010\u2011"
_TERMINAL = tuple(".!?:;…。！？；」』”\"')»]")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_SPEAKABLE_RE = re.compile(r"[^\W_]")
_PAGE_NUM_RE = re.compile(
    r"^\W*(?:p(?:age|ág(?:ina)?|\.)?\s*)?\d{1,4}(?:\s*(?:/|of|de|-)\s*\d{1,4})?\W*$",
    re.IGNORECASE,
)
_ROMAN_RE = re.compile(r"^[ivxlcdm]{1,7}$", re.IGNORECASE)
_DIVIDER_RE = re.compile(r"^[-–—_=*#~•·.\s]{3,}$")


def norm_char(ch):
    """Normalized form of one character for matching ('' when dropped)."""
    if ch in _DROP_CHARS:
        return ""
    out = unicodedata.normalize("NFKC", ch).casefold()
    return "".join(c for c in out if c not in _DROP_CHARS)


def normalize_with_map(text):
    """Return (normalized string, list mapping each normalized char -> index in text)."""
    chars = []
    index = []
    for i, ch in enumerate(text):
        n = norm_char(ch)
        for c in n:
            chars.append(c)
            index.append(i)
    return "".join(chars), index


def is_speakable(text):
    return bool(_SPEAKABLE_RE.search(text or ""))


def is_cjk(ch):
    return bool(ch) and bool(_CJK_RE.match(ch))


def join_lines(prev, nxt):
    """Join two text fragments, removing end-of-line hyphenation."""
    if not prev:
        return nxt
    if not nxt:
        return prev
    p = prev.rstrip()
    n = nxt.lstrip()
    if (
        len(p) >= 2
        and p[-1] in _HYPHENS
        and p[-2].isalpha()
        and n[:1].islower()
    ):
        return p[:-1] + n
    if is_cjk(p[-1:]) or is_cjk(n[:1]):
        return p + n
    return p + " " + n


def ends_sentence(text):
    return text.rstrip().endswith(_TERMINAL)


def furniture_key(text):
    """Key used to spot running headers/footers (digits are wildcards)."""
    t = re.sub(r"\d+", "#", text.strip().casefold())
    return re.sub(r"\s+", " ", t)


# ----------------------------------------------------------------- OCR
_OCR_LANGS = {
    "en": "eng", "es": "spa", "zh": "chi_sim", "fr": "fra", "de": "deu",
    "it": "ita", "pt": "por", "ru": "rus", "ja": "jpn", "ko": "kor",
    "nl": "nld", "pl": "pol", "tr": "tur", "uk": "ukr", "ca": "cat",
}


def tessdata_dir():
    """Locate Tesseract's tessdata folder, or None when OCR is unavailable."""
    env = os.environ.get("TESSDATA_PREFIX")
    if env and os.path.isdir(env):
        return env
    try:
        found = fitz.get_tessdata()
        if found and os.path.isdir(found):
            return found
    except Exception:  # noqa: BLE001 - not installed
        pass
    candidates = []
    exe = shutil.which("tesseract")
    if exe:
        candidates.append(os.path.join(os.path.dirname(exe), "tessdata"))
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if base:
            candidates.append(os.path.join(base, "Tesseract-OCR", "tessdata"))
    candidates += ["/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tessdata"]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def ocr_available():
    return tessdata_dir() is not None and hasattr(fitz.Page, "get_textpage_ocr")


def ocr_language(voice_lang):
    """Best installed Tesseract language for a voice language code ('en_US')."""
    tess = tessdata_dir()
    if not tess:
        return "eng"
    installed = {
        os.path.splitext(n)[0]
        for n in os.listdir(tess)
        if n.endswith(".traineddata")
    }
    wanted = _OCR_LANGS.get((voice_lang or "en")[:2].lower(), "eng")
    if wanted in installed:
        return wanted if wanted == "eng" or "eng" not in installed else f"{wanted}+eng"
    return "eng" if "eng" in installed else (sorted(installed) or ["eng"])[0]


# ---------------------------------------------------------------- model
class Line:
    __slots__ = ("page", "block", "text", "boxes", "bbox", "size", "keep", "section")

    def __init__(self, page, block, text, boxes, bbox, size):
        self.page = page
        self.block = block
        self.text = text
        self.boxes = boxes      # array('f'): x0,y0,x1,y1 per character
        self.bbox = bbox
        self.size = size
        self.keep = True
        self.section = 0

    def char_box(self, i):
        b = self.boxes
        return b[4 * i], b[4 * i + 1], b[4 * i + 2], b[4 * i + 3]


class Paragraph:
    __slots__ = ("text", "section", "page", "heading")

    def __init__(self, text, section, page, heading=False):
        self.text = text
        self.section = section
        self.page = page
        self.heading = heading


class Section:
    __slots__ = ("title", "paragraphs")

    def __init__(self, title):
        self.title = title
        self.paragraphs = []


class PdfTextModel:
    """Extracted text of a PDF with a char-level position index."""

    def __init__(
        self,
        pdf_path,
        remove_furniture=True,
        detect_sections=True,
        ocr="auto",
        ocr_lang="eng",
        default_title="",
        front_title="Front matter",
        progress_cb=None,
        cancel_check=None,
    ):
        self.pdf_path = pdf_path
        self.lines = []
        self.page_matrix = []   # PDF-space conversion per page (a,b,c,d,e,f)
        self.page_count = 0
        self.ocr_pages = 0
        self.sections = []
        self._extract(ocr, ocr_lang, progress_cb, cancel_check)
        if remove_furniture:
            self._remove_furniture()
        starts = self._section_starts(detect_sections, front_title)
        self._build_sections(starts, default_title or "")
        self._build_haystack()

    # -------------------------------------------------------- extraction
    def _extract(self, ocr, ocr_lang, progress_cb, cancel_check):
        use_ocr = ocr in (True, "auto", "on") and ocr_available()
        if use_ocr and not os.environ.get("TESSDATA_PREFIX"):
            os.environ["TESSDATA_PREFIX"] = tessdata_dir()
        flags = fitz.TEXT_PRESERVE_WHITESPACE | getattr(fitz, "TEXT_MEDIABOX_CLIP", 0)
        doc = fitz.open(self.pdf_path)
        try:
            self.page_count = doc.page_count
            self.toc = doc.get_toc(simple=True) or []
            self._page_heights = []
            for pno, page in enumerate(doc):
                if cancel_check is not None:
                    cancel_check()
                if progress_cb:
                    progress_cb(pno, self.page_count)
                inv = ~page.transformation_matrix
                self.page_matrix.append((inv.a, inv.b, inv.c, inv.d, inv.e, inv.f))
                r = page.rect if page.rotation == 0 else None
                self._page_heights.append((r.y0, r.y1) if r is not None else None)
                textpage = None
                raw = page.get_text("rawdict", flags=flags)
                if use_ocr and not self._has_text(raw) and (
                    page.get_images(full=False) or page.get_drawings()
                ):
                    try:
                        textpage = page.get_textpage_ocr(
                            flags=flags, language=ocr_lang, dpi=300, full=True
                        )
                        raw = page.get_text("rawdict", flags=flags, textpage=textpage)
                        self.ocr_pages += 1
                    except Exception:  # noqa: BLE001 - OCR is best effort
                        pass
                self._add_page(pno, raw)
        finally:
            doc.close()

    @staticmethod
    def _has_text(raw):
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        if not ch["c"].isspace():
                            return True
        return False

    def _add_page(self, pno, raw):
        for bno, block in enumerate(raw.get("blocks", [])):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                chars = []
                size = 0.0
                for span in line.get("spans", []):
                    sz = float(span.get("size", 0.0))
                    for ch in span.get("chars", []):
                        c = ch["c"]
                        if c == "\ufffd":
                            continue
                        chars.append((c, ch["bbox"]))
                        if not c.isspace():
                            size = max(size, sz)
                # trim surrounding whitespace
                while chars and chars[0][0].isspace():
                    chars.pop(0)
                while chars and chars[-1][0].isspace():
                    chars.pop()
                if not chars:
                    continue
                text = "".join(c for c, _ in chars)
                boxes = array("f")
                for _, b in chars:
                    boxes.extend((b[0], b[1], b[2], b[3]))
                x0 = min(b[0] for _, b in chars)
                y0 = min(b[1] for _, b in chars)
                x1 = max(b[2] for _, b in chars)
                y1 = max(b[3] for _, b in chars)
                self.lines.append(
                    Line(pno, bno, text, boxes, (x0, y0, x1, y1), size)
                )

    # ------------------------------------------------- furniture removal
    def _in_band(self, line):
        bounds = self._page_heights[line.page]
        if bounds is None:
            return False
        top, bottom = bounds
        band = (bottom - top) * 0.09
        return line.bbox[3] <= top + band or line.bbox[1] >= bottom - band

    def _remove_furniture(self):
        n_pages = self.page_count
        pages_per_key = {}
        for ln in self.lines:
            if self._in_band(ln):
                pages_per_key.setdefault(furniture_key(ln.text), set()).add(ln.page)
        threshold = max(3, int(n_pages * 0.25))
        repeated = {
            k for k, pages in pages_per_key.items()
            if n_pages >= 4 and len(pages) >= threshold
        }
        for ln in self.lines:
            t = ln.text.strip()
            if _DIVIDER_RE.match(t):
                ln.keep = False
            elif self._in_band(ln) and (
                furniture_key(t) in repeated
                or _PAGE_NUM_RE.match(t)
                or _ROMAN_RE.match(t)
            ):
                ln.keep = False
            elif re.fullmatch(r"\d{1,4}", t):
                ln.keep = False  # stray page number inside the text area

    # ------------------------------------------------------- sections
    def _kept(self):
        return [ln for ln in self.lines if ln.keep]

    def _section_starts(self, detect, front_title):
        """Return [(line_index_in_kept, title)] for each section start."""
        if not detect:
            return []
        kept = self._kept()
        if not kept:
            return []
        starts = self._toc_starts(kept)
        if not starts:
            starts = self._heading_starts(kept)
        if starts and starts[0][0] > 0:
            before = "".join(ln.text for ln in kept[: starts[0][0]])
            if is_speakable(before):
                starts.insert(0, (0, front_title))
            else:
                starts[0] = (0, starts[0][1])
        return starts

    def _toc_starts(self, kept):
        toc = [e for e in self.toc if len(e) >= 3 and e[2] >= 1]
        if not toc:
            return []
        entries = []
        for level in sorted({e[0] for e in toc}):
            cand = [e for e in toc if e[0] == level]
            if len(cand) >= 2:
                entries = cand
                break
        if not entries or len(entries) > 400:
            return []
        first_line_of_page = {}
        for i, ln in enumerate(kept):
            first_line_of_page.setdefault(ln.page, i)
        starts = []
        last = -1
        for _lvl, title, page in entries:
            pidx = page - 1
            # first kept line on that page (or the next page with text)
            i0 = None
            for p in range(pidx, min(pidx + 3, self.page_count)):
                if p in first_line_of_page:
                    i0 = first_line_of_page[p]
                    break
            if i0 is None:
                continue
            nt = normalize_with_map(title)[0]
            chosen = i0
            if nt:
                for j in range(i0, min(i0 + 60, len(kept))):
                    if kept[j].page > pidx + 1:
                        break
                    nl = normalize_with_map(kept[j].text)[0]
                    k = min(len(nt), len(nl), 20)
                    if k >= 3 and nt[:k] == nl[:k]:
                        chosen = j
                        break
            if chosen <= last:
                continue
            starts.append((chosen, re.sub(r"\s+", " ", title).strip()[:120]))
            last = chosen
        return starts if len(starts) >= 2 else []

    def _heading_starts(self, kept):
        weights = Counter()
        for ln in kept:
            weights[round(ln.size, 1)] += len(ln.text)
        if not weights:
            return []
        body = weights.most_common(1)[0][0]
        if body <= 0:
            return []
        block_sizes = Counter((ln.page, ln.block) for ln in kept)
        starts = []
        i = 0
        while i < len(kept):
            ln = kept[i]
            t = ln.text.strip()
            if (
                ln.size >= body * 1.3
                and 2 <= len(t) <= 90
                and block_sizes[(ln.page, ln.block)] <= 3
                and is_speakable(t)
                and not t.endswith((".", ","))
            ):
                title = t
                j = i + 1
                while (
                    j < len(kept)
                    and kept[j].page == ln.page
                    and kept[j].block == ln.block
                    and kept[j].size >= body * 1.3
                ):
                    title = join_lines(title, kept[j].text.strip())
                    j += 1
                starts.append((i, title[:120]))
                i = j
                continue
            i += 1
        if 2 <= len(starts) <= max(3, self.page_count * 2):
            return starts
        return []

    def _build_sections(self, starts, default_title):
        kept = self._kept()
        if not starts:
            starts = [(0, default_title)]
        bounds = [s[0] for s in starts]
        heading_lines = set()
        for idx, (line_i, title) in enumerate(starts):
            self.sections.append(Section(title))
            # mark the heading line(s) so they get a longer pause
            nt = normalize_with_map(title)[0]
            acc = ""
            for j in range(line_i, min(line_i + 4, len(kept))):
                acc += normalize_with_map(kept[j].text)[0]
                if nt and nt.startswith(acc):
                    heading_lines.add(j)
                else:
                    break
        for i, ln in enumerate(kept):
            ln.section = max(0, bisect.bisect_right(bounds, i) - 1)

        paragraphs = []
        cur = None
        prev_line = None
        for i, ln in enumerate(kept):
            is_head = i in heading_lines
            new_para = (
                cur is None
                or ln.section != cur.section
                or ln.page != prev_line.page
                or ln.block != prev_line.block
                or is_head != cur.heading
            )
            if new_para:
                cur = Paragraph(ln.text, ln.section, ln.page, is_head)
                paragraphs.append(cur)
            else:
                cur.text = join_lines(cur.text, ln.text)
            prev_line = ln

        # merge paragraphs that continue across blocks / pages
        merged = []
        for p in paragraphs:
            if (
                merged
                and not p.heading
                and not merged[-1].heading
                and merged[-1].section == p.section
                and not ends_sentence(merged[-1].text)
                and (p.text[:1].islower() or is_cjk(p.text[:1]))
            ):
                merged[-1].text = join_lines(merged[-1].text, p.text)
            else:
                merged.append(p)
        for p in merged:
            p.text = re.sub(r"[ \t\u00a0]{2,}", " ", p.text).strip()
            if is_speakable(p.text):
                self.sections[p.section].paragraphs.append(p)
        # drop sections that ended up empty (e.g. a TOC entry pointing at an image)
        self.sections = [s for s in self.sections if s.paragraphs]

    # ------------------------------------------------------- locating
    def _build_haystack(self, include_all=False):
        hay = []
        map_line = array("i")
        map_char = array("i")
        for li, ln in enumerate(self.lines):
            if not ln.keep and not include_all:
                continue
            for ci, ch in enumerate(ln.text):
                n = norm_char(ch)
                for c in n:
                    hay.append(c)
                    map_line.append(li)
                    map_char.append(ci)
        self.hay = "".join(hay)
        self.map_line = map_line
        self.map_char = map_char

    def include_all_lines(self):
        """Search every line (used to re-align books made by older versions)."""
        self._build_haystack(include_all=True)

    @property
    def has_text(self):
        return any(s.paragraphs for s in self.sections)

    def find(self, text, cursor=0):
        """Locate ``text``; returns (hay_start, needle_map, matched_len) or None."""
        needle, nmap = normalize_with_map(text)
        if not needle:
            return None
        pos = self.hay.find(needle, cursor)
        if pos < 0:
            pos = self.hay.find(needle)
        if pos >= 0:
            return pos, nmap, len(needle)
        # Partial fallback: longest prefix that can still be found.
        for k in (len(needle) * 2 // 3, len(needle) // 2, 40, 24, 12):
            if 8 <= k < len(needle):
                pos = self.hay.find(needle[:k], cursor)
                if pos < 0:
                    pos = self.hay.find(needle[:k])
                if pos >= 0:
                    return pos, nmap, k
        return None

    def _rects_for_range(self, a, b):
        """Per-line rectangles (page, x0, y0, x1, y1) in PDF space."""
        out = []
        i = a
        while i < b:
            li = self.map_line[i]
            c0 = c1 = self.map_char[i]
            j = i + 1
            while j < b and self.map_line[j] == li:
                c1 = self.map_char[j]
                j += 1
            ln = self.lines[li]
            xs0, ys0, xs1, ys1 = [], [], [], []
            for c in range(min(c0, c1), max(c0, c1) + 1):
                x0, y0, x1, y1 = ln.char_box(c)
                xs0.append(x0); ys0.append(y0); xs1.append(x1); ys1.append(y1)
            out.append((ln.page,) + self._to_pdf(ln.page, min(xs0), min(ys0), max(xs1), max(ys1)))
            i = j
        return out

    def _to_pdf(self, page, x0, y0, x1, y1):
        a, b, c, d, e, f = self.page_matrix[page]
        pts = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
        xs = [a * x + c * y + e for x, y in pts]
        ys = [b * x + d * y + f for x, y in pts]
        return (
            round(min(xs), 2), round(min(ys), 2),
            round(max(xs), 2), round(max(ys), 2),
        )

    def geometry(self, text, cursor=0):
        """Highlight geometry for one sentence.

        Returns (geometry dict or None, new_cursor). The dict has
        ``rects`` (one per line) and ``words`` (rect + timing fraction),
        all in PDF user-space coordinates.
        """
        found = self.find(text, cursor)
        if found is None:
            return None, cursor
        pos, nmap, mlen = found
        rects = self._rects_for_range(pos, pos + mlen)
        weights = word_weights(text)
        total = weights[-1] or 1.0
        words = []
        for m in WORD_RE.finditer(text):
            k0 = bisect.bisect_left(nmap, m.start())
            k1 = bisect.bisect_left(nmap, m.end())
            if k0 >= k1 or k1 > mlen:
                continue
            f0 = round(weights[m.start()] / total, 4)
            f1 = round(weights[m.end()] / total, 4)
            for r in self._rects_for_range(pos + k0, pos + k1):
                words.append(list(r) + [f0, f1])
        geo = {"v": 2, "rects": [list(r) for r in rects], "words": words}
        return geo, pos + mlen


WORD_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
    r"|[^\W_\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+"
    r"(?:['’][^\W_]+)*"
)


def word_weights(text):
    """Cumulative speaking-time weights per character position (len+1)."""
    acc = [0.0]
    total = 0.0
    for ch in text:
        if ch.isalnum():
            w = 1.0
        elif ch in ",;:、，；：":
            w = 4.0
        elif ch in ".!?。！？…":
            w = 2.0
        elif ch.isspace():
            w = 0.4
        else:
            w = 0.5
        total += w
        acc.append(total)
    return acc


# ------------------------------------------------------------ sentences
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "sra", "srta", "st", "jr", "vs",
    "etc", "e.g", "i.e", "fig", "no", "nº", "vol", "pp", "p", "cap", "ed",
    "ej", "pág", "aprox", "dra", "lic", "ing", "av", "approx", "inc", "ltd",
    "co", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec", "ene", "abr", "ago", "dic", "u.s", "a.m", "p.m",
}
_CLOSERS = "\"'”’»)]」』）】"


def split_sentences(text):
    """Split a paragraph into sentences (handles CJK and abbreviations)."""
    text = text.strip()
    out = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        cut = None
        if ch in "。！？；":
            j = i + 1
            while j < n and text[j] in _CLOSERS + "。！？":
                j += 1
            cut = j
        elif ch in ".!?…":
            j = i + 1
            while j < n and text[j] in ".!?…":
                j += 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            if j >= n or text[j].isspace():
                k = j
                while k < n and text[k].isspace():
                    k += 1
                nxt = text[k:k + 1]
                word = re.search(r"([\w.]+)\W*$", text[start:i])
                w = word.group(1).lower().rstrip(".") if word else ""
                is_abbrev = ch == "." and (
                    w in _ABBREV
                    or (len(w) == 1 and w.isalpha())   # initials "J. R. R."
                    or (w.isdigit() and nxt[:1].islower())
                )
                if not is_abbrev and (not nxt or not nxt.islower()):
                    cut = j
        if cut is not None:
            piece = text[start:cut].strip()
            if piece:
                out.append(piece)
            start = cut
            i = cut
            continue
        i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    # merge fragments with nothing to say ("…", "1.") into the previous one
    merged = []
    for s in out:
        if merged and not is_speakable(s):
            merged[-1] = f"{merged[-1]} {s}"
        else:
            merged.append(s)
    result = []
    for s in merged:
        result.extend(_split_long(s))
    return result


def _split_long(sentence, limit=450):
    """Break run-on sentences at commas/spaces so TTS and highlights stay sane."""
    if len(sentence) <= limit:
        return [sentence]
    parts = []
    rest = sentence
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind("，"), window.rfind("、"))
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut < limit // 3:
            cut = limit - 1
        parts.append(rest[: cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        parts.append(rest)
    return parts
