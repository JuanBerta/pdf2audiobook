"""Minimal UI localization for PDF2Audiobook (English / Spanish / Chinese).

All user-visible strings live in TRANSLATIONS. Use tr("key", ...) anywhere in
the UI or conversion pipeline to get the current language's text; named
placeholders like {count} are formatted with tr's keyword arguments.
"""
import os
import re

SUPPORTED_LANGUAGES = ["en", "es", "zh"]

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Español",
    "zh": "中文",
}

TRANSLATIONS = {
    "en": {
        "convert_btn": "Convert PDF to audiobook",
        "open_dir_btn": "Open library folder",
        "nothing_playing": "Nothing playing",
        "play": "Play",
        "stop": "Stop",
        "pause": "Pause",
        "prev": "Previous chapter",
        "next": "Next chapter",
        "repeat_off": "Repeat off",
        "repeat_all": "Repeat all",
        "repeat_one": "Repeat one",
        "unknown_author": "unknown author",
        "chunks": "{count} chunks",
        "listened_progress": "listened {pos} / {total}",
        "voice_label": "voice: {voice}",
        "delete_from_library": "Delete from library",
        "delete_book_title": "Delete book",
        "delete_book_msg": "Remove this audiobook from the library? (audio files are kept on disk)",
        "yes": "Yes",
        "no": "No",
        "busy_title": "Busy",
        "busy_msg": "A conversion is already running.",
        "model_missing_title": "Voice model missing",
        "model_missing_msg": (
            "No Piper voice model found in:\n{dir}\n\n"
            "Run:  python scripts/download_models.py\n"
            "to download voices, then try again."
        ),
        "select_pdf": "Select PDF",
        "pdf_files": "PDF files",
        "book_title_dlg": "Book title",
        "title_label": "Title:",
        "author_dlg": "Author (optional)",
        "author_label": "Author:",
        "choose_voice_dlg": "Choose voice",
        "voice_model_label": "Voice model:",
        "extracting": "Extracting PDF text...",
        "synth_chunk": "Synthesizing chunk {current}/{total}...",
        "done": "Done",
        "done_title": "Conversion finished",
        "done_msg": "Audiobook saved to your library (id {book_id}).",
        "failed_title": "Conversion failed",
        "playback_error_title": "Playback error",
        "now_playing": "{title} - {chapter} ({state})",
        "state_playing": "playing",
        "state_paused": "paused",
        "menu_language": "Language",
        "menu_theme": "Theme",
        "theme_dark": "Dark",
        "theme_light": "Light",
        "stat_books": "{count} books",
        "search_placeholder": "Search books...",
        "menu_sleep_timer": "Sleep timer",
        "sleep_off": "Off",
        "sleep_minutes": "{minutes} minutes",
        "bookmarks_menu": "Bookmarks",
        "add_bookmark": "Add bookmark",
        "bookmark_added": "Bookmark added",
        "no_bookmarks": "No bookmarks yet",
        "speed_dlg": "Speech rate (synthesis)",
        "queue_progress": "Job {current}/{total}: {title}",
        "cancel": "Cancel",
        "pause_conv": "Pause",
        "resume_conv": "Resume",
        "choose_pdfs": "Select PDFs",
        "update_available_title": "Update available",
        "update_available_msg": "New version {version} is available. Open the releases page?",
        "update_ok": "Up to date",
        "single_instance_title": "Already running",
        "single_instance_msg": "PDF2Audiobook is already running.",
        "no_text_error": (
            "No readable text found in the PDF. "
            "Scanned/image-only PDFs require OCR, which is not supported yet."
        ),
        "piper_not_installed": "piper-tts is not installed. Run: pip install piper-tts",
        "model_not_found": (
            "Voice model not found: {path}\n"
            "Download one with scripts/download_models.py"
        ),
        "readalong_toggle": "Read PDF",
        "readalong_no_data": (
            "No read-along data for this book. Re-convert its PDF to enable "
            "synchronized reading."
        ),
        "readalong_page": "Page {page}",
    },
    "es": {
        "convert_btn": "Convertir PDF a audiolibro",
        "open_dir_btn": "Abrir carpeta de la biblioteca",
        "nothing_playing": "Nada en reproducción",
        "play": "Reproducir",
        "stop": "Detener",
        "pause": "Pausa",
        "prev": "Capítulo anterior",
        "next": "Capítulo siguiente",
        "repeat_off": "Repetición desactivada",
        "repeat_all": "Repetir todo",
        "repeat_one": "Repetir uno",
        "unknown_author": "autor desconocido",
        "chunks": "{count} fragmentos",
        "listened_progress": "escuchado {pos} / {total}",
        "voice_label": "voz: {voice}",
        "delete_from_library": "Eliminar de la biblioteca",
        "delete_book_title": "Eliminar libro",
        "delete_book_msg": "¿Eliminar este audiolibro de la biblioteca? (los archivos de audio se conservan en el disco)",
        "yes": "Sí",
        "no": "No",
        "busy_title": "Ocupado",
        "busy_msg": "Ya hay una conversión en curso.",
        "model_missing_title": "Falta el modelo de voz",
        "model_missing_msg": (
            "No se encontró ningún modelo de voz Piper en:\n{dir}\n\n"
            "Ejecute:  python scripts/download_models.py\n"
            "para descargar voces e inténtelo de nuevo."
        ),
        "select_pdf": "Seleccionar PDF",
        "pdf_files": "Archivos PDF",
        "book_title_dlg": "Título del libro",
        "title_label": "Título:",
        "author_dlg": "Autor (opcional)",
        "author_label": "Autor:",
        "choose_voice_dlg": "Elegir voz",
        "voice_model_label": "Modelo de voz:",
        "extracting": "Extrayendo texto del PDF...",
        "synth_chunk": "Sintetizando fragmento {current}/{total}...",
        "done": "Listo",
        "done_title": "Conversión finalizada",
        "done_msg": "Audiolibro guardado en su biblioteca (id {book_id}).",
        "failed_title": "Error de conversión",
        "playback_error_title": "Error de reproducción",
        "now_playing": "{title} - {chapter} ({state})",
        "state_playing": "reproduciendo",
        "state_paused": "en pausa",
        "menu_language": "Idioma",
        "menu_theme": "Tema",
        "theme_dark": "Oscuro",
        "theme_light": "Claro",
        "stat_books": "{count} libros",
        "search_placeholder": "Buscar libros...",
        "menu_sleep_timer": "Temporizador de reposo",
        "sleep_off": "Apagado",
        "sleep_minutes": "{minutes} minutos",
        "bookmarks_menu": "Marcadores",
        "add_bookmark": "Añadir marcador",
        "bookmark_added": "Marcador añadido",
        "no_bookmarks": "Sin marcadores",
        "speed_dlg": "Velocidad de voz (síntesis)",
        "queue_progress": "Tarea {current}/{total}: {title}",
        "cancel": "Cancelar",
        "pause_conv": "Pausar",
        "resume_conv": "Reanudar",
        "choose_pdfs": "Seleccionar PDF",
        "update_available_title": "Actualización disponible",
        "update_available_msg": "La nueva versión {version} está disponible. ¿Abrir la página de lanzamientos?",
        "update_ok": "Actualizado",
        "single_instance_title": "Ya en ejecución",
        "single_instance_msg": "PDF2Audiobook ya está en ejecución.",
        "no_text_error": (
            "No se encontró texto legible en el PDF. "
            "Los PDF escaneados (solo imagen) requieren OCR, que aún no es compatible."
        ),
        "piper_not_installed": "piper-tts no está instalado. Ejecute: pip install piper-tts",
        "model_not_found": (
            "No se encontró el modelo de voz: {path}\n"
            "Descargue uno con scripts/download_models.py"
        ),
        "readalong_toggle": "Leer PDF",
        "readalong_no_data": (
            "Este libro no tiene datos de lectura sincronizada. "
            "Vuelva a convertir su PDF para activarla."
        ),
        "readalong_page": "Página {page}",
    },
    "zh": {
        "convert_btn": "将 PDF 转换为有声书",
        "open_dir_btn": "打开书库文件夹",
        "nothing_playing": "当前没有播放内容",
        "play": "播放",
        "stop": "停止",
        "pause": "暂停",
        "prev": "上一章",
        "next": "下一章",
        "repeat_off": "重复关闭",
        "repeat_all": "列表循环",
        "repeat_one": "单章循环",
        "unknown_author": "未知作者",
        "chunks": "{count} 个片段",
        "listened_progress": "已收听 {pos} / {total}",
        "voice_label": "语音: {voice}",
        "delete_from_library": "从书库中删除",
        "delete_book_title": "删除书籍",
        "delete_book_msg": "从书库中移除该有声书？（音频文件将保留在磁盘上）",
        "yes": "是",
        "no": "否",
        "busy_title": "忙碌",
        "busy_msg": "已有转换任务正在进行。",
        "model_missing_title": "缺少语音模型",
        "model_missing_msg": (
            "在以下位置未找到 Piper 语音模型：\n{dir}\n\n"
            "请运行：python scripts/download_models.py\n"
            "下载语音后重试。"
        ),
        "select_pdf": "选择 PDF",
        "pdf_files": "PDF 文件",
        "book_title_dlg": "书籍标题",
        "title_label": "标题：",
        "author_dlg": "作者（可选）",
        "author_label": "作者：",
        "choose_voice_dlg": "选择语音",
        "voice_model_label": "语音模型：",
        "extracting": "正在提取 PDF 文本...",
        "synth_chunk": "正在合成片段 {current}/{total}...",
        "done": "完成",
        "done_title": "转换完成",
        "done_msg": "有声书已保存到书库（id {book_id}）。",
        "failed_title": "转换失败",
        "playback_error_title": "播放错误",
        "now_playing": "{title} - {chapter}（{state}）",
        "state_playing": "播放中",
        "state_paused": "已暂停",
        "menu_language": "语言",
        "menu_theme": "主题",
        "theme_dark": "深色",
        "theme_light": "浅色",
        "stat_books": "{count} 本书",
        "search_placeholder": "搜索书籍...",
        "menu_sleep_timer": "睡眠定时器",
        "sleep_off": "关闭",
        "sleep_minutes": "{minutes} 分钟",
        "bookmarks_menu": "书签",
        "add_bookmark": "添加书签",
        "bookmark_added": "已添加书签",
        "no_bookmarks": "暂无书签",
        "speed_dlg": "语速（合成）",
        "queue_progress": "任务 {current}/{total}: {title}",
        "cancel": "取消",
        "pause_conv": "暂停",
        "resume_conv": "继续",
        "choose_pdfs": "选择 PDF 文件",
        "update_available_title": "发现新版本",
        "update_available_msg": "发现新版本 {version}。是否打开发布页面？",
        "update_ok": "已是最新版本",
        "single_instance_title": "程序已在运行",
        "single_instance_msg": "PDF2Audiobook 已在运行。",
        "no_text_error": "在 PDF 中未找到可读文本。扫描版（纯图片）PDF 需要 OCR 支持，暂不支持。",
        "piper_not_installed": "未安装 piper-tts。请运行：pip install piper-tts",
        "model_not_found": "未找到语音模型：{path}\n请使用 scripts/download_models.py 下载一个。",
        "readalong_toggle": "阅读 PDF",
        "readalong_no_data": "本书暂无听读同步数据，请重新转换其 PDF 以启用。",
        "readalong_page": "第 {page} 页",
    },
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

_current_lang = "en"


def set_language(code):
    """Switch the active UI language (must be in SUPPORTED_LANGUAGES)."""
    global _current_lang
    if code not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported language: {code!r}")
    _current_lang = code


def get_language():
    return _current_lang


def tr(key, **kwargs):
    """Return the current language's text for `key`, formatting placeholders."""
    text = TRANSLATIONS.get(_current_lang, {}).get(key)
    if text is None:
        text = TRANSLATIONS["en"].get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def placeholders(text):
    """Extract the set of named placeholders from a template string."""
    return set(_PLACEHOLDER_RE.findall(text))


def validate():
    """Check that every language has the same keys and placeholders as English.

    Returns a list of human-readable problems (empty means all good).
    """
    problems = []
    en = TRANSLATIONS["en"]
    en_keys = set(en)
    en_placeholders = {k: placeholders(v) for k, v in en.items()}
    for code in SUPPORTED_LANGUAGES:
        table = TRANSLATIONS.get(code, {})
        keys = set(table)
        if keys != en_keys:
            missing = sorted(en_keys - keys)
            extra = sorted(keys - en_keys)
            if missing:
                problems.append(f"[{code}] missing keys: {missing}")
            if extra:
                problems.append(f"[{code}] extra keys: {extra}")
        for key in en_keys:
            if key not in table:
                continue
            if placeholders(table[key]) != en_placeholders[key]:
                problems.append(
                    f"[{code}] placeholder mismatch for {key!r}: "
                    f"{sorted(placeholders(table[key]))} != "
                    f"{sorted(en_placeholders[key])}"
                )
    return problems


# Convenience: resolve settings storage path without importing Qt at module load.
def settings_path():
    appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, "PDF2Audiobook", "settings.txt")
