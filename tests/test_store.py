"""Rules that must hold regardless of the window: run with `py -m unittest discover tests`."""

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clipapp  # noqa: E402
from clipapp import crypto, win32  # noqa: E402
from clipapp.store import DEFAULT_DAYS, DEFAULT_MAX_ITEMS, Store  # noqa: E402

DAY = 86400


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.db")
        self.list_id = self.store.lists()[0]["id"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def texts(self):
        return [r["text"] for r in self.store.list_items(self.list_id)]

    def fill(self, *values):
        for value in values:
            self.store.add_list_item(self.list_id, value)

    def test_starts_with_one_list(self):
        self.assertEqual(len(self.store.lists()), 1)

    def test_in_order_pastes_oldest_first(self):
        self.fill("a", "b", "c")
        self.assertEqual(self.texts(), ["a", "b", "c"])
        self.assertEqual(self.store.next_item(self.list_id)["text"], "a")

    def test_reverse_pastes_newest_first(self):
        self.fill("a", "b", "c")
        self.store.set_reversed(self.list_id, True)
        self.assertEqual(self.texts(), ["c", "b", "a"])
        self.assertEqual(self.store.next_item(self.list_id)["text"], "c")

    def test_pasting_drains_list_in_order(self):
        self.fill("a", "b", "c")
        pasted = []
        while (item := self.store.next_item(self.list_id)) is not None:
            pasted.append(item["text"])
            self.store.delete_list_item(item["id"])
        self.assertEqual(pasted, ["a", "b", "c"])
        self.assertEqual(self.store.list_size(self.list_id), 0)

    def test_same_text_can_appear_twice_in_a_list(self):
        self.fill("a", "a")
        self.assertEqual(self.texts(), ["a", "a"])

    def test_restore_puts_item_back_in_place(self):
        self.fill("a", "b", "c")
        item = self.store.next_item(self.list_id)
        self.store.delete_list_item(item["id"])
        self.assertTrue(self.store.restore_list_item(item))
        self.assertEqual(self.texts(), ["a", "b", "c"])

    def test_restore_after_a_move_puts_item_first_again(self):
        self.fill("a", "b", "c")
        item = self.store.next_item(self.list_id)
        self.store.delete_list_item(item["id"])
        last = self.store.list_items(self.list_id)[1]
        self.store.move_item(self.list_id, last["id"], 0)  # renumbers: "c" now holds "a"'s old place
        self.assertEqual(self.texts(), ["c", "b"])
        self.assertTrue(self.store.restore_list_item(item))
        self.assertEqual(self.texts(), ["a", "c", "b"])

    def test_restore_in_reverse_puts_item_first_again(self):
        self.fill("a", "b", "c")
        self.store.set_reversed(self.list_id, True)
        item = self.store.next_item(self.list_id)  # "c"
        self.store.delete_list_item(item["id"])
        self.fill("d")  # takes "c"'s old place
        self.assertEqual(self.texts(), ["d", "b", "a"])
        self.assertTrue(self.store.restore_list_item(item))
        self.assertEqual(self.texts(), ["c", "d", "b", "a"])

    def test_undoing_several_pastes_rebuilds_the_list(self):
        self.fill("a", "b", "c")
        pasted = []
        for _ in range(2):
            pasted.append(self.store.next_item(self.list_id))
            self.store.delete_list_item(pasted[-1]["id"])
        for item in reversed(pasted):
            self.store.restore_list_item(item)
        self.assertEqual(self.texts(), ["a", "b", "c"])

    def test_damaged_key_is_reported_like_a_foreign_one(self):
        self.store.set("vault_key", "não é hex")
        self.store.close()
        with self.assertRaises(crypto.LockedError):
            self.store = Store(Path(self.dir.name) / "t.db")
        self.store = Store(Path(self.dir.name) / "outro.db")  # for tearDown

    def test_expiring_history_names_what_pruning_deletes(self):
        now = time.time()
        self.store.add_history("novo", now=now)
        self.store.add_history("velho", now=now - 8 * DAY)  # last, or the next copy would prune it
        self.assertEqual(self.store.expiring_history(), {"velho"})
        self.store.prune_history()
        self.assertEqual(self.store.expiring_history(), set())

    def test_restore_after_list_deleted_is_refused(self):
        other = self.store.create_list("x")
        item_id = self.store.add_list_item(other, "a")
        item = self.store.get_list_item(item_id)
        self.store.delete_list(other)
        self.assertFalse(self.store.restore_list_item(item))

    def test_move_in_order(self):
        self.fill("a", "b", "c", "d")
        item = self.store.list_items(self.list_id)[3]
        self.store.move_item(self.list_id, item["id"], 0)
        self.assertEqual(self.texts(), ["d", "a", "b", "c"])

    def test_move_keeps_visible_order_when_reversed(self):
        self.fill("a", "b", "c", "d")
        self.store.set_reversed(self.list_id, True)
        self.assertEqual(self.texts(), ["d", "c", "b", "a"])
        item = self.store.list_items(self.list_id)[3]
        self.store.move_item(self.list_id, item["id"], 1)
        self.assertEqual(self.texts(), ["d", "a", "c", "b"])

    def test_new_item_goes_last_or_first_depending_on_order(self):
        self.fill("a")
        self.fill("b")
        self.assertEqual(self.texts()[-1], "b")
        self.store.set_reversed(self.list_id, True)
        self.fill("c")
        self.assertEqual(self.texts()[0], "c")

    def test_deleting_a_list_deletes_its_items(self):
        other = self.store.create_list("x")
        item_id = self.store.add_list_item(other, "a")
        self.store.delete_list(other)
        self.assertIsNone(self.store.get_list_item(item_id))

    def test_history_moves_repeated_text_to_top(self):
        self.store.add_history("a")
        self.store.add_history("b")
        self.store.add_history("a")
        self.assertEqual([r["text"] for r in self.store.history()], ["a", "b"])

    def test_default_limits_are_7_days_and_100_items(self):
        self.assertEqual(self.store.history_limits(), (7, 100))
        self.assertEqual((DEFAULT_DAYS, DEFAULT_MAX_ITEMS), (7, 100))

    def test_history_keeps_only_the_newest_items(self):
        for n in range(DEFAULT_MAX_ITEMS + 5):
            self.store.add_history(f"item {n}")
        rows = self.store.history()
        self.assertEqual(len(rows), DEFAULT_MAX_ITEMS)
        self.assertEqual(rows[0]["text"], f"item {DEFAULT_MAX_ITEMS + 4}")

    def test_history_older_than_the_limit_is_deleted(self):
        now = time.time()
        self.store.add_history("8 dias", now=now - 8 * DAY)
        self.store.add_history("6 dias", now=now - 6 * DAY)
        self.assertEqual(self.store.prune_history(), 1)
        self.assertEqual([r["text"] for r in self.store.history()], ["6 dias"])

    def test_copying_again_restarts_the_clock(self):
        now = time.time()
        self.store.add_history("repetido", now=now - 8 * DAY)
        self.store.add_history("repetido")
        self.assertEqual(self.store.prune_history(), 0)

    def test_changed_limits_are_used_and_kept(self):
        now = time.time()
        self.store.add_history("20 dias", now=now - 20 * DAY)
        self.store.set_history_limits(30, 2)
        self.assertEqual(self.store.prune_history(), 0)
        self.store.add_history("b")
        self.store.add_history("c")
        self.assertEqual([r["text"] for r in self.store.history()], ["c", "b"])
        self.assertEqual(self.store.history_limits(), (30, 2))

    def test_count_beyond_limits_matches_what_pruning_deletes(self):
        for n in range(5):
            self.store.add_history(f"item {n}")
        self.assertEqual(self.store.count_beyond_limits(7, 3), 2)
        self.assertEqual(len(self.store.history()), 5)
        self.store.set_history_limits(7, 3)
        self.assertEqual(self.store.prune_history(), 2)

    def test_limits_do_not_touch_repertoire_or_lists(self):
        self.store.add_saved("guardado")
        self.fill("na lista")
        self.store.set_history_limits(1, 1)
        self.store.prune_history(now=time.time() + 400 * DAY)
        self.assertEqual(len(self.store.saved()), 1)
        self.assertEqual(self.texts(), ["na lista"])

    def test_broken_limit_setting_falls_back_to_default(self):
        self.store.set("history_days", "abc")
        self.assertEqual(self.store.history_limits()[0], DEFAULT_DAYS)

    def test_history_search_treats_wildcards_literally(self):
        self.store.add_history("100% pronto")
        self.store.add_history("100 pronto")
        self.assertEqual([r["text"] for r in self.store.history("100%")], ["100% pronto"])

    def test_search_ignores_case_with_accents(self):
        self.store.add_history("AÇÃO urgente")
        self.assertEqual(len(self.store.history("ação")), 1)

    def test_history_and_repertoire_follow_every_write(self):
        self.store.add_history("um")
        self.store.add_saved("salvo")
        self.assertEqual([r["text"] for r in self.store.history()], ["um"])  # fills the cache
        self.assertEqual(self.store.saved_texts(), {"salvo"})
        self.store.add_history("dois")
        self.assertEqual([r["text"] for r in self.store.history()], ["dois", "um"])
        self.store.delete_history(self.store.history("um")[0]["id"])
        self.assertEqual([r["text"] for r in self.store.history()], ["dois"])
        item_id = self.store.saved()[0]["id"]
        self.store.update_saved(item_id, "editado", "")
        self.assertEqual(self.store.saved_texts(), {"editado"})
        self.store.delete_saved(item_id)
        self.assertEqual(self.store.saved(), [])

    def test_a_returned_list_does_not_change_the_cache(self):
        self.store.add_history("um")
        self.store.history().clear()
        self.assertEqual(len(self.store.history()), 1)

    def test_cache_sees_writes_from_another_connection(self):
        self.store.add_history("um")
        self.store.history()  # fills the cache
        other = Store(Path(self.dir.name) / "t.db")
        try:
            other.add_history("dois")
        finally:
            other.close()
        self.assertEqual([r["text"] for r in self.store.history()], ["dois", "um"])

    def test_saved_keeps_one_copy_of_a_text(self):
        first = self.store.add_saved("a")
        second = self.store.add_saved("a")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.saved()), 1)

    def test_saved_search_finds_title(self):
        self.store.add_saved("corpo", title="Endereço")
        self.assertEqual(len(self.store.saved("ender")), 1)

    def test_clearing_history_keeps_repertoire(self):
        self.store.add_history("a")
        self.store.add_saved("a")
        self.store.clear_history()
        self.assertEqual(self.store.history(), [])
        self.assertEqual(len(self.store.saved()), 1)

    def test_unicode_survives(self):
        text = "ação 日本語 😀\nlinha 2"
        self.store.add_saved(text)
        self.assertEqual(self.store.saved()[0]["text"], text)

    # ---- nothing readable on disk ---------------------------------------------

    def file_bytes(self):
        return (Path(self.dir.name) / "t.db").read_bytes()

    def assert_not_in_file(self, text):
        data = self.file_bytes()
        self.assertNotIn(text.encode("utf-8"), data)
        self.assertNotIn(text.encode("utf-16-le"), data)

    def test_no_saved_text_is_readable_in_the_file(self):
        self.store.add_history("segredo-do-historico")
        self.store.add_saved("segredo-do-repertorio", title="titulo-secreto")
        self.store.add_list_item(self.list_id, "segredo-da-lista")
        self.store.rename_list(self.list_id, "nome-secreto")
        for text in ("segredo-do-historico", "segredo-do-repertorio", "titulo-secreto", "segredo-da-lista",
                     "nome-secreto", "Lista 1"):
            self.assert_not_in_file(text)

    def test_store_reopens_with_the_same_key(self):
        self.store.add_saved("continua aqui", title="título")
        self.store.close()
        self.store = Store(Path(self.dir.name) / "t.db")
        self.assertEqual(self.store.saved()[0]["text"], "continua aqui")
        self.assertEqual(self.store.saved()[0]["title"], "título")
        self.assertEqual(self.store.lists()[0]["name"], "Lista 1")

    def test_repeats_are_found_without_storing_a_plain_hash(self):
        import hashlib

        self.store.add_history("mesmo texto")
        self.store.add_history("mesmo texto")
        self.assertEqual(len(self.store.history()), 1)
        self.assertNotIn(hashlib.sha256("mesmo texto".encode()).digest(), self.file_bytes())

    def test_damaged_item_is_left_out_not_shown_as_garbage(self):
        self.store.add_history("bom")
        self.store.add_history("ruim")
        row = self.store._one("SELECT id, text FROM history ORDER BY id DESC LIMIT 1")
        damaged = bytes(row["text"][:-1]) + bytes([row["text"][-1] ^ 1])
        self.store._run("UPDATE history SET text = ? WHERE id = ?", damaged, row["id"])
        self.assertEqual([r["text"] for r in self.store.history()], ["bom"])

    # ---- deleted for good -----------------------------------------------------

    def stored(self, table, item_id):
        return bytes(self.store._one(f"SELECT text FROM {table} WHERE id = ?", item_id)["text"])

    def assert_erased(self, cipher):
        self.assertNotIn(cipher, self.file_bytes())

    def test_deleted_history_is_overwritten_in_the_file(self):
        self.store.add_history("apagar")
        item_id = self.store.history()[0]["id"]
        cipher = self.stored("history", item_id)
        self.assertIn(cipher, self.file_bytes())  # or this test would prove nothing
        self.store.delete_history(item_id)
        self.assert_erased(cipher)

    def test_deleted_saved_item_is_overwritten(self):
        item_id = self.store.add_saved("apagar")
        cipher = self.stored("saved", item_id)
        self.assertIn(cipher, self.file_bytes())
        self.store.delete_saved(item_id)
        self.assert_erased(cipher)

    def test_deleted_list_item_is_overwritten(self):
        item_id = self.store.add_list_item(self.list_id, "apagar")
        cipher = self.stored("list_items", item_id)
        self.assertIn(cipher, self.file_bytes())
        self.store.delete_list_item(item_id)
        self.assert_erased(cipher)

    def test_edit_overwrites_the_old_text(self):
        item_id = self.store.add_saved("versão antiga")
        cipher = self.stored("saved", item_id)
        self.assertIn(cipher, self.file_bytes())
        self.store.update_saved(item_id, "versão nova", "")
        self.assert_erased(cipher)

    def test_clearing_and_expiring_overwrite_too(self):
        now = time.time()
        self.store.add_history("expira", now=now - 8 * DAY)
        expired = self.stored("history", self.store.history()[0]["id"])
        self.store.prune_history()
        self.assert_erased(expired)

        self.store.add_history("limpar")
        cleared = self.stored("history", self.store.history()[0]["id"])
        self.store.clear_history()
        self.assert_erased(cleared)

        item_id = self.store.add_list_item(self.list_id, "na lista")
        listed = self.stored("list_items", item_id)
        self.store.delete_list(self.list_id)
        self.assert_erased(listed)


