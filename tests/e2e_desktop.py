"""End-to-end test on the real desktop: real clipboard, real hotkeys, real paste.

Opens the app (with a throwaway data folder) and a small target window, then presses
keys as a user would and checks what actually arrived in the target. It takes the
foreground for ~20 seconds and overwrites the clipboard (text is put back at the end;
an image or files on the clipboard are lost). Keys are only sent while the target is in
front, so nothing lands in another program.

    py tests/e2e_desktop.py [--shots DIR]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from ctypes import byref, sizeof
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import struct  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clipapp import APP_NAME, clips, win32  # noqa: E402
from clipapp.engine import WINDOW_CLASS, WM_QUIT_REQUEST  # noqa: E402
from clipapp.store import Store  # noqa: E402
from clipapp.win32 import user32  # noqa: E402

TARGET_TITLE = "ClipApp E2E target"
TARGET_CODE = r"""
import os, sys, tkinter as tk
out = sys.argv[1]
root = tk.Tk(); root.title(%r); root.attributes("-topmost", True)
root.geometry("420x220+" + (sys.argv[2] if len(sys.argv) > 3 else "80") + "+" + (sys.argv[3] if len(sys.argv) > 3 else "80"))
text = tk.Text(root); text.pack(fill="both", expand=True); text.focus_force()
last = None
def dump():
    global last
    now = text.get("1.0", "end-1c")
    if now != last:
        try:  # swapped in whole, so a reader never sees a half-written file
            with open(out + ".tmp", "w", encoding="utf-8", newline="") as f: f.write(now)
            os.replace(out + ".tmp", out)
            last = now
        except OSError: pass  # the test is reading it right now: next tick
    root.after(30, dump)
