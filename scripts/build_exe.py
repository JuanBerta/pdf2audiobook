"""Build the standalone Windows app with PyInstaller.

Produces a folder distribution ``dist\\PDF2Audiobook\\PDF2Audiobook.exe``
(the exe sits next to its ``_internal`` folder) plus a release zip
``dist\\PDF2Audiobook-v<version>-windows-x64.zip``. The full PyInstaller log
is written to ``build\\build_log.txt``.

Usage:
    python scripts/build_exe.py            # build + zip
    python scripts/build_exe.py --no-zip   # build only
"""
import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "PDF2Audiobook"
# Never needed at runtime; keeps the bundle small even in a "fat" Python.
EXCLUDES = [
    "torch", "torchvision", "torchaudio", "tensorflow", "keras", "transformers",
    "scipy", "pandas", "matplotlib", "cv2", "sklearn", "sympy", "numba", "jax",
    "IPython", "jupyter", "notebook", "tkinter", "PIL", "onnx",
    "pymupdf.layout", "pymupdf_layout", "pymupdf4llm", "pytest",
]


def app_version():
    with open(os.path.join(ROOT, "app", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    version = app_version()
    build_dir = os.path.join(ROOT, "build")
    os.makedirs(build_dir, exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--icon", os.path.join(ROOT, "assets", "icon.ico"),
        "--specpath", build_dir,
        "--workpath", os.path.join(build_dir, "work"),
        "--distpath", os.path.join(ROOT, "dist"),
        "--add-data", os.path.join(ROOT, "assets") + os.pathsep + "assets",
        "--hidden-import", "sounddevice",
        "--hidden-import", "soundfile",
        "--hidden-import", "pymupdf",
        # PyMuPDF: binaries + data only. "--collect-all pymupdf" would import
        # every submodule, and pymupdf.layout pulls in heavy ML stacks
        # (torch/tensorflow/...) when they happen to be installed.
        "--collect-binaries", "pymupdf",
        "--collect-data", "pymupdf",
    ]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    for pkg in ("piper", "piper_phonemize", "onnxruntime", "espeakbridge"):
        if importlib.util.find_spec(pkg) is not None:
            cmd += ["--collect-all", pkg]
    for pkg in ("imageio_ffmpeg",):  # optional bundled ffmpeg for M4B export
        if importlib.util.find_spec(pkg) is not None:
            cmd += ["--collect-all", pkg]
    cmd.append(os.path.join(ROOT, "run.py"))
    print(f"Building {NAME} {version}")
    print("Running:", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    log_path = os.path.join(build_dir, "build_log.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("STDOUT:\n" + result.stdout + "\n\nSTDERR:\n" + result.stderr)

    exe = os.path.join(ROOT, "dist", NAME, f"{NAME}.exe")
    print("returncode:", result.returncode, "| log:", log_path)
    if result.returncode != 0 or not os.path.exists(exe):
        print(result.stderr[-3000:])
        return result.returncode or 1
    print("exe:", exe)

    if not args.no_zip:
        base = os.path.join(ROOT, "dist", f"{NAME}-v{version}-windows-x64")
        if os.path.exists(base + ".zip"):
            os.remove(base + ".zip")
        shutil.make_archive(base, "zip", os.path.join(ROOT, "dist"), NAME)
        print("zip:", base + ".zip", f"({os.path.getsize(base + '.zip') / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