class IdentityTest(unittest.TestCase):
    def test_renaming_the_app_does_not_move_the_saved_data(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"APPDATA": folder}):
            os.environ.pop("CLIPAPP_DATA", None)
            self.assertEqual(clipapp.data_dir().name, "ClipApp")
        self.assertNotEqual(clipapp.APP_NAME, "ClipApp")

    def test_logo_draws_a_stack_at_every_size(self):
        rgb = (110, 168, 254)
        for size in (16, 32, 48):
            rgba = win32._icon_rgba(rgb, size)
            self.assertEqual(len(rgba), size * size * 4)

            def pixel(x, y):  # in the 32-pixel grid the logo is designed on
                column, row = int(x * size / 32), int(y * size / 32)
                return rgba[(row * size + column) * 4:(row * size + column) * 4 + 4]

            self.assertEqual(pixel(0, 0)[3], 0)  # corners stay transparent
            self.assertEqual(tuple(pixel(5, 20)), (*rgb, 255))  # the front card is solid
            self.assertTrue(0 < pixel(16, 1)[3] < 255)  # the back one peeks out above, lighter

    def test_logo_can_be_used_by_tk_and_the_tray(self):
        self.assertTrue(win32.icon_png((1, 2, 3), 16).startswith(b"\x89PNG"))
        icon = win32.make_icon((1, 2, 3))
        self.assertTrue(icon)
        win32.user32.DestroyIcon(icon)


