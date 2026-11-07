"""Chapter playback facade over the sounddevice backend."""
import os

from .audio_backend import SoundDevicePlayer


class AudioPlayer:
    """Plays one track file at a time with seek, pause and speed."""

    def __init__(self):
        self.current_path = None
        self._backend = SoundDevicePlayer()

    def play(self, path, position=0.0):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self._backend.play(path, position=position)
        self.current_path = path

    def play_array(self, data, sample_rate):
        self._backend.play_array(data, sample_rate)
        self.current_path = "<memory>"

    def seek(self, position):
        self._backend.seek(position)

    def pause(self):
        self._backend.pause()

    def resume(self):
        self._backend.resume()

    def stop(self):
        self._backend.stop()
        self.current_path = None

    @property
    def is_loaded(self):
        return self.current_path is not None and self._backend.is_loaded

    @property
    def is_paused(self):
        return self._backend.is_paused

    @property
    def is_finished(self):
        return self.current_path is not None and self._backend.is_finished

    @property
    def is_playing(self):
        return (
            self.current_path is not None
            and self._backend.is_active
            and not self._backend.is_paused
            and not self._backend.is_finished
        )

    @property
    def position(self):
        return self._backend.position

    @property
    def duration(self):
        return self._backend.duration

    @property
    def rate(self):
        return self._backend.rate

    def set_rate(self, rate):
        self._backend.set_rate(rate)
