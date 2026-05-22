"""Скачивает субтитры с YouTube-видео (без скачивания самого видео).

Использование:
    python subs.py --url https://...     — один URL
    python subs.py --interactive         — спросить URL в терминале
    python subs.py --lang ru,en,de       — какие языки качать (по умолчанию ru,en)
    python subs.py --auto-only           — только авто-сгенерированные
    python subs.py --manual-only         — только созданные автором

Папка сохранения берётся из settings.txt:
    subs_path=...   (если нет — используется output_path)
"""

import yt_dlp
import os
import re
import sys
import datetime
import subprocess
import argparse
import imageio_ffmpeg
import cookie_cache

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_settings():
    path = os.path.join(BASE_DIR, "settings.txt")
    settings = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                settings[key.strip()] = value.strip()
    return settings


settings = load_settings()
# Папка для субтитров: subs_path → output_path → рядом со скриптом
SUBS_DIR = settings.get("subs_path") or settings.get("output_path", BASE_DIR)
LOG_FILE = os.path.join(BASE_DIR, "last_run.log")

NODE_EXE = os.path.join(
    BASE_DIR, "venv", "Lib", "site-packages", "nodejs_wheel", "node.exe"
)

SKIP_WARNINGS = [
    "[GetPOT]",
    "No request handlers",
    "No supported JavaScript runtime",
    "pot:bgutil",
    "PO Token",
    "po_token",
    "does not support cookies",
    "Error reaching GET",
]

# Для субтитров PO-токен не нужен вообще — клиенты берём самые нетребовательные
PLAYER_CLIENTS_NO_COOKIES = ["tv_simply", "android_vr"]
PLAYER_CLIENTS_WITH_COOKIES = ["web_creator", "web_embedded"]
COOKIE_BROWSERS = [
    ("chromium", "Chromium"),
]


class FlushingLog:
    """Сразу сбрасывает каждую запись, чтобы лог пережил аварийное закрытие."""
    def __init__(self, file):
        self.file = file
        self.at_line_start = True

    def write(self, text):
        for chunk in text.splitlines(True):
            if self.at_line_start:
                stamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
                self.file.write(stamp)
            self.file.write(chunk)
            self.at_line_start = chunk.endswith("\n")
        self.file.flush()

    def close(self):
        self.file.close()


def log_message(log, msg):
    log.write(msg + "\n")


def print_log(log, msg=""):
    print(msg)
    log_message(log, msg)


class YtDlpLogger:
    def __init__(self, log):
        self.log = log

    def _skip(self, msg):
        return any(p in msg for p in SKIP_WARNINGS)

    def debug(self, msg):
        self.log.write(msg + "\n")

    def info(self, msg):
        self.log.write(msg + "\n")
        if not self._skip(msg):
            print(msg)

    def warning(self, msg):
        self.log.write(f"WARNING: {msg}\n")
        if not self._skip(msg):
            print(f"⚠  {msg}")

    def error(self, msg):
        self.log.write(f"ERROR: {msg}\n")
        print(f"✗  {short_error(msg)}")


def check_browser_running():
    if sys.platform != "win32":
        return None
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["tasklist"],
            capture_output=True, text=True, timeout=5,
            creationflags=creationflags,
        )
        output = result.stdout.lower()
        if "chromium.exe" in output:
            return "Chromium"
    except Exception:
        pass
    return None


def browser_cookie_paths(browser_key):
    local = os.environ.get("LOCALAPPDATA", "")
    if browser_key == "chromium":
        roots = [os.path.join(local, "Chromium", "User Data")]
    else:
        return []

    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root):
            depth = current.count(os.sep) - root.count(os.sep)
            if depth > 3:
                dirs[:] = []
                continue
            if "Cookies" in files:
                found.append(os.path.join(current, "Cookies"))
            if "cookies.sqlite" in files:
                found.append(os.path.join(current, "cookies.sqlite"))
    return found


def available_cookie_browsers(log):
    available = []
    for key, name in COOKIE_BROWSERS:
        if browser_cookie_paths(key):
            available.append((key, name))
        else:
            log_message(log, f"Cookies {name}: база cookies не найдена, пропускаю.")
    return available


