"""Thin ctypes layer over the Windows APIs the app needs.

Argument and return types are declared for every call: on 64-bit Windows the
ctypes defaults truncate handles and LPARAMs to 32 bits.
"""

from __future__ import annotations

import ctypes
import re
import struct
import time
import winreg
import zlib
from ctypes import POINTER, Structure, Union, byref, sizeof
from ctypes import wintypes as w

from .clips import FILES, IMAGE, TEXT, Clip, describe_image, hdrop, paths_from_hdrop, thumbnail

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)

# messages
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_TIMER = 0x0113
WM_HOTKEY = 0x0312
WM_CLIPBOARDUPDATE = 0x031D
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_GETICON = 0x007F
WM_SETTINGCHANGE = 0x001A
ICON_BIG = 1

# hotkeys and input
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
VK_CONTROL, VK_SHIFT, VK_MENU, VK_SPACE = 0x11, 0x10, 0x12, 0x20
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_V, VK_Q = 0x56, 0x51
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

# clipboard
CF_UNICODETEXT, CF_DIB, CF_HDROP = 13, 8, 15
GMEM_MOVEABLE = 0x0002
MAX_IMAGE_BYTES = 64 * 1024 * 1024  # a 4K screenshot is about 33 MB before compression
MAX_RICH_BYTES = 8 * 1024 * 1024  # the formatting next to a text

# tray
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0800, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

# windows
SW_RESTORE = 9
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x00000080, 0x00000020, 0x08000000
ERROR_ALREADY_EXISTS = 183
ASFW_ANY = 0xFFFFFFFF
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR = 34, 35, 36
DWMWCP_ROUND = 2
SM_CXICON, SM_CXSMICON = 11, 49
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25  # TOKEN_INFORMATION_CLASS.TokenIntegrityLevel


class WNDCLASSW(Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR)]