class CryptoTest(unittest.TestCase):
    def test_aes_matches_the_published_gcm_test_vectors(self):
        # AES-256 test cases 13 and 14 from the GCM specification (McGrew & Viega).
        aes = crypto._AesGcm(bytes(32))
        self.assertEqual(aes.encrypt(bytes(12), b"", b"")[1].hex(), "530f8afbc74536b9a963b4f1c4cb738b")
        ciphertext, tag = aes.encrypt(bytes(12), bytes(16), b"")
        self.assertEqual(ciphertext.hex(), "cea7403d4d606b6e074ec5d3baf39d18")
        self.assertEqual(tag.hex(), "d0d1c8a799996bf0265b98b5d48ab919")
        self.assertEqual(aes.decrypt(bytes(12), ciphertext, tag, b""), bytes(16))

    def test_round_trip(self):
        vault = crypto.Vault(crypto.new_master_key())
        for text in ("", "a", "ação 日本語 😀\nlinha 2", "x" * 100_000):
            self.assertEqual(vault.open(vault.seal(text, b"c"), b"c"), text)

    def test_same_text_never_encrypts_the_same_way_twice(self):
        vault = crypto.Vault(crypto.new_master_key())
        self.assertNotEqual(vault.seal("igual", b"c"), vault.seal("igual", b"c"))

    def test_another_key_cannot_open(self):
        blob = crypto.Vault(crypto.new_master_key()).seal("segredo", b"c")
        with self.assertRaises(crypto.DecryptError):
            crypto.Vault(crypto.new_master_key()).open(blob, b"c")

    def test_any_changed_byte_is_refused(self):
        vault = crypto.Vault(crypto.new_master_key())
        blob = bytearray(vault.seal("segredo", b"c"))
        for index in range(len(blob)):
            blob[index] ^= 1
            with self.assertRaises(crypto.DecryptError):
                vault.open(bytes(blob), b"c")
            blob[index] ^= 1

    def test_value_cannot_be_moved_to_another_column(self):
        vault = crypto.Vault(crypto.new_master_key())
        with self.assertRaises(crypto.DecryptError):
            vault.open(vault.seal("segredo", b"saved.text"), b"history.text")

    def test_key_is_wrapped_by_windows(self):
        key = crypto.new_master_key()
        wrapped = crypto.wrap_key(key)
        self.assertNotIn(key, wrapped)
        self.assertEqual(crypto.unwrap_key(wrapped), key)

    def test_key_that_windows_refuses_is_reported(self):
        wrapped = bytearray(crypto.wrap_key(crypto.new_master_key()))
        wrapped[len(wrapped) // 2] ^= 1
        with self.assertRaises(crypto.LockedError):
            crypto.unwrap_key(bytes(wrapped))

    def test_window_and_engine_can_encrypt_at_the_same_time(self):
        vault = crypto.Vault(crypto.new_master_key())
        failures = []

        def work(n):
            try:
                for i in range(300):
                    text = f"thread {n} item {i} " * 20
                    if vault.open(vault.seal(text, b"c"), b"c") != text:
                        failures.append((n, i))
            except Exception as error:  # noqa: BLE001
                failures.append(error)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])


