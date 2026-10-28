"""Audio playback using the sounddevice time-stretch backend."""
import os

from .audio_backend import SoundDevicePlayer


class AudioPlayer:
    """Wraps SoundDevicePlayer for chapter playback with seek and speed."""

    def __init__(self):
        self.current_path = None
        self._backend = SoundDevicePlayer()

    def play(self, path, position=0.0):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self._backend.play(path, position=position)
        self.current_path = path

    def pause(self):
        if not self.is_playing:
            return
        self._backend.pause()

    def resume(self):
        if not self.is_paused:
            return
        self._backend.resume()

    def stop(self):
        self._backend.stop()
        self.current_path = None

    @property
    def is_paused(self):
        return self._backend._paused  # noqa: SLF001 - thin facade

    @property
    def is_playing(self):
        return (
            self.current_path is not None
            and self._backend.is_active
            and not self._backend._paused  # noqa: SLF001
            and not self._backend._finished  # noqa: SLF001
        )

    @property
    def position(self):
        return self._backend.position

    @property
    def rate(self):
        return self._backend.rate

    def set_rate(self, rate):
        self._backend.set_rate(rate)
