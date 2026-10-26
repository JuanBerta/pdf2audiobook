"""Audio playback via pygame."""
import os
import time

import pygame


class AudioPlayer:
    """Wraps pygame.mixer.music for chapter playback with seek support."""

    def __init__(self):
        if not pygame.mixer.get_init():
            try:
                pygame.mixer.init()
            except pygame.error:
                # Fall back without audio device (e.g. CI / headless runs).
                pass
        self.current_path = None
        self._base_pos = 0.0
        self._start_time = 0.0
        self._paused_at = 0.0
        self._paused = False
        self._audio_ok = pygame.mixer.get_init() is not None

    def play(self, path, position=0.0):
        if not self._audio_ok:
            raise RuntimeError("No audio device available")
        pygame.mixer.music.stop()
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        pygame.mixer.music.load(path)
        pygame.mixer.music.play(start=position)
        self.current_path = path
        self._base_pos = position
        self._start_time = time.time()
        self._paused = False
        self._paused_at = 0.0

    def pause(self):
        if self._paused or not self._audio_ok:
            return
        if pygame.mixer.music.get_busy():
            self._paused_at = self.position
            pygame.mixer.music.pause()
            self._paused = True

    def resume(self):
        if not self._paused or not self._audio_ok:
            return
        pygame.mixer.music.unpause()
        self._start_time = time.time()
        self._paused = False

    def stop(self):
        if self._audio_ok:
            pygame.mixer.music.stop()
        self.current_path = None
        self._paused = False

    @property
    def is_playing(self):
        return self._audio_ok and bool(pygame.mixer.music.get_busy())

    @property
    def position(self):
        if self._paused:
            return self._paused_at
        if self.current_path is None or not self._audio_ok:
            return 0.0
        return self._base_pos + (time.time() - self._start_time)
