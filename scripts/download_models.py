"""Download Piper voice models from Hugging Face.

Usage:
    python scripts/download_models.py                 # Chinese (default)
    python scripts/download_models.py --voice en_US-lessac-medium
    python scripts/download_models.py --voice en_US-amy-medium
    python scripts/download_models.py --out custom/path
"""
import argparse
import os
import sys
import urllib.request

# Piper voices live under rhasspy/piper-voices (v1.0.0 tag, onnx + json).
VOICES = {
    # Chinese (Mandarin)
    "zh_CN-huayan-medium": "zh/zh_CN/huayan/medium",
    # English
    "en_US-lessac-medium": "en/en_US/lessac/medium",
    "en_US-amy-medium": "en/en_US/amy/medium",
    "en_GB-alba-medium": "en/en_GB/alba/medium",
    # Spanish
    "es_ES-davefx-medium": "es/es_ES/davefx/medium",
    "es_MX-claude-high": "es/es_MX/claude/high",
}

BASE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
)


def default_out_dir():
    local = os.environ.get("LOCALAPPDATA")
    base = local or os.path.expanduser("~")
    return os.path.join(base, "PDF2Audiobook", "models")


def download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  {os.path.basename(dest)} ...")
    urllib.request.urlretrieve(url, dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", default="zh_CN-huayan-medium")
    parser.add_argument("--out", default=default_out_dir())
    args = parser.parse_args()

    if args.voice not in VOICES:
        print(f"Unknown voice '{args.voice}'. Available:")
        for v in sorted(VOICES):
            print(f"  {v}")
        sys.exit(1)

    folder = VOICES[args.voice]
    files = [f"{args.voice}.onnx", f"{args.voice}.onnx.json"]
    print(f"Downloading {args.voice} to {args.out}")
    for fname in files:
        url = f"{BASE_URL}/{folder}/{fname}"
        dest = os.path.join(args.out, fname)
        if os.path.exists(dest):
            print(f"  {fname} already exists, skipping")
            continue
        download(url, dest)
    print("Done.")


if __name__ == "__main__":
    main()