class WindowPositionTest(unittest.TestCase):
    def on_screen(self, geometry):
        from clipapp import ui

        app = types.SimpleNamespace(px=lambda value: value)
        with mock.patch.object(win32, "virtual_screen", return_value=(-1920, 0, 1920, 1080)):
            return ui.App._on_screen(app, geometry)

    def test_monitor_left_of_the_main_one_is_accepted(self):
        self.assertTrue(self.on_screen("360x560+-1500+200"))

    def test_positions_tk_does_not_save_are_refused(self):
        self.assertFalse(self.on_screen("360x560-10+200"))
        self.assertFalse(self.on_screen("360x560+--1+200"))
        self.assertFalse(self.on_screen("400x500+99999+100"))


class EngineCase(unittest.TestCase):
    """An engine whose thread never starts: what it decides, without Windows' side of it."""

    def setUp(self):
        from clipapp.engine import Engine

        self.Engine = Engine
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.db")
        self.events = []
        patch = mock.patch.object(Engine, "_tray")  # no tray icon outside the running app
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def engine(self):
        return self.Engine(self.store, self.events.append)


class EngineTest(EngineCase):
    def test_a_broken_active_list_setting_falls_back_to_the_first_list(self):
        for value in ("abc", "", "999"):
            self.store.set("active_list", value)
            self.assertEqual(self.engine().active_list, self.store.lists()[0]["id"])

    def test_expired_history_leaves_the_clipboard_too(self):
        engine = self.engine()
        self.store.add_history("novo")
        self.store.add_history("velho", now=time.time() - 8 * DAY)  # last, or the next copy would prune it
        with mock.patch.object(win32, "clear_if_holds") as clear:
            engine._prune()
        clear.assert_called_once_with(None, {"velho"})
        self.assertEqual([r["text"] for r in self.store.history()], ["novo"])

    def test_nothing_expired_leaves_the_clipboard_alone(self):
        engine = self.engine()
        self.store.add_history("novo")
        with mock.patch.object(win32, "clear_if_holds") as clear:
            engine._prune()
        clear.assert_not_called()


