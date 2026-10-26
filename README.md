# PDF2Audiobook

Turn PDF documents into listenable audiobooks with a local desktop app.

Select any PDF in the UI, and the app extracts its text, splits it into
paragraph-sized chunks, and synthesizes speech with the **Piper** neural TTS
engine. Converted books land in a local gallery (SQLite-backed) where you can
play, pause, skip chapters, and resume from your last listening position.

Everything runs **offline and locally** - no accounts, no cloud uploads.

![icon](assets/icon.png)

## Features

- PDF -> audiobook conversion with live progress
- Gallery of converted audiobooks (SQLite library)
- Chapter-based playback with resume (remembers where you stopped)
- Local neural text-to-speech (Piper, runs entirely on your machine)
- Multiple voice models supported (Chinese, English, Spanish, ...)
- Packaged as a standalone Windows executable

## Tech stack

| Layer      | Choice                              |
|------------|-------------------------------------|
| PDF text   | PyMuPDF (fitz)                      |
| TTS        | Piper (piper-tts, ONNX neural TTS)  |
| Playback   | pygame.mixer                        |
| UI         | PyQt6                               |
| Library    | SQLite                              |
| Packaging  | PyInstaller                         |

## Install & run from source

```bash
pip install -r requirements.txt

# Download one or more voice models (Chinese by default):
python scripts/download_models.py
python scripts/download_models.py --voice en_US-lessac-medium
python scripts/download_models.py --voice es_ES-davefx-medium

# Optional: generate the app icon
python scripts/make_icon.py

# Launch
python run.py
```

Voice models are stored in `%LOCALAPPDATA%\PDF2Audiobook\models`.
Available voices - see `scripts/download_models.py`:

- Chinese: `zh_CN-huayan-medium` (default)
- English: `en_US-lessac-medium`, `en_US-amy-medium`, `en_GB-alba-medium`
- Spanish: `es_ES-davefx-medium`, `es_MX-claude-high`

Each model is ~60 MB; download as many as you like. Every model present in
the models folder shows up in the conversion dialog, where you pick which
voice will narrate the audiobook. The chosen voice is saved with the book
and shown in the library.

## Usage

1. Click **Convert PDF to audiobook** and pick a PDF
2. Give it a title and (optionally) an author
3. Choose the voice model for the narration (every downloaded model is listed)
4. Wait for the conversion to finish - each paragraph chunk becomes a chapter
5. Double-click the book in the gallery to start listening
6. Close and reopen anytime: playback continues where you left off

## Build a standalone .exe

```powershell
python scripts/build_exe.py
```

This runs PyInstaller and writes a full log to `build_log.txt`. The
executable appears at `dist\PDF2Audiobook\PDF2Audiobook.exe` (a folder
distribution; the exe sits alongside its `_internal` dependencies).

Voice models are **not** bundled; run `scripts/download_models.py` once on the
target machine (or drop `.onnx` + `.onnx.json` files into the models folder).

## Project layout

```
app/
  main.py        # PyQt6 desktop app (gallery + player + conversion)
  converter.py   # PDF extraction, chunking, Piper synthesis
  database.py    # SQLite library
  player.py      # pygame playback
  worker.py      # background conversion thread
run.py           # application entry point
scripts/
  build_exe.py        # PyInstaller packaging
  download_models.py  # fetch Piper voices from Hugging Face
  make_icon.py        # generate the app icon
  make_test_pdf.py    # create a sample PDF for testing
sample.pdf       # small sample document used by tests
```

## License

MIT - see [LICENSE](LICENSE). The app icon is generated programmatically by
`scripts/make_icon.py` and is also MIT-licensed.

> Convert only documents you have the right to use.