def clear(_e=None): text.delete("1.0", "end"); return "break"
root.bind("<F9>", clear)
dump(); root.mainloop()
""" % TARGET_TITLE

VK_F9, VK_MENU = 0x78, 0x12
failures: list[str] = []
passed = 0


def check(name: str, got, expected) -> None:
    global passed
    if got == expected:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}\n       got      {got!r}\n       expected {expected!r}")


def read_target(path: Path) -> str:
    for _ in range(20):
        try:
            return path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError:  # the target is swapping the file in, or hasn't written it yet
            time.sleep(0.02)
    raise Abort(f"could not read {path}")


def test_area() -> tuple[int, int, int, int]:
    """Where the test's windows go: a second monitor when there is one, so the main one stays
    free to look at. Keyboard focus and the clipboard are still shared: don't type or copy."""
    class Info(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.DWORD), ("rcMonitor", ctypes.wintypes.RECT),
                    ("rcWork", ctypes.wintypes.RECT), ("dwFlags", ctypes.wintypes.DWORD)]

    areas = []
    visit_type = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HANDLE, ctypes.wintypes.HDC,
                                    ctypes.POINTER(ctypes.wintypes.RECT), ctypes.wintypes.LPARAM)

    def visit(monitor, _dc, _rect, _data):
        info = Info(cbSize=ctypes.sizeof(Info))
        ctypes.windll.user32.GetMonitorInfoW(ctypes.wintypes.HANDLE(monitor), byref(info))
        work = info.rcWork
        areas.append((bool(info.dwFlags & 1), (work.left, work.top, work.right, work.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, visit_type(visit), 0)
    secondary = [area for primary, area in areas if not primary]
    return (secondary or [area for _primary, area in areas] or [(0, 0, 1920, 1080)])[0]


def target_position() -> list[str]:
    left, top, _right, _bottom = test_area()
    return [str(left + 40), str(top + 40)]


def app_geometry() -> str:
    """The app's window, inside the test area (in pixels: the tests run DPI-aware, like the app)."""
    left, top, right, bottom = test_area()
    width, height = 540, min(840, bottom - top - 120)
    return f"{width}x{height}+{max(left, right - width - 40)}+{max(top, bottom - height - 80)}"


def quiet_popen(args: list, **kwargs) -> subprocess.Popen:
    """Doesn't inherit our output pipe, so a stuck child can't hold the test's caller open."""
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def keys(*events) -> None:
    """events: (vk, is_up) in order."""
    array = (win32.INPUT * len(events))(*(win32._key(vk, up=up) for vk, up in events))
    user32.SendInput(len(events), array, sizeof(win32.INPUT))


class Abort(Exception):
    pass


def clipboard_owner_window():
    """A window that answers messages, like any real program that copies something.
    Windows tells the last copier when the clipboard is emptied and waits for the reply,
    so a window nobody services would stall the app's next paste."""
    created = threading.Event()
    holder = []

    def pump() -> None:
        holder.append(user32.CreateWindowExW(0, "STATIC", "e2e", 0, 0, 0, 0, 0, None, None, None, None))
        created.set()
        msg = win32.w.MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            user32.DispatchMessageW(byref(msg))

    threading.Thread(target=pump, daemon=True).start()
    created.wait(5)
    return holder[0]


class Desktop:
    def __init__(self, tmp: Path, shots: Path | None):
        self.tmp, self.shots = tmp, shots
        self.out = tmp / "target.txt"
        self.hwnd_clip = clipboard_owner_window()
        env = dict(os.environ, CLIPAPP_DATA=str(tmp), CLIPAPP_DEBUG="1")
        self.env = env
        self.target_proc = None
        seed = Store(tmp / "clips.db")  # tests/e2e_ui.py covers the privacy notice
        seed.set("privacy_notice", "hide")
        seed.set("geometry", app_geometry())
        seed.close()
        self.app = quiet_popen([sys.executable, str(ROOT / "PasteStack.pyw")], env=env)
        try:
            self._open_windows(tmp)
        except Abort:
            self.app.kill()
            if self.target_proc:
                self.target_proc.kill()
            raise

    def _open_windows(self, tmp: Path) -> None:
        deadline = time.monotonic() + 10
        while not user32.FindWindowW(WINDOW_CLASS, None):
            if time.monotonic() > deadline or self.app.poll() is not None:
                raise Abort("the app did not start; see clipapp.log")
            time.sleep(0.05)
        time.sleep(0.8)
        self.target_proc = quiet_popen([sys.executable, "-c", TARGET_CODE, str(self.out), *target_position()])
        deadline = time.monotonic() + 8
        while not user32.FindWindowW(None, TARGET_TITLE):
            if time.monotonic() > deadline:
                raise Abort("target window did not open")
            time.sleep(0.05)
        self.target = user32.FindWindowW(None, TARGET_TITLE)
        self.store = Store(tmp / "clips.db")
        self.list_id = self.store.lists()[0]["id"]

    def front(self) -> None:
        """Puts the target in front, or stops the test: never type into anything else."""
        for _attempt in range(10):
            if user32.GetForegroundWindow() == self.target:
                return
            keys((VK_MENU, False), (VK_MENU, True))  # an Alt tap lifts the foreground lock
            user32.SetForegroundWindow(self.target)
            time.sleep(0.15)
        raise Abort("could not bring the target to the front (did someone click elsewhere?)")

    def still_front(self) -> None:
        """For keys sent while Ctrl is held, where front() can't step in without breaking the
        sequence: stops the test instead of typing into whatever came to the front."""
        if user32.GetForegroundWindow() != self.target:
            keys((win32.VK_CONTROL, True))
            raise Abort("another window came to the front during the test")

    def hotkey_cycle(self) -> None:
        self.front()
        keys((win32.VK_CONTROL, False), (win32.VK_SHIFT, False), (win32.VK_Q, False), (win32.VK_Q, True),
             (win32.VK_SHIFT, True), (win32.VK_CONTROL, True))
        time.sleep(0.2)

    def copy(self, text: str, *formats: str) -> None:
        """A copy made by another program."""
        data = text.encode("utf-16-le") + b"\0\0"
        handle = win32._global_bytes(data)
        assert win32._open_clipboard(self.hwnd_clip)
        try:
            user32.EmptyClipboard()
            user32.SetClipboardData(win32.CF_UNICODETEXT, handle)
            for name in formats:
                user32.SetClipboardData(user32.RegisterClipboardFormatW(name), win32._global_bytes(b"\0\0\0\0"))
        finally:
            user32.CloseClipboard()
        time.sleep(0.25)

    def ctrl_v(self, hold: float = 0.04) -> None:
        self.front()
        keys((win32.VK_CONTROL, False), (win32.VK_V, False))
        time.sleep(hold)
        keys((win32.VK_V, True), (win32.VK_CONTROL, True))
        time.sleep(0.25)

    def typed(self) -> str:
        time.sleep(0.1)
        return read_target(self.out)

    def clear_target(self) -> None:
        self.front()
        keys((VK_F9, False), (VK_F9, True))
        time.sleep(0.1)

    def items(self) -> list[str]:
        return [r["text"] for r in self.store.list_items(self.list_id)]

    def copy_formats(self, parts: list) -> None:
        """A copy made by another program, with these (format, bytes) on the clipboard."""
        assert win32._open_clipboard(self.hwnd_clip)
        try:
            user32.EmptyClipboard()
            for fmt, data in parts:
                user32.SetClipboardData(fmt, win32._global_bytes(data))
        finally:
            user32.CloseClipboard()
        time.sleep(0.35)

    def clipboard_bytes(self, fmt: int) -> bytes | None:
        assert win32._open_clipboard(self.hwnd_clip)
        try:
            return win32._bytes_on_open_clipboard(fmt, win32.MAX_IMAGE_BYTES)
        finally:
            user32.CloseClipboard()

    def ctrl_v_is_taken(self) -> bool:
        """True while the app holds Ctrl+V, i.e. the paste mode is on."""
        probe = 0xB000
        if user32.RegisterHotKey(None, probe, win32.MOD_CONTROL, win32.VK_V):
            user32.UnregisterHotKey(None, probe)
            return False
        return True

    def shot(self, name: str) -> None:
        if not self.shots:
            return
        from PIL import ImageGrab

        hwnd = user32.FindWindowW("TkTopLevel", APP_NAME)
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, byref(rect))
        time.sleep(0.15)
        ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(self.shots / f"{name}.png")

    def close(self) -> None:
        hwnd = user32.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            user32.PostMessageW(hwnd, WM_QUIT_REQUEST, 0, 0)
        try:
            check("app exits cleanly when asked", self.app.wait(timeout=5), 0)
        except subprocess.TimeoutExpired:
            check("app exits cleanly when asked", "still running", 0)
            self.app.kill()
        self.target_proc.kill()
        self.store.close()


def run(d: Desktop) -> None:
    print("capture")
    d.copy("primeira cópia")
    check("a copy from another program reaches the history", d.store.history()[0]["text"], "primeira cópia")
    d.copy("segredo", "ExcludeClipboardContentFromMonitorProcessing")
    check("password-manager flag: not captured", d.store.history("segredo"), [])
    d.copy("primeira cópia")
    check("same text is not duplicated in history", len(d.store.history("primeira")), 1)
    check("capture alone does not touch the list", d.items(), [])

    print("record a list with the hotkey, paste in order")
    d.hotkey_cycle()
    check("record mode does not hold Ctrl+V", d.ctrl_v_is_taken(), False)
    for text in ("um", "dois", "três 😀\nsegunda linha"):
        d.copy(text)
    d.copy("um")  # copying the same thing again is a new entry in the list
    check("each copy entered the list", d.items(), ["um", "dois", "três 😀\nsegunda linha", "um"])
    d.shot("lists_recording")
    d.hotkey_cycle()
    check("paste mode holds Ctrl+V", d.ctrl_v_is_taken(), True)
    d.shot("lists_pasting")
    d.clear_target()
    d.ctrl_v()
    check("1st Ctrl+V pastes the first item", d.typed(), "um")
    check("...and removes it from the list", d.items(), ["dois", "três 😀\nsegunda linha", "um"])
    win_v = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
    cloud = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")
    check("pasted text asks to stay out of Win+V and the cloud",
          (bool(user32.IsClipboardFormatAvailable(win_v)), bool(user32.IsClipboardFormatAvailable(cloud))), (True, True))
    d.ctrl_v()
    check("2nd Ctrl+V pastes the second item", d.typed(), "umdois")
    d.ctrl_v()
    check("3rd keeps accents, emoji and line breaks", d.typed(), "umdoistrês 😀\nsegunda linha")
    check("own writes are not captured back", d.store.history()[0]["text"], "um")
    d.ctrl_v()
    check("4th finishes the list", d.items(), [])
    check("paste mode turns itself off when empty", d.ctrl_v_is_taken(), False)
    before = d.typed()
    d.ctrl_v()
    check("Ctrl+V is normal again afterwards", d.typed(), before + "um")

    print("reverse order")
    d.store.set_reversed(d.list_id, True)
    d.hotkey_cycle()
    for text in ("a", "b", "c"):
        d.copy(text)
    check("reversed list shows newest first", d.items(), ["c", "b", "a"])
    d.hotkey_cycle()
    d.clear_target()
    d.ctrl_v()
    d.ctrl_v()
    d.ctrl_v()
    check("pastes newest to oldest", d.typed(), "cba")
    check("list drained", d.items(), [])
    d.store.set_reversed(d.list_id, False)

    print("images, files and formatting")
    from test_clips import make_dib

    dib = make_dib(320, 200, color=(30, 140, 220))
    d.copy_formats([(win32.CF_DIB, dib)])
    top = d.store.history()[0]
    check("an image copied elsewhere is kept as an image", (top["kind"], top["text"]), ("image", "Imagem 320 × 200"))
    check("...with a thumbnail", bool(top["has_thumb"]), True)
    html = ("Version:0.9\r\nStartHTML:0000000097\r\nEndHTML:0000000170\r\nStartFragment:0000000131\r\n"
            "EndFragment:0000000134\r\n<html><body><!--StartFragment--><b>oi</b><!--EndFragment--></body></html>").encode()
    d.copy_formats([(win32.CF_UNICODETEXT, "oi negrito".encode("utf-16-le") + b"\0\0"), (win32.CF_HTML, html)])
    top = d.store.history()[0]
    check("text with formatting is kept with it", d.store.clip("history", top["id"]).formats.get("html"), html)
    folder = Path(tempfile.mkdtemp(prefix="pastestack_files_"))
    files = [folder / "relatório.txt", folder / "foto.png"]
    for path in files:
        path.write_text("x", encoding="utf-8")
    d.copy_formats([(win32.CF_HDROP, clips.hdrop([str(p) for p in files]))])
    top = d.store.history()[0]
    check("files copied in Explorer are kept as files", (top["kind"], top["text"]), ("files", "\n".join(map(str, files))))

    d.store.add_list_item(d.list_id, d.store.clip("history", d.store.history()[2]["id"]))  # the image
    d.store.add_list_item(d.list_id, d.store.clip("history", d.store.history()[1]["id"]))  # the formatted text
    d.store.add_list_item(d.list_id, d.store.clip("history", d.store.history()[0]["id"]))  # the files
    d.hotkey_cycle()  # record
    d.hotkey_cycle()  # paste
    d.clear_target()
    d.ctrl_v()
    check("an image goes back on the clipboard byte for byte", d.clipboard_bytes(win32.CF_DIB), dib)
    d.ctrl_v()
    check("formatted text pastes its text", d.typed(), "oi negrito")
    check("...with its formatting next to it", d.clipboard_bytes(win32.CF_HTML), html)
    d.clear_target()
    d.ctrl_v()
    check("files paste as their paths where only text goes", d.typed(), "\n".join(map(str, files)))
    check("...and as files for Explorer", clips.paths_from_hdrop(d.clipboard_bytes(win32.CF_HDROP) or b""),
          [str(p) for p in files])
    check("...always as a copy, never a move", d.clipboard_bytes(user32.RegisterClipboardFormatW("Preferred DropEffect")),
          struct.pack("<I", 1))
    check("list done", d.items(), [])
    d.store.set("keep_formatting", "0")
    d.store.add_list_item(d.list_id, d.store.clip("history", d.store.history()[1]["id"]))
    d.hotkey_cycle()
    d.hotkey_cycle()
    d.clear_target()
    d.ctrl_v()
    check("with formatting turned off, only the text goes", (d.typed(), d.clipboard_bytes(win32.CF_HTML)),
          ("oi negrito", None))
    d.store.set("keep_formatting", "1")
    for path in files:
        path.unlink()
    folder.rmdir()

    print("key held down, Ctrl kept down, fast presses")
    for text in ("x1", "x2", "x3", "x4", "x5"):
        d.store.add_list_item(d.list_id, text)
    d.hotkey_cycle()  # record
    d.hotkey_cycle()  # paste
    d.clear_target()
    d.front()
    keys((win32.VK_CONTROL, False))
    for _ in range(6):  # what the keyboard sends while a key is held
        d.still_front()
        keys((win32.VK_V, False))
        time.sleep(0.05)
    keys((win32.VK_V, True))
    time.sleep(0.3)
    check("holding V pastes only one item", d.typed(), "x1")
    d.still_front()
    keys((win32.VK_V, False), (win32.VK_V, True))
    time.sleep(0.3)
    d.still_front()
    keys((win32.VK_V, False), (win32.VK_V, True))
    time.sleep(0.3)
    keys((win32.VK_CONTROL, True))
    time.sleep(0.2)
    check("Ctrl held, tapping V pastes one each", d.typed(), "x1x2x3")
    d.front()
    keys((win32.VK_CONTROL, False))
    for _ in range(2):
        d.still_front()
        keys((win32.VK_V, False))
        time.sleep(0.04)
        keys((win32.VK_V, True))
        time.sleep(0.08)
    keys((win32.VK_CONTROL, True))
    time.sleep(0.5)
    check("two quick presses arrive in order, no repeats", d.typed(), "x1x2x3x4x5")
    check("mode off at the end", d.ctrl_v_is_taken(), False)

    print("quick paste with the real shortcut")
    d.copy("primeiro do colar rápido")
    d.copy("segundo do colar rápido")
    d.clear_target()
    d.front()
    keys((win32.VK_CONTROL, False), (win32.VK_SHIFT, False), (win32.VK_V, False), (win32.VK_V, True),
         (win32.VK_SHIFT, True), (win32.VK_CONTROL, True))
    time.sleep(0.8)
    popup = user32.FindWindowW("TkTopLevel", f"{APP_NAME} colar rápido")
    check("Ctrl+Shift+V opens the quick paste list", bool(popup), True)
    in_front = bool(popup) and user32.GetForegroundWindow() == popup
    check("...in front, ready to type", in_front, True)
    if in_front:  # never press Enter anywhere else
        keys((0x28, False), (0x28, True), (0x0D, False), (0x0D, True))  # Down, Enter
        time.sleep(0.8)
        check("Down and Enter paste the second item where you were", d.typed(), "primeiro do colar rápido")
        check("...and the list closes", bool(user32.FindWindowW("TkTopLevel", f"{APP_NAME} colar rápido")), False)

    print("what reaches the disk")
    files = b"".join(path.read_bytes() for path in d.tmp.iterdir() if path.name.startswith("clip"))
    for text in ("primeira cópia", "segunda linha", "segredo"):
        check(f"{text!r} not readable in the database or the log",
              (text.encode("utf-8") in files, text.encode("utf-16-le") in files), (False, False))

    print("second launch")
    second = quiet_popen([sys.executable, str(ROOT / "PasteStack.pyw")], env=d.env)
    try:
        check("a second launch exits right away", second.wait(timeout=6), 0)
    except subprocess.TimeoutExpired:
        check("a second launch exits right away", "still running", 0)
        second.kill()
    time.sleep(0.4)

    print("screens")
    d.store.add_saved("Rua das Flores, 123 - São Paulo", title="Endereço")
    d.store.add_saved("Atenciosamente,\nEquipe")
    for text in ("Primeiro parágrafo do relatório", "https://exemplo.com/pagina", "SELECT * FROM clientes WHERE ativo = 1"):
        d.store.add_list_item(d.list_id, text)
    d.shot("after_second_launch")


def refuse_if_app_is_open() -> None:
    """A running copy would take the hotkeys, and closing "the app" would close that one."""
    if user32.FindWindowW(WINDOW_CLASS, None):
        sys.exit("Feche o app primeiro (bandeja, Sair): este teste usa os mesmos atalhos.")


def main() -> int:
    refuse_if_app_is_open()
    win32.set_dpi_aware()
    shots = None
    if "--shots" in sys.argv:
        shots = Path(sys.argv[sys.argv.index("--shots") + 1])
        shots.mkdir(parents=True, exist_ok=True)

    helper = clipboard_owner_window()
    previous = win32.read_text(helper)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        desktop = None
        try:
            desktop = Desktop(Path(tmp), shots)
            run(desktop)
        except Abort as error:
            failures.append(str(error))
            print(f"ABORTED: {error}")
        except Exception:  # noqa: BLE001
            failures.append("crashed")
            traceback.print_exc()
        finally:
            if desktop:
                desktop.close()
            log = Path(tmp) / "clipapp.log"
            if log.exists():
                lines = log.read_text(encoding="utf-8").splitlines()
                shown = lines if failures else [line for line in lines if " DEBUG " not in line and " INFO " not in line]
                if shown:
                    print("--- app log ---")
                    for line in shown:
                        print(line)

    if previous is not None:
        win32.write_text(helper, previous)
        print("(clipboard text put back)")
    print(f"\n{passed} ok, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
