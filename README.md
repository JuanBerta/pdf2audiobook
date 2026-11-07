# PDF2Audiobook

Turn PDF documents into listenable audiobooks with a local desktop app.

Pick one or more PDFs, choose a voice, and the app extracts the text, splits
it into chapters and narrates it with the **Piper** neural TTS engine.
Finished books land in a local library where you can listen, resume where
you stopped, follow along in the PDF, and export a standard M4B audiobook.

Everything runs **offline and locally** - no accounts, no cloud uploads
(only the optional voice downloads and update check use the internet).

![icon](assets/icon.png)

## Features

- **Conversion queue** - drop PDFs on the window or use *Convert PDFs…*; one
  dialog sets titles, author, voice (with a ▶ preview), narration speed,
  audio format and text options. Add more books while a conversion runs.
- **Real chapters** - taken from the PDF's table of contents, or detected
  from large-font headings; long chapters are split into tracks.
- **Clean narration** - running headers, footers and page numbers are
  skipped, hyphenated line breaks are re-joined, abbreviations ("Mr.",
  "e.g.") and Chinese punctuation are handled when splitting sentences.
- **Scanned PDFs** - image-only pages are read with OCR when
  [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) is installed.
- **Small files** - MP3 by default (about 8× smaller than WAV); WAV optional.
- **Resumable** - cancel or quit mid-conversion; finished tracks are kept and
  the book continues from where it stopped (right-click → *Resume conversion*).
- **Player** - resume position, pitch-preserving speed 0.75×–3×, ±10 s
  jumps, repeat modes, bookmarks, sleep timer (minutes or end of track),
  optional system-wide media keys.
- **Read along** - *Read PDF* opens the PDF in your browser with the current
  sentence and word highlighted (each sentence is narrated separately, so
  timing is exact). Click any sentence - in the list or on the page - to jump
  there; transport buttons control playback.
- **M4B export** with chapter markers and cover art (needs ffmpeg).
- **Voice manager** - browse and download any Piper voice from inside the app.
- UI in English / Español / 中文, dark and light themes.

## Tech stack

| Layer      | Choice                                     |
|------------|--------------------------------------------|
| PDF text   | PyMuPDF (char-level `rawdict`), Tesseract   |
| TTS        | Piper (piper-tts, ONNX neural TTS)         |
| Playback   | sounddevice + streaming WSOLA time-stretch |
| Audio I/O  | soundfile (libsndfile, MP3/WAV)            |
| UI         | PyQt6; read-along page with PDF.js          |
| Library    | SQLite (WAL)                               |
| Export     | ffmpeg (M4B/AAC)                           |
| Packaging  | PyInstaller                                |

## Install & run from source

```bash
pip install -r requirements.txt
python run.py
```

On first start the app offers to open the **voice manager** to download a
voice. Voices can also be fetched from the command line:

```bash
python scripts/download_models.py --voice en_US-lessac-medium
```

Voice models are stored in `%LOCALAPPDATA%\PDF2Audiobook\models` (drop any
`.onnx` + `.onnx.json` pair there). The library, audio and logs live in
`%LOCALAPPDATA%\PDF2Audiobook`.

Optional extras: **ffmpeg** on `PATH` (or `pip install imageio-ffmpeg`) for
M4B export, and **Tesseract OCR** for scanned PDFs.

## Keyboard shortcuts

| Key                  | Action                      |
|----------------------|-----------------------------|
| Space                | Play / pause                |
| Ctrl+Left / Right    | Previous / next track       |
| Alt+Left / Right     | Back / forward 10 seconds   |
| Ctrl+Up / Down       | Faster / slower             |
| R                    | Cycle repeat mode           |
| Ctrl+B / Ctrl+Shift+B| Add bookmark / bookmarks    |
| Ctrl+O               | Convert PDFs                |

## Tests

```bash
python -m pytest -q tests
```

## Build a standalone .exe

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1   # tests + build + smoke test
# or just:
python scripts/build_exe.py
```

The executable appears at `dist\PDF2Audiobook\PDF2Audiobook.exe` (a folder
distribution; the exe sits alongside its `_internal` dependencies) and a
release zip `dist\PDF2Audiobook-v<version>-windows-x64.zip` is created. The
build log is written to `build\build_log.txt`. Voice models are **not**
bundled.

## Project layout

```
app/
  main.py          # main window: library, player, queue, menus
  dialogs.py       # convert dialog, voice manager, bookmarks, edit details
  textmodel.py     # PDF extraction, cleanup, chapters, OCR, sentence locator
  converter.py     # planning, Piper synthesis, resume, realign, M4B export
  audio_backend.py # sounddevice output with WSOLA speed control
  player.py        # playback facade
  worker.py        # conversion queue thread + generic task thread
  database.py      # SQLite library (+ migrations)
  browser_pdf.py   # local read-along server (token protected)
  voices.py        # Piper voice catalog / downloads
  mediakeys.py     # optional global media keys (Windows)
  updater.py       # GitHub release check
  i18n.py          # UI localization (en / es / zh)
  theme.py         # dark / light stylesheets
assets/readalong/  # browser reader page + PDF.js
scripts/           # build, voice download, icon generation
tests/             # pytest suite
```

## License

MIT - see [LICENSE](LICENSE). The app icon is generated programmatically by
`scripts/make_icon.py` and is also MIT-licensed.

> Convert only documents you have the right to use.
