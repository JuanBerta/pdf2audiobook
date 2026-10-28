"""Check the GitHub releases page for the latest published version."""
import json
import urllib.request

REPO = "JuanBerta/pdf2audiobook"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 10


def check_for_updates():
    """Return the latest version tag (e.g. '1.2.0') or '' when unreachable."""
    req = urllib.request.Request(
        RELEASES_URL,
        headers={
            "User-Agent": "pdf2audiobook-updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name") or ""
    return tag.lstrip("v") if tag.startswith("v") else tag