class PrivacyNoticeTest(EngineCase):
    def test_nothing_starts_the_capture_before_the_notice_is_answered(self):
        from clipapp.engine import OFF, RECORD

        engine = self.engine()
        self.assertTrue(engine.paused)
        engine.set_mode(RECORD)
        engine.set_paused(False)
        self.assertEqual((engine.mode, engine.paused), (OFF, True))

    def test_closing_the_notice_keeps_the_capture_paused(self):
        engine = self.engine()
        engine.close_notice(False)
        self.assertTrue(engine.paused)

    def test_entendi_starts_the_capture(self):
        engine = self.engine()
        engine.close_notice(True)
        self.assertFalse(engine.paused)

    def test_a_pause_lasts_across_restarts(self):
        self.store.set("privacy_notice", "hide")
        engine = self.engine()
        self.assertFalse(engine.paused)
        engine.set_paused(True)
        self.assertTrue(self.engine().paused)
        self.store.set("privacy_notice", "show")
        later = self.engine()
        later.close_notice(True)  # the notice doesn't undo the user's own pause
        self.assertTrue(later.paused)


class PasteTargetTest(unittest.TestCase):
    def test_own_process_integrity_is_known(self):
        self.assertIsNotNone(win32.integrity_level(os.getpid()))

    def test_a_normal_window_is_not_blocked(self):
        self.assertFalse(win32.input_blocked(None))
        self.assertFalse(win32.input_blocked(win32.user32.GetDesktopWindow()))


if __name__ == "__main__":
    unittest.main()
