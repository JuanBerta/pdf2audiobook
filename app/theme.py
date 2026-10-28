"""Application themes (QSS stylesheets) for PDF2Audiobook.

Provides a polished dark theme (default) and a light theme, plus a helper to
apply the chosen stylesheet to the whole application.
"""
from PyQt6.QtWidgets import QApplication

ACCENT = "#2ecc71"
ACCENT_DIM = "#1f8f52"

_DARK_BG = "#1e1f26"
_DARK_SURFACE = "#272932"
_DARK_SURFACE_2 = "#32343f"
_DARK_TEXT = "#e8e9ed"
_DARK_MUTED = "#9aa0b0"

_LIGHT_BG = "#f4f5f7"
_LIGHT_SURFACE = "#ffffff"
_LIGHT_SURFACE_2 = "#ececf1"
_LIGHT_TEXT = "#1c1d21"
_LIGHT_MUTED = "#6b6f7a"


def _qss(bg, surface, surface2, text, muted):
    return f"""
* {{
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
    color: {text};
}}
QMainWindow, QDialog {{
    background-color: {bg};
}}
QWidget {{
    background-color: transparent;
}}
QLabel {{
    color: {text};
    background: transparent;
}}
QLabel#nowPlayingLabel {{
    font-size: 14px;
    font-weight: 600;
    padding: 6px 8px;
}}
QLabel#mutedLabel {{
    color: {muted};
}}
QPushButton {{
    background-color: {surface2};
    border: 1px solid {surface2};
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 24px;
}}
QPushButton:hover {{
    background-color: {surface};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT_DIM};
}}
QPushButton:checked {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
    color: #ffffff;
}}
QPushButton#primaryButton {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT_DIM};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#primaryButton:hover {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QLineEdit {{
    background-color: {surface2};
    border: 1px solid {surface2};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QListWidget {{
    background-color: {surface};
    border: 1px solid {surface2};
    border-radius: 8px;
    padding: 6px;
    outline: none;
}}
QListWidget::item {{
    color: {text};
    border-radius: 6px;
    padding: 4px;
}}
QListWidget::item:hover {{
    background-color: {surface2};
}}
QListWidget::item:selected {{
    background-color: {ACCENT_DIM};
    color: #ffffff;
}}
QSlider::groove:horizontal {{
    height: 5px;
    background: {surface2};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
    background: #ffffff;
    border: 2px solid {ACCENT};
}}
QProgressBar {{
    background-color: {surface2};
    border: 1px solid {surface2};
    border-radius: 6px;
    height: 14px;
    text-align: center;
    color: {text};
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}
QMenuBar {{
    background-color: {surface};
}}
QMenuBar::item {{
    padding: 4px 10px;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background-color: {ACCENT_DIM};
}}
QMenu {{
    background-color: {surface};
    border: 1px solid {surface2};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 24px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {ACCENT_DIM};
    color: #ffffff;
}}
QStatusBar {{
    background-color: {surface};
    color: {muted};
    border-top: 1px solid {surface2};
}}
QToolTip {{
    background-color: {surface2};
    color: {text};
    border: 1px solid {ACCENT};
    padding: 4px 6px;
}}
QMessageBox {{
    background-color: {surface};
}}
"""


def apply_theme(app: QApplication, mode: str = "dark") -> None:
    """Apply the requested theme stylesheet to the app instance."""
    if mode == "light":
        app.setStyleSheet(
            _qss(_LIGHT_BG, _LIGHT_SURFACE, _LIGHT_SURFACE_2, _LIGHT_TEXT, _LIGHT_MUTED)
        )
    else:
        app.setStyleSheet(
            _qss(_DARK_BG, _DARK_SURFACE, _DARK_SURFACE_2, _DARK_TEXT, _DARK_MUTED)
        )
