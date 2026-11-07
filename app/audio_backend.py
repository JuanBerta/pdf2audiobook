"""sounddevice-based audio backend with pitch-preserving variable speed.

Audio is decoded once into memory. At 1.0x the samples are copied straight
through; at any other speed a streaming WSOLA (waveform-similarity
overlap-add) time-stretcher changes the tempo while keeping the voice's
pitch, so 1.5x/2x narration does not sound like chipmunks.

Stream lifecycle notes:
* When the end of the audio is reached the callback raises
  ``sd.CallbackStop``; PortAudio plays the final buffer and deactivates the
  stream itself. (The old implementation closed "the current stream" from a
  timer, which killed the *next* chapter's stream whenever auto-advance
  started it within that window - chapters were skipped.)
* Speed changes and seeks only update state used by the callback; no
  stream restart or file reload is needed.
"""
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

MIN_RATE = 0.5
MAX_RATE = 3.0
FADE_SECONDS = 0.02


class _Wsola:
    """Streaming WSOLA time-stretcher over an in-memory signal."""

    def __init__(self, data, sr):
        self.data = data if data.ndim > 1 else data[:, None]
        self.mono = self.data.mean(axis=1) if self.data.shape[1] > 1 else self.data[:, 0]
        self.n = len(self.mono)
        frame = 1
        while frame < sr * 0.04:          # ~40 ms analysis frames
            frame *= 2
        self.W = frame
        self.H = frame // 2               # 50 % overlap
        self.tol = self.H // 2            # similarity search radius
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(self.W) / self.W)).astype(np.float32)
        self.reset(0.0)

    def reset(self, pos):
        self.pos = float(pos)
        self.prev = None
        self.ola = np.zeros((self.W, self.data.shape[1]), dtype=np.float32)
        self.fifo = np.zeros((0, self.data.shape[1]), dtype=np.float32)
        self.flushed = False

    @property
    def done(self):
        return self.flushed and len(self.fifo) == 0

    def _segment(self, start):
        seg = self.data[start:start + self.W]
        if len(seg) < self.W:
            pad = np.zeros((self.W - len(seg), self.data.shape[1]), dtype=np.float32)
            seg = np.concatenate([seg, pad])
        return seg

    def _frame(self, rate):
        nominal = int(round(self.pos))
        if nominal >= self.n:
            if not self.flushed:
                self.fifo = np.concatenate([self.fifo, self.ola[: self.W - self.H]])
                self.ola[:] = 0
                self.flushed = True
            return
        start = nominal
        if self.prev is not None:
            target = self.prev + self.H
            lo = max(0, nominal - self.tol)
            hi = min(self.n - self.W, nominal + self.tol)
            if hi > lo and target + self.W <= self.n:
                tmpl = self.mono[target:target + self.W]
                region = self.mono[lo:hi + self.W]
                corr = np.correlate(region, tmpl, mode="valid")
                start = lo + int(np.argmax(corr))
        self.ola += self._segment(start) * self.win[:, None]
        self.fifo = np.concatenate([self.fifo, self.ola[: self.H]])
        self.ola[: self.W - self.H] = self.ola[self.H:]
        self.ola[self.W - self.H:] = 0
        self.prev = start
        self.pos += self.H * rate

    def read(self, frames, rate):
        while len(self.fifo) < frames and not self.flushed:
            self._frame(rate)
        out = self.fifo[:frames]
        self.fifo = self.fifo[frames:]
        return out


