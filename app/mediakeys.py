"""Optional global keyboard media keys on Windows (play/pause, next, prev, stop).

Uses RegisterHotKey so the keys work while another window has focus. This
is opt-in: while registered, Windows routes these keys to PDF2Audiobook
instead of other players, so the user enables it from the Playback menu.
"""
import ctypes
import sys

from PyQt6.QtCore import QAbstractNativeEventFilter

WM_HOTKEY = 0x0312
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
_IDS = {
    0x5A01: ("toggle", VK_MEDIA_PLAY_PAUSE),
    0x5A02: ("next", VK_MEDIA_NEXT_TRACK),
    0x5A03: ("prev", VK_MEDIA_PREV_TRACK),
    0x5A04: ("stop", VK_MEDIA_STOP),
}


class GlobalMediaKeys(QAbstractNativeEventFilter):
    """Registers the media keys for a window and dispatches them to ``callback``."""

    def __init__(self, hwnd, callback):
        super().__init__()
        self._hwnd = int(hwnd)
        self._callback = callback
        self._registered = []

    @staticmethod
    def supported():
        return sys.platform == "win32"

    def register(self):
        if not self.supported():
            return False
        user32 = ctypes.windll.user32
        for hk_id, (_, vk) in _IDS.items():
            if user32.RegisterHotKey(self._hwnd, hk_id, 0, vk):
                self._registered.append(hk_id)
        return bool(self._registered)

    def unregister(self):
        if not self.supported():
            return
        user32 = ctypes.windll.user32
        for hk_id in self._registered:
            user32.UnregisterHotKey(self._hwnd, hk_id)
        self._registered = []

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt API
        try:
            if event_type == b"windows_generic_MSG" and self._registered:
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam in _IDS:
                    self._callback(_IDS[msg.wParam][0])
                    return True, 0
        except Exception:  # noqa: BLE001 - never break the event loop
            pass
        return False, 0