def is_cookie_error(exc):
    text = str(exc).lower()
    return (
        "could not copy chrome cookie database" in text
        or "cookiesfrombrowser" in text
        or "could not find cookies" in text
        or ("cookie" in text and ("lock" in text or "database" in text or "copy" in text))
    )


def is_sign_in_error(exc):
    text = str(exc).lower()
    return (
        "sign in to confirm" in text
        or "not a bot" in text
        or "use --cookies-from-browser" in text
        or "use --cookies" in text
    )


def is_network_error(exc):
    text = str(exc).lower()
    return (
        "read timed out" in text
        or "timed out" in text
        or "unexpected_eof_while_reading" in text
        or "connection reset" in text
        or "connection aborted" in text
        or "ssl" in text and "eof" in text
        or "giving up after" in text and "retries" in text
    )


def short_error(exc):
    text = str(exc)
    lower = text.lower()
    if is_sign_in_error(exc):
        return "YouTube требует авторизацию или проверку, что пользователь не бот."
    if is_network_error(exc):
        return "Сетевая ошибка при скачивании с YouTube/googlevideo. Повтори запуск позже."
    if "could not find" in lower and "cookies database" in lower:
        return "База cookies Chromium не найдена."
    if is_cookie_error(exc):
        return "Не удалось прочитать cookies Chromium."
    first_line = text.replace("\r", "\n").split("\n", 1)[0].strip()
    return first_line[:300] + ("..." if len(first_line) > 300 else "")


def cookie_attempt(browser_key, browser_name):
    return (
        f"куки {browser_name} (web_creator)",
        {
            "cookiesfrombrowser": (browser_key,),
            "extractor_args_youtube": {"player_client": PLAYER_CLIENTS_WITH_COOKIES},
        },
    )


def cached_cookie_attempt(log):
    try:
        path = cookie_cache.restore_to_temp_file(BASE_DIR)
    except Exception as e:
        log.write(f"COOKIE CACHE RESTORE ERROR: {e}\n")
        cookie_cache.remove_cache(BASE_DIR)
        return None
    return (
        "зашифрованный кэш cookies",
        {
            "cookiefile": path,
            "temp_cookiefile": path,
            "cached_cookiefile": True,
            "extractor_args_youtube": {"player_client": PLAYER_CLIENTS_WITH_COOKIES},
        },
    )


def list_existing_subs():
    """Снимок файлов субтитров в SUBS_DIR — чтобы потом показать, что нового появилось."""
    exts = (".srt", ".vtt", ".ass", ".ttml", ".txt")
    try:
        return set(f for f in os.listdir(SUBS_DIR) if f.lower().endswith(exts))
    except FileNotFoundError:
        return set()


def srt_to_plain_text(srt_path):
    """Парсит .srt в чистый текст без таймкодов, номеров, HTML-тегов и переносов строк.
    Дополнительно схлопывает перекрывающиеся фрагменты (rolling captions в авто-субтитрах
    YouTube, где каждый сегмент содержит хвост предыдущего)."""
    with open(srt_path, "r", encoding="utf-8") as f:
        raw = f.read()

    segments = []
    current_text = []
    in_text = False
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            if current_text:
                segments.append(" ".join(current_text))
                current_text = []
            in_text = False
            continue
        # Таймкод — переключает в режим "дальше идёт текст"
        if "-->" in line and not in_text:
            in_text = True
            continue
        # Номер сегмента (только ДО таймкода, чтобы не съесть числа в самом тексте)
        if line.isdigit() and not in_text and not current_text:
            continue
        if in_text:
            # Чистим HTML/стилевые теги: <c>, <b>, <c.colorE5E5E5>, &nbsp; и т.п.
            cleaned = re.sub(r"<[^>]+>", "", line)
            cleaned = re.sub(r"&[a-z]+;", " ", cleaned).strip()
            if cleaned:
                current_text.append(cleaned)
    if current_text:
        segments.append(" ".join(current_text))

    # Схлопывание перекрывающихся сегментов (rolling captions)
    result_words = []
    for seg in segments:
        words = seg.split()
        if not words:
            continue
        overlap = 0
        max_check = min(len(result_words), len(words))
        for k in range(max_check, 0, -1):
            if result_words[-k:] == words[:k]:
                overlap = k
                break
        result_words.extend(words[overlap:])

    text = " ".join(result_words)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def convert_and_cleanup(srt_files, log):
    """Каждый .srt → .txt с чистым текстом; .srt удаляется."""
    produced = []
    for srt_path in srt_files:
        try:
            text = srt_to_plain_text(srt_path)
        except Exception as e:
            log.write(f"CONVERT ERROR: {srt_path}: {e}\n")
            print_log(log, f"✗ Не удалось обработать {os.path.basename(srt_path)}: {e}")
            continue
        txt_path = os.path.splitext(srt_path)[0] + ".txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.remove(srt_path)
        except OSError:
            pass
        produced.append(txt_path)
    return produced


