"""Build the standalone .exe with PyInstaller.

Writes a full build log to build_log.txt next to this script's project.

Usage:
    python scripts/build_exe.py
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
        "--windowed",
        "--name",
        "PDF2Audiobook",
        "--icon",
        os.path.join(ROOT, "assets", "icon.ico"),
        "--add-data",
        os.path.join(ROOT, "assets") + os.pathsep + "assets",
        "--collect-all",
        "piper",
        "--collect-all",
        "piper_phonemize",
        "--collect-all",
        "onnxruntime",
        os.path.join(ROOT, "run.py"),
    ]
    print("Running:", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)

    log_path = os.path.join(ROOT, "build_log.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("STDOUT:\n" + result.stdout + "\n\nSTDERR:\n" + result.stderr)

    exe = os.path.join(ROOT, "dist", "PDF2Audiobook", "PDF2Audiobook.exe")
    print("returncode:", result.returncode)
    print("stdout bytes:", len(result.stdout), "| stderr bytes:", len(result.stderr))
    print("exe exists:", os.path.exists(exe))
    if os.path.exists(exe):
        print("exe size MB:", round(os.path.getsize(exe) / 1e6, 1))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
