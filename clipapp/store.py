"""Everything that gets saved: history, repertoire, lists and settings (SQLite).

Text, titles and list names are encrypted before reaching the file (see crypto.py), so
searching happens here in Python. Deleting is for good: secure_delete makes SQLite
overwrite deleted content with zeros instead of just marking it free.

Used from two threads (the window and the clipboard engine), so every call takes the
lock. Lists keep items by `pos` in the order they were captured; the paste order is that
order, or its reverse when the list is marked `reversed`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from . import clips
from .clips import TEXT, Clip
from .crypto import (DecryptError, LockedError, Vault, WrongPassword, new_master_key, unwrap_key,
                     unwrap_with_password, wrap_key, wrap_with_password)

log = logging.getLogger(__name__)

DEFAULT_DAYS = 7
DEFAULT_MAX_ITEMS = 100
MAX_DAYS = 365
MAX_ITEMS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY,
    text BLOB NOT NULL,
    text_hash BLOB NOT NULL UNIQUE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS saved(
    id INTEGER PRIMARY KEY,
    title BLOB NOT NULL,
    text BLOB NOT NULL,
    text_hash BLOB NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS saved_by_hash ON saved(text_hash);
CREATE TABLE IF NOT EXISTS lists(
    id INTEGER PRIMARY KEY,
    name BLOB NOT NULL,
    reversed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS list_items(
    id INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    text BLOB NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS list_items_by_list ON list_items(list_id, pos);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Added to the items' tables in version 0.4. Files made before get them on opening.
# kind: "text", "image" or "files" (see clips.py). extra: the formats next to the text
# (formatting, an image's bytes), packed and encrypted. thumb: an image's small PNG, encrypted.
_ITEM_COLUMNS = ("kind TEXT NOT NULL DEFAULT 'text'", "extra BLOB", "thumb BLOB")
_ITEM_TABLES = ("history", "saved", "list_items")

# Which columns are encrypted, and the context each one is sealed under.
_HISTORY = {"text": b"history.text"}
_SAVED = {"title": b"saved.title", "text": b"saved.text"}
_LISTS = {"name": b"lists.name"}
_ITEMS = {"text": b"list_items.text"}
_TEXT_CONTEXT = {"history": _HISTORY["text"], "saved": _SAVED["text"], "list_items": _ITEMS["text"]}

_BEYOND_LIMITS = ("created < ? OR id NOT IN "
                  "(SELECT id FROM history ORDER BY created DESC, id DESC LIMIT ?)")


def _matches(query: str, *values: str) -> bool:
    needle = query.casefold()
    return any(needle in value.casefold() for value in values)


class Store:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        # History and repertoire as last decrypted. The window asks for them on every copy,
        # every key typed in the search and every minute, so they are only decrypted again
        # after something is written. Emptied on any write, so deleted text doesn't linger,
        # and when another connection changed the file (the tests open a second one).
        self._cache: dict[str, list[dict]] = {}
        self._cache_version = None
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5)
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA secure_delete = ON")
            self._db.execute("PRAGMA temp_store = MEMORY")
            self._db.executescript(_SCHEMA)
            self._add_item_columns()
            self._master: bytes | None = None
            self._vault_object: Vault | None = None
            if not self.has_password:
                self._unlocked_with(self._open_vault())
        except BaseException:
            self._db.close()  # or the file stays locked, and can't be deleted to start over
            raise

    # ---- locking -----------------------------------------------------------

    @property
    def _vault(self) -> Vault:
        if self._vault_object is None:
            raise LockedError("locked: the password hasn't been typed")
        return self._vault_object

    @property
    def locked(self) -> bool:
        return self._vault_object is None

    @property
    def has_password(self) -> bool:
        return self.get("vault_key_pw") is not None

    def _unlocked_with(self, key: bytes) -> None:
        self._master, self._vault_object = key, Vault(key)
        if not self.list_ids():
            self.create_list("Lista 1")

    def unlock(self, password: str) -> bool:
        """False for a wrong password. Raises LockedError if the key doesn't open on this account."""
        try:
            key = unwrap_with_password(bytes.fromhex(self.get("vault_key_pw", "")), password)
        except WrongPassword:
            return False
        except ValueError:
            raise LockedError("the saved key is damaged") from None
        with self._lock:
            self._unlocked_with(key)
        return True

    def lock(self) -> None:
        """Forgets the key until the password is typed again. Only with a password."""
        if self.has_password:
            with self._lock:
                self._master = self._vault_object = None
                self._cache.clear()

    def check_password(self, password: str) -> bool:
        try:
            unwrap_with_password(bytes.fromhex(self.get("vault_key_pw", "")), password)
            return True
        except (WrongPassword, LockedError, ValueError):
            return False

    def set_password(self, password: str) -> None:
        """Creates or changes the password. The key sealed by Windows alone is deleted, for good."""
        with self._lock:
            key = self._master
            if key is None:
                raise LockedError("unlock first")
            self._db.execute("BEGIN")
            try:
                self._db.execute("INSERT INTO settings(key, value) VALUES('vault_key_pw', ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                 (wrap_with_password(key, password).hex(),))
                self._db.execute("DELETE FROM settings WHERE key = 'vault_key'")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._cache.clear()

    def remove_password(self) -> None:
        """Back to the key sealed by Windows alone. The caller checks the password first."""
        with self._lock:
            key = self._master
            if key is None:
                raise LockedError("unlock first")
            self._db.execute("BEGIN")
            try:
                self._db.execute("INSERT INTO settings(key, value) VALUES('vault_key', ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (wrap_key(key).hex(),))
                self._db.execute("DELETE FROM settings WHERE key = 'vault_key_pw'")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._cache.clear()

    def wipe(self) -> None:
        """For a forgotten password: everything saved goes, for good, and a new key starts."""
        with self._lock:
            for table in ("list_items", "lists", "history", "saved"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute("DELETE FROM settings WHERE key IN ('vault_key', 'vault_key_pw', 'active_list', 'ignore_apps')")
            self._cache.clear()
            key = new_master_key()
            self.set("vault_key", wrap_key(key).hex())
            self._unlocked_with(key)

    def list_ids(self) -> list[int]:
        """The lists, without opening their names: works while locked."""
        return [row["id"] for row in self._all("SELECT id FROM lists ORDER BY id")]

    def _add_item_columns(self) -> None:
        for table in _ITEM_TABLES:
            have = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for column in _ITEM_COLUMNS:
                if column.split()[0] not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")

    def _open_vault(self) -> bytes:
        """The key. Raises crypto.LockedError if the saved one doesn't open on this Windows
        account, or is damaged."""
        wrapped = self.get("vault_key")
        if wrapped is not None:
            try:
                data = bytes.fromhex(wrapped)
            except ValueError:
                raise LockedError("the saved key is damaged") from None
            return unwrap_key(data)
        key = new_master_key()
        self.set("vault_key", wrap_key(key).hex())
        return key

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
            self._db.close()

    def _all(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, *args) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _run(self, sql: str, *args) -> sqlite3.Cursor:
        with self._lock:
            self._cache.clear()
            return self._db.execute(sql, args)

    def _cached(self, name: str, sql: str, columns: dict) -> list[dict]:
        with self._lock:
            # Changes only when another connection commits; our own writes clear the cache in _run.
            version = self._db.execute("PRAGMA data_version").fetchone()[0]
            if version != self._cache_version:
                self._cache.clear()
                self._cache_version = version
            if name not in self._cache:
                self._cache[name] = self._read(self._all(sql), columns)
            return self._cache[name]

    def _read(self, rows, columns: dict) -> list[dict]:
        """Decrypts rows. One that fails is left out and logged, not shown as garbage."""
        items = []
        for row in rows:
            item = dict(row)
            try:
                for column, context in columns.items():
                    item[column] = self._vault.open(item[column], context)
            except DecryptError:
                log.warning("skipped a saved item that could not be decrypted (id %s)", item.get("id"))
                continue
            items.append(item)
        return items

    def _read_one(self, row, columns: dict) -> dict | None:
        items = self._read([row], columns) if row else []
        return items[0] if items else None

    # ---- what a copy holds -------------------------------------------------

    def _fingerprint(self, clip: Clip) -> bytes:
        return self._vault.fingerprint(clip.identity())

    def _sealed(self, table: str, clip: Clip) -> tuple:
        """(kind, extra, thumb) as they go in the table."""
        extra = self._vault.seal_bytes(clips.pack(clip.formats), f"{table}.extra".encode()) if clip.formats else None
        thumb = self._vault.seal_bytes(clip.thumb, f"{table}.thumb".encode()) if clip.thumb else None
        return clip.kind, extra, thumb

    def clip(self, table: str, item_id: int) -> Clip | None:
        """Everything an item holds, ready to go back on the clipboard."""
        if table not in _ITEM_TABLES:
            raise ValueError(table)
        row = self._one(f"SELECT text, kind, extra, thumb FROM {table} WHERE id = ?", item_id)
        if row is None:
            return None
        try:
            text = self._vault.open(row["text"], _TEXT_CONTEXT[table])
            formats = clips.unpack(self._vault.open_bytes(row["extra"], f"{table}.extra".encode())) if row["extra"] else {}
            thumb = self._vault.open_bytes(row["thumb"], f"{table}.thumb".encode()) if row["thumb"] else None
        except (DecryptError, zlib.error, ValueError, IndexError):
            log.warning("could not open saved item %s %s", table, item_id)
            return None
        return Clip(row["kind"] or TEXT, text, formats, thumb)

    def thumb(self, table: str, item_id: int) -> bytes | None:
        if table not in _ITEM_TABLES:
            raise ValueError(table)
        row = self._one(f"SELECT thumb FROM {table} WHERE id = ?", item_id)
        if row is None or row["thumb"] is None:
            return None
        try:
            return self._vault.open_bytes(row["thumb"], f"{table}.thumb".encode())
        except DecryptError:
            return None

    # ---- settings ----------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", key)
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._run("INSERT INTO settings(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, str(value))

    def get_secret(self, key: str, default: str = "") -> str:
        """A setting that says something about the user (which programs they ignore), encrypted."""
        value = self.get(key)
        if value is None:
            return default
        try:
            return self._vault.open(bytes.fromhex(value), f"settings.{key}".encode())
        except (ValueError, DecryptError):
            return default

    def set_secret(self, key: str, value: str) -> None:
        self.set(key, self._vault.seal(value, f"settings.{key}".encode()).hex())

    # ---- history -----------------------------------------------------------

    def history_limits(self) -> tuple[int, int]:
        """(days to keep, most items to keep)"""
        def number(key: str, default: int, most: int) -> int:
            try:
                return max(1, min(int(self.get(key, default)), most))
            except ValueError:
                return default

        return number("history_days", DEFAULT_DAYS, MAX_DAYS), number("history_max", DEFAULT_MAX_ITEMS, MAX_ITEMS)

    def set_history_limits(self, days: int, max_items: int) -> None:
        self.set("history_days", days)
        self.set("history_max", max_items)

    @staticmethod
    def _cutoff(days: int, now: float | None) -> float:
        return (time.time() if now is None else now) - days * 86400

    def count_beyond_limits(self, days: int, max_items: int, now: float | None = None) -> int:
        """How many history items these limits would delete right now."""
        return self._one(f"SELECT COUNT(*) AS n FROM history WHERE {_BEYOND_LIMITS}",
                         self._cutoff(days, now), max_items)["n"]

    def expiring_history(self, now: float | None = None) -> set[str]:
        """The texts that prune_history() would delete right now, with the saved limits."""
        days, max_items = self.history_limits()
        rows = self._all(f"SELECT id, text FROM history WHERE {_BEYOND_LIMITS}", self._cutoff(days, now), max_items)
        return {item["text"] for item in self._read(rows, _HISTORY)}

    def prune_history(self, now: float | None = None) -> int:
        """Deletes what is older or further back than the limits. Returns how many went."""
        days, max_items = self.history_limits()
        return self._run(f"DELETE FROM history WHERE {_BEYOND_LIMITS}", self._cutoff(days, now), max_items).rowcount

    def add_history(self, item: str | Clip, now: float | None = None) -> None:
        """Copying something already in the history moves it back to the top, with the
        formatting of this latest copy."""
        clip = item if isinstance(item, Clip) else Clip.plain(item)
        stamp = time.time() if now is None else now
        with self._lock:
            kind, extra, thumb = self._sealed("history", clip)
            self._run("INSERT INTO history(text, text_hash, created, kind, extra, thumb) VALUES(?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT(text_hash) DO UPDATE SET created = excluded.created, kind = excluded.kind, "
                      "extra = excluded.extra, thumb = excluded.thumb",
                      self._vault.seal(clip.text, _HISTORY["text"]), self._fingerprint(clip), stamp, kind, extra, thumb)
            self.prune_history(now)

    def history(self, query: str = "") -> list[dict]:
        items = self._cached("history", "SELECT id, text, created, kind, text_hash, thumb IS NOT NULL AS has_thumb "
                                        "FROM history ORDER BY created DESC, id DESC", _HISTORY)
        return [item for item in items if _matches(query, item["text"])] if query else list(items)

    def delete_history(self, item_id: int) -> None:
        self._run("DELETE FROM history WHERE id = ?", item_id)

    def clear_history(self) -> None:
        self._run("DELETE FROM history")

    # ---- repertoire --------------------------------------------------------

    def add_saved(self, item: str | Clip, title: str = "") -> int:
        """The same copy is only kept once; saving it again returns the existing one."""
        clip = item if isinstance(item, Clip) else Clip.plain(item)
        with self._lock:
            fingerprint = self._fingerprint(clip)
            row = self._one("SELECT id FROM saved WHERE text_hash = ?", fingerprint)
            if row:
                return row["id"]
            kind, extra, thumb = self._sealed("saved", clip)
            return self._run("INSERT INTO saved(title, text, text_hash, created, kind, extra, thumb) "
                             "VALUES(?, ?, ?, ?, ?, ?, ?)",
                             self._vault.seal(title, _SAVED["title"]), self._vault.seal(clip.text, _SAVED["text"]),
                             fingerprint, time.time(), kind, extra, thumb).lastrowid

    def update_saved(self, item_id: int, text: str, title: str) -> None:
        """Changing the text drops the formatting it was copied with, which no longer matches."""
        fingerprint = self._vault.fingerprint(text)
        self._run("UPDATE saved SET text = ?, title = ?, text_hash = ?, "
                  "extra = CASE WHEN text_hash = ? THEN extra ELSE NULL END WHERE id = ?",
                  self._vault.seal(text, _SAVED["text"]), self._vault.seal(title, _SAVED["title"]),
                  fingerprint, fingerprint, item_id)

    def delete_saved(self, item_id: int) -> None:
        self._run("DELETE FROM saved WHERE id = ?", item_id)

    def saved(self, query: str = "") -> list[dict]:
        items = self._cached("saved", "SELECT id, title, text, created, kind, text_hash, thumb IS NOT NULL AS has_thumb "
                                      "FROM saved ORDER BY created DESC, id DESC", _SAVED)
        return [item for item in items if _matches(query, item["title"], item["text"])] if query else list(items)

    def saved_texts(self) -> set[str]:
        return {item["text"] for item in self.saved()}

    def saved_hashes(self) -> set[bytes]:
        """To show which history items are in the repertoire, without decrypting anything."""
        return {row["text_hash"] for row in self._all("SELECT text_hash FROM saved")}

    # ---- lists -------------------------------------------------------------

    def lists(self) -> list[dict]:
        return self._read(self._all("SELECT l.*, (SELECT COUNT(*) FROM list_items i WHERE i.list_id = l.id) AS size "
                                    "FROM lists l ORDER BY l.id"), _LISTS)

    def get_list(self, list_id: int) -> dict | None:
        return self._read_one(self._one("SELECT * FROM lists WHERE id = ?", list_id), _LISTS)

    def create_list(self, name: str) -> int:
        return self._run("INSERT INTO lists(name) VALUES(?)", self._vault.seal(name, _LISTS["name"])).lastrowid

    def rename_list(self, list_id: int, name: str) -> None:
        self._run("UPDATE lists SET name = ? WHERE id = ?", self._vault.seal(name, _LISTS["name"]), list_id)

    def delete_list(self, list_id: int) -> None:
        self._run("DELETE FROM lists WHERE id = ?", list_id)

    def set_reversed(self, list_id: int, value: bool) -> None:
        self._run("UPDATE lists SET reversed = ? WHERE id = ?", int(value), list_id)

    def list_size(self, list_id: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM list_items WHERE list_id = ?", list_id)["n"]

    def _paste_order(self, list_id: int) -> str:
        row = self._one("SELECT reversed FROM lists WHERE id = ?", list_id)
        return "DESC" if row and row["reversed"] else "ASC"

    def list_items(self, list_id: int) -> list[dict]:
        """In paste order: the first row is the next one to be pasted."""
        direction = self._paste_order(list_id)
        items = self._read(self._all(f"SELECT * FROM list_items WHERE list_id = ? "
                                     f"ORDER BY pos {direction}, id {direction}", list_id), _ITEMS)
        for item in items:
            item["has_thumb"] = item["thumb"] is not None
        return items

    def add_list_item(self, list_id: int, item: str | Clip) -> int:
        clip = item if isinstance(item, Clip) else Clip.plain(item)
        with self._lock:
            top = self._one("SELECT MAX(pos) AS p FROM list_items WHERE list_id = ?", list_id)["p"]
            pos = 0 if top is None else top + 1
            kind, extra, thumb = self._sealed("list_items", clip)
            return self._run("INSERT INTO list_items(list_id, pos, text, created, kind, extra, thumb) "
                             "VALUES(?, ?, ?, ?, ?, ?, ?)",
                             list_id, pos, self._vault.seal(clip.text, _ITEMS["text"]), time.time(),
                             kind, extra, thumb).lastrowid

    def next_item(self, list_id: int) -> dict | None:
        items = self.list_items(list_id)
        return items[0] if items else None

    def get_list_item(self, item_id: int) -> dict | None:
        return self._read_one(self._one("SELECT * FROM list_items WHERE id = ?", item_id), _ITEMS)

    def update_list_item(self, item_id: int, text: str) -> None:
        """Changing the text drops the formatting it was copied with, which no longer matches."""
        with self._lock:
            old = self.get_list_item(item_id)
            keep = old is not None and old["text"] == text
            self._run("UPDATE list_items SET text = ?, extra = CASE WHEN ? THEN extra ELSE NULL END WHERE id = ?",
                      self._vault.seal(text, _ITEMS["text"]), int(keep), item_id)

    def delete_list_item(self, item_id: int) -> None:
        self._run("DELETE FROM list_items WHERE id = ?", item_id)

    def restore_list_item(self, item: dict) -> bool:
        """Puts a removed item back where it was. False if its list no longer exists."""
        with self._lock:
            if not self.get_list(item["list_id"]):
                return False
            # Its old position may be taken by now (a move renumbers the list), so whatever sits
            # there or after it, in paste order, steps back: the item comes before it again.
            if self._paste_order(item["list_id"]) == "ASC":
                self._run("UPDATE list_items SET pos = pos + 1 WHERE list_id = ? AND pos >= ?", item["list_id"], item["pos"])
            else:
                self._run("UPDATE list_items SET pos = pos - 1 WHERE list_id = ? AND pos <= ?", item["list_id"], item["pos"])
            # extra and thumb go back as they were stored: sealed for this same column.
            self._run("INSERT INTO list_items(list_id, pos, text, created, kind, extra, thumb) VALUES(?, ?, ?, ?, ?, ?, ?)",
                      item["list_id"], item["pos"], self._vault.seal(item["text"], _ITEMS["text"]), item["created"],
                      item.get("kind") or TEXT, item.get("extra"), item.get("thumb"))
            return True

    def clear_list(self, list_id: int) -> None:
        self._run("DELETE FROM list_items WHERE list_id = ?", list_id)

    def move_item(self, list_id: int, item_id: int, new_index: int) -> None:
        """Moves an item to `new_index` in paste order."""
        with self._lock:
            direction = self._paste_order(list_id)
            ids = [row["id"] for row in self._all(f"SELECT id FROM list_items WHERE list_id = ? "
                                                  f"ORDER BY pos {direction}, id {direction}", list_id)]
            if item_id not in ids:
                return
            ids.remove(item_id)
            ids.insert(max(0, min(new_index, len(ids))), item_id)
            if direction == "DESC":
                ids.reverse()
            self._db.execute("BEGIN")
            try:
                for pos, row_id in enumerate(ids):
                    self._db.execute("UPDATE list_items SET pos = ? WHERE id = ?", (pos, row_id))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
