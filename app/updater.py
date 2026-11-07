"""Check the GitHub releases page for the latest published version."""
import json
import re
import urllib.request

REPO = "JuanBerta/pdf2audiobook"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
TIMEOUT = 10


def parse_version(text):
    """'v1.10.2-beta' -> (1, 10, 2). Non-numeric parts are ignored."""
    nums = re.findall(r"\d+", (text or "").split("-", 1)[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def check_for_updates():
    """Return the latest release version (e.g. '1.2.0'). Raises on network errors."""
    req = urllib.request.Request(
        RELEASES_URL,
        headers={"User-Agent": "pdf2audiobook-updater", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - fixed https host
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name") or ""
    return tag[1:] if tag[:1] in ("v", "V") else tag
