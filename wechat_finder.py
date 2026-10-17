#!/usr/bin/env python3
"""Find a WeChat Channels video URL in the desktop WeChat process.

The lookup follows the mechanism used by xuncv/WeChatDownloader: find the
FinderFeedWebWnd window, then scan its process memory for the finder.video
CDN URL. Only matching URL bytes are retained.
"""

import ctypes
import re
import sys
from ctypes import wintypes


WINDOW_CLASS = "FinderFeedWebWnd"
WINDOW_TITLE_PART = "视频号"
ASCII_URL_RE = re.compile(
    rb"https://finder\.video\.qq\.com/251/20302/[^\s\x00\"<>\\]+"
)


def _utf16le(data):
    return b"".join(bytes((byte, 0)) for byte in data)


UTF16_URL_RE = re.compile(
    _utf16le(b"https://finder.video.qq.com/251/20302/")
    + rb"(?:[\x20-\x7e]\x00)+"
)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READ_CHUNK = 4 * 1024 * 1024
CARRY_SIZE = 4096
MAX_REGION_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _find_window_pids():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    pids = set()
    wnd_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [wnd_enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    @wnd_enum_proc
    def enum_proc(hwnd, _lparam):
        class_buf = ctypes.create_unicode_buffer(256)
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, len(class_buf))
        user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        if class_buf.value == WINDOW_CLASS and WINDOW_TITLE_PART in title_buf.value:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        return True

    if not user32.EnumWindows(enum_proc, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sorted(pids)


def _extract_ascii_urls(data, found):
    for match in ASCII_URL_RE.finditer(data):
        url = match.group(0).decode("ascii", errors="ignore").rstrip("\\")
        if url:
            found[url] = None


def _extract_utf16_urls(data, found):
    for match in UTF16_URL_RE.finditer(data):
        raw = match.group(0)
        url = raw.decode("utf-16le", errors="ignore").rstrip("\\")
        if ASCII_URL_RE.fullmatch(url.encode("ascii", errors="ignore")):
            found[url] = None


def _scan_process(pid, found, max_total_bytes):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        mbi = MEMORY_BASIC_INFORMATION()
        address = 0
        scanned = 0
        virtual_query_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)

        while scanned < max_total_bytes:
            result = kernel32.VirtualQueryEx(
                process,
                ctypes.c_void_p(address),
                ctypes.byref(mbi),
                virtual_query_size,
            )
            if result != virtual_query_size:
                break

            region_size = int(mbi.RegionSize or 0)
            base = int(mbi.BaseAddress or 0)
            address = base + region_size

            readable = (
                mbi.State == MEM_COMMIT
                and not (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))
                and mbi.Type in (MEM_PRIVATE, MEM_MAPPED)
            )
            if not readable or region_size == 0:
                continue

            remaining = min(region_size, MAX_REGION_BYTES)
            offset = 0
            carry = b""
            while offset < remaining and scanned < max_total_bytes:
                size = min(READ_CHUNK, remaining - offset)
                buffer = ctypes.create_string_buffer(size)
                nread = ctypes.c_size_t()
                ok = kernel32.ReadProcessMemory(
                    process,
                    ctypes.c_void_p(base + offset),
                    buffer,
                    ctypes.c_size_t(size),
                    ctypes.byref(nread),
                )
                if not ok or nread.value == 0:
                    break

                data = carry + buffer.raw[: nread.value]
                _extract_ascii_urls(data, found)
                _extract_utf16_urls(data, found)
                scanned += nread.value
                offset += nread.value
                carry = data[-CARRY_SIZE:] if len(data) > CARRY_SIZE else data

        return scanned
    finally:
        kernel32.CloseHandle(process)


def find_wechat_video_urls(max_urls=20, max_total_bytes=MAX_TOTAL_BYTES):
    """Return unique WeChat Channels video URLs found in desktop WeChat."""
    if sys.platform != "win32":
        raise RuntimeError("微信视频号抓取仅支持 Windows 桌面版微信")

    pids = _find_window_pids()
    if not pids:
        return []

    found = {}
    for pid in pids:
        try:
            _scan_process(pid, found, max_total_bytes)
        except OSError:
            continue

    return list(found)[:max_urls]


if __name__ == "__main__":
    print(find_wechat_video_urls())
