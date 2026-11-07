import json
import urllib.error
import urllib.request

import pytest

from app.browser_pdf import ReadAlongServer
from app.i18n import validate
from app.updater import is_newer, parse_version


def test_translations_complete():
    assert validate() == []


def test_version_compare():
    assert parse_version("v1.10.2") == (1, 10, 2)
    assert is_newer("1.10.0", "1.9.9")
    assert not is_newer("1.4.1", "1.5.0")  # never "update" to an older release
    assert not is_newer("1.5.0", "1.5.0")


def test_readalong_server_requires_token(sample_pdf):
    s = ReadAlongServer()
    seeks = []
    s.set_seek_callback(lambda t, i: seeks.append((t, i)))
    s.start()
    try:
        s.set_book(sample_pdf, [{"chapter_idx": 0, "text": "x", "bbox": "null"}], [], "en")
        base = s.url.split("?")[0]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "api/data")
        assert err.value.code == 403
        req = urllib.request.Request(base + "api/seek", data=b'{"audio_start": 3}', method="POST")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req)
        assert seeks == []
        data = json.load(urllib.request.urlopen(s.url.replace("/?", "/api/data?")))
        assert data["has_data"] is True
        req = urllib.request.Request(s.url.replace("/?", "/api/seek?"),
                                     data=b'{"audio_start": 3, "chapter_idx": 0}', method="POST")
        urllib.request.urlopen(req)
        assert seeks == [(3.0, 0)]
    finally:
        s.shutdown()