class SoundDevicePlayer:
    """Plays one decoded file with seek, pause and variable speed."""

    def __init__(self):
        self._lock = threading.RLock()
        self._data = None          # (N, C) float32
        self._sr = 22050
        self._rate = 1.0
        self._cursor = 0.0         # input frame position
        self._stream = None
        self._paused = False
        self._finished = False
        self._fade_in_left = 0
        self._wsola = None

    # ------------------------------------------------------------ state
    @property
    def rate(self):
        return self._rate

    @property
    def is_active(self):
        s = self._stream
        return s is not None and s.active

    @property
    def is_paused(self):
        return self._paused

    @property
    def is_finished(self):
        return self._finished

    @property
    def is_loaded(self):
        return self._data is not None

    @property
    def duration(self):
        with self._lock:
            if self._data is None:
                return 0.0
            return len(self._data) / self._sr

    @property
    def position(self):
        with self._lock:
            if self._data is None:
                return 0.0
            pos = self._wsola.pos if self._rate != 1.0 and self._wsola else self._cursor
            return min(pos, len(self._data)) / self._sr

    # ---------------------------------------------------------- control
    def set_rate(self, rate):
        rate = float(rate)
        if not MIN_RATE <= rate <= MAX_RATE:
            raise ValueError(f"Rate must be between {MIN_RATE} and {MAX_RATE}")
        with self._lock:
            if self._data is not None:
                cur = self._current_pos_frames()
                self._cursor = cur
                if self._wsola is not None:
                    self._wsola.reset(cur)
            self._rate = rate

    def _current_pos_frames(self):
        if self._rate != 1.0 and self._wsola is not None:
            return self._wsola.pos
        return self._cursor

    def load(self, path):
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        if data.size == 0:
            raise ValueError(f"Audio file is empty: {path}")
        return data, int(sr)

    def play(self, path, position=0.0):
        data, sr = self.load(path)
        self.play_array(data, sr, position)

    def play_array(self, data, sr, position=0.0):
        data = np.asarray(data, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, None]
        self.stop()
        with self._lock:
            self._data = data
            self._sr = int(sr)
            self._wsola = _Wsola(data, self._sr)
            self._cursor = min(max(0.0, float(position) * sr), float(len(data)))
            self._wsola.reset(self._cursor)
            self._paused = False
            self._finished = False
            self._fade_in_left = int(FADE_SECONDS * self._sr)
        self._start_stream()

    def seek(self, position):
        """Jump within the loaded audio without reloading it."""
        with self._lock:
            if self._data is None:
                return
            self._cursor = min(max(0.0, float(position) * self._sr), float(len(self._data) - 1))
            self._wsola.reset(self._cursor)
            self._fade_in_left = int(FADE_SECONDS * self._sr)
            was_finished = self._finished
            self._finished = False
            paused = self._paused
        if not paused and (was_finished or not self.is_active):
            self._start_stream()

    def pause(self):
        with self._lock:
            if self._data is None or self._paused or self._finished:
                return
            self._paused = True
            stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:  # noqa: BLE001
                pass

    def resume(self):
        with self._lock:
            if not self._paused or self._data is None:
                return
            self._paused = False
            self._fade_in_left = int(FADE_SECONDS * self._sr)
            self._cursor = self._current_pos_frames()
            self._wsola.reset(self._cursor)
        self._start_stream()

    def stop(self):
        with self._lock:
            stream = self._stream
            self._stream = None
            self._data = None
            self._wsola = None
            self._paused = False
            self._finished = False
        self._close(stream)

    @staticmethod
    def _close(stream):
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _start_stream(self):
        with self._lock:
            old = self._stream
            self._stream = None
            data = self._data
        self._close(old)
        if data is None:
            return
        stream = sd.OutputStream(
            samplerate=self._sr,
            channels=data.shape[1],
            dtype="float32",
            callback=self._callback,
            latency="high",
        )
        with self._lock:
            self._stream = stream
        stream.start()

    # ------------------------------------------------------------ callback
    def _callback(self, outdata, frames, time_info, status):  # noqa: ARG002
        with self._lock:
            data = self._data
            if data is None or self._paused or self._finished:
                outdata.fill(0.0)
                if data is None or self._finished:
                    raise sd.CallbackStop()
                return
            n = len(data)
            if self._rate == 1.0:
                start = int(self._cursor)
                chunk = data[start:start + frames]
                self._cursor = float(start + len(chunk))
                end = self._cursor >= n
            else:
                chunk = self._wsola.read(frames, self._rate)
                self._cursor = self._wsola.pos
                end = self._wsola.done
            got = len(chunk)
            if got:
                out = chunk.copy()
                if self._fade_in_left > 0:
                    nf = min(self._fade_in_left, got)
                    out[:nf] *= np.linspace(0.0, 1.0, nf, dtype=np.float32)[:, None]
                    self._fade_in_left -= nf
                outdata[:got] = out
            if got < frames:
                outdata[got:] = 0.0
            if end:
                self._finished = True
                self._cursor = float(n)
                raise sd.CallbackStop()
