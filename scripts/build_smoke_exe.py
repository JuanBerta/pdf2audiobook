"""Build a console smoke-test exe for the packaged environment.

Verifies the PyMuPDF + Piper stack works inside a PyInstaller bundle.
Output: dist\\PDF2AudiobookSmoke\\PDF2AudiobookSmoke.exe

Usage:
    python scripts/build_smoke_exe.py
    dist\\PDF2AudiobookSmoke\\PDF2AudiobookSmoke.exe
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--console",
        "--name",
        "PDF2AudiobookSmoke",
        "--collect-all",
        "pymupdf",
        "--collect-all",
        "piper",
        "--collect-all",
        "piper_phonemize",
        "--collect-all",
        "onnxruntime",
        os.path.join(ROOT, "smoke_entry.py"),
    ]
    print("Running:", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)

    log_path = os.path.join(ROOT, "build_log.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("STDOUT:\n" + result.stdout + "\n\nSTDERR:\n" + result.stderr)

    exe = os.path.join(ROOT, "dist", "PDF2AudiobookSmoke", "PDF2AudiobookSmoke.exe")
    print("returncode:", result.returncode, "| stderr bytes:", len(result.stderr))
    print("exe exists:", os.path.exists(exe))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
