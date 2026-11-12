"""The newer features that need no desktop: run with `py -m unittest discover -s tests -p "test_*.py"`."""

import os
import sys
import unittest
import winreg
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipapp import APP_NAME, autostart  # noqa: E402

TEST_KEY = r"Software\PasteStackTest\UnitRun"


class AutostartTest(unittest.TestCase):
    """Uses a key of its own: the real Run key is never touched."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CLIPAPP_RUN_KEY": TEST_KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._remove_key)

    @staticmethod
    def _remove_key():
        for path in (TEST_KEY, TEST_KEY.rsplit("\\", 1)[0]):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except OSError:
                pass

    def test_off_until_turned_on(self):
        self.assertFalse(autostart.is_enabled())

    def test_on_and_off(self):
        autostart.set_enabled(True)
        self.assertTrue(autostart.is_enabled())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            value = winreg.QueryValueEx(key, APP_NAME)[0]
        self.assertIn(f"{APP_NAME}.pyw", value)
        self.assertTrue(value.endswith(autostart.TRAY_FLAG))
        autostart.set_enabled(False)
        self.assertFalse(autostart.is_enabled())

    def test_turning_off_twice_is_fine(self):
        autostart.set_enabled(False)
        autostart.set_enabled(False)

    def test_command_opens_without_a_console(self):
        self.assertIn("pythonw.exe", autostart.command().lower())

    def test_refresh_rewrites_a_stale_path(self):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, r'"C:\old\pythonw.exe" "C:\old\x.pyw" --tray')
        autostart.refresh()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            self.assertEqual(winreg.QueryValueEx(key, APP_NAME)[0], autostart.command())

    def test_refresh_leaves_it_off_when_off(self):
        autostart.refresh()
        self.assertFalse(autostart.is_enabled())


class PasswordTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        from clipapp.store import Store

        self.Store = Store
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.store = Store(self.path)
        self.store.add_history("segredo")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def reopen(self):
        self.store.close()
        self.store = self.Store(self.path)

    def test_no_password_by_default(self):
        self.assertFalse(self.store.has_password)
        self.assertFalse(self.store.locked)

    def test_with_a_password_it_opens_locked(self):
        from clipapp.crypto import LockedError

        self.store.set_password("senha-boa")
        self.reopen()
        self.assertTrue(self.store.locked)
        with self.assertRaises(LockedError):
            self.store.history()
        self.assertFalse(self.store.unlock("errada"))
        self.assertTrue(self.store.locked)
        self.assertTrue(self.store.unlock("senha-boa"))
        self.assertEqual(self.store.history()[0]["text"], "segredo")

    def test_the_key_windows_alone_could_open_is_gone(self):
        self.store.set_password("senha-boa")
        self.assertIsNone(self.store.get("vault_key"))
        self.assertIsNotNone(self.store.get("vault_key_pw"))

    def test_locking_forgets_the_key(self):
        self.store.set_password("senha-boa")
        self.store.lock()
        self.assertTrue(self.store.locked)
        self.assertTrue(self.store.unlock("senha-boa"))

    def test_lock_without_a_password_does_nothing(self):
        self.store.lock()
        self.assertFalse(self.store.locked)

    def test_works_while_locked_without_the_key(self):
        self.store.set_password("senha-boa")
        self.reopen()
        self.assertEqual(len(self.store.list_ids()), 1)
        self.assertEqual(self.store.prune_history(), 0)
        self.store.set("x", "1")

    def test_change_and_remove(self):
        self.store.set_password("primeira")
        self.assertTrue(self.store.check_password("primeira"))
        self.store.set_password("segunda")
        self.assertFalse(self.store.check_password("primeira"))
        self.reopen()
        self.assertFalse(self.store.unlock("primeira"))
        self.assertTrue(self.store.unlock("segunda"))
        self.store.remove_password()
        self.reopen()
        self.assertFalse(self.store.locked)
        self.assertEqual(self.store.history()[0]["text"], "segredo")

    def test_starting_over_deletes_everything(self):
        self.store.set_password("senha-boa")
        self.reopen()
        self.store.wipe()
        self.assertFalse(self.store.locked)
        self.assertFalse(self.store.has_password)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(len(self.store.lists()), 1)
        self.reopen()
        self.assertFalse(self.store.locked)

    def test_engine_starts_while_locked(self):
        from clipapp.engine import Engine

        self.store.set_password("senha-boa")
        self.reopen()
        engine = Engine(self.store, lambda _event: None)
        self.assertEqual(engine.active_list, self.store.list_ids()[0])
        self.assertEqual(engine.list_name(), "?")


class IgnoredProgramsTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        from clipapp.engine import Engine
        from clipapp.store import Store

        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.store = Store(self.path)
        self.store.set("privacy_notice", "hide")
        self.engine = Engine(self.store, lambda _event: None)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def capture_from(self, program, text):
        """What _capture does with a copy of `text` made in `program` (no clipboard involved)."""
        from clipapp import win32
        from clipapp.clips import Clip

        with mock.patch.object(win32, "clipboard_source_program", return_value=program), \
                mock.patch.object(win32, "read_clip", return_value=Clip.plain(text)):
            self.engine._capture()

    def test_off_by_default(self):
        self.store.set_secret("ignore_apps", "banco.exe")
        self.capture_from("banco.exe", "saldo")
        self.assertEqual([r["text"] for r in self.store.history()], ["saldo"])

    def test_when_on_copies_from_listed_programs_are_not_kept(self):
        self.store.set("ignore_apps_on", "1")
        self.store.set_secret("ignore_apps", "banco.exe\nkeepass.exe")
        self.capture_from("banco.exe", "saldo")
        self.capture_from("chrome.exe", "receita de bolo")
        self.assertEqual([r["text"] for r in self.store.history()], ["receita de bolo"])

    def test_on_with_an_empty_list_keeps_everything(self):
        self.store.set("ignore_apps_on", "1")
        self.capture_from("banco.exe", "saldo")
        self.assertEqual(len(self.store.history()), 1)

    def test_the_list_is_encrypted(self):
        self.store.set_secret("ignore_apps", "aplicativo-do-banco.exe")
        self.assertNotIn(b"aplicativo-do-banco", self.path.read_bytes())
        self.assertEqual(self.store.get_secret("ignore_apps"), "aplicativo-do-banco.exe")

    def test_names_typed_in_any_way_are_understood(self):
        from clipapp.ui import _program_names

        typed = ' Banco.EXE \n\n"C:\\Program Files\\KeePass\\KeePass.exe"\nnotepad\nbanco.exe\n'
        self.assertEqual(_program_names(typed), ["banco.exe", "keepass.exe", "notepad.exe"])

    def test_this_process_has_a_name(self):
        from clipapp import win32

        self.assertTrue(win32.program_name(os.getpid()).endswith(".exe"))
        self.assertIsInstance(win32.open_programs(), list)


class WindowRowsTest(unittest.TestCase):
    """In ui.py, TEXT is the text color: the kind of a copy must not be compared against it."""

    def test_a_text_item_is_a_text_row(self):
        from clipapp import ui

        row = ui.item_row({"id": 1, "text": "oi", "created": 0, "kind": "text"}, "saved")
        self.assertEqual(row.kind, "text")
        self.assertEqual(ui.Row(1, "t", "m", "x").kind, "text")
        self.assertEqual(ui.item_row({"id": 2, "text": "antigo", "created": 0}, "history").kind, "text")

    def test_no_kind_is_compared_with_a_color(self):
        import re

        source = (Path(__file__).resolve().parent.parent / "clipapp" / "ui.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"kind\"?\]?\s*[!=]=\s*TEXT\b", source), [])


class HotkeyTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        from clipapp.store import Store

        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_names_read_like_the_keyboard(self):
        from clipapp import win32

        self.assertEqual(win32.hotkey_text(win32.MOD_CONTROL | win32.MOD_SHIFT, win32.VK_SPACE), "Ctrl+Shift+Espaço")
        self.assertEqual(win32.hotkey_text(win32.MOD_CONTROL | win32.MOD_ALT, 0x4B), "Ctrl+Alt+K")
        self.assertEqual(win32.hotkey_text(0, 0x77), "F8")
        self.assertEqual(win32.hotkey_text(win32.MOD_WIN | win32.MOD_SHIFT, 0x31), "Shift+Win+1")

    def test_defaults_until_changed(self):
        from clipapp.engine import HOTKEYS, load_hotkeys

        self.assertEqual(load_hotkeys(self.store), {name: default for name, _what, default in HOTKEYS})

    def test_changes_and_turning_off_are_kept(self):
        from clipapp import win32
        from clipapp.engine import load_hotkeys, save_hotkeys

        keys = load_hotkeys(self.store)
        keys["quick"] = (win32.MOD_ALT, 0x56)
        keys["cycle"] = None
        save_hotkeys(self.store, keys)
        again = load_hotkeys(self.store)
        self.assertEqual(again["quick"], (win32.MOD_ALT, 0x56))
        self.assertIsNone(again["cycle"])

    def test_a_damaged_setting_falls_back_to_the_default(self):
        from clipapp.engine import HOTKEYS, load_hotkeys

        self.store.set("hotkey_window", "abc")
        self.assertEqual(load_hotkeys(self.store)["window"], HOTKEYS[0][2])

    def test_shortcuts_that_would_break_typing_are_refused(self):
        from clipapp import win32
        from clipapp.ui import hotkey_problem

        self.assertIsNotNone(hotkey_problem(0, 0x41))  # a letter alone
        self.assertIsNotNone(hotkey_problem(win32.MOD_SHIFT, 0x41))  # a capital letter
        self.assertIsNotNone(hotkey_problem(win32.MOD_CONTROL, 0x43))  # Ctrl+C
        self.assertIsNotNone(hotkey_problem(win32.MOD_ALT, 0x73))  # Alt+F4
        self.assertIsNone(hotkey_problem(0, 0x77))  # F8
        self.assertIsNone(hotkey_problem(win32.MOD_CONTROL | win32.MOD_SHIFT, 0x56))


if __name__ == "__main__":
    unittest.main()
