"""Piper voice catalog: list installed voices and download new ones."""
import json
import os
import urllib.request

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
CATALOG_URL = f"{BASE_URL}/voices.json"
USER_AGENT = "PDF2Audiobook"

# Piper voice names use "<lang>_<region>-<voice>[-<quality>]".
LANG_LABELS = {
    "ar_JO": "Arabic", "ca_ES": "Catalan", "cs_CZ": "Czech", "da_DK": "Danish",
    "de_DE": "German", "el_GR": "Greek", "en_GB": "English (UK)", "en_US": "English (US)",
    "es_ES": "Spanish (Spain)", "es_MX": "Spanish (Mexico)", "fa_IR": "Persian",
    "fi_FI": "Finnish", "fr_FR": "French", "hu_HU": "Hungarian", "is_IS": "Icelandic",
    "it_IT": "Italian", "ka_GE": "Georgian", "kk_KZ": "Kazakh", "lb_LU": "Luxembourgish",
    "ne_NP": "Nepali", "nl_BE": "Dutch (Belgium)", "nl_NL": "Dutch", "no_NO": "Norwegian",
    "pl_PL": "Polish", "pt_BR": "Portuguese (Brazil)", "pt_PT": "Portuguese (Portugal)",
    "ro_RO": "Romanian", "ru_RU": "Russian", "sk_SK": "Slovak", "sl_SI": "Slovenian",
    "sr_RS": "Serbian", "sv_SE": "Swedish", "sw_CD": "Swahili", "tr_TR": "Turkish",
    "uk_UA": "Ukrainian", "vi_VN": "Vietnamese", "zh_CN": "Chinese (Mandarin)",
    "ja_JP": "Japanese", "ko_KR": "Korean",
}

PREVIEW_TEXT = {
    "en": "Hello! This is how this voice sounds when it reads your books.",
    "es": "¡Hola! Así suena esta voz cuando lee tus libros.",
    "zh": "你好！这就是这个声音朗读你的书时的效果。",
    "fr": "Bonjour ! Voici comment cette voix lit vos livres.",
    "de": "Hallo! So klingt diese Stimme, wenn sie Ihre Bücher vorliest.",
    "it": "Ciao! Ecco come suona questa voce quando legge i tuoi libri.",
    "pt": "Olá! É assim que esta voz soa ao ler os seus livros.",
    "ru": "Здравствуйте! Так звучит этот голос, когда читает ваши книги.",
    "nl": "Hallo! Zo klinkt deze stem wanneer hij je boeken voorleest.",
    "pl": "Cześć! Tak brzmi ten głos, gdy czyta twoje książki.",
    "ca": "Hola! Així sona aquesta veu quan llegeix els teus llibres.",
    "uk": "Привіт! Так звучить цей голос, коли читає ваші книжки.",
    "tr": "Merhaba! Bu ses kitaplarınızı okurken böyle duyulur.",
}


def voice_display_name(model_name):
    """Turn 'en_US-lessac-medium' into 'English (US) — lessac medium'."""
    stem = os.path.basename(model_name)
    for ext in (".onnx.json", ".onnx"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
    parts = stem.split("-", 1)
    lang = parts[0] if parts else stem
    rest = parts[1].replace("-", " ") if len(parts) > 1 else ""
    label = LANG_LABELS.get(lang, lang.replace("_", " "))
    return f"{label} — {rest}".strip(" —")


def preview_text(voice_model):
    return PREVIEW_TEXT.get((voice_model or "en")[:2].lower(), PREVIEW_TEXT["en"])


def list_voice_models(models_dir):
    """Return [(model_path, config_path, display_name), ...] sorted by name."""
    if not os.path.isdir(models_dir):
        return []
    result = []
    for name in sorted(os.listdir(models_dir)):
        if not name.endswith(".onnx"):
            continue
        model = os.path.join(models_dir, name)
        config = model + ".json"
        if not os.path.exists(config):
            config = None
        result.append((model, config, voice_display_name(name)))
    return result


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https host


def fetch_catalog(cache_path=None):
    """Download the voice catalog (falls back to a cached copy offline)."""
    try:
        with _get(CATALOG_URL) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(raw)
    except Exception:
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            raise
    voices = []
    for key, v in data.items():
        files = v.get("files", {})
        onnx = next((p for p in files if p.endswith(".onnx")), None)
        cfg = next((p for p in files if p.endswith(".onnx.json")), None)
        if not onnx or not cfg:
            continue
        lang = v.get("language", {}) or {}
        voices.append({
            "key": key,
            "lang_code": lang.get("code", key.split("-")[0]),
            "language": "{} ({})".format(
                lang.get("name_english", ""), lang.get("country_english", "")
            ).replace(" ()", "") or LANG_LABELS.get(key.split("-")[0], key),
            "native": lang.get("name_native", ""),
            "name": v.get("name", ""),
            "quality": v.get("quality", ""),
            "speakers": v.get("num_speakers", 1),
            "size": files[onnx].get("size_bytes", 0),
            "files": [onnx, cfg],
        })
    voices.sort(key=lambda x: (x["language"], x["name"], x["quality"]))
    return voices


def download_voice(voice, models_dir, progress_cb=None, cancel_event=None):
    """Download a catalog voice (.onnx + .onnx.json) into models_dir."""
    os.makedirs(models_dir, exist_ok=True)
    total = voice.get("size") or 0
    done = 0
    for rel in voice["files"]:
        name = os.path.basename(rel)
        dest = os.path.join(models_dir, name)
        tmp = dest + ".part"
        with _get(f"{BASE_URL}/{rel}", timeout=60) as resp, open(tmp, "wb") as f:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    f.close()
                    os.remove(tmp)
                    from .converter import CancelledError

                    raise CancelledError()
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(min(99, int(100 * done / total)), name)
        os.replace(tmp, dest)
    if progress_cb:
        progress_cb(100, "")
    return os.path.join(models_dir, os.path.basename(voice["files"][0]))


def delete_voice(model_path):
    for p in (model_path, model_path + ".json"):
        if os.path.exists(p):
            os.remove(p)