class KEYBDINPUT(Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(Structure):  # only here so the union has its real size
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _InputUnion(Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(Structure):
    _fields_ = [("type", w.DWORD), ("u", _InputUnion)]


class GUID(Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", w.HICON)]


class ICONINFO(Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)


_sig(user32.RegisterClassW, w.ATOM, POINTER(WNDCLASSW))
_sig(user32.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
_sig(user32.DestroyWindow, w.BOOL, w.HWND)
_sig(user32.DefWindowProcW, LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
_sig(user32.GetMessageW, w.BOOL, POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
_sig(user32.TranslateMessage, w.BOOL, POINTER(w.MSG))
_sig(user32.DispatchMessageW, LRESULT, POINTER(w.MSG))
_sig(user32.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
_sig(user32.PostQuitMessage, None, ctypes.c_int)
_sig(user32.RegisterWindowMessageW, w.UINT, w.LPCWSTR)
_sig(user32.FindWindowW, w.HWND, w.LPCWSTR, w.LPCWSTR)
_sig(user32.SetTimer, ULONG_PTR, w.HWND, ULONG_PTR, w.UINT, w.LPVOID)
_sig(user32.KillTimer, w.BOOL, w.HWND, ULONG_PTR)

_sig(user32.AddClipboardFormatListener, w.BOOL, w.HWND)
_sig(user32.RemoveClipboardFormatListener, w.BOOL, w.HWND)
_sig(user32.OpenClipboard, w.BOOL, w.HWND)
_sig(user32.CloseClipboard, w.BOOL)
_sig(user32.EmptyClipboard, w.BOOL)
_sig(user32.GetClipboardData, w.HANDLE, w.UINT)
_sig(user32.SetClipboardData, w.HANDLE, w.UINT, w.HANDLE)
_sig(user32.IsClipboardFormatAvailable, w.BOOL, w.UINT)
_sig(user32.RegisterClipboardFormatW, w.UINT, w.LPCWSTR)
_sig(kernel32.GlobalAlloc, w.HGLOBAL, w.UINT, ctypes.c_size_t)
_sig(kernel32.GlobalLock, w.LPVOID, w.HGLOBAL)
_sig(kernel32.GlobalUnlock, w.BOOL, w.HGLOBAL)
_sig(kernel32.GlobalFree, w.HGLOBAL, w.HGLOBAL)
_sig(kernel32.GlobalSize, ctypes.c_size_t, w.HGLOBAL)

_sig(user32.RegisterHotKey, w.BOOL, w.HWND, ctypes.c_int, w.UINT, w.UINT)
_sig(user32.UnregisterHotKey, w.BOOL, w.HWND, ctypes.c_int)
_sig(user32.SendInput, w.UINT, w.UINT, POINTER(INPUT), ctypes.c_int)
_sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_sig(user32.MapVirtualKeyW, w.UINT, w.UINT, w.UINT)
_sig(user32.GetKeyNameTextW, ctypes.c_int, w.LONG, w.LPWSTR, ctypes.c_int)

_sig(user32.GetForegroundWindow, w.HWND)
_sig(user32.SetForegroundWindow, w.BOOL, w.HWND)
_sig(user32.AllowSetForegroundWindow, w.BOOL, w.DWORD)
_sig(user32.IsWindow, w.BOOL, w.HWND)
_sig(user32.IsIconic, w.BOOL, w.HWND)
_sig(user32.ShowWindow, w.BOOL, w.HWND, ctypes.c_int)
_sig(user32.GetWindowThreadProcessId, w.DWORD, w.HWND, POINTER(w.DWORD))
_sig(user32.GetClassNameW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
_sig(user32.SetWinEventHook, w.HANDLE, w.DWORD, w.DWORD, w.HMODULE, WINEVENTPROC, w.DWORD, w.DWORD, w.DWORD)
_sig(user32.UnhookWinEvent, w.BOOL, w.HANDLE)
_sig(user32.GetWindowLongPtrW, ctypes.c_ssize_t, w.HWND, ctypes.c_int)
_sig(user32.SetWindowLongPtrW, ctypes.c_ssize_t, w.HWND, ctypes.c_int, ctypes.c_ssize_t)
_sig(user32.GetCursorPos, w.BOOL, POINTER(w.POINT))
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)

_sig(user32.CreatePopupMenu, w.HMENU)
_sig(user32.AppendMenuW, w.BOOL, w.HMENU, w.UINT, ULONG_PTR, w.LPCWSTR)
_sig(user32.TrackPopupMenuEx, w.BOOL, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID)
_sig(user32.DestroyMenu, w.BOOL, w.HMENU)
_sig(user32.CreateIconIndirect, w.HICON, POINTER(ICONINFO))
_sig(user32.DestroyIcon, w.BOOL, w.HICON)
_sig(user32.SendMessageW, LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
_sig(gdi32.CreateBitmap, w.HBITMAP, ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, w.LPCVOID)
_sig(gdi32.DeleteObject, w.BOOL, w.HGDIOBJ)
_sig(shell32.Shell_NotifyIconW, w.BOOL, w.DWORD, POINTER(NOTIFYICONDATAW))
_sig(shell32.SetCurrentProcessExplicitAppUserModelID, ctypes.c_long, w.LPCWSTR)

_sig(kernel32.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
_sig(kernel32.CreateMutexW, w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
_sig(kernel32.GetCurrentProcessId, w.DWORD)
_sig(kernel32.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
_sig(kernel32.CloseHandle, w.BOOL, w.HANDLE)
_sig(advapi32.OpenProcessToken, w.BOOL, w.HANDLE, w.DWORD, POINTER(w.HANDLE))
_sig(advapi32.GetTokenInformation, w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, POINTER(w.DWORD))
_sig(advapi32.GetSidSubAuthorityCount, POINTER(ctypes.c_ubyte), w.LPVOID)
_sig(advapi32.GetSidSubAuthority, POINTER(w.DWORD), w.LPVOID, w.DWORD)
_sig(dwmapi.DwmSetWindowAttribute, ctypes.c_long, w.HWND, w.DWORD, w.LPCVOID, w.DWORD)


# ---- clipboard ---------------------------------------------------------------

# Written next to our own text so the capture can tell it apart from a real Ctrl+C.
OWN_FORMAT = user32.RegisterClipboardFormatW("ClipApp.OwnWrite")
# The standard ways password managers and the like ask to be left out of clipboard tools.
_EXCLUDE = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
_VIEWER_IGNORE = user32.RegisterClipboardFormatW("Clipboard Viewer Ignore")
_CAN_INCLUDE = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
_CAN_UPLOAD = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")
CF_HTML = user32.RegisterClipboardFormatW("HTML Format")
CF_RTF = user32.RegisterClipboardFormatW("Rich Text Format")
_DROP_EFFECT = user32.RegisterClipboardFormatW("Preferred DropEffect")


class ClipTooBig(Exception):
    """An image bigger than MAX_IMAGE_BYTES: not kept."""


def _open_clipboard(hwnd, tries: int = 12) -> bool:
    """Another program can be holding the clipboard for a moment: retry briefly."""
    for attempt in range(tries):
        if user32.OpenClipboard(hwnd):
            return True
        time.sleep(0.01 + 0.005 * attempt)
    return False


def _excluded_from_history() -> bool:
    if user32.IsClipboardFormatAvailable(_EXCLUDE) or user32.IsClipboardFormatAvailable(_VIEWER_IGNORE):
        return True
    if user32.IsClipboardFormatAvailable(_CAN_INCLUDE):
        handle = user32.GetClipboardData(_CAN_INCLUDE)
        ptr = kernel32.GlobalLock(handle) if handle else None
        if ptr:
            try:
                return ctypes.c_uint32.from_address(ptr).value == 0
            finally:
                kernel32.GlobalUnlock(handle)
    return False


def _text_on_open_clipboard() -> str | None:
    handle = user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        chars = kernel32.GlobalSize(handle) // 2
        return ctypes.wstring_at(ptr, chars).split("\0", 1)[0]
    finally:
        kernel32.GlobalUnlock(handle)


def _bytes_on_open_clipboard(fmt: int, limit: int) -> bytes | None:
    """A format's bytes, or None if it isn't there or is bigger than `limit`."""
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    size = kernel32.GlobalSize(handle)
    if not size or size > limit:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        kernel32.GlobalUnlock(handle)


def read_text(hwnd) -> str | None:
    """The clipboard's text, or None if there is none or it must not be captured."""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard(hwnd):
        return None
    try:
        if user32.IsClipboardFormatAvailable(OWN_FORMAT) or _excluded_from_history():
            return None
        return _text_on_open_clipboard()
    finally:
        user32.CloseClipboard()


def read_clip(hwnd) -> Clip | None:
    """What the clipboard holds, or None when there is nothing to keep or it must not be
    captured. Files win over text, and text (with its formatting) over an image: Excel,
    for one, puts a picture of the cells next to their text. Raises ClipTooBig."""
    if not any(user32.IsClipboardFormatAvailable(fmt) for fmt in (CF_UNICODETEXT, CF_HDROP, CF_DIB)):
        return None
    if not _open_clipboard(hwnd):
        return None
    dib = None
    try:
        if user32.IsClipboardFormatAvailable(OWN_FORMAT) or _excluded_from_history():
            return None
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            data = _bytes_on_open_clipboard(CF_HDROP, MAX_RICH_BYTES)
            paths = paths_from_hdrop(data) if data else []
            if paths:
                return Clip(FILES, "\n".join(paths))
        text = _text_on_open_clipboard() if user32.IsClipboardFormatAvailable(CF_UNICODETEXT) else None
        if text is not None and text.strip():
            formats = {}
            for name, fmt in (("html", CF_HTML), ("rtf", CF_RTF)):
                if user32.IsClipboardFormatAvailable(fmt):
                    data = _bytes_on_open_clipboard(fmt, MAX_RICH_BYTES)
                    if data:
                        formats[name] = data
            return Clip(TEXT, text, formats)
        if user32.IsClipboardFormatAvailable(CF_DIB):
            handle = user32.GetClipboardData(CF_DIB)
            if handle and kernel32.GlobalSize(handle) > MAX_IMAGE_BYTES:
                raise ClipTooBig()
            dib = _bytes_on_open_clipboard(CF_DIB, MAX_IMAGE_BYTES)
    finally:
        user32.CloseClipboard()
    if not dib:
        return None
    # The thumbnail is made after closing the clipboard, so other programs aren't kept waiting.
    return Clip(IMAGE, describe_image(dib), {"dib": dib}, thumbnail(dib, png_from_rgba))


def clear_if_holds(hwnd, texts) -> bool:
    """Empties the clipboard if it still holds one of these texts."""
    wanted = {text.replace("\r\n", "\n") for text in texts}
    if not wanted or not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard(hwnd):
        return False
    try:
        current = _text_on_open_clipboard()
        return current is not None and current.replace("\r\n", "\n") in wanted and bool(user32.EmptyClipboard())
    finally:
        user32.CloseClipboard()


def _global_bytes(data: bytes):
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return None
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def _unicode(text: str) -> bytes:
    return re.sub(r"\r?\n", "\r\n", text).encode("utf-16-le") + b"\0\0"


def write_clip(hwnd, clip: Clip, plain: bool = False) -> bool:
    """Puts a copy back on the clipboard, marked as ours so it is not captured again, and
    asking Windows to keep it out of its own clipboard history (Win+V) and cloud sync.
    plain: the text alone, without the formatting it was copied with."""
    if clip.kind == FILES:
        # Always as a copy: pasting from the history must never move the files themselves.
        parts = [(CF_HDROP, hdrop(clip.paths)), (_DROP_EFFECT, struct.pack("<I", 1)),
                 (CF_UNICODETEXT, _unicode("\n".join(clip.paths)))]
    elif clip.kind == IMAGE:
        if not clip.formats.get("dib"):
            return False
        parts = [(CF_DIB, clip.formats["dib"])]
    elif clip.kind != TEXT:
        return False
    else:
        parts = [(CF_UNICODETEXT, _unicode(clip.text))]
        if not plain:
            parts += [(fmt, clip.formats[name]) for name, fmt in (("html", CF_HTML), ("rtf", CF_RTF))
                      if clip.formats.get(name)]
    parts += [(OWN_FORMAT, b"\x01"), (_CAN_INCLUDE, bytes(4)), (_CAN_UPLOAD, bytes(4))]
    handles = [(fmt, _global_bytes(data)) for fmt, data in parts if data]
    if len(handles) != len([data for _fmt, data in parts if data]) or not _open_clipboard(hwnd):
        for _fmt, handle in handles:
            if handle:
                kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        for index, (fmt, handle) in enumerate(handles):
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)
                if index == 0:  # the copy itself didn't go: give up on the rest
                    for _other, rest in handles[1:]:
                        kernel32.GlobalFree(rest)
                    return False
        return True
    finally:
        user32.CloseClipboard()


def write_text(hwnd, text: str) -> bool:
    return write_clip(hwnd, Clip.plain(text))


# ---- keyboard ----------------------------------------------------------------

def key_is_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def wait_key_release(vk: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while key_is_down(vk) and time.monotonic() < deadline:
        time.sleep(0.005)


def _key(vk: int, up: bool = False) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    event.u.ki = KEYBDINPUT(wVk=vk, wScan=user32.MapVirtualKeyW(vk, 0),
                            dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=0)
    return event


_KEY_NAMES = {VK_SPACE: "Espaço", 0x0D: "Enter", 0x09: "Tab", 0x08: "Backspace", 0x2E: "Delete", 0x2D: "Insert",
              0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown", 0x25: "←", 0x26: "↑", 0x27: "→",
              0x28: "↓", 0x1B: "Esc", 0x13: "Pause", 0x2C: "PrintScreen"}
MODIFIER_KEYS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def key_name(vk: int) -> str:
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if vk in _KEY_NAMES:
        return _KEY_NAMES[vk]
    name = ctypes.create_unicode_buffer(32)  # the keyboard layout's own name: "Ç", "´", "Num 5"
    if user32.GetKeyNameTextW(user32.MapVirtualKeyW(vk, 0) << 16, name, 32) and name.value:
        return name.value.capitalize() if len(name.value) > 1 else name.value.upper()
    return f"Tecla {vk}"


def hotkey_text(mods: int, vk: int) -> str:
    parts = [label for flag, label in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))
             if mods & flag]
    return "+".join([*parts, key_name(vk)])


def held_modifiers() -> int:
    """The modifier keys down right now, as RegisterHotKey flags."""
    mods = 0
    for vks, flag in (((VK_CONTROL,), MOD_CONTROL), ((VK_MENU,), MOD_ALT), ((VK_SHIFT,), MOD_SHIFT),
                      ((VK_LWIN, VK_RWIN), MOD_WIN)):
        if any(key_is_down(vk) for vk in vks):
            mods |= flag
    return mods


def send_ctrl_v() -> bool:
    """Types Ctrl+V. Ctrl is always pressed first, so a release at the wrong moment
    can't turn this into a typed "v"; it is only let go if the user wasn't holding it."""
    ctrl_held = key_is_down(VK_CONTROL)
    events = [_key(VK_CONTROL), _key(VK_V), _key(VK_V, up=True)]
    if not ctrl_held:
        events.append(_key(VK_CONTROL, up=True))
    array = (INPUT * len(events))(*events)
    return user32.SendInput(len(events), array, sizeof(INPUT)) == len(events)


# ---- windows -----------------------------------------------------------------

_OWN_PID = kernel32.GetCurrentProcessId()
# Things that take the foreground without being somewhere you would paste into.
_NOT_TARGETS = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow",
                "Windows.UI.Core.CoreWindow", "Progman", "WorkerW", "#32768"}


def _window_pid(hwnd) -> int:
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    return pid.value


def is_own_window(hwnd) -> bool:
    return _window_pid(hwnd) == _OWN_PID


_sig(user32.GetClipboardOwner, w.HWND)
_sig(user32.IsWindowVisible, w.BOOL, w.HWND)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, w.HWND)
_sig(kernel32.QueryFullProcessImageNameW, w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, POINTER(w.DWORD))
_WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
_sig(user32.EnumWindows, w.BOOL, _WNDENUMPROC, w.LPARAM)


def program_name(pid: int) -> str | None:
    """The program's file name in lower case ("chrome.exe"), or None if Windows won't say."""
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        path = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(process, 0, path, byref(size)):
            return None
        return path.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(process)


def clipboard_source_program() -> str | None:
    """The program the copy came from: the clipboard's owner, or else the one in front."""
    owner = user32.GetClipboardOwner()
    hwnd = owner if owner and not is_own_window(owner) else user32.GetForegroundWindow()
    if not hwnd or is_own_window(hwnd):
        return None
    return program_name(_window_pid(hwnd))


def open_programs() -> list[str]:
    """The programs with a window open now, by file name, for picking one to ignore."""
    names = set()

    def visit(hwnd, _param):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 and not is_own_window(hwnd):
            name = program_name(_window_pid(hwnd))
            if name:
                names.add(name)
        return True

    user32.EnumWindows(_WNDENUMPROC(visit), 0)
    return sorted(names)


def integrity_level(pid: int) -> int | None:
    """How much Windows trusts a process: 0x2000 for a normal program, 0x3000 for one run as
    administrator. None if Windows won't say (system processes)."""
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        token = w.HANDLE()
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, byref(token)):
            return None
        try:
            size = w.DWORD()
            advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, byref(size))
            if not size.value:
                return None
            label = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, label, size.value, byref(size)):
                return None
            sid = ctypes.c_void_p.from_buffer(label).value  # TOKEN_MANDATORY_LABEL starts with the SID
            last = advapi32.GetSidSubAuthorityCount(sid)[0] - 1
            return advapi32.GetSidSubAuthority(sid, last)[0]
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


_OWN_INTEGRITY = integrity_level(_OWN_PID)


def input_blocked(hwnd) -> bool:
    """True when Windows drops our simulated keys before they reach this window, because its
    program runs with more trust than ours, usually "as administrator" (UIPI)."""
    if not hwnd or _OWN_INTEGRITY is None:
        return False
    level = integrity_level(_window_pid(hwnd))
    return level is not None and level > _OWN_INTEGRITY


def is_paste_target(hwnd) -> bool:
    if not hwnd or is_own_window(hwnd):
        return False
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)
    return name.value not in _NOT_TARGETS


def focus_window(hwnd, timeout: float = 0.4) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.01)
    return False


class GUITHREADINFO(Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hwndActive", w.HWND), ("hwndFocus", w.HWND),
                ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND), ("hwndMoveSize", w.HWND),
                ("hwndCaret", w.HWND), ("rcCaret", w.RECT)]


class MONITORINFO(Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


_sig(user32.GetGUIThreadInfo, w.BOOL, w.DWORD, POINTER(GUITHREADINFO))
_sig(user32.ClientToScreen, w.BOOL, w.HWND, POINTER(w.POINT))
_sig(user32.MonitorFromPoint, w.HANDLE, w.POINT, w.DWORD)
_sig(user32.GetMonitorInfoW, w.BOOL, w.HANDLE, POINTER(MONITORINFO))


def caret_point(hwnd) -> tuple[int, int] | None:
    """Below the text cursor of the window's program, on screen, when it uses the classic
    Windows caret. Browsers and many modern apps draw their own and give None."""
    if not hwnd:
        return None
    info = GUITHREADINFO(cbSize=sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(user32.GetWindowThreadProcessId(hwnd, None), byref(info)) or not info.hwndCaret:
        return None
    caret = info.rcCaret
    if caret.right - caret.left <= 0 and caret.bottom - caret.top <= 0:
        return None
    point = w.POINT(caret.left, caret.bottom)
    if not user32.ClientToScreen(info.hwndCaret, byref(point)):
        return None
    return point.x, point.y


def cursor_point() -> tuple[int, int]:
    point = w.POINT()
    user32.GetCursorPos(byref(point))
    return point.x, point.y


def work_area_at(x: int, y: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the monitor at this point, without the taskbar."""
    info = MONITORINFO(cbSize=sizeof(MONITORINFO))
    monitor = user32.MonitorFromPoint(w.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    if monitor and user32.GetMonitorInfoW(monitor, byref(info)):
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    return virtual_screen()


def style_popup(hwnd, border: str) -> None:
    """Rounded corners and a thin border for a window drawn without a title bar."""
    corner = ctypes.c_int(DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, byref(corner), sizeof(corner))
    _set_color(hwnd, DWMWA_BORDER_COLOR, border)


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of all monitors together."""
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    return left, top, left + user32.GetSystemMetrics(78), top + user32.GetSystemMetrics(79)


def _set_color(hwnd, attribute: int, color: str) -> None:
    value = w.DWORD(int(color[5:7] + color[3:5] + color[1:3], 16))  # Windows wants 0x00BBGGRR
    dwmapi.DwmSetWindowAttribute(hwnd, attribute, byref(value), sizeof(value))


def style_titlebar(hwnd, caption: str, border: str, text: str, dark: bool = True) -> None:
    """A title bar in the window's own colors. Colors need Windows 11; before that they
    are ignored and the bar is just dark or light."""
    value = ctypes.c_int(1 if dark else 0)
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, byref(value), sizeof(value))
    _set_color(hwnd, DWMWA_CAPTION_COLOR, caption)
    _set_color(hwnd, DWMWA_BORDER_COLOR, border)
    _set_color(hwnd, DWMWA_TEXT_COLOR, text)


def style_overlay(hwnd, border: str) -> None:
    """A window that never takes focus or clicks: safe to show while the user is pasting."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT)
    corner = ctypes.c_int(DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, byref(corner), sizeof(corner))
    _set_color(hwnd, DWMWA_BORDER_COLOR, border)


WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK = 0x02B1, 0x7
try:
    _wtsapi32 = ctypes.WinDLL("wtsapi32")
    _sig(_wtsapi32.WTSRegisterSessionNotification, w.BOOL, w.HWND, w.DWORD)
    _sig(_wtsapi32.WTSUnRegisterSessionNotification, w.BOOL, w.HWND)
except OSError:
    _wtsapi32 = None


def watch_session_lock(hwnd) -> None:
    """Has Windows send WM_WTSSESSION_CHANGE to this window, e.g. when the PC is locked."""
    if _wtsapi32:
        _wtsapi32.WTSRegisterSessionNotification(hwnd, 0)  # NOTIFY_FOR_THIS_SESSION


def stop_watching_session_lock(hwnd) -> None:
    if _wtsapi32:
        _wtsapi32.WTSUnRegisterSessionNotification(hwnd)


def set_app_id(app_id: str) -> None:
    """Without this, the taskbar files the window under Python, with Python's icon."""
    shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def apps_use_light_theme() -> bool:
    """Whether Windows is set to light mode for apps (Settings > Personalization > Colors)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return False  # older Windows, which had no light mode for apps


def set_menu_theme(dark: bool) -> None:
    """Native menus in this process (the tray's included) follow the window's theme.
    uxtheme, Windows 10 1903+; exported by ordinal only."""
    try:
        uxtheme = ctypes.WinDLL("uxtheme")
        set_mode = uxtheme[135]  # SetPreferredAppMode
        set_mode.argtypes = [ctypes.c_int]
        set_mode(2 if dark else 3)  # force dark, force light
        uxtheme[136]()  # FlushMenuThemes
    except (OSError, AttributeError):
        pass


def icon_sizes() -> tuple[int, int]:
    """The (small, big) icon sizes Windows uses at the current scale: 24 and 48 at 150%."""
    return user32.GetSystemMetrics(SM_CXSMICON) or 16, user32.GetSystemMetrics(SM_CXICON) or 32


def set_dpi_aware() -> None:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except OSError:
        pass


# ---- icon --------------------------------------------------------------------

def _rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float, zoom: float) -> float:
    """Coverage (0..1) of a pixel by a rounded rectangle, with an edge one pixel soft."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    qx, qy = abs(x - cx) - (x1 - x0) / 2 + r, abs(y - cy) - (y1 - y0) / 2 + r
    outside = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5
    distance = outside + min(max(qx, qy), 0) - r
    return max(0.0, min(1.0, 0.5 - distance * zoom))


def _over(dst: tuple, color: tuple, alpha: float) -> tuple:
    r, g, b, a = dst
    out = alpha + a * (1 - alpha)
    if out <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    mix = lambda c, d: (c * alpha + d * a * (1 - alpha)) / out
    return (mix(color[0], r), mix(color[1], g), mix(color[2], b), out)


def _icon_rgba(rgb: tuple[int, int, int], size: int) -> bytes:
    """The logo: a stack of cards seen from the front, in `rgb`, with lines of text on the front
    one. The two behind peek out above it, narrower and lighter, and a transparent gap keeps
    the cards apart on any taskbar color. Designed on a 32x32 grid; below 32 pixels the front
    card gets two thicker lines instead of three, so 16 and 24 stay crisp."""
    zoom = size / 32
    pixels = bytearray(size * size * 4)
    white = (245, 247, 250)
    bottom, gap = 30, 2
    cards = ((8.5, 0.5, 23.5, 3.5, 0.40), (5.5, 4.5, 26.5, 4, 0.70), (3, 9, 29, 5, 1.0))  # left, top, right, corner, opacity, from the back
    if size >= 32:
        lines, thick = ((8, 14, 24), (8, 19, 24), (8, 24, 18)), 2.6  # left, top, right
    else:
        lines, thick = ((8, 14, 24), (8, 20, 19)), 4
    for py in range(size):
        for px in range(size):
            x, y = (px + 0.5) / zoom, (py + 0.5) / zoom
            c = (0.0, 0.0, 0.0, 0.0)
            for left, top, right, corner, opacity in cards:
                hole = _rounded_rect(x, y, left - gap, top - gap, right + gap, bottom + gap, corner + gap, zoom)
                c = (c[0], c[1], c[2], c[3] * (1 - hole))
                c = _over(c, rgb, opacity * _rounded_rect(x, y, left, top, right, bottom, corner, zoom))
            for left, top, right in lines:
                c = _over(c, white, _rounded_rect(x, y, left, top, right, top + thick, thick / 2, zoom))
            i = (py * size + px) * 4
            pixels[i:i + 4] = bytes((round(c[0]), round(c[1]), round(c[2]), round(c[3] * 255)))
    return bytes(pixels)


def png_from_rgba(rgba: bytes, width: int, height: int) -> bytes:
    """A PNG, which is what Tk takes for images with soft edges."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def icon_png(rgb: tuple[int, int, int], size: int) -> bytes:
    """The logo as a PNG, for the window and the taskbar."""
    return png_from_rgba(_icon_rgba(rgb, size), size, size)


def make_icon(rgb: tuple[int, int, int], size: int | None = None):
    """The icon as a Windows icon handle, for the tray. Drawn at the size the tray shows
    (24 at 150%), since one Windows shrinks from 32 comes out blurred. The bitmap wants BGRA."""
    size = size or icon_sizes()[0]
    rgba = bytearray(_icon_rgba(rgb, size))
    rgba[0::4], rgba[2::4] = rgba[2::4], rgba[0::4]
    buffer = bytes(rgba)
    color = gdi32.CreateBitmap(size, size, 1, 32, buffer)
    mask = gdi32.CreateBitmap(size, size, 1, 1, bytes(size * size // 8))
    info = ICONINFO(fIcon=True, xHotspot=0, yHotspot=0, hbmMask=mask, hbmColor=color)
    icon = user32.CreateIconIndirect(byref(info))
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    return icon