def download_subs(url, langs, mode, log):
    """mode: 'both' | 'manual' | 'auto'"""
    cookies_file = os.path.join(BASE_DIR, "cookies.txt")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    write_manual = mode in ("both", "manual")
    write_auto = mode in ("both", "auto")

    base_opts = {
        # Не скачиваем само видео — только субтитры
        "skip_download": True,
        "writesubtitles": write_manual,
        "writeautomaticsub": write_auto,
        "subtitleslangs": langs,
        # Стараемся взять srt напрямую, иначе vtt (конвертируем ниже)
        "subtitlesformat": "srt/vtt/best",
        "postprocessors": [
            # Приводим всё к srt (универсально для плееров)
            {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
        ],
        "outtmpl": os.path.join(SUBS_DIR, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_exe,
        "logger": YtDlpLogger(log),
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "http_chunk_size": 10 * 1024 * 1024,
        "js_runtimes": (
            {"node": {"path": NODE_EXE}} if os.path.exists(NODE_EXE) else {"node": {}}
        ),
        "remote_components": ["ejs:github", "ejs:npm"],
    }

    attempts = []
    if os.path.exists(cookies_file):
        attempts.append((
            "файл cookies.txt (web_creator)",
            {
                "cookiefile": cookies_file,
                "extractor_args_youtube": {"player_client": PLAYER_CLIENTS_WITH_COOKIES},
            },
        ))
    if cookie_cache.has_cache(BASE_DIR):
        attempt = cached_cookie_attempt(log)
        if attempt:
            attempts.append(attempt)
    browser = check_browser_running()
    if browser:
        print_log(log, f"⚠  {browser} запущен — если потребуются куки, закрой браузер.")
    attempts.extend(cookie_attempt(key, name) for key, name in available_cookie_browsers(log))
    attempts.append((
        "без куки (tv_simply, android_vr)",
        {"extractor_args_youtube": {"player_client": PLAYER_CLIENTS_NO_COOKIES}},
    ))

    before = list_existing_subs()

    print_log(log, f"\nСубтитры: {url}")
    print_log(log, f"  языки: {', '.join(langs)}   режим: {mode}")
    last_error = None
    auth_error_seen = False
    network_error_seen = False
    for i, (label, extra_opts) in enumerate(attempts):
        ydl_opts = dict(base_opts)
        extractor_args = {}
        extra_opts = dict(extra_opts)
        temp_cookiefile = extra_opts.pop("temp_cookiefile", None)
        cached_cookiefile = extra_opts.pop("cached_cookiefile", False)
        ya = extra_opts.pop("extractor_args_youtube", None)
        if ya:
            extractor_args["youtube"] = ya
        ydl_opts["extractor_args"] = extractor_args
        ydl_opts.update(extra_opts)

        print_log(log, f"  [{i+1}/{len(attempts)}] Попытка: {label}")
        if "cookiesfrombrowser" in ydl_opts:
            print_log(log, "  Читаю cookies браузера. Это может занять до минуты.")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
                if "cookiesfrombrowser" in ydl_opts:
                    try:
                        cookie_cache.save_jar(BASE_DIR, ydl.cookiejar)
                        log.write("COOKIE CACHE: обновлен из Chromium\n")
                    except Exception as e:
                        log.write(f"COOKIE CACHE SAVE ERROR: {e}\n")
            last_error = None
            break
        except Exception as e:
            last_error = e
            log.write(f"Попытка '{label}' не удалась: {e}\n")
            print(f"  ⚠  '{label}' — {short_error(e)}")
            if is_network_error(e):
                network_error_seen = True
                print_log(log, "  ⚠  Это сетевой сбой во время скачивания, а не проблема cookies. Останавливаю перебор попыток.")
                break
            if cached_cookiefile and is_sign_in_error(e):
                cookie_cache.remove_cache(BASE_DIR)
                log.write("COOKIE CACHE: удален, потому что YouTube отклонил cookies\n")
            if is_cookie_error(e):
                continue
            if is_sign_in_error(e):
                auth_error_seen = True
                print_log(log, "  ⚠  YouTube требует авторизацию. Пробую другой источник cookies.")
                continue
            continue
        finally:
            if temp_cookiefile:
                try:
                    os.remove(temp_cookiefile)
                except FileNotFoundError:
                    pass

    if last_error is not None and auth_error_seen and not network_error_seen:
        print_log(log, "  ⚠  Все источники cookies отклонены YouTube. Открой Chromium, заново войди в YouTube, закрой Chromium и повтори запуск.")
    if last_error is not None:
        raise last_error

    # Находим новые файлы и конвертируем srt → txt (только текст, без метаданных)
    after = list_existing_subs()
    new_files = sorted(after - before)
    new_srt = [os.path.join(SUBS_DIR, n) for n in new_files if n.lower().endswith(".srt")]

    if new_srt:
        produced = convert_and_cleanup(new_srt, log)
        for path in produced:
            print_log(log, f"✓ {os.path.basename(path)}")
        log.write("Subs: " + "; ".join(os.path.basename(p) for p in produced) + "\n")
    elif new_files:
        # yt-dlp отдал что-то не-.srt (редкий случай). Показываем как есть.
        for name in new_files:
            print_log(log, f"✓ {name}")
        log.write("Subs (raw): " + "; ".join(new_files) + "\n")
    else:
        print_log(log, "⚠  Новых файлов субтитров не появилось "
                  "(возможно, в видео нет субтитров на запрошенных языках).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Скачивание субтитров с YouTube (без видео)."
    )
    parser.add_argument("--url", help="Ссылка на YouTube-видео")
    parser.add_argument("--interactive", action="store_true",
                        help="Спросить ссылку в терминале")
    parser.add_argument("--lang", default="ru,en",
                        help="Языки через запятую (по умолчанию: ru,en). "
                             "Можно 'all' — все доступные.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--auto-only", action="store_true",
                            help="Только авто-сгенерированные")
    mode_group.add_argument("--manual-only", action="store_true",
                            help="Только созданные автором")
    args = parser.parse_args()

    langs = [x.strip() for x in args.lang.split(",") if x.strip()]
    if args.auto_only:
        mode = "auto"
    elif args.manual_only:
        mode = "manual"
    else:
        mode = "both"

    os.makedirs(SUBS_DIR, exist_ok=True)

    with open(LOG_FILE, "w", encoding="utf-8", buffering=1) as log_file:
        log = FlushingLog(log_file)
        log.write(f"=== Запуск subs.py: "
                  f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
        log.write(f"Папка субтитров: {SUBS_DIR}\n\n")

        if args.url:
            urls = [args.url]
        elif args.interactive:
            url = input("\nВставь ссылку на видео: ").strip()
            urls = [url] if url else []
        else:
            urls = []

        if not urls:
            print_log(log, "Нет URL для скачивания.")
        else:
            for url in urls:
                try:
                    download_subs(url, langs, mode, log)
                except Exception as e:
                    log.write(f"ERROR: {e}\n")
                    print(f"✗ Ошибка: {short_error(e)}")
