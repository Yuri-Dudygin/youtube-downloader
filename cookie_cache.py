import ctypes
import os
import tempfile
from ctypes import wintypes


CRYPTPROTECT_UI_FORBIDDEN = 0x1
CACHE_NAME = "cookies.cache.dpapi"
ENTROPY = b"youtube-downloader-cookie-cache-v1"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32


def _blob_from_bytes(data):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _bytes_from_blob(blob):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def _protect(data):
    data_blob, data_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(ENTROPY)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(data_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = (data_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    return _bytes_from_blob(out_blob)


def _unprotect(data):
    data_blob, data_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(ENTROPY)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(data_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = (data_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    return _bytes_from_blob(out_blob)


def cache_path(base_dir):
    return os.path.join(base_dir, CACHE_NAME)


def has_cache(base_dir):
    path = cache_path(base_dir)
    return os.path.exists(path) and os.path.getsize(path) > 0


def write_cache(base_dir, cookie_text):
    encrypted = _protect(cookie_text.encode("utf-8"))
    with open(cache_path(base_dir), "wb") as f:
        f.write(encrypted)


def remove_cache(base_dir):
    try:
        os.remove(cache_path(base_dir))
    except FileNotFoundError:
        pass


def restore_to_temp_file(base_dir):
    with open(cache_path(base_dir), "rb") as f:
        encrypted = f.read()
    cookie_text = _unprotect(encrypted).decode("utf-8")
    fd, path = tempfile.mkstemp(prefix="youtube-cookies-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(cookie_text)
    return path


def save_jar(base_dir, cookiejar):
    fd, path = tempfile.mkstemp(prefix="youtube-cookies-save-", suffix=".txt")
    os.close(fd)
    try:
        cookiejar.save(path, ignore_discard=True, ignore_expires=True)
        with open(path, "r", encoding="utf-8") as f:
            cookie_text = f.read()
        write_cache(base_dir, cookie_text)
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
