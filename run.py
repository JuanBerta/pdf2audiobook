"""Entry point used by PyInstaller and direct runs.

Run from source:  python run.py
"""
import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
