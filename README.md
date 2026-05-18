# YouTube Downloader

Скачивает видео с YouTube, автоматически создаёт MP3-копию и отдельно умеет скачивать субтитры в TXT.

Работает на Windows.

## Требования

- Python 3.10+
- Chromium — для авторизации в YouTube
- Аккаунт YouTube/Google, в который выполнен вход в Chromium

Chromium нужен именно для cookies. Перед запуском `run.bat` или `run_subs.bat` Chromium должен быть закрыт, чтобы скрипт мог прочитать cookies.

## Установка

1. Установить Python 3.10+ с [python.org](https://www.python.org/downloads/)
   — при установке отметить галочку **"Add Python to PATH"**
2. Установить Chromium и войти в YouTube через этот браузер
3. Закрыть Chromium
4. Запустить `setup.bat` — он создаст виртуальное окружение `venv` и установит зависимости

Если окружение сломалось или зависимости устарели, можно снова запустить `setup.bat`.

## Скачивание видео

Запустить `run.bat`, вставить ссылку на видео и нажать Enter.

Результат:

- MP4 сохраняется в `output_path`
- MP3 сохраняется в `mp3_path`

Или скачать одно видео напрямую:

```
python load.py --url https://www.youtube.com/watch?v=...
```

## Скачивание субтитров

Запустить `run_subs.bat`, вставить ссылку на видео и нажать Enter.

По умолчанию скачиваются русские и английские субтитры, если они есть. Субтитры сохраняются как чистый TXT без таймкодов.

Примеры прямого запуска:

```
python subs.py --url https://www.youtube.com/watch?v=...
python subs.py --url https://www.youtube.com/watch?v=... --lang ru,en,de
python subs.py --url https://www.youtube.com/watch?v=... --auto-only
python subs.py --url https://www.youtube.com/watch?v=... --manual-only
```

## Настройки (settings.txt)

```
output_path=C:\Path\To\Videos          # папка для сохранения видео
mp3_path=C:\Path\To\Music              # папка для сохранения MP3
subs_path=C:\Path\To\Subtitles         # папка для сохранения TXT-субтитров
```

Если `subs_path` не указан, субтитры сохраняются в `output_path`.

## Cookies

Скрипт сначала пробует работать без cookies. Если YouTube требует авторизацию, скрипт читает cookies из Chromium.

После успешного чтения cookies создаётся зашифрованный кэш:

```
cookies.cache.dpapi
```

Он хранится в папке проекта и привязан к текущему пользователю Windows. При следующих запусках скрипт сначала использует этот кэш, чтобы не читать Chromium каждый раз. Если YouTube отклонит сохранённые cookies, кэш будет удалён и обновлён при следующем успешном чтении Chromium.

Расшифрованные временные cookies создаются в системной временной папке Windows и удаляются после попытки.

## Логи

Последний запуск пишется в:

```
last_run.log
```

Каждая строка лога начинается с временной метки. Лог сбрасывается на диск во время работы, поэтому при аварийном закрытии терминала уже записанная информация сохранится.

## Структура файлов

```
youtube/
├── load.py          # скачивание видео и создание MP3
├── subs.py          # скачивание субтитров в TXT
├── cookie_cache.py  # зашифрованный кэш cookies
├── settings.txt     # пути сохранения
├── last_run.log     # лог последнего запуска
├── cookies.cache.dpapi # зашифрованный кэш cookies (создаётся автоматически)
├── run.bat          # запуск с вводом ссылки
├── run_subs.bat     # запуск скачивания субтитров
└── setup.bat        # установка (запустить один раз)
```
