"""SoundDevice-based audio backend with time-stretch playback.

Replaces pygame.mixer.music so the player can change speed on the fly
(rate 0.5x - 2.5x) while keeping pitch-aware linear interpolation and
smooth resume. Audio is decoded once into memory; the output callback
resamples with linear interpolation at `rate`.
"""
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

MIN_RATE = 0.25
MAX_RATE = 4.0
FADE_SECONDS = 0.03


class SoundDevicePlayer:
    """Streams one WAV in memory with variable playback rate."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = None          # (N,) mono or (N, C) float32
        self._sr = 48000
        self._rate = 1.0
        self._cursor = 0.0         # input frame position (float)
        self._stream = None
        self._paused = False
        self._finished = False
        self._fade_in_left = 0     # output frames remaining for fade-in
        self._fade_out_left = 0    # output frames remaining for fade-out
        self._fade_out_total = 0

    # ------------------------------------------------------------ helpers
    @property
    def rate(self):
        return self._rate

    @property
    def is_active(self):
        return self._stream is not None and self._stream.active

    @property
    def position(self):
        with self._lock:
            if self._data is None:
                return 0.0
            return self._cursor / self._sr

    def set_rate(self, rate):
        rate = float(rate)
        if not MIN_RATE <= rate <= MAX_RATE:
            raise ValueError(f"Rate must be between {MIN_RATE} and {MAX_RATE}")
        with self._lock:
            self._rate = rate
            if self._stream is not None and self._stream.active:
                self._restart_stream()

    def play(self, path, position=0.0):
        self.stop()
        data, sr = sf.read(path, dtype="float32", always_2d=False)
        if data.size == 0:
            raise ValueError(f"Audio file is empty: {path}")
        with self._lock:
            self._data = data
            self._sr = int(sr)
            self._cursor = max(0.0, float(position) * sr)
            self._paused = False
            self._finished = False
            self._fade_in_left = int(FADE_SECONDS * self._output_sr())
            self._fade_out_left = 0
        self._restart_stream()

    def pause(self):
        with self._lock:
            if not self.is_active or self._paused:
                return
            self._paused = True
        self._stream.stop()

    def resume(self):
        with self._lock:
            if not self._paused:
                return
            self._paused = False
            self._fade_in_left = int(FADE_SECONDS * self._output_sr())
            self._restart_stream()

    def stop(self):
        with self._lock:
            stream = self._stream
            self._stream = None
            self._data = None
            self._paused = False
            self._finished = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass

    def _output_sr(self):
        return int(round(self._sr * self._rate))

    def _restart_stream(self):
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        data = self._data
        if data is None:
            return
        channels = data.shape[1] if data.ndim > 1 else 1
        self._stream = sd.OutputStream(
            samplerate=self._output_sr(),
            channels=channels,
            dtype="float32",
            callback=self._callback,
            latency="low",
        )
        self._stream.start()

    # ------------------------------------------------------------ callback
    def _callback(self, outdata, frames, time_info, status):  # noqa: ARG002
        data = self._data
        if data is None:
            outdata.fill(0.0)
            return
        with self._lock:
            if self._paused or self._finished:
                outdata.fill(0.0)
                return
            sr = self._sr
            rate = self._rate
            cursor = self._cursor
        step = 1.0 / rate
        idx = cursor + step * np.arange(frames, dtype=np.float64)
        n = len(data)
        if idx[0] >= n:
            outdata.fill(0.0)
            with self._lock:
                self._finished = True
                self._cursor = float(n)
            return
        idx = np.clip(idx, 0, n - 1)
        i0 = idx.astype(np.int64)
        frac = (idx - i0).astype(np.float32)
        i1 = np.minimum(i0 + 1, n - 1)
        if data.ndim == 1:
            out = data[i0] * (1.0 - frac) + data[i1] * frac
            out = out[:, None]
        else:
            out = np.empty((frames, data.shape[1]), dtype=np.float32)
            for c in range(data.shape[1]):
                out[:, c] = data[i0, c] * (1.0 - frac) + data[i1, c] * frac
        with self._lock:
            self._cursor = cursor + step * frames
            if self._cursor >= n:
                self._finished = True
            fade_in = self._fade_in_left
            fade_out = self._fade_out_left
            if fade_in > 0:
                nf = min(fade_in, frames)
                gain = np.linspace(0.0, 1.0, nf, dtype=np.float32)
                out[:nf] *= gain[:, None]
                self._fade_in_left = max(0, fade_in - frames)
            if fade_out > 0:
                nf = min(fade_out, frames)
                gain = np.linspace(1.0, 0.0, nf, dtype=np.float32)
                out[-nf:] *= gain[:, None]
                self._fade_out_left = max(0, fade_out - frames)
            if self._finished and self._fade_out_left == 0:
                self._schedule_stop()
        outdata[:] = out[:frames]

    def _schedule_stop(self):
        """Close the stream shortly after the last sample (via timer)."""
        def _close():
            with self._lock:
                stream = self._stream
                self._stream = None
            if stream is not None and stream.active:
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Timer(0.2, _close).start()
