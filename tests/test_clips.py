"""Images, files and formatting, as far as they go without the desktop."""

import os
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipapp import clips, win32  # noqa: E402
from clipapp.clips import FILES, IMAGE, TEXT, Clip  # noqa: E402
from clipapp.store import Store  # noqa: E402


def make_dib(width, height, bits=32, color=(10, 20, 30), top_down=False):
    """A DIB like the clipboard's: header, then rows padded to 4 bytes, bottom row first."""
    row = (width * bits + 31) // 32 * 4
    header = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bits, 0, row * height, 0, 0, 0, 0)
    pixel = bytes((color[2], color[1], color[0])) + (b"\xff" if bits == 32 else b"")
    line = (pixel * width).ljust(row, b"\0")
    return header + line * height


class ClipTest(unittest.TestCase):
    def test_pack_round_trip(self):
        formats = {"html": b"<b>oi</b>" * 1000, "rtf": b"{\\rtf1 oi}", "dib": bytes(range(256)) * 50}
        self.assertEqual(clips.unpack(clips.pack(formats)), formats)
        self.assertEqual(clips.unpack(clips.pack({})), {})

    def test_hdrop_round_trip(self):
        paths = [r"C:\Users\x\relatório.pdf", r"D:\fotos\praia 2024.jpg", r"\\servidor\pasta\a.txt"]
        self.assertEqual(clips.paths_from_hdrop(clips.hdrop(paths)), paths)

    def test_hdrop_from_an_old_program_in_ansi(self):
        data = struct.pack("<IiiII", 20, 0, 0, 0, 0) + b"C:\\a.txt\0C:\\b.txt\0\0"
        self.assertEqual(clips.paths_from_hdrop(data), ["C:\\a.txt", "C:\\b.txt"])

    def test_file_names_shown(self):
        self.assertEqual(clips.describe_files([r"C:\a\x.txt"]), "x.txt")
        self.assertEqual(clips.describe_files([r"C:\a\x.txt", r"C:\b\y.pdf", r"C:\c", r"C:\d"]), "x.txt, y.pdf e mais 2")

    def test_image_size_and_description(self):
        dib = make_dib(300, 200)
        self.assertEqual(clips.dib_size(dib), (300, 200))
        self.assertEqual(clips.describe_image(dib), "Imagem 300 × 200")

    def test_thumbnail_keeps_shape_and_color(self):
        for bits in (32, 24):
            for top_down in (False, True):
                png = clips.thumbnail(make_dib(300, 100, bits, (200, 40, 10), top_down), win32.png_from_rgba)
                self.assertTrue(png.startswith(b"\x89PNG"))
                width, height = struct.unpack(">II", png[16:24])
                self.assertEqual((width, height), (96, 32))

    def test_small_images_are_not_enlarged(self):
        png = clips.thumbnail(make_dib(10, 6), win32.png_from_rgba)
        self.assertEqual(struct.unpack(">II", png[16:24]), (10, 6))

    def test_a_bitmap_this_cannot_read_gives_no_thumbnail(self):
        broken = make_dib(10, 10)[:60]
        self.assertIsNone(clips.thumbnail(broken, win32.png_from_rgba))
        paletted = struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 8, 0, 0, 0, 0, 0, 0) + bytes(1100)
        self.assertIsNone(clips.thumbnail(paletted, win32.png_from_rgba))

    def test_same_text_with_or_without_formatting_is_one_copy(self):
        self.assertEqual(Clip(TEXT, "oi", {"html": b"<b>oi</b>"}).identity(), Clip.plain("oi").identity())

    def test_two_images_of_the_same_size_are_two_copies(self):
        a = Clip(IMAGE, "Imagem 2 × 2", {"dib": make_dib(2, 2, color=(1, 1, 1))})
        b = Clip(IMAGE, "Imagem 2 × 2", {"dib": make_dib(2, 2, color=(2, 2, 2))})
        self.assertNotEqual(a.identity(), b.identity())


class StoreKindsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.store = Store(self.path)
        self.list_id = self.store.lists()[0]["id"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def image(self, color=(5, 6, 7)):
        dib = make_dib(40, 30, color=color)
        return Clip(IMAGE, clips.describe_image(dib), {"dib": dib}, clips.thumbnail(dib, win32.png_from_rgba))

    def test_image_round_trip_in_every_table(self):
        clip = self.image()
        self.store.add_history(clip)
        saved_id = self.store.add_saved(clip)
        item_id = self.store.add_list_item(self.list_id, clip)
        history_id = self.store.history()[0]["id"]
        for table, item in (("history", history_id), ("saved", saved_id), ("list_items", item_id)):
            again = self.store.clip(table, item)
            self.assertEqual((again.kind, again.text, again.formats, again.thumb),
                             (IMAGE, clip.text, clip.formats, clip.thumb), table)
            self.assertEqual(self.store.thumb(table, item), clip.thumb)

    def test_listing_says_what_kind_without_opening_the_image(self):
        self.store.add_history(self.image())
        self.store.add_history("texto")
        rows = self.store.history()
        self.assertEqual([(r["kind"], bool(r["has_thumb"])) for r in rows], [(TEXT, False), (IMAGE, True)])
        self.assertNotIn("extra", rows[0])

    def test_formatting_kept_and_replaced_by_the_latest_copy(self):
        self.store.add_history(Clip(TEXT, "oi", {"html": b"<b>oi</b>"}))
        self.store.add_history(Clip(TEXT, "oi", {"html": b"<i>oi</i>"}))
        rows = self.store.history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.clip("history", rows[0]["id"]).formats, {"html": b"<i>oi</i>"})

    def test_files_round_trip(self):
        clip = Clip(FILES, "C:\\a.txt\nC:\\b.pdf")
        self.store.add_history(clip)
        again = self.store.clip("history", self.store.history()[0]["id"])
        self.assertEqual(again.paths, ["C:\\a.txt", "C:\\b.pdf"])

    def test_editing_a_text_drops_the_formatting_but_renaming_keeps_it(self):
        item_id = self.store.add_saved(Clip(TEXT, "oi", {"html": b"<b>oi</b>"}))
        self.store.update_saved(item_id, "oi", "Saudação")
        self.assertEqual(self.store.clip("saved", item_id).formats, {"html": b"<b>oi</b>"})
        self.store.update_saved(item_id, "olá", "Saudação")
        self.assertEqual(self.store.clip("saved", item_id).formats, {})
        list_item = self.store.add_list_item(self.list_id, Clip(TEXT, "oi", {"rtf": b"{\\rtf oi}"}))
        self.store.update_list_item(list_item, "oi")
        self.assertEqual(self.store.clip("list_items", list_item).formats, {"rtf": b"{\\rtf oi}"})
        self.store.update_list_item(list_item, "tchau")
        self.assertEqual(self.store.clip("list_items", list_item).formats, {})

    def test_undo_brings_back_an_image_whole(self):
        clip = self.image()
        item_id = self.store.add_list_item(self.list_id, clip)
        item = self.store.get_list_item(item_id)
        self.store.delete_list_item(item_id)
        self.assertTrue(self.store.restore_list_item(item))
        again = self.store.clip("list_items", self.store.list_items(self.list_id)[0]["id"])
        self.assertEqual(again.formats, clip.formats)

    def test_star_state_without_decrypting(self):
        self.store.add_history("oi")
        self.store.add_saved("oi")
        row = self.store.history()[0]
        self.assertIn(row["text_hash"], self.store.saved_hashes())

    def test_images_and_formatting_are_encrypted_in_the_file(self):
        marker = b"FORMATACAO-SECRETA-123"
        self.store.add_history(Clip(TEXT, "x", {"html": marker * 3}))
        self.store.add_history(Clip(IMAGE, "Imagem 4 × 1", {"dib": make_dib(4, 1) + marker}))
        data = self.path.read_bytes()
        self.assertNotIn(marker, data)

    def test_deleting_an_image_overwrites_it(self):
        self.store.add_history(self.image())
        row = self.store.history()[0]
        blob = bytes(self.store._one("SELECT extra FROM history WHERE id = ?", row["id"])["extra"])
        self.assertIn(blob, self.path.read_bytes())
        self.store.delete_history(row["id"])
        self.assertNotIn(blob, self.path.read_bytes())

    def test_a_file_from_version_0_3_opens_with_its_items(self):
        """The tables exactly as 0.3 made them, with an item already saved in each."""
        from clipapp.crypto import Vault, new_master_key, wrap_key

        self.store.close()
        old = Path(self.dir.name) / "old.db"
        db = sqlite3.connect(old)
        db.executescript("""
            CREATE TABLE history(id INTEGER PRIMARY KEY, text BLOB NOT NULL, text_hash BLOB NOT NULL UNIQUE,
                                 created REAL NOT NULL);
            CREATE TABLE saved(id INTEGER PRIMARY KEY, title BLOB NOT NULL, text BLOB NOT NULL,
                               text_hash BLOB NOT NULL, created REAL NOT NULL);
            CREATE INDEX saved_by_hash ON saved(text_hash);
            CREATE TABLE lists(id INTEGER PRIMARY KEY, name BLOB NOT NULL, reversed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE list_items(id INTEGER PRIMARY KEY, list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                                    pos INTEGER NOT NULL, text BLOB NOT NULL, created REAL NOT NULL);
            CREATE INDEX list_items_by_list ON list_items(list_id, pos);
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        key = new_master_key()
        vault = Vault(key)
        db.execute("INSERT INTO settings VALUES('vault_key', ?)", (wrap_key(key).hex(),))
        db.execute("INSERT INTO history(text, text_hash, created) VALUES(?, ?, ?)",
                   (vault.seal("antigo", b"history.text"), vault.fingerprint("antigo"), time.time() - 60))
        db.execute("INSERT INTO saved(title, text, text_hash, created) VALUES(?, ?, ?, ?)",
                   (vault.seal("Título", b"saved.title"), vault.seal("guardado", b"saved.text"),
                    vault.fingerprint("guardado"), time.time()))
        db.execute("INSERT INTO lists(name) VALUES(?)", (vault.seal("Minha lista", b"lists.name"),))
        db.execute("INSERT INTO list_items(list_id, pos, text, created) VALUES(1, 0, ?, ?)",
                   (vault.seal("na lista", b"list_items.text"), time.time()))
        db.commit()
        db.close()

        self.store = Store(old)
        self.assertEqual([(r["text"], r["kind"]) for r in self.store.history()], [("antigo", TEXT)])
        self.assertEqual(self.store.clip("history", 1).text, "antigo")
        self.assertEqual(self.store.saved()[0]["title"], "Título")
        self.assertEqual([r["text"] for r in self.store.list_items(1)], ["na lista"])
        self.store.add_history("antigo")  # the same text again: still one item
        self.assertEqual(len(self.store.history()), 1)
        self.store.add_history(self.image())
        self.assertEqual(self.store.history()[0]["kind"], IMAGE)

    def test_items_saved_before_images_read_as_text(self):
        self.store.add_history("antigo")
        self.store._run("UPDATE history SET kind = 'text', extra = NULL, thumb = NULL")
        clip = self.store.clip("history", self.store.history()[0]["id"])
        self.assertEqual((clip.kind, clip.text, clip.formats), (TEXT, "antigo", {}))


if __name__ == "__main__":
    unittest.main()
