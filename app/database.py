"""SQLite storage for the audiobook library."""
import json
import os
import sqlite3

_BOOK_FIELDS = {
    "title", "author", "voice_model", "source_pdf", "cover_path",
    "total_chapters", "total_duration", "current_chapter", "position",
    "rate", "book_dir", "audio_format", "signature", "options",
}


class AudiobookDB:
    """Thin SQLite wrapper storing books, tracks, bookmarks and alignments.

    A new connection is opened per call so the UI thread and the conversion
    worker can use the same library safely; WAL mode lets the UI keep
    reading while the worker writes.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    voice_model TEXT NOT NULL DEFAULT '',
                    source_pdf TEXT NOT NULL DEFAULT '',
                    cover_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    total_chapters INTEGER NOT NULL DEFAULT 0,
                    total_duration REAL NOT NULL DEFAULT 0,
                    current_chapter INTEGER NOT NULL DEFAULT 0,
                    position REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    idx INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    audio_path TEXT NOT NULL,
                    duration REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bookmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    chapter_idx INTEGER NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS sentence_alignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    chapter_idx INTEGER NOT NULL,
                    sent_idx INTEGER NOT NULL,
                    page_no INTEGER NOT NULL DEFAULT 0,
                    bbox TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    audio_start REAL NOT NULL DEFAULT 0,
                    audio_end REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sent_align_book
                    ON sentence_alignments(book_id, chapter_idx, sent_idx);
                CREATE INDEX IF NOT EXISTS idx_chapters_book
                    ON chapters(book_id, idx);
                CREATE INDEX IF NOT EXISTS idx_bookmarks_book
                    ON bookmarks(book_id);
                """
            )
            # Migrations for libraries created by older versions.
            book_cols = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
            for name, ddl in (
                ("voice_model", "TEXT NOT NULL DEFAULT ''"),
                ("rate", "REAL NOT NULL DEFAULT 1.0"),
                ("book_dir", "TEXT NOT NULL DEFAULT ''"),
                ("audio_format", "TEXT NOT NULL DEFAULT 'wav'"),
                ("signature", "TEXT NOT NULL DEFAULT ''"),
                ("options", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in book_cols:
                    conn.execute(f"ALTER TABLE books ADD COLUMN {name} {ddl}")
            ch_cols = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
            for name, ddl in (
                ("section_idx", "INTEGER NOT NULL DEFAULT -1"),
                ("section_title", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in ch_cols:
                    conn.execute(f"ALTER TABLE chapters ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------ books
    def add_book(self, title, author="", source_pdf="", cover_path="",
                 voice_model="", rate=1.0, audio_format="mp3", signature="",
                 options=""):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO books (title, author, source_pdf, cover_path, voice_model,"
                " rate, audio_format, signature, options) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, author, source_pdf, cover_path, voice_model, rate,
                 audio_format, signature, options),
            )
            return cur.lastrowid

    def update_book(self, book_id, **fields):
        fields = {k: v for k, v in fields.items() if k in _BOOK_FIELDS}
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE books SET {cols} WHERE id=?", (*fields.values(), book_id))

    def finalize_book(self, book_id, total_chapters, total_duration):
        self.update_book(book_id, total_chapters=total_chapters, total_duration=total_duration)

    def set_cover(self, book_id, cover_path):
        self.update_book(book_id, cover_path=cover_path)

    _BOOK_SELECT = (
        "SELECT b.*, (SELECT COUNT(*) FROM chapters c WHERE c.book_id=b.id) AS chapter_count,"
        " (SELECT COALESCE(SUM(duration), 0) FROM chapters c WHERE c.book_id=b.id)"
        " AS audio_duration FROM books b"
    )

    def list_books(self):
        with self._connect() as conn:
            rows = conn.execute(
                self._BOOK_SELECT + " ORDER BY b.created_at DESC, b.id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_book(self, book_id):
        with self._connect() as conn:
            row = conn.execute(self._BOOK_SELECT + " WHERE b.id=?", (book_id,)).fetchone()
            return dict(row) if row else None

    def update_progress(self, book_id, chapter_idx, position):
        self.update_book(book_id, current_chapter=chapter_idx, position=position)

    def delete_book(self, book_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM books WHERE id=?", (book_id,))

    def find_incomplete_book(self, source_pdf, signature=None):
        """Most recent unfinished book for the same PDF (and plan signature)."""
        sql = "SELECT * FROM books WHERE source_pdf=? AND total_chapters=0"
        args = [source_pdf]
        if signature is not None:
            sql += " AND signature=?"
            args.append(signature)
        with self._connect() as conn:
            row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
            return dict(row) if row else None

    # --------------------------------------------------------- chapters
    def add_chapter(self, book_id, idx, title, audio_path, duration=0.0,
                    section_idx=-1, section_title=""):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO chapters (book_id, idx, title, audio_path, duration,"
                " section_idx, section_title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (book_id, idx, title, audio_path, duration, section_idx, section_title),
            )
            return cur.lastrowid

    def add_chapter_with_alignments(self, book_id, idx, title, audio_path, duration,
                                    section_idx, section_title, alignments):
        """Store a finished track and its sentence alignment atomically."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chapters (book_id, idx, title, audio_path, duration,"
                " section_idx, section_title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (book_id, idx, title, audio_path, duration, section_idx, section_title),
            )
            conn.executemany(
                "INSERT INTO sentence_alignments (book_id, chapter_idx, sent_idx, page_no,"
                " bbox, text, audio_start, audio_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [self._align_row(book_id, idx, i, item) for i, item in enumerate(alignments)],
            )

    def get_chapters(self, book_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chapters WHERE book_id=? ORDER BY idx ASC", (book_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_chapters(self, book_id):
        self.truncate_book(book_id, 0)

    def valid_chapter_prefix(self, book_id):
        """Number of leading tracks (0..k-1) that exist, in order, on disk."""
        k = 0
        for ch in self.get_chapters(book_id):
            if ch["idx"] != k or not os.path.exists(ch["audio_path"]) or ch["duration"] <= 0:
                break
            k += 1
        return k

    def truncate_book(self, book_id, keep):
        """Drop tracks/alignments with index >= keep (before resuming)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chapters WHERE book_id=? AND idx>=?", (book_id, keep))
            conn.execute(
                "DELETE FROM sentence_alignments WHERE book_id=? AND chapter_idx>=?",
                (book_id, keep),
            )

    # ------------------------------------------------ read-along alignment
    @staticmethod
    def _align_row(book_id, chapter_idx, i, item):
        return (
            book_id, chapter_idx, i, item.get("page_no", 0),
            json.dumps(item.get("bbox")), item.get("text", ""),
            item.get("audio_start", 0.0), item.get("audio_end", 0.0),
        )

    def add_sentence_alignments(self, book_id, chapter_idx, items):
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO sentence_alignments (book_id, chapter_idx, sent_idx, page_no,"
                " bbox, text, audio_start, audio_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [self._align_row(book_id, chapter_idx, i, it) for i, it in enumerate(items)],
            )

    def get_sentence_alignments(self, book_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sentence_alignments WHERE book_id=? "
                "ORDER BY chapter_idx ASC, sent_idx ASC",
                (book_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_alignment_geometry(self, updates):
        """updates: iterable of (row_id, page_no, bbox_json)."""
        with self._connect() as conn:
            conn.executemany(
                "UPDATE sentence_alignments SET page_no=?, bbox=? WHERE id=?",
                [(p, b, i) for i, p, b in updates],
            )

    def clear_sentence_alignments(self, book_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM sentence_alignments WHERE book_id=?", (book_id,))

    # ---------------------------------------------------------- bookmarks
    def add_bookmark(self, book_id, chapter_idx, position, note=""):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO bookmarks (book_id, chapter_idx, position, note) VALUES (?, ?, ?, ?)",
                (book_id, chapter_idx, position, note),
            )
            return cur.lastrowid

    def list_bookmarks(self, book_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bookmarks WHERE book_id=? ORDER BY chapter_idx ASC, position ASC",
                (book_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_bookmark(self, bookmark_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM bookmarks WHERE id=?", (bookmark_id,))
