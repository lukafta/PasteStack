"""End-to-end test of the window itself: buttons, rows, dialogs, tabs and the tray.

Runs the real app in this process (real engine, real clipboard, a real target window in
another process) with a throwaway data folder, and sends the widgets the same events a
mouse would. It takes the foreground for ~20 seconds and overwrites the clipboard (text
is put back at the end).

    py tests/e2e_ui.py [--shots DIR]
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import tempfile
import time
import traceback
from ctypes import byref
from pathlib import Path

TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["CLIPAPP_DATA"] = TMP.name
TEST_RUN_KEY = r"Software\PasteStackTest\Run"  # never the real Run key: removed at the end
os.environ["CLIPAPP_RUN_KEY"] = TEST_RUN_KEY

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import tkinter as tk  # noqa: E402

import e2e_desktop as base  # noqa: E402
from e2e_desktop import TARGET_CODE, TARGET_TITLE, VK_MENU, check, keys, quiet_popen  # noqa: E402

from clipapp import autostart, ui, win32  # noqa: E402
from clipapp.engine import OFF, PASTE, RECORD, WM_TRAY, Engine  # noqa: E402
from clipapp.store import Store  # noqa: E402
from clipapp.win32 import user32  # noqa: E402

LONG_TEXT = "Um texto bem comprido para ver se a linha corta direito com reticências " * 4
MULTILINE = "linha um\nlinha dois\nlinha três"
EMOJI = "ação 日本語 😀 fim"


class Driver:
    def __init__(self, shots: Path | None):
        self.shots = shots
        self.owner = base.clipboard_owner_window()
        self.out = Path(TMP.name) / "target.txt"
        self.target_proc = quiet_popen([sys.executable, "-c", TARGET_CODE, str(self.out), *base.target_position()])
        deadline = time.monotonic() + 8
        while not user32.FindWindowW(None, TARGET_TITLE):
            if time.monotonic() > deadline:
                raise base.Abort("target window did not open")
            time.sleep(0.05)
        self.target = user32.FindWindowW(None, TARGET_TITLE)

        self.store = Store(Path(TMP.name) / "clips.db")
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.store.set("geometry", base.app_geometry())  # on the second monitor, when there is one
        self.engine = Engine(self.store, self.events.put)
        self.engine.start()
        self.engine.ready.wait(5)
        self.app = ui.App(self.store, self.engine, self.events)
        self.app.first_show()

    # ---- helpers ------------------------------------------------------------

    @property
    def rows(self) -> ui.RowList:
        return self.app.rows

    def titles(self) -> list[str]:
        return [row.title for row in self.rows.rows]

    def list_texts(self) -> list[str]:
        return [r["text"] for r in self.store.list_items(self.engine.active_list)]

    def front_target(self) -> None:
        for _ in range(10):
            if user32.GetForegroundWindow() == self.target:
                return
            keys((VK_MENU, False), (VK_MENU, True))
            user32.SetForegroundWindow(self.target)
            time.sleep(0.15)
        raise base.Abort("could not bring the target to the front")

    def front_app(self) -> None:
        for _ in range(10):
            if win32.is_own_window(user32.GetForegroundWindow()):
                return
            keys((VK_MENU, False), (VK_MENU, True))
            self.app.root.focus_force()
            self.app.root.update()
            time.sleep(0.15)
        raise base.Abort("could not bring the app to the front")

    def copy(self, text: str) -> None:
        handle = win32._global_bytes(text.encode("utf-16-le") + b"\0\0")
        assert win32._open_clipboard(self.owner)
        try:
            user32.EmptyClipboard()
            user32.SetClipboardData(win32.CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()

    def typed(self) -> str:
        return base.read_target(self.out)

    def text_outside_rows(self) -> list[str]:
        """Text drawn past the canvas edges: cut off where the user can't read it."""
        width = self.rows.winfo_width()
        cut = []
        for item in self.rows.find_all():
            if self.rows.type(item) == "text":
                left, _top, right, _bottom = self.rows.bbox(item)
                if left < 0 or right > width:
                    cut.append(self.rows.itemcget(item, "text"))
        return cut

    def clear_target(self) -> None:
        self.front_target()
        keys((base.VK_F9, False), (base.VK_F9, True))

    def row_y(self, index: int) -> int:
        return index * self.rows.row_h + self.rows.row_h // 2 - self.rows.offset

    def click_action(self, index: int, slot: int) -> None:
        self.app.root.update()
        x, y = self.rows.action_center(slot), self.row_y(index)
        stamp = int(time.monotonic() * 1000)  # without a time, Tk sees every click as a double-click
        self.rows.event_generate("<Motion>", x=x, y=y)
        self.rows.event_generate("<Button-1>", x=x, y=y, time=stamp)
        self.rows.event_generate("<ButtonRelease-1>", x=x, y=y, time=stamp)
        self.app.root.update()

    def double_click(self, index: int) -> None:
        self.app.root.update()
        x, y = self.app.px(60), self.row_y(index)
        self.rows.event_generate("<Motion>", x=x, y=y)
        for _ in range(2):  # Tk recognizes two quick clicks in the same place as a double-click
            self.rows.event_generate("<Button-1>", x=x, y=y)
            self.rows.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.app.root.update()

    def drag(self, start: int, end: int) -> None:
        self.app.root.update()
        x = self.app.px(60)
        y0, y1 = self.row_y(start), end * self.rows.row_h - self.rows.offset
        self.rows.event_generate("<Motion>", x=x, y=y0)
        self.rows.event_generate("<Button-1>", x=x, y=y0)
        self.rows.event_generate("<B1-Motion>", x=x, y=(y0 + y1) // 2)
        self.rows.event_generate("<B1-Motion>", x=x, y=y1)
        self.rows.event_generate("<ButtonRelease-1>", x=x, y=y1)
        self.app.root.update()

    def dialogs(self) -> list[tk.Toplevel]:
        return [widget for widget in self.app.root.winfo_children()
                if isinstance(widget, tk.Toplevel) and widget is not self.app.hud.win]

    def has_dialog(self) -> bool:
        return bool(self.dialogs())

    def dialog(self) -> tk.Toplevel:
        """The one on top."""
        found = self.dialogs()
        if not found:
            raise AssertionError("no dialog open")
        return found[-1]

    def press(self, label: str) -> None:
        for button in self.find(self.dialog(), ui.Btn):
            if button.text == label:
                button._click(None)
                self.app.root.update()
                return
        raise AssertionError(f"no {label!r} button")

    def tick(self, index: int = 0) -> None:
        self.find(self.dialog(), tk.Checkbutton)[index].invoke()

    def switch(self, label: str) -> tk.Checkbutton:
        """The checkbox in the open dialog whose text contains `label`."""
        for box in self.find(self.dialog(), tk.Checkbutton):
            if label in box.cget("text"):
                return box
        raise AssertionError(f"no {label!r} checkbox")

    def db_bytes(self) -> bytes:
        return (Path(TMP.name) / "clips.db").read_bytes()

    def cipher_of(self, text: str) -> bytes:
        item = self.store.history(text)[0]
        return bytes(self.store._one("SELECT text FROM history WHERE id = ?", item["id"])["text"])

    @staticmethod
    def find(parent, kind):
        found = []
        for child in parent.winfo_children():
            if isinstance(child, kind):
                found.append(child)
            found.extend(Driver.find(child, kind))
        return found

    def shot(self, name: str, window=None) -> None:
        if not self.shots:
            return
        from PIL import ImageGrab

        self.app.root.update()
        widget = window or self.app.root
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(int(widget.wm_frame(), 16), byref(rect))
        ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(self.shots / f"{name}.png")

    def close(self) -> None:
        self.target_proc.kill()
        if self.app.root.winfo_exists():
            self.app.quit()
        self.store.close()


def script(d: Driver):
    app, store, engine = d.app, d.store, d.engine

    print("privacy notice")
    yield 400
    check("the app opens with the notice", d.has_dialog(), True)
    d.shot("0_privacy", d.dialog())
    check("capture waits for it", engine.paused, True)
    d.copy("copiado antes de aceitar")
    yield 250
    check("nothing is captured before accepting", store.history(), [])
    d.press("Entendi")
    yield 200
    check("accepting closes it", d.has_dialog(), False)
    check("accepting starts the capture", engine.paused, False)
    check("without the check, it comes back next time", store.get("privacy_notice") != "hide", True)
    check("...and capture waits again", Engine(store, lambda _event: None).paused, True)
    ui.open_privacy_notice(app, lambda _accepted: None)
    yield 200
    d.tick()
    d.press("Entendi")
    yield 100
    check("the check turns it off", store.get("privacy_notice"), "hide")
    check("...and capture starts straight away next time", Engine(store, lambda _event: None).paused, False)

    print("history")
    for text in ("alpha", LONG_TEXT, MULTILINE, EMOJI, "gamma"):
        d.copy(text)
        yield 220
    check("copies show up, newest first", d.titles()[0], "gamma")
    check("all of them", len(d.rows.rows), 5)
    check("line breaks described", "3 linhas" in d.rows.rows[2].meta, True)
    check("search hint visible from the start", bool(app.placeholder.winfo_ismapped()), True)
    d.rows.event_generate("<Motion>", x=d.app.px(60), y=d.row_y(1))
    yield 60
    check("no text cut off at the edges", d.text_outside_rows(), [])
    d.shot("1_history")

    d.click_action(0, 2)
    check("star saves to the repertoire", [r["text"] for r in store.saved()], ["gamma"])
    check("star turns on", d.rows.rows[0].actions[2][0], ui.I_STAR_ON)
    d.click_action(4, 1)
    check("+ adds to the current list", d.list_texts(), ["alpha"])
    d.click_action(4, 0)
    yield 300
    check("trash asks first", d.has_dialog(), True)
    check("...before deleting anything", len(store.history("alpha")), 1)
    d.shot("0_delete_notice", d.dialog())
    d.press("Cancelar")
    yield 100
    check("cancel keeps it", len(store.history("alpha")), 1)
    yield 600  # or Tk takes this click and the previous one for a double-click
    d.click_action(4, 0)
    yield 200
    d.press("Apagar de vez")
    yield 150
    check("confirming removes from history", store.history("alpha"), [])
    check("...and from the screen", "alpha" in d.titles(), False)
    check("without the check, it still asks next time", store.get("confirm_one") != "no", True)

    print("deleted for good")
    secret = "texto-para-apagar-de-vez"
    d.copy(secret)
    yield 250
    cipher = d.cipher_of(secret)
    check("the file holds it encrypted", cipher in d.db_bytes(), True)
    check("...and not readable", secret.encode() in d.db_bytes(), False)
    d.click_action(0, 0)
    yield 200
    d.tick()
    d.press("Apagar de vez")
    yield 300
    check("gone from the history", store.history(secret), [])
    check("overwritten in the file", cipher in d.db_bytes(), False)
    check("off the clipboard too", win32.read_text(d.owner), None)
    check("the check turns the notice off", store.get("confirm_one"), "no")
    d.copy("outro item")
    yield 250
    d.click_action(0, 0)
    yield 200
    check("the next delete doesn't ask", (d.has_dialog(), store.history("outro item")), (False, []))
    d.copy("fica no clipboard")
    yield 250
    d.click_action(1, 0)  # some other row
    yield 250
    check("deleting something else leaves the clipboard alone", win32.read_text(d.owner), "fica no clipboard")
    d.copy("gamma")
    yield 250
    store.delete_history(store.history("fica no clipboard")[0]["id"])
    app.refresh()

    d.search = app.search_var.set("GAM")
    yield 220
    check("search ignores case", d.titles(), ["gamma"])
    app.search_var.set("não existe")
    yield 220
    check("empty search result", d.titles(), [])
    d.shot("2_no_results")
    app.search_var.set("")
    yield 220
    check("clearing the search brings all back", len(d.rows.rows), 4)

    print("double-click pastes where you were")
    d.clear_target()
    yield 150
    d.front_app()
    d.double_click(0)
    yield 450
    check("text arrives in the previous program", d.typed(), "gamma")
    check("focus goes back to it", user32.GetForegroundWindow(), d.target)
    check("history keeps the item", len(d.rows.rows), 4)
    check("own paste is not captured again", store.history()[0]["text"], "gamma")

    print("repertoire")
    app.set_tab("saved")
    yield 60
    check("saved item listed", d.titles(), ["gamma"])
    d.front_app()
    app.new_saved()
    yield 450  # past the window-open animation, for the picture
    dialog = d.dialog()
    frame = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(int(dialog.wm_frame(), 16), byref(frame))
    check("dialog stays inside the screen", frame.right <= win32.virtual_screen()[2], True)
    check("window at the screen edge is accepted back", app._on_screen(app.root.geometry()), True)
    check("position on an unplugged monitor is refused", app._on_screen("400x500+99999+100"), False)
    d.shot("3_editor", dialog)
    d.find(dialog, tk.Entry)[0].insert(0, "Endereço")
    d.find(dialog, tk.Text)[0].insert("1.0", "Rua das Flores, 123\nSão Paulo")
    dialog.event_generate("<Control-Return>")
    yield 120
    check("new item appears with its title", d.titles()[0], "Endereço")
    check("title is searchable", [r["title"] for r in store.saved("ender")], ["Endereço"])
    d.click_action(0, 1)
    yield 200
    dialog = d.dialog()
    box = d.find(dialog, tk.Text)[0]
    check("editor opens with the text", box.get("1.0", "end-1c"), "Rua das Flores, 123\nSão Paulo")
    box.delete("1.0", "end")
    box.insert("1.0", "Rua Nova, 9")
    dialog.event_generate("<Control-Return>")
    yield 120
    check("edit is saved", store.saved("Nova")[0]["text"], "Rua Nova, 9")
    d.click_action(0, 1)
    yield 200
    d.dialog().event_generate("<Escape>")
    yield 120
    check("Esc cancels the editor", store.saved("Nova")[0]["text"], "Rua Nova, 9")
    d.shot("4_repertoire")
    d.clear_target()
    yield 150
    d.front_app()
    d.double_click(0)
    yield 450
    check("double-click pastes the saved text", d.typed(), "Rua Nova, 9")
    check("repertoire keeps it after pasting", len(store.saved()), 2)
    d.click_action(1, 2)
    check("+ from the repertoire", d.list_texts(), ["alpha", "gamma"])

    print("lists")
    app.set_tab("lists")
    yield 60
    for text in ("três", "quatro"):
        store.add_list_item(engine.active_list, text)
    app.refresh()
    app._sync_mode()
    check("list name on its button", app.list_btn.text, "Lista 1 · 4")
    check("rows numbered", [r.badge for r in d.rows.rows], ["1", "2", "3", "4"])
    check("next item marked", (d.rows.rows[0].accent, d.rows.rows[1].accent), (True, False))
    check("no text cut off in the list", d.text_outside_rows(), [])
    d.shot("5_list")

    d.drag(3, 0)
    yield 60
    check("drag to the top", d.list_texts(), ["quatro", "alpha", "gamma", "três"])
    d.drag(0, 4)
    yield 60
    check("drag to the bottom", d.list_texts(), ["alpha", "gamma", "três", "quatro"])

    app.order_btn._click(None)
    yield 60
    check("order button reverses", d.list_texts(), ["quatro", "três", "gamma", "alpha"])
    check("its label follows", app.order_btn.text, "Inversa")
    check("the top row is still the next one", d.titles()[0], "quatro")
    d.drag(3, 0)
    yield 60
    check("drag works in reverse too", d.list_texts(), ["alpha", "quatro", "três", "gamma"])
    d.shot("6_list_reversed")
    app.order_btn._click(None)
    yield 60
    check("reversing back", d.list_texts(), ["gamma", "três", "quatro", "alpha"])

    d.click_action(0, 1)
    yield 200
    dialog = d.dialog()
    box = d.find(dialog, tk.Text)[0]
    box.delete("1.0", "end")
    box.insert("1.0", "gamma editado")
    dialog.event_generate("<Control-Return>")
    yield 120
    check("edit a list item", d.list_texts()[0], "gamma editado")

    d.clear_target()
    yield 150
    d.front_app()
    d.double_click(1)
    yield 450
    check("double-click pastes that item", d.typed(), "três")
    check("...and takes it out of the list", d.list_texts(), ["gamma editado", "quatro", "alpha"])
    app.undo_btn._click(None)
    yield 150
    check("undo brings back a pasted item", d.list_texts(), ["gamma editado", "três", "quatro", "alpha"])
    d.click_action(1, 0)
    yield 200
    check("trash deletes it", d.list_texts(), ["gamma editado", "quatro", "alpha"])
    app.undo_btn._click(None)
    yield 150
    check("undo can't bring back a deleted item", d.list_texts(), ["gamma editado", "quatro", "alpha"])

    print("list buttons")
    d.front_app()
    app.record_btn._click(None)
    yield 150
    check("Gravar turns recording on", engine.mode, RECORD)
    check("its label follows", app.record_btn.text, "Gravando")
    d.copy("gravado pelo botão")
    yield 250
    check("copy goes into the list", d.list_texts()[-1], "gravado pelo botão")
    d.shot("7_recording")
    app.paste_btn._click(None)
    yield 150
    check("Colar switches to pasting", engine.mode, PASTE)
    check("Gravar is off again", app.record_btn.text, "Gravar")
    d.shot("8_pasting")
    app.paste_btn._click(None)
    yield 150
    check("Colar again turns it off", engine.mode, OFF)

    app._new_list()
    yield 200
    dialog = d.dialog()
    entry = d.find(dialog, tk.Entry)[0]
    entry.delete(0, "end")
    entry.insert(0, "Emails")
    dialog.event_generate("<Return>")
    yield 200
    check("new list is created and selected", app.list_btn.text, "Emails · 0")
    check("it starts empty", d.titles(), [])
    d.shot("9_empty_list")
    app.paste_btn._click(None)
    yield 150
    check("Colar on an empty list stays off", engine.mode, OFF)
    app._rename_list()
    yield 200
    dialog = d.dialog()
    entry = d.find(dialog, tk.Entry)[0]
    entry.delete(0, "end")
    entry.insert(0, "Respostas")
    dialog.event_generate("<Return>")
    yield 120
    check("rename", app.list_btn.text, "Respostas · 0")
    store.add_list_item(engine.active_list, "x")
    app.refresh()
    app._clear_list()
    yield 200
    check("emptying a list still asks: that notice is separate", d.has_dialog(), True)
    d.press("Apagar de vez")
    yield 150
    check("empty the list", d.list_texts(), [])
    app._delete_list()
    yield 200
    check("deleting a list asks", d.has_dialog(), True)
    d.tick()
    d.press("Apagar de vez")
    yield 200
    check("deleting goes back to another list", app.list_btn.text.startswith("Lista 1"), True)
    check("one list left", len(store.lists()), 1)
    check("its check turned that notice off", store.get("confirm_many"), "no")
    app._delete_list()
    yield 200
    check("no more asking for that", d.has_dialog(), False)
    check("deleting the last list leaves a new empty one", (len(store.lists()), d.titles()), (1, []))

    print("pause, pin, window")
    app.set_tab("history")
    yield 60
    app.pause_btn._click(None)
    yield 150
    check("pause", engine.paused, True)
    d.copy("durante a pausa")
    yield 250
    check("nothing captured while paused", store.history("durante"), [])
    d.shot("10_paused")
    app.pause_btn._click(None)
    yield 150
    d.copy("depois da pausa")
    yield 250
    check("capture resumes", store.history()[0]["text"], "depois da pausa")

    check("always on top by default", bool(app.root.attributes("-topmost")), True)
    app.pin_btn._click(None)
    check("pin turns it off", bool(app.root.attributes("-topmost")), False)
    check("...and is remembered", store.get("topmost"), "0")
    app.pin_btn._click(None)

    app.hide()
    yield 120
    check("X hides, doesn't quit", app.root.state(), "withdrawn")
    d.copy("copiado com a janela fechada")
    yield 250
    check("keeps capturing in the background", store.history()[0]["text"], "copiado com a janela fechada")
    d.front_target()
    keys((win32.VK_CONTROL, False), (win32.VK_SHIFT, False), (win32.VK_SPACE, False), (win32.VK_SPACE, True),
         (win32.VK_SHIFT, True), (win32.VK_CONTROL, True))
    yield 350
    check("hotkey opens the window", app.root.state(), "normal")
    check("...in front", win32.is_own_window(user32.GetForegroundWindow()), True)
    check("...showing what was copied meanwhile", d.titles()[0], "copiado com a janela fechada")
    keys((win32.VK_CONTROL, False), (win32.VK_SHIFT, False), (win32.VK_SPACE, False), (win32.VK_SPACE, True),
         (win32.VK_SHIFT, True), (win32.VK_CONTROL, True))
    yield 350
    check("hotkey closes it again", app.root.state(), "withdrawn")
    user32.PostMessageW(engine.hwnd, WM_TRAY, 1, win32.WM_LBUTTONUP)
    yield 350
    check("tray click opens it", app.root.state(), "normal")

    # Ctrl+1/2/3
    d.front_app()
    app.root.focus_force()
    yield 100
    app.root.event_generate("<Control-Key-2>")
    yield 80
    check("Ctrl+2 goes to the repertoire", app.tab, "saved")
    app.root.event_generate("<Control-Key-1>")
    yield 80

    # Arrow keys and Enter in the history.
    d.clear_target()
    yield 150
    d.front_app()
    app.search.focus_force()
    yield 80
    app.search.event_generate("<Down>")
    app.search.event_generate("<Down>")
    check("arrows select", d.rows.selected, 1)
    expected = d.rows.rows[1].text
    app.search.event_generate("<Return>")
    yield 450
    check("Enter pastes the selection", d.typed(), expected)

    print("taskbar")
    frame = int(app.root.wm_frame(), 16)
    check("it is a normal window, with a taskbar button",
          bool(user32.GetWindowLongPtrW(frame, win32.GWL_EXSTYLE) & win32.WS_EX_TOOLWINDOW), False)
    # Tk's iconphoto puts the icon on the window class, which is where the taskbar falls back to.
    class_icon = ctypes.windll.user32.GetClassLongPtrW
    class_icon.restype, class_icon.argtypes = ctypes.c_ssize_t, [ctypes.wintypes.HWND, ctypes.c_int]
    check("it has an icon for the taskbar", bool(class_icon(frame, -14)), True)  # GCLP_HICON
    app.root.iconify()
    yield 300
    check("minimizing keeps it on the taskbar, not in the tray", app.root.state(), "iconic")
    user32.PostMessageW(engine.hwnd, WM_TRAY, 1, win32.WM_LBUTTONUP)
    yield 350
    check("the tray icon brings it back", app.root.state(), "normal")
    app.root.iconify()
    yield 300
    d.front_target()
    keys((win32.VK_CONTROL, False), (win32.VK_SHIFT, False), (win32.VK_SPACE, False), (win32.VK_SPACE, True),
         (win32.VK_SHIFT, True), (win32.VK_CONTROL, True))
    yield 350
    check("so does the hotkey", app.root.state(), "normal")

    print("tray menu")
    d.front_app()
    user32.PostMessageW(engine.hwnd, WM_TRAY, 1, win32.WM_RBUTTONUP)
    yield 500
    if d.shots:  # only around the menu, not the whole desktop
        from PIL import ImageGrab

        cursor = win32.w.POINT()
        user32.GetCursorPos(byref(cursor))
        box = (cursor.x - 450, cursor.y - 450, cursor.x + 450, cursor.y + 450)
        ImageGrab.grab(bbox=box, all_screens=True).save(d.shots / "11_tray_menu.png")
    keys((0x1B, False), (0x1B, True))  # Esc closes it
    yield 300
    check("engine still alive after the menu", engine.is_alive(), True)

    print("window does not lose itself")
    app.hide()
    yield 100
    app.show()
    yield 150
    saved = store.get("geometry")
    check("position remembered", bool(saved), True)

    print("settings")
    check("defaults", store.history_limits(), (7, 100))
    store.set("confirm_one", "yes")
    store.set("confirm_many", "yes")
    store.clear_history()
    for n in range(5):
        store.add_history(f"item {n}")
    app.set_tab("history")
    app.open_settings()
    yield 450
    dialog = d.dialog()
    d.shot("12_settings", dialog)
    days, items = d.find(dialog, tk.Entry)[:2]
    check("shows the current limits", (days.get(), items.get()), ("7", "100"))
    for label in ("Abrir com o Windows", "aviso de privacidade", "apagar um item", "apagar vários"):
        d.switch(label)  # each is there, or this stops the test

    def fill(entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    fill(days, "abc")
    d.press("Salvar")
    yield 80
    check("refuses what isn't a number", (d.has_dialog(), store.history_limits()), (True, (7, 100)))
    fill(days, "0")
    d.press("Salvar")
    yield 80
    check("refuses 0 days", (d.has_dialog(), store.history_limits()), (True, (7, 100)))
    fill(days, "30")
    fill(items, "9999")
    d.press("Salvar")
    yield 80
    check("refuses too many items", (d.has_dialog(), store.history_limits()), (True, (7, 100)))

    fill(items, "3")
    d.press("Salvar")
    yield 300
    check("lowering the limit warns first", len(d.dialogs()), 2)
    d.shot("12_settings_warning", d.dialog())
    check("...before deleting anything", len(store.history()), 5)
    d.press("Cancelar")
    yield 150
    check("cancel keeps everything", (len(store.history()), store.history_limits()), (5, (7, 100)))
    check("...and settings stay open", len(d.dialogs()), 1)
    d.press("Salvar")
    yield 300
    d.press("Apagar de vez")
    yield 300
    check("confirming saves the new limits", store.history_limits(), (30, 3))
    check("...deletes what is over the limit", [r["text"] for r in store.history()], ["item 4", "item 3", "item 2"])
    check("...updates the screen", len(d.rows.rows), 3)
    check("...and closes", d.has_dialog(), False)
    store.add_history("item 5")
    check("the new limit keeps applying", len(store.history()), 3)

    app.open_settings()
    yield 300
    d.switch("aviso de privacidade").invoke()  # back on
    d.switch("apagar um item").invoke()  # off
    days, items = d.find(d.dialog(), tk.Entry)[:2]
    fill(days, "7")
    fill(items, "100")
    d.press("Salvar")
    yield 200
    check("switches are saved", (store.get("privacy_notice"), store.get("confirm_one")), ("show", "no"))
    check("raising the limit doesn't ask", d.has_dialog(), False)

    print("open with Windows (in a test key, not the real one)")
    check("off by default", autostart.is_enabled(), False)
    app.open_settings()
    yield 300
    d.switch("Abrir com o Windows").invoke()
    d.press("Salvar")
    yield 200
    check("the switch adds it", autostart.is_enabled(), True)
    check("...starting in the tray", autostart.command().endswith(autostart.TRAY_FLAG), True)
    app.open_settings()
    yield 300
    box = d.switch("Abrir com o Windows")
    check("settings show it on", bool(int(box.getvar(box.cget("variable")))), True)
    d.switch("Abrir com o Windows").invoke()
    d.press("Salvar")
    yield 200
    check("the switch removes it", autostart.is_enabled(), False)
    engine._emit(("settings",))
    yield 400
    check("the tray menu opens settings", d.has_dialog(), True)
    d.dialog().event_generate("<Escape>")
    yield 100

    app._clear_history()
    yield 250
    check("clearing the history asks", d.has_dialog(), True)
    d.press("Apagar de vez")
    yield 200
    check("clearing the history", store.history(), [])

    print("deletes on its own, with nobody touching anything (about a minute)")
    store.add_history("recente")
    store.add_history("oito dias atrás", now=time.time() - 8 * 86400)  # last, or the next copy would prune it
    app.refresh()
    check("old item there at first", len(store.history("oito dias")), 1)
    yield 62_000
    check("the old item went by itself", store.history("oito dias"), [])
    check("the recent one stayed", len(store.history("recente")), 1)
    check("the screen followed", d.titles(), ["recente"])


def remove_test_run_key() -> None:
    import winreg

    for path in (TEST_RUN_KEY, TEST_RUN_KEY.rsplit("\\", 1)[0]):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass


def main() -> int:
    base.refuse_if_app_is_open()
    win32.set_dpi_aware()
    win32.set_app_id("ClipApp.Test")
    shots = None
    if "--shots" in sys.argv:
        shots = Path(sys.argv[sys.argv.index("--shots") + 1])
        shots.mkdir(parents=True, exist_ok=True)

    helper = base.clipboard_owner_window()
    previous = win32.read_text(helper)
    driver = None
    try:
        driver = Driver(shots)
        errors: list = []
        tk.Tk.report_callback_exception = lambda _self, *exc: errors.append("".join(traceback.format_exception(*exc)))
        steps = script(driver)

        def advance() -> None:
            try:
                delay = next(steps)
            except StopIteration:
                driver.app.root.quit()
                return
            except base.Abort as error:
                base.failures.append(str(error))
                print(f"ABORTED: {error}")
                driver.app.root.quit()
                return
            except Exception:  # noqa: BLE001
                base.failures.append("crashed")
                traceback.print_exc()
                driver.app.root.quit()
                return
            driver.app.root.after(delay, advance)

        driver.app.root.after(300, advance)
        driver.app.root.mainloop()
        check("no errors inside window callbacks", errors, [])
    except base.Abort as error:
        base.failures.append(str(error))
        print(f"ABORTED: {error}")
    finally:
        if driver:
            driver.close()
        remove_test_run_key()

    if previous is not None:
        win32.write_text(helper, previous)
        print("(clipboard text put back)")
    print(f"\n{base.passed} ok, {len(base.failures)} failed")
    return 1 if base.failures else 0


if __name__ == "__main__":
    sys.exit(main())
