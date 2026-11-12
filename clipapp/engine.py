"""The part that keeps running in the background, on its own thread.

A hidden window receives what Windows announces: the clipboard changed, a global
hotkey was pressed, the tray icon was clicked, another program came to the front.
All clipboard writes and simulated keystrokes happen here; the Tk window asks for
them through call() and hears back through the `emit` queue.

How "Ctrl+V pastes the next item" works: while the paste mode is on, Ctrl+V is a
registered hotkey, so a real Ctrl+V reaches us instead of the program in front.
We put the next item on the clipboard and press Ctrl+V for the user. The hotkey is
only registered during that mode, and this way the app never sees other keystrokes
(a keyboard hook would see everything the user types).
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from ctypes import byref, sizeof

from . import APP_NAME, win32
from .clips import FILES, Clip
from .store import Store
from .win32 import user32

log = logging.getLogger(__name__)

OFF, RECORD, PASTE = "off", "record", "paste"

WINDOW_CLASS = "ClipAppEngineWindow"
WM_CALL = win32.WM_APP + 1
WM_SHOW = win32.WM_APP + 2  # sent by a second launch of the app
WM_TRAY = win32.WM_APP + 3
WM_QUIT_REQUEST = win32.WM_APP + 4  # lets another process close the app cleanly

HK_PASTE = 3  # Ctrl+V, only while the paste mode is on: not one the user sets
# The shortcuts the user can change: name, what it does, default (modifiers, virtual key).
HOTKEYS = (
    ("window", "Abrir e fechar a janela", (win32.MOD_CONTROL | win32.MOD_SHIFT, win32.VK_SPACE)),
    ("quick", "Colar rápido no cursor", (win32.MOD_CONTROL | win32.MOD_SHIFT, win32.VK_V)),
    ("cycle", "Gravar, colar e desligar a lista", (win32.MOD_CONTROL | win32.MOD_SHIFT, win32.VK_Q)),
)
_HOTKEY_IDS = {"window": 1, "cycle": 2, "quick": 4}
_PASTE_MODS = win32.MOD_CONTROL | win32.MOD_NOREPEAT


def load_hotkeys(store: Store) -> dict:
    """name -> (modifiers, virtual key), or None for one the user turned off."""
    keys = {}
    for name, _what, default in HOTKEYS:
        saved = store.get(f"hotkey_{name}")
        if saved == "off":
            keys[name] = None
            continue
        try:
            mods, vk = (int(part) for part in saved.split(":"))
            keys[name] = (mods, vk)
        except (AttributeError, ValueError):
            keys[name] = default
    return keys


def save_hotkeys(store: Store, keys: dict) -> None:
    for name, combo in keys.items():
        store.set(f"hotkey_{name}", "off" if combo is None else f"{combo[0]}:{combo[1]}")

TIMER_CAPTURE, TIMER_PRUNE = 1, 2
PRUNE_EVERY_MS = 60_000  # the history's limits apply even if nothing new is copied
CAPTURE_DELAY_MS = 90  # programs often update the clipboard several times for one copy
SAME_COPY_WINDOW = 0.3
MAX_CHARS = 2_000_000
HOTKEY_GAP = 0.03  # the simulated Ctrl+V must get through before the hotkey is back
SETTLE = 0.09  # the program in front reads the clipboard after the keystroke: don't change it yet
UNDO_LIMIT = 100

ID_SHOW, ID_RECORD, ID_PASTE, ID_PAUSE, ID_QUIT, ID_SETTINGS, ID_LOCK = 1, 2, 3, 4, 5, 6, 7

_STATE_COLORS = {OFF: (110, 168, 254), RECORD: (240, 97, 109), PASTE: (76, 195, 138), "paused": (139, 146, 158)}
APP_ICON_COLOR = _STATE_COLORS[OFF]


class Engine(threading.Thread):
    def __init__(self, store: Store, emit):
        super().__init__(name="engine", daemon=True)
        self.store = store
        self._emit = emit
        self.mode = OFF
        # Nothing is captured until the privacy notice has been read, when it is going to show,
        # and a pause the user chose lasts until they resume, even across restarts.
        self.awaiting_notice = store.get("privacy_notice") != "hide"
        self.paused = self.awaiting_notice or store.get("paused") == "1"
        self.target = None  # the program a double-click pastes into
        self.undo: list = []  # items pasted out of lists, newest last. Only in memory.
        self.hwnd = None
        self.ready = threading.Event()
        self._calls: queue.SimpleQueue = queue.SimpleQueue()
        self._paste_hotkey = False
        self._last_capture = ("", 0.0)
        self._icons: dict = {}
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.hotkeys = load_hotkeys(store)
        self.hotkey_failures: list[str] = []  # names Windows refused, taken by another program

        self.active_list = 0
        self.pick_active_list()

    def pick_active_list(self) -> None:
        """The list from last time, or the first one. Reads ids only, so it works while locked."""
        ids = self.store.list_ids()
        try:
            saved = int(self.store.get("active_list", ""))
        except ValueError:
            saved = None
        self.active_list = saved if saved in ids else (ids[0] if ids else 0)

    # ---- called from the window's thread ------------------------------------

    def call(self, fn, *args) -> None:
        """Runs fn on the engine thread, in order."""
        self._calls.put((fn, args))
        user32.PostMessageW(self.hwnd, WM_CALL, 0, 0)

    def stop(self) -> None:
        user32.PostMessageW(self.hwnd, win32.WM_CLOSE, 0, 0)

    # ---- thread and hidden window -------------------------------------------

    def run(self) -> None:
        instance = win32.kernel32.GetModuleHandleW(None)
        self._wndproc = win32.WNDPROC(self._on_message)  # kept: Windows holds on to this pointer
        win_class = win32.WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=instance, lpszClassName=WINDOW_CLASS)
        user32.RegisterClassW(byref(win_class))
        self.hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, APP_NAME, 0, 0, 0, 0, 0, None, None, instance, None)
        if not self.hwnd:
            log.critical("could not create the engine window: error %s", ctypes.get_last_error())
            return

        user32.AddClipboardFormatListener(self.hwnd)
        self._foreground_proc = win32.WINEVENTPROC(self._on_foreground)
        self._hook = user32.SetWinEventHook(win32.EVENT_SYSTEM_FOREGROUND, win32.EVENT_SYSTEM_FOREGROUND, None,
                                            self._foreground_proc, 0, 0, win32.WINEVENT_OUTOFCONTEXT)
        foreground = user32.GetForegroundWindow()
        if win32.is_paste_target(foreground):
            self.target = foreground

        self._register_hotkeys()

        self._icons = {state: win32.make_icon(rgb) for state, rgb in _STATE_COLORS.items()}
        self._tray(win32.NIM_ADD)
        win32.watch_session_lock(self.hwnd)  # to lock along with Windows, when there is a password
        self._prune()
        user32.SetTimer(self.hwnd, TIMER_PRUNE, PRUNE_EVERY_MS, None)
        self.ready.set()

        msg = win32.w.MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

    def _on_message(self, hwnd, message, wparam, lparam):
        try:
            if message == win32.WM_CLIPBOARDUPDATE:
                user32.SetTimer(hwnd, TIMER_CAPTURE, CAPTURE_DELAY_MS, None)
            elif message == win32.WM_TIMER and wparam == TIMER_CAPTURE:
                user32.KillTimer(hwnd, TIMER_CAPTURE)
                self._capture()
            elif message == win32.WM_TIMER and wparam == TIMER_PRUNE:
                self._prune()
            elif message == win32.WM_HOTKEY:
                self._on_hotkey(wparam)
            elif message == WM_CALL:
                self._drain_calls()
            elif message == WM_TRAY:
                self._on_tray(lparam & 0xFFFF)
            elif message == WM_SHOW:
                self._emit(("show",))
            elif message == WM_QUIT_REQUEST:
                self._emit(("quit",))
            elif message == win32.WM_SETTINGCHANGE and lparam and ctypes.wstring_at(lparam) == "ImmersiveColorSet":
                self._emit(("theme",))  # Windows switched between light and dark
            elif message == win32.WM_WTSSESSION_CHANGE and wparam == win32.WTS_SESSION_LOCK:
                if self.store.has_password and self.store.get("lock_with_windows") != "0":
                    self.lock()
            elif message == self._taskbar_created:
                self._tray(win32.NIM_ADD)  # Explorer restarted: the icon must be added again
            elif message == win32.WM_DESTROY:
                self._cleanup()
                user32.PostQuitMessage(0)
                return 0
            else:
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception:  # noqa: BLE001 - an exception must not cross into Windows
            log.exception("engine message %s failed", hex(message))
        return 0

    def _drain_calls(self) -> None:
        while True:
            try:
                fn, args = self._calls.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - one failed action must not stop the engine
                log.exception("engine call %s failed", getattr(fn, "__name__", fn))

    def _cleanup(self) -> None:
        win32.stop_watching_session_lock(self.hwnd)
        user32.RemoveClipboardFormatListener(self.hwnd)
        for hk in (*_HOTKEY_IDS.values(), HK_PASTE):
            user32.UnregisterHotKey(self.hwnd, hk)
        if self._hook:
            user32.UnhookWinEvent(self._hook)
        self._tray(win32.NIM_DELETE)
        for icon in self._icons.values():
            user32.DestroyIcon(icon)

    def _on_foreground(self, _hook, _event, hwnd, _obj, _child, _thread, _time) -> None:
        try:
            if win32.is_paste_target(hwnd):
                self.target = hwnd
        except Exception:  # noqa: BLE001
            log.exception("foreground tracking failed")

    # ---- capture ------------------------------------------------------------

    def _prune(self) -> None:
        if self.store.locked:  # the limits still apply; only the clipboard check needs the key
            if self.store.prune_history():
                self._emit(("history",))
            return
        expiring = self.store.expiring_history()
        if expiring:
            self.store.prune_history()
            self.forget(expiring)  # expired is deleted for good too: off the clipboard as well
            self._emit(("history",))

    def ignored_programs(self) -> set[str]:
        """The programs whose copies aren't kept, when the user turned that on."""
        if self.store.get("ignore_apps_on") != "1":
            return set()
        return {name for name in self.store.get_secret("ignore_apps").split("\n") if name}

    def _capture(self) -> None:
        if self.paused or self.store.locked:  # locked, there is no key to encrypt anything with
            return
        ignored = self.ignored_programs()
        if ignored and win32.clipboard_source_program() in ignored:
            log.debug("capture: skipped a copy from an ignored program")  # never which one
            return
        try:
            clip = win32.read_clip(self.hwnd)
        except win32.ClipTooBig:
            self._hud("Imagem grande demais: não foi guardada", "warn")
            return
        if clip is None:
            return
        if len(clip.text) > MAX_CHARS:
            self._hud("Cópia grande demais: não foi guardada", "warn")
            return
        now = time.monotonic()
        identity = clip.identity()
        previous, at = self._last_capture
        if identity == previous and now - at < SAME_COPY_WINDOW:
            return
        self._last_capture = (identity, now)
        self.store.add_history(clip)
        self._emit(("history",))
        if self.mode == RECORD:
            self.store.add_list_item(self.active_list, clip)
            self._emit(("list",))
            size = self.store.list_size(self.active_list)
            self._hud(f"+1 em “{self.list_name()}” · {size} {'item' if size == 1 else 'itens'}", RECORD)

    # ---- modes --------------------------------------------------------------

    def set_mode(self, mode: str, announce: bool = True) -> None:
        if mode != OFF and self.store.locked:
            self._hud(f"Destrave o {APP_NAME} primeiro", "warn")
            self._emit(("show",))
            return
        if mode == RECORD and self.awaiting_notice:
            self._hud("Leia o aviso de privacidade antes de gravar", "warn")
            return
        if mode == PASTE:
            if not self.store.list_size(self.active_list):
                self._hud("Lista vazia: nada para colar", "warn")
                mode = OFF
            elif not self._paste_hotkey and not self._register_paste_hotkey():
                self._hud("Outro programa está usando o Ctrl+V", "warn")
                mode = OFF
        if mode != PASTE:
            self._unregister_paste_hotkey()
        changed, self.mode = self.mode != mode, mode
        if mode == RECORD and self.paused:
            self._remember_pause(False)
        self._tray(win32.NIM_MODIFY)
        self._emit(("mode",))
        if not changed or not announce:
            return
        if mode == RECORD:
            self._hud(f"Gravando em “{self.list_name()}”: copie com Ctrl+C", RECORD)
        elif mode == PASTE:
            size = self.store.list_size(self.active_list)
            self._hud(f"Colar “{self.list_name()}” · {size} {'item' if size == 1 else 'itens'}: use Ctrl+V", PASTE)
        else:
            self._hud("Modo de lista desligado", "info")

    def cycle_mode(self) -> None:
        self.set_mode({OFF: RECORD, RECORD: PASTE, PASTE: OFF}[self.mode])

    def lock(self) -> None:
        """Forgets the key, and everything decrypted the engine was holding on to."""
        if not self.store.has_password or self.store.locked:
            return
        self.set_mode(OFF, announce=False)
        self.undo = []
        self._last_capture = ("", 0.0)
        self.store.lock()
        self._tray(win32.NIM_MODIFY)
        self._emit(("locked",))

    def unlocked(self) -> None:
        """The window took the password (or started over): the tray and the lists catch up."""
        self.pick_active_list()
        self._tray(win32.NIM_MODIFY)
        self._emit(("mode",))
        self._emit(("list",))

    def _remember_pause(self, paused: bool) -> None:
        self.paused = paused
        self.store.set("paused", "1" if paused else "0")

    def set_paused(self, paused: bool) -> None:
        if not paused and self.awaiting_notice:
            self._hud("Leia o aviso de privacidade antes de retomar a captura", "warn")
            return
        self._remember_pause(paused)
        if paused and self.mode == RECORD:
            self.set_mode(OFF)
        self._tray(win32.NIM_MODIFY)
        self._emit(("mode",))
        self._hud("Captura pausada" if paused else "Captura ativa", "info")

    def close_notice(self, accepted: bool) -> None:
        """Only "Entendi" starts the capture, and not over a pause the user chose before.
        Closing the notice any other way leaves the capture paused."""
        self.awaiting_notice = False
        if accepted and self.store.get("paused") != "1":
            self.set_paused(False)
        else:
            self._hud("Captura pausada: retome quando quiser", "info")

    def set_active_list(self, list_id: int) -> None:
        self.active_list = list_id
        self.store.set("active_list", list_id)
        self._stop_pasting_if_empty()
        self._emit(("list",))
        self._emit(("mode",))

    # ---- pasting ------------------------------------------------------------

    def _register_paste_hotkey(self) -> bool:
        self._paste_hotkey = bool(user32.RegisterHotKey(self.hwnd, HK_PASTE, _PASTE_MODS, win32.VK_V))
        return self._paste_hotkey

    def _unregister_paste_hotkey(self) -> None:
        if self._paste_hotkey:
            user32.UnregisterHotKey(self.hwnd, HK_PASTE)
            self._paste_hotkey = False

    def _send_paste(self) -> bool:
        """Presses Ctrl+V for the program in front, without triggering our own hotkey.
        False if Windows refused the keystrokes."""
        armed = self._paste_hotkey
        if armed:
            user32.UnregisterHotKey(self.hwnd, HK_PASTE)
        sent = win32.send_ctrl_v()
        if armed:
            time.sleep(HOTKEY_GAP)
            if not user32.RegisterHotKey(self.hwnd, HK_PASTE, _PASTE_MODS, win32.VK_V):
                self._paste_hotkey = False
                self.set_mode(OFF)
        time.sleep(SETTLE)
        return sent

    # ---- shortcuts ----------------------------------------------------------

    def hotkey_label(self, name: str) -> str:
        """The shortcut as the user reads it, or "" when it is turned off."""
        combo = self.hotkeys.get(name)
        return win32.hotkey_text(*combo) if combo else ""

    def _unregister_hotkeys(self) -> None:
        for hk in _HOTKEY_IDS.values():
            user32.UnregisterHotKey(self.hwnd, hk)

    def _register_hotkeys(self) -> None:
        self._unregister_hotkeys()
        failed = []
        for name, combo in self.hotkeys.items():
            if combo and not user32.RegisterHotKey(self.hwnd, _HOTKEY_IDS[name], combo[0] | win32.MOD_NOREPEAT, combo[1]):
                failed.append(name)
        self.hotkey_failures = failed
        if failed:
            labels = [self.hotkey_label(name) for name in failed]
            log.warning("hotkeys already taken: %s", labels)
            self._emit(("warn", "Atalho ocupado por outro programa: " + ", ".join(labels)))
        else:
            self._emit(("warn", None))

    def set_hotkeys(self, keys: dict) -> None:
        self.hotkeys = dict(keys)
        save_hotkeys(self.store, self.hotkeys)
        self._register_hotkeys()
        self._emit(("hotkeys",))

    def suspend_hotkeys(self, suspended: bool) -> None:
        """While the settings wait for a new shortcut, pressing an old one must not run it."""
        if suspended:
            self._unregister_hotkeys()
        else:
            self._register_hotkeys()

    def _on_hotkey(self, hotkey: int) -> None:
        if hotkey == _HOTKEY_IDS["window"]:
            self._emit(("toggle", "hotkey"))
        elif hotkey == _HOTKEY_IDS["cycle"]:
            self.cycle_mode()
        elif hotkey == _HOTKEY_IDS["quick"]:
            self._emit(("quick",))
        elif hotkey == HK_PASTE:
            self._on_paste_key()

    def _on_paste_key(self) -> None:
        # Pasting only when V is released keeps a held key from draining the list:
        # V is not "down" for Windows after our simulated keystroke, so the key's
        # auto-repeat would count as a new press.
        log.debug("paste key: pressed, foreground %s", user32.GetForegroundWindow())
        win32.wait_key_release(win32.VK_V)
        foreground = user32.GetForegroundWindow()
        log.debug("paste key: released, foreground %s", foreground)
        if self.store.locked:
            self.set_mode(OFF)
            self._send_paste()
            return
        if win32.is_own_window(foreground):
            log.debug("paste key: own window in front, plain paste")
            self._send_paste()  # a normal paste into our own search box or editor
            return
        if win32.input_blocked(foreground):
            log.debug("paste key: the program in front runs as administrator")
            self._hud(f"Este programa roda como administrador e não aceita a colagem do {APP_NAME}", "warn")
            return
        item = self.store.next_item(self.active_list)
        if item is None:
            self.set_mode(OFF)
            self._send_paste()
            return
        clip = self.store.clip("list_items", item["id"])
        if clip is None:
            self._hud("Não consegui abrir este item: ele continua na lista", "warn")
            return
        if not self._write(clip):
            return
        log.debug("paste key: clipboard written, %s, %d characters", clip.kind, len(clip.text))  # never the text
        if not self._send_paste():
            self._hud("O Windows recusou a colagem: o item continua na lista", "warn")
            return
        self._remove_from_list(item)
        log.debug("paste key: done")
        remaining = self.store.list_size(self.active_list)
        if remaining:
            self._hud(f"Colado · {'falta' if remaining == 1 else 'faltam'} {remaining}", PASTE)
        else:
            self._hud("Lista concluída", PASTE)
            self.set_mode(OFF, announce=False)

    def _remove_from_list(self, item) -> None:
        self.store.delete_list_item(item["id"])
        self.undo.append(item)
        del self.undo[:-UNDO_LIMIT]
        self._emit(("list",))

    @property
    def keep_formatting(self) -> bool:
        return self.store.get("keep_formatting") != "0"

    def _write(self, clip: Clip, plain: bool = False) -> bool:
        """Puts a copy on the clipboard, or says why not."""
        if clip.kind == FILES:
            present = [path for path in clip.paths if os.path.exists(path)]
            if not present:
                self._hud("Esses arquivos não existem mais", "warn")
                return False
            if len(present) < len(clip.paths):
                missing = len(clip.paths) - len(present)
                self._hud(f"{missing} {'arquivo não existe' if missing == 1 else 'arquivos não existem'} mais: "
                          "colando o resto", "warn")
                clip = Clip(FILES, "\n".join(present))
        if not win32.write_clip(self.hwnd, clip, plain=plain or not self.keep_formatting):
            self._hud("A área de transferência está ocupada: tente de novo", "warn")
            return False
        return True

    def _paste_into_target(self, clip: Clip, plain: bool = False) -> bool:
        if not self._write(clip, plain):
            return False
        target = self.target
        if not target or not user32.IsWindow(target) or not win32.focus_window(target):
            self._hud("Copiado: clique onde quer colar e use Ctrl+V", "info")
            return False
        if win32.input_blocked(target):
            self._hud("Copiado: este programa roda como administrador, então cole você mesmo", "info")
            return False
        time.sleep(0.04)
        if not self._send_paste():
            self._hud("Copiado: o Windows recusou a colagem, então cole você mesmo", "info")
            return False
        return True

    def paste_text(self, text: str) -> None:
        self._paste_into_target(Clip.plain(text))

    def paste_item(self, table: str, item_id: int, plain: bool = False) -> None:
        """Pastes a history or repertoire item, with everything it was copied with."""
        clip = self.store.clip(table, item_id)
        if clip is None:
            self._hud("Não consegui abrir este item", "warn")
            return
        self._paste_into_target(clip, plain)

    def paste_list_item(self, item_id: int, plain: bool = False) -> None:
        item = self.store.get_list_item(item_id)
        clip = self.store.clip("list_items", item_id) if item else None
        if clip and self._paste_into_target(clip, plain):
            self._remove_from_list(item)
            self._stop_pasting_if_empty()

    def copy_text(self, text: str) -> None:
        if win32.write_text(self.hwnd, text):
            self._hud("Copiado", "info")

    def copy_item(self, table: str, item_id: int, plain: bool = False) -> None:
        clip = self.store.clip(table, item_id)
        if clip is not None and self._write(clip, plain):
            self._hud("Copiado", "info")

    # ---- deleting for good ---------------------------------------------------

    def forget(self, texts) -> None:
        """Deleted for good also means off the clipboard, if it is still sitting there."""
        self._last_capture = ("", 0.0)
        win32.clear_if_holds(self.hwnd, texts)

    def _stop_pasting_if_empty(self) -> None:
        if self.mode == PASTE and not self.store.list_size(self.active_list):
            self.set_mode(OFF)

    def delete_list_item(self, item_id: int) -> None:
        """The user deleted it, so unlike a pasted item it can't be undone."""
        item = self.store.get_list_item(item_id)
        if item:
            self.store.delete_list_item(item_id)
            self.forget({item["text"]})
            self._emit(("list",))
            self._stop_pasting_if_empty()

    def _purge_list(self, list_id: int) -> None:
        texts = {item["text"] for item in self.store.list_items(list_id)}
        texts |= {item["text"] for item in self.undo if item["list_id"] == list_id}
        self.undo = [item for item in self.undo if item["list_id"] != list_id]
        self.store.clear_list(list_id)
        self.forget(texts)

    def clear_list(self, list_id: int) -> None:
        self._purge_list(list_id)
        self._emit(("list",))
        self._stop_pasting_if_empty()

    def delete_list(self, list_id: int, next_list_id: int) -> None:
        self.set_active_list(next_list_id)
        self._purge_list(list_id)
        self.store.delete_list(list_id)
        self._emit(("list",))

    def undo_removal(self) -> None:
        if not self.undo:
            return
        item = self.undo.pop()
        if self.store.restore_list_item(item):
            self._hud("Item de volta na lista", "info")
        self._emit(("list",))

    # ---- feedback -----------------------------------------------------------

    def _hud(self, text: str, kind: str) -> None:
        self._emit(("hud", text, kind))

    def list_name(self) -> str:
        """The active list's name. The window asks too: it only reads the store."""
        if self.store.locked:
            return "?"
        row = self.store.get_list(self.active_list)
        return row["name"] if row else "?"

    # ---- tray ---------------------------------------------------------------

    def _state(self) -> str:
        return "paused" if self.paused or self.store.locked else self.mode

    def _tray(self, action: int) -> None:
        data = win32.NOTIFYICONDATAW(cbSize=sizeof(win32.NOTIFYICONDATAW), hWnd=self.hwnd, uID=1)
        if action != win32.NIM_DELETE:
            data.uFlags = win32.NIF_MESSAGE | win32.NIF_ICON | win32.NIF_TIP
            data.uCallbackMessage = WM_TRAY
            data.hIcon = self._icons[self._state()]
            tips = {OFF: "capturando", RECORD: "gravando lista", PASTE: "colando lista", "paused": "pausado"}
            data.szTip = f"{APP_NAME}: {'travado' if self.store.locked else tips[self._state()]}"
        win32.shell32.Shell_NotifyIconW(action, byref(data))

    def _on_tray(self, event: int) -> None:
        if event == win32.WM_LBUTTONUP:
            self._emit(("toggle", "tray"))
        elif event == win32.WM_RBUTTONUP:
            self._tray_menu()

    def _tray_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        if self.store.locked:
            entries = [(ID_SHOW, "Destravar…", False), None, (ID_QUIT, "Sair", False)]
        else:
            entries = [
                (ID_SHOW, "Abrir janela", False),
                (ID_SETTINGS, "Configurações", False),
                None,
                (ID_RECORD, "Gravar lista", self.mode == RECORD),
                (ID_PASTE, "Colar lista", self.mode == PASTE),
                (ID_PAUSE, "Pausar captura", self.paused),
                *([(ID_LOCK, "Travar agora", False)] if self.store.has_password else []),
                None,
                (ID_QUIT, "Sair", False),
            ]
        try:
            for entry in entries:
                if entry is None:
                    user32.AppendMenuW(menu, win32.MF_SEPARATOR, 0, None)
                else:
                    flags = win32.MF_STRING | (win32.MF_CHECKED if entry[2] else 0)
                    user32.AppendMenuW(menu, flags, entry[0], entry[1])
            point = win32.w.POINT()
            user32.GetCursorPos(byref(point))
            user32.SetForegroundWindow(self.hwnd)  # so that a click elsewhere closes the menu
            choice = user32.TrackPopupMenuEx(menu, win32.TPM_RIGHTBUTTON | win32.TPM_RETURNCMD,
                                             point.x, point.y, self.hwnd, None)
            user32.PostMessageW(self.hwnd, win32.WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        if choice == ID_SHOW:
            self._emit(("show",))
        elif choice == ID_SETTINGS:
            self._emit(("settings",))
        elif choice == ID_RECORD:
            self.set_mode(OFF if self.mode == RECORD else RECORD)
        elif choice == ID_PASTE:
            self.set_mode(OFF if self.mode == PASTE else PASTE)
        elif choice == ID_PAUSE:
            self.set_paused(not self.paused)
        elif choice == ID_LOCK:
            self.lock()
        elif choice == ID_QUIT:
            self._emit(("quit",))


def signal_running_instance() -> None:
    """A second launch asks the first one to open its window, then exits."""
    hwnd = user32.FindWindowW(WINDOW_CLASS, None)
    if hwnd:
        user32.AllowSetForegroundWindow(win32.ASFW_ANY)
        user32.PostMessageW(hwnd, WM_SHOW, 0, 0)
