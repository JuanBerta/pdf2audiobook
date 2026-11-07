import time

import numpy as np
import soundfile as sf

from app.audio_backend import SoundDevicePlayer, _Wsola

SR = 22050


def _peak_freq(y):
    seg = y[SR // 2: SR // 2 + 8192]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return np.fft.rfftfreq(len(seg), 1 / SR)[np.argmax(spec)]


def test_wsola_changes_tempo_but_not_pitch():
    t = np.arange(SR * 3) / SR
    x = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    for rate in (0.75, 1.5, 2.0):
        w = _Wsola(x, SR)
        out = []
        while not w.done:
            out.append(w.read(512, rate))
        y = np.concatenate(out)[:, 0]
        assert abs(len(x) / len(y) - rate) < 0.08
        assert abs(_peak_freq(y) - 220) < 5


def _wav(tmp_path, name, seconds=0.6):
    p = str(tmp_path / name)
    sf.write(p, np.zeros(int(SR * seconds), np.float32) + 0.01, SR)
    return p


def test_next_track_is_not_killed_after_previous_ends(tmp_path):
    # Regression: a timer started at the end of track N closed the stream of
    # track N+1 when auto-advance began within 0.2 s -> chapters skipped.
    a, b = _wav(tmp_path, "a.wav"), _wav(tmp_path, "b.wav", 2.0)
    for _ in range(10):
        p = SoundDevicePlayer()
        p.play(a)
        while not p.is_finished:
            time.sleep(0.005)
        time.sleep(0.05)
        p.play(b)
        time.sleep(0.3)
        assert p.is_active and not p.is_finished
        p.stop()


def test_seek_pause_resume(tmp_path):
    p = SoundDevicePlayer()
    p.play(_wav(tmp_path, "c.wav", 5.0))
    p.seek(2.0)
    assert 2.0 <= p.position < 2.6
    p.pause()
    pos = p.position
    time.sleep(0.1)
    assert p.position == pos and p.is_paused
    p.resume()
    time.sleep(0.1)
    assert p.position > pos
    p.set_rate(1.5)
    assert p.rate == 1.5
    p.stop()
