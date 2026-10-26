"""SQLite storage for the audiobook library."""
import os
import sqlite3


class AudiobookDB:
    """Thin SQLite wrapper storing books and their audio chapters."""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
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
                """
            )
            # Migration: older libraries lack the voice_model column.
            cols = [row[1] for row in conn.execute("PRAGMA table_info(books)")]
            if "voice_model" not in cols:
                conn.execute(
                    "ALTER TABLE books ADD COLUMN voice_model TEXT NOT NULL DEFAULT ''"
                )

    def add_book(self, title, author="", source_pdf="", cover_path="", voice_model=""):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO books (title, author, source_pdf, cover_path, voice_model) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, author, source_pdf, cover_path, voice_model),
            )
            return cur.lastrowid

    def add_chapter(self, book_id, idx, title, audio_path, duration=0.0):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO chapters (book_id, idx, title, audio_path, duration) "
                "VALUES (?, ?, ?, ?, ?)",
                (book_id, idx, title, audio_path, duration),
            )
            return cur.lastrowid

    def finalize_book(self, book_id, total_chapters, total_duration):
        with self._connect() as conn:
            conn.execute(
                "UPDATE books SET total_chapters=?, total_duration=? WHERE id=?",
                (total_chapters, total_duration, book_id),
            )

    def list_books(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT b.*, COUNT(c.id) AS chapter_count FROM books b "
                "LEFT JOIN chapters c ON c.book_id = b.id "
                "GROUP BY b.id ORDER BY b.created_at DESC, b.id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_book(self, book_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM books WHERE id=?", (book_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_chapters(self, book_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chapters WHERE book_id=? ORDER BY idx ASC",
                (book_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_progress(self, book_id, chapter_idx, position):
        with self._connect() as conn:
            conn.execute(
                "UPDATE books SET current_chapter=?, position=? WHERE id=?",
                (chapter_idx, position, book_id),
            )

    def delete_book(self, book_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM books WHERE id=?", (book_id,))
