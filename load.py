import yt_dlp
import os
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
            if line and "=" in line:
                key, _, value = line.partition("=")
                settings[key.strip()] = value.strip()
    return settings

settings = load_settings()
OUTPUT_DIR = settings.get("output_path", BASE_DIR)
MP3_DIR = settings.get("mp3_path", OUTPUT_DIR)
LOG_FILE = os.path.join(BASE_DIR, "last_run.log")

# Путь к node.exe из пакета nodejs_wheel_binaries (ставится через pip)
NODE_EXE = os.path.join(
    BASE_DIR, "venv", "Lib", "site-packages", "nodejs_wheel", "node.exe"
)

# Шумные предупреждения yt-dlp, которые не нужны в консоли
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

# Клиенты YouTube. tv_simply и android_vr не требуют PO-токенов и отдают
# рабочие форматы без куков. web_creator/web_embedded — для куков.
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
    """Выводит в консоль только важные сообщения, всё пишет в лог."""
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
    """Возвращает Chromium, если он запущен, иначе None."""
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
    """Проверяет, связана ли ошибка с чтением куков браузера."""
    text = str(exc).lower()
    return (
        "could not copy chrome cookie database" in text
        or "cookiesfrombrowser" in text
        or "could not find cookies" in text
        or ("cookie" in text and ("lock" in text or "database" in text or "copy" in text))
    )


def is_sign_in_error(exc):
    """YouTube запросил авторизацию или проверку, что пользователь не бот."""
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


def progress_hook(d):
    if d["status"] == "downloading":
        pct  = d.get("_percent_str", "?").strip()
        spd  = d.get("_speed_str", "?").strip()
        eta  = d.get("_eta_str", "?").strip()
        print(f"\r  {pct}  |  {spd}  |  ETA {eta}   ", end="", flush=True)
    elif d["status"] == "finished":
        print()


def extract_mp3(video_path, ffmpeg_exe, log):
    """Создаёт MP3-копию из видеофайла."""
    basename = os.path.splitext(os.path.basename(video_path))[0] + ".mp3"
    mp3_path = os.path.join(MP3_DIR, basename)
    print_log(log, f"Создаю MP3: {os.path.basename(mp3_path)}")
    result = subprocess.run(
        [ffmpeg_exe, "-y", "-i", video_path, "-vn", "-q:a", "0", mp3_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        log.write(f"MP3: {mp3_path}\n")
        print_log(log, f"✓ MP3 готов: {os.path.basename(mp3_path)}")
    else:
        err = result.stderr.decode(errors="ignore")
        log.write(f"MP3 ERROR: {err}\n")
        print_log(log, f"✗ Ошибка создания MP3: {err}")


def download_video(url, log):
    cookies_file = os.path.join(BASE_DIR, "cookies.txt")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    base_opts = {
        "outtmpl": os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_exe,
        "logger": YtDlpLogger(log),
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "http_chunk_size": 10 * 1024 * 1024,
        # Путь к Node.js (лежит в venv через nodejs_wheel_binaries) — нужен
        # для расшифровки nsig challenge от YouTube
        "js_runtimes": {"node": {"path": NODE_EXE}} if os.path.exists(NODE_EXE) else {"node": {}},
        # Разрешаем yt-dlp скачать EJS (JS challenge solver) с GitHub при
        # первом запуске — без этого nsig не расшифровывается и YouTube
        # отдаёт только картинки-превью
        "remote_components": ["ejs:github", "ejs:npm"],
        "progress_hooks": [progress_hook],
    }

    # Составляем список попыток. Сначала БЕЗ куков с tv_simply/android_vr —
    # они не требуют PO-токена и отдают рабочие форматы для публичных видео.
    # Если не сработало — с куками (возрастные/приватные видео).
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

    # Запоминаем mp4-файлы в папке до скачивания
    before = set(
        f for f in os.listdir(OUTPUT_DIR)
        if f.endswith(".mp4") and not any(c in f for c in [".f", ".webm"])
    )

    print_log(log, f"\nСкачиваю: {url}")
    last_error = None
    auth_error_seen = False
    network_error_seen = False
    for i, (label, extra_opts) in enumerate(attempts):
        ydl_opts = dict(base_opts)
        # extractor_args собираем отдельно, чтобы не перетереть базовые
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
                # Куки недоступны, следующая попытка (если есть) тоже без куков
                continue
            if is_sign_in_error(e):
                auth_error_seen = True
                print_log(log, "  ⚠  YouTube требует авторизацию. Пробую другой источник cookies.")
                continue
            # Пробуем следующую попытку — возможно, другой клиент сработает
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

    # Находим новый mp4 — тот которого не было до скачивания
    after = set(
        f for f in os.listdir(OUTPUT_DIR)
        if f.endswith(".mp4") and not any(c in f for c in [".f", ".webm"])
    )
    new_files = after - before

    if new_files:
        video_path = os.path.join(OUTPUT_DIR, new_files.pop())
        print_log(log, f"✓ Видео: {os.path.basename(video_path)}")
        extract_mp3(video_path, ffmpeg_exe, log)
    else:
        print_log(log, "✓ Скачано")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Ссылка на YouTube-видео")
    parser.add_argument("--interactive", action="store_true", help="Спросить ссылку в терминале")
    args = parser.parse_args()

    with open(LOG_FILE, "w", encoding="utf-8", buffering=1) as log_file:
        log = FlushingLog(log_file)
        log.write(f"=== Запуск: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")

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
                    download_video(url, log)
                except Exception as e:
                    log.write(f"ERROR: {e}\n")
                    print(f"✗ Ошибка: {short_error(e)}")
