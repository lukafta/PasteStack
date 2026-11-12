"""The small window: History, Repertoire and Lists, plus the floating notice (HUD)."""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import queue
import re
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import messagebox

from . import APP_NAME, autostart, clips, data_dir, win32
from .clips import FILES, IMAGE, describe_files
from .crypto import MIN_PASSWORD, LockedError
from .paint import Painter
from .engine import APP_ICON_COLOR, HOTKEYS, OFF, PASTE, RECORD, Engine, signal_running_instance
from .store import MAX_DAYS, MAX_ITEMS, Store

log = logging.getLogger(__name__)

# The dark palette. use_theme() swaps in LIGHT (below) before the window is built, and
# again when the theme changes, after which the window is rebuilt: so code reads these
# names when it draws, never as default arguments.
BG = "#13151a"
SURFACE = "#1b1e25"
HOVER = "#22262f"
SELECTED = "#2a2f3b"
LINE = "#2a2e38"
LINE_STRONG = "#3b414e"
CHECK_LINE = "#606776"  # an empty checkbox: 3:1 against the background, like any control
TEXT = "#eceef2"
DIM = "#9aa2af"
FAINT = "#808895"  # the faintest text that still reads: 4.5:1 on SURFACE
SELECT_TEXT = "#2f4a7a"
# The same accent, red and green as the logo and the tray icon's states.
ACCENT = "#6ea8fe"
ACCENT_FG = "#6ea8fe"  # the accent as text, icons and thin lines
ACCENT_HOVER = "#8ab9ff"
ACCENT_BG = "#1a2438"
ACCENT_LINE = "#2c3f63"
ON_ACCENT = "#0c1322"
RED = "#f0616d"
RED_BG = "#351c22"
RED_HOVER = "#45242c"
RED_LINE = "#5c2d36"
GREEN = "#4cc38a"
GREEN_BG = "#15302a"
GREEN_HOVER = "#1b3d33"
GREEN_LINE = "#25564a"
AMBER = "#e5b454"

# Same language in light: layered neutrals, the same blue for fills, and the colors that
# carry text a shade deeper, since the logo's pale blue, red and green don't read on white.
LIGHT = {
    "BG": "#f4f5f8", "SURFACE": "#ffffff", "HOVER": "#e9ecf1", "SELECTED": "#dfe4ec",
    "LINE": "#dcdfe5", "LINE_STRONG": "#c3c8d1", "CHECK_LINE": "#868d9a",
    "TEXT": "#1b1e25", "DIM": "#565e6c", "FAINT": "#646b78", "SELECT_TEXT": "#cfe0fc",
    "ACCENT": "#6ea8fe", "ACCENT_FG": "#2360c4", "ACCENT_HOVER": "#5a99f8", "ACCENT_BG": "#e6effd",
    "ACCENT_LINE": "#b6cff6", "ON_ACCENT": "#0c1322",
    "RED": "#bd2c39", "RED_BG": "#fcebed", "RED_HOVER": "#f9dfe2", "RED_LINE": "#f0bcc2",
    "GREEN": "#11704a", "GREEN_BG": "#e3f4eb", "GREEN_HOVER": "#d5eee2", "GREEN_LINE": "#a8d9c0",
    "AMBER": "#925c00",
}
DARK = {name: globals()[name] for name in LIGHT}
THEMES = (("auto", "Automático"), ("light", "Claro"), ("dark", "Escuro"))

# Segoe Fluent Icons / Segoe MDL2 Assets
I_STAR, I_STAR_ON = "", ""
I_ADD, I_EDIT, I_DELETE, I_CLOSE = "", "", "", ""
I_PIN, I_PINNED = "", ""
I_SEARCH, I_RECORD, I_PLAY, I_PAUSE = "", "", "", ""
I_UNDO, I_DOWN, I_UP, I_CHEVRON, I_CLEAR = "", "", "", "", ""
I_SETTINGS = ""

I_HISTORY, I_LIST = chr(0xE81C), chr(0xE8FD)
I_IMAGE, I_FILES = chr(0xEB9F), chr(0xE8A5)
I_LOCK = chr(0xE72E)
I_BROOM = chr(0xEA99)  # empties a whole list or the history: not an X, which reads as "close"

HUD_COLORS: dict = {}
KINDS: dict = {}


def use_theme(theme: str) -> None:
    """Puts the "light" or "dark" palette in the color names above."""
    globals().update(LIGHT if theme == "light" else DARK)
    HUD_COLORS.clear()
    HUD_COLORS.update({RECORD: RED, PASTE: GREEN, "info": ACCENT, "warn": AMBER})
    # A button's (fill, fill on hover, border, color, color on hover). None: no fill or no border.
    KINDS.clear()
    KINDS.update({
        "ghost": (None, HOVER, None, TEXT, TEXT),
        "ghost_danger": (None, RED_BG, None, DIM, RED),
        "outline": (SURFACE, HOVER, LINE, TEXT, TEXT),
        "primary": (ACCENT, ACCENT_HOVER, None, ON_ACCENT, ON_ACCENT),
        "danger": (RED_BG, RED_HOVER, RED_LINE, RED, RED),
        "tab_on": (SELECTED, SELECTED, None, TEXT, TEXT),
        "tab_off": (None, HOVER, None, DIM, TEXT),
        "record_on": (RED_BG, RED_HOVER, RED_LINE, RED, RED),
        "paste_on": (GREEN_BG, GREEN_HOVER, GREEN_LINE, GREEN, GREEN),
    })


def resolve_theme(choice: str | None) -> str:
    """The setting ("auto", "light" or "dark") as the palette to use now."""
    if choice in ("light", "dark"):
        return choice
    return "light" if win32.apps_use_light_theme() else "dark"


use_theme("dark")


def flatten(text: str) -> str:
    return " ".join(text[:600].split()) or "(em branco)"


def when(timestamp: float) -> str:
    delta = time.time() - timestamp
    if delta < 60:
        return "agora"
    if delta < 3600:
        return f"há {int(delta // 60)} min"
    if delta < 86400:
        return f"há {int(delta // 3600)} h"
    if delta < 172800:
        return "ontem"
    return time.strftime("%d/%m/%Y", time.localtime(timestamp))


def count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def describe(text: str, timestamp: float) -> str:
    parts = [when(timestamp)]
    lines = text.count("\n") + 1
    if lines > 1:
        parts.append(f"{lines} linhas")
    elif len(text) > 80:
        parts.append(f"{len(text)} caracteres")
    return " · ".join(parts)


@dataclass
class Row:
    key: int
    title: str
    meta: str
    text: str
    actions: tuple = ()  # (icon, action name, hint), drawn from the right edge on hover
    badge: str = ""
    accent: bool = False
    kind: str = clips.TEXT
    table: str = "history"  # where the item is: "history", "saved" or "list_items"
    has_thumb: bool = False


def item_row(item: dict, table: str, **extra) -> Row:
    """A history, repertoire or list item as a row: an image shows its size, files their names."""
    kind = item.get("kind") or clips.TEXT
    if kind == FILES:
        paths = item["text"].split("\n")
        title, meta = describe_files(paths), f"{when(item['created'])} · {count(len(paths), 'arquivo', 'arquivos')}"
    elif kind == IMAGE:
        title, meta = item["text"], when(item["created"])
    else:
        title, meta = flatten(item["text"]), describe(item["text"], item["created"])
    if item.get("title"):  # a repertoire item with a name: the name, then what it holds
        title, meta = item["title"], title if kind != clips.TEXT else flatten(item["text"])
    return Row(item["id"], title, meta, item["text"], kind=kind, table=table, has_thumb=bool(item.get("has_thumb")),
               **extra)


class Btn(tk.Canvas):
    """A rounded button: an icon, a label, or both. `kind` picks its colors (see KINDS);
    `fg` overrides the color, and `icon_fg` the icon's alone."""

    def __init__(self, parent, app, command, icon="", text="", hint="", kind="ghost", fg=None, icon_right=False,
                 font=None, icon_fg=None):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, cursor="hand2")
        self.app, self.command, self.hint = app, command, hint
        self.icon, self.text, self.kind, self.fg, self.icon_fg = icon, text, kind, fg, icon_fg
        self.icon_right = icon_right
        self.font = font or app.f_ui
        self.hovered = False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _draw(self) -> None:
        app = self.app
        fill, hover_fill, border, color, hover_color = KINDS[self.kind]
        if self.hovered:
            fill = hover_fill
        if self.fg and self.fg != DIM:
            color = self.fg  # a color that means something (the pin's blue, the pause's amber) stays
        elif self.hovered:
            color = hover_color
        elif self.fg:
            color = self.fg
        icon_color = self.icon_fg or color
        height = app.px(28)
        icon_width = app.f_icon.measure(self.icon) if self.icon else 0
        text_width = self.font.measure(self.text) if self.text else 0
        pad = app.px(10)
        gap = app.px(6) if self.icon and self.text else 0
        width = pad * 2 + icon_width + gap + text_width if self.text else height
        self.configure(width=width, height=height)
        self.delete("all")
        app.paint.rect(self, 0, 0, width, height, app.px(7), fill=fill, border=border)
        middle = height // 2
        if not self.text:
            self.create_text(width // 2, middle, text=self.icon, fill=icon_color, font=app.f_icon)
            return
        x = pad
        if self.icon and not self.icon_right:
            self.create_text(x, middle, text=self.icon, fill=icon_color, font=app.f_icon, anchor="w")
            x += icon_width + gap
        self.create_text(x, middle, text=self.text, fill=color, font=self.font, anchor="w")
        if self.icon and self.icon_right:
            self.create_text(x + text_width + gap, middle, text=self.icon, fill=icon_color, font=app.f_icon, anchor="w")

    def _enter(self, _event) -> None:
        self.hovered = True
        self._draw()
        if self.hint:
            self.app.show_hint(self.hint)

    def _leave(self, _event) -> None:
        self.hovered = False
        self._draw()
        self.app.show_hint(None)

    def _click(self, _event) -> None:
        if self.command:
            self.command()

    def set(self, icon=None, text=None, fg=None, kind=None, hint=None) -> None:
        if icon is not None:
            self.icon = icon
        if text is not None:
            self.text = text
        if fg is not None:
            self.fg = fg
        if kind is not None:
            self.kind = kind
        if hint is not None:
            self.hint = hint
        self._draw()


class RoundBox(tk.Canvas):
    """A rounded field holding one widget (an Entry, a Text, a row of buttons). Its border
    lights up on focus, in the accent unless `focus_color` says otherwise."""

    def __init__(self, parent, app, icon="", width=None, pad_x=10, pad_y=6, focus_color=None, radius=7):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, height=app.px(30))
        if width:
            self.configure(width=width)
        self.app, self.icon, self.focus_color, self.radius = app, icon, focus_color, radius
        self.pad_x, self.pad_y = app.px(pad_x), app.px(pad_y)
        self.left = app.px(32) if icon else self.pad_x
        self.focused = False
        self.window = None
        self.bind("<Configure>", lambda _e: self._draw())

    def hold(self, widget) -> None:
        self.window = self.create_window(self.left, self.pad_y, window=widget, anchor="nw")
        widget.bind("<FocusIn>", lambda _e: self._focus(True), add="+")
        widget.bind("<FocusOut>", lambda _e: self._focus(False), add="+")
        if isinstance(widget, tk.Entry):
            self.configure(height=widget.winfo_reqheight() + 2 * self.pad_y)

    def _focus(self, focused: bool) -> None:
        self.focused = focused
        self._draw()

    def _draw(self) -> None:
        self.delete("back")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 4:
            return
        self.app.paint.rect(self, 0, 0, width, height, self.app.px(self.radius), fill=SURFACE,
                            border=(self.focus_color or ACCENT_FG) if self.focused else LINE, tags="back")
        if self.icon:
            self.create_text(self.left // 2 + self.app.px(1), height // 2, text=self.icon, fill=FAINT,
                             font=self.app.f_icon, tags="back")
        if self.window is not None:
            self.itemconfigure(self.window, width=max(1, width - self.left - self.pad_x),
                               height=max(1, height - 2 * self.pad_y))


class RowList(tk.Canvas):
    """A list drawn directly on a canvas: only visible rows exist, so it stays fast and
    does not flicker when a copy arrives. Clicks go to `owner` (activate, row_action,
    row_menu, reorder, show_hint): the main window, or the quick paste list."""

    def __init__(self, parent, app, owner=None):
        super().__init__(parent, bg=BG, highlightthickness=0, bd=0)
        self.app = app
        self.owner = owner or app
        self.rows: list[Row] = []
        self.empty: tuple = ()
        self.empty_icon = ""
        self.offset = 0
        self.selected: int | None = None
        self.hover: int | None = None
        self.hover_action: str | None = None
        self.reorderable = False
        self.row_h = app.f_ui.metrics("linespace") + app.f_small.metrics("linespace") + app.px(16)
        self._press: tuple | None = None
        self._drop: int | None = None
        self._fit_cache: dict = {}
        self._thumbs: dict = {}  # (table, id) -> PhotoImage, only for the rows shown now
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double)
        self.bind("<Button-3>", self._on_right)
        self.bind("<MouseWheel>", self._on_wheel)

    # ---- data ---------------------------------------------------------------

    def set_rows(self, rows: list[Row], empty: tuple, icon: str, reorderable: bool = False) -> None:
        selected_key = self.rows[self.selected].key if self.selected is not None and self.selected < len(self.rows) else None
        if [row.key for row in rows] != [row.key for row in self.rows]:
            self._press = self._drop = None  # a drag in progress would point at the wrong row now
        self.rows, self.empty, self.empty_icon, self.reorderable = rows, empty, icon, reorderable
        self.selected = next((i for i, row in enumerate(rows) if row.key == selected_key), None)
        # Decrypted thumbnails stay in memory only while their row is listed.
        listed = {(row.table, row.key) for row in rows if row.has_thumb}
        self._thumbs = {key: image for key, image in self._thumbs.items() if key in listed}
        self._clamp()
        self._redraw()

    def reset_view(self) -> None:
        self.offset = 0
        self.selected = None

    def current(self) -> Row | None:
        return self.rows[self.selected] if self.selected is not None and self.selected < len(self.rows) else None

    def move_selection(self, delta: int) -> None:
        if not self.rows:
            return
        index = 0 if self.selected is None else max(0, min(self.selected + delta, len(self.rows) - 1))
        self.selected = index
        height = self.winfo_height()
        top, bottom = index * self.row_h, (index + 1) * self.row_h
        if height <= 1:  # not on screen yet: nothing to scroll into view
            pass
        elif top < self.offset:
            self.offset = top
        elif bottom > self.offset + height:
            self.offset = bottom - height
        self._redraw()

    # ---- drawing ------------------------------------------------------------

    def _clamp(self) -> None:
        most = max(0, len(self.rows) * self.row_h - self.winfo_height())
        self.offset = max(0, min(self.offset, most))

    def _fit(self, font, text: str, width: int) -> str:
        key = (font.name, text, width)
        cached = self._fit_cache.get(key)
        if cached is not None:
            return cached
        if len(self._fit_cache) > 2000:
            self._fit_cache.clear()
        clipped = text[:max(8, width // 3)]
        if font.measure(clipped) <= width and len(clipped) == len(text):
            result = text
        else:
            low, high = 0, len(clipped)
            while low < high:
                middle = (low + high + 1) // 2
                if font.measure(clipped[:middle] + "…") <= width:
                    low = middle
                else:
                    high = middle - 1
            result = clipped[:low].rstrip() + "…"
        self._fit_cache[key] = result
        return result

    def _redraw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 10:
            return
        if not self.rows:
            self._draw_empty(width, height)
            return
        first = self.offset // self.row_h
        last = min(len(self.rows) - 1, (self.offset + height) // self.row_h)
        for index in range(first, last + 1):
            self._draw_row(index, width)
        app, total = self.app, len(self.rows) * self.row_h
        if total > height:  # scroll position
            bar = max(app.px(24), height * height // total)
            top = (height - bar) * self.offset // max(1, total - height)
            app.paint.rect(self, width - app.px(5), top, width - app.px(2), top + bar, app.px(2), fill=LINE_STRONG)
        if self._drop is not None:
            y = self._drop * self.row_h - self.offset
            app.paint.rect(self, app.px(8), y - app.px(1), width - app.px(8), y + app.px(1), app.px(1), fill=ACCENT_FG)

    def _draw_empty(self, width: int, height: int) -> None:
        """An icon, a title, and lines of help: (text, color), or (text, color, number) for a
        numbered step. Steps sit left-aligned in a centered block, which reads better than
        centered lines that wrap."""
        app = self.app
        size, x = app.px(56), width // 2
        y = max(app.px(24), height // 4)
        app.paint.rect(self, x - size // 2, y, x - size // 2 + size, y + size, size // 2, fill=SURFACE, border=LINE)
        self.create_text(x, y + size // 2, text=self.empty_icon, fill=DIM, font=app.f_icon_big)
        y += size + app.px(14)
        wrap = min(width - app.px(48), app.px(280))
        chip, chip_gap = app.px(20), app.px(10)
        was_step = False
        for index, (line, color, *step) in enumerate(self.empty):
            if step:
                y += 0 if was_step else app.px(6)
                left = x - wrap // 2
                top = y + (app.f_ui.metrics("linespace") - chip) // 2
                app.paint.rect(self, left, top, left + chip, top + chip, chip // 2, fill=SURFACE, border=LINE)
                self.create_text(left + chip // 2, top + chip // 2, text=str(step[0]), fill=DIM, font=app.f_small)
                item = self.create_text(left + chip + chip_gap, y, text=line, fill=color, font=app.f_ui,
                                        justify="left", width=wrap - chip - chip_gap, anchor="nw")
                y = self.bbox(item)[3] + app.px(10)
            else:
                y += app.px(6) if was_step else 0
                item = self.create_text(x, y, text=line, fill=color, font=app.f_tab if index == 0 else app.f_ui,
                                        justify="center", width=wrap, anchor="n")
                y = self.bbox(item)[3] + app.px(6)
            was_step = bool(step)

    def _thumbnail(self, row: Row, height: int):
        """The row's image, shrunk to fit `height`, or None."""
        key = (row.table, row.key)
        if key not in self._thumbs:
            data = self.app.store.thumb(row.table, row.key)
            image = None
            if data:
                try:
                    image = tk.PhotoImage(master=self, data=data, format="png")
                    if image.height() > height:
                        factor = -(-image.height() // height)
                        image = image.subsample(factor, factor)
                except tk.TclError:
                    image = None
            self._thumbs[key] = image
        return self._thumbs[key]

    def action_center(self, slot: int) -> int:
        """Where a row's action icon goes: slot 0 is the one at the right edge."""
        app = self.app
        size = app.px(26)
        return self.winfo_width() - app.px(8) - app.px(6) - size * slot - size // 2

    def _draw_row(self, index: int, width: int) -> None:
        app, row = self.app, self.rows[index]
        y = index * self.row_h - self.offset
        margin, gap = app.px(8), app.px(1)
        middle = y + self.row_h // 2
        hovered, selected = index == self.hover, index == self.selected
        fill = SELECTED if selected else (HOVER if hovered else (ACCENT_BG if row.accent else None))
        if fill:
            app.paint.rect(self, margin, y + gap, width - margin, y + self.row_h - gap, app.px(8), fill=fill,
                           border=ACCENT_LINE if row.accent and not selected else None)
        if selected:  # a bar on the left, so the row the arrow keys are on doesn't look like a hover
            app.paint.rect(self, margin + app.px(4), middle - app.px(9), margin + app.px(7), middle + app.px(9),
                           app.px(2), fill=ACCENT_FG)
        left = margin + app.px(12)
        if row.badge:
            self.create_text(left + app.px(7), middle, text=row.badge, anchor="center",
                             fill=ACCENT_FG if row.accent else (DIM if selected or hovered else FAINT), font=app.f_small)
            left += app.px(24)
        if row.kind != clips.TEXT:  # a thumbnail for an image, an icon for files (or an image without one)
            box = self.row_h - app.px(16)
            image = self._thumbnail(row, box) if row.kind == IMAGE and row.has_thumb else None
            if image is not None:
                self.create_image(left, middle, image=image, anchor="w")
                left += image.width() + app.px(10)
            else:
                app.paint.rect(self, left, middle - box // 2, left + box, middle + box // 2, app.px(6),
                               fill=SURFACE, border=LINE)
                self.create_text(left + box // 2, middle, text=I_IMAGE if row.kind == IMAGE else I_FILES, fill=DIM,
                                 font=app.f_icon)
                left += box + app.px(10)
        right = width - margin - app.px(10)
        if hovered and row.actions:
            size = app.px(26)
            for slot, (icon, name, _hint) in enumerate(row.actions):
                x = self.action_center(slot)
                active = name == self.hover_action
                if active:
                    half = size // 2
                    back = RED_BG if name == "delete" else (LINE_STRONG if selected else SELECTED)
                    app.paint.rect(self, x - half, middle - half, x + half, middle + half, app.px(6), fill=back)
                color = RED if active and name == "delete" else (TEXT if active else DIM)
                if icon == I_STAR_ON:
                    color = AMBER
                self.create_text(x, middle, text=icon, fill=color, font=app.f_icon)
            right = self.action_center(len(row.actions) - 1) - size // 2 - app.px(4)
        text_width = max(20, right - left)
        self.create_text(left, middle - app.px(1), text=self._fit(app.f_ui, row.title, text_width), anchor="sw",
                         fill=TEXT, font=app.f_ui)
        self.create_text(left, middle + app.px(1), text=self._fit(app.f_small, row.meta, text_width), anchor="nw",
                         fill=ACCENT_FG if row.accent else DIM, font=app.f_small)

    # ---- mouse --------------------------------------------------------------

    def _index_at(self, y: int) -> int | None:
        index = (y + self.offset) // self.row_h
        return index if 0 <= index < len(self.rows) else None

    def _action_at(self, index: int | None, x: int) -> tuple | None:
        if index is None:
            return None
        size = self.app.px(26)
        for slot, action in enumerate(self.rows[index].actions):
            if abs(x - self.action_center(slot)) <= size // 2:
                return action
        return None

    def _on_motion(self, event) -> None:
        index = self._index_at(event.y)
        action = self._action_at(index, event.x)
        name = action[1] if action else None
        if index != self.hover or name != self.hover_action:
            self.hover, self.hover_action = index, name
            self.owner.show_hint(action[2] if action else None)
            self._redraw()

    def _on_leave(self, _event) -> None:
        if self.hover is not None:
            self.hover = self.hover_action = None
            self.owner.show_hint(None)
            self._redraw()

    def _on_press(self, event) -> None:
        self.focus_set()
        index = self._index_at(event.y)
        self._press = None
        if index is None:
            return
        action = self._action_at(index, event.x)
        if action:
            self.owner.row_action(action[1], self.rows[index])
            return
        self.selected = index
        self._press = (index, event.y)
        self._redraw()

    def _on_drag(self, event) -> None:
        if not self.reorderable or self._press is None:
            return
        if self._drop is None and abs(event.y - self._press[1]) < self.app.px(6):
            return
        self._drop = max(0, min(len(self.rows), (event.y + self.offset + self.row_h // 2) // self.row_h))
        self._redraw()

    def _on_release(self, _event) -> None:
        press, drop = self._press, self._drop
        self._press = self._drop = None
        if press is None or drop is None:
            return
        start = press[0]
        new_index = drop if drop <= start else drop - 1
        self._redraw()
        if new_index != start:
            self.owner.reorder(self.rows[start], new_index)

    def _on_double(self, event) -> None:
        index = self._index_at(event.y)
        if index is not None and not self._action_at(index, event.x):
            self.owner.activate(self.rows[index])

    def _on_right(self, event) -> None:
        index = self._index_at(event.y)
        if index is not None:
            self.selected = index
            self._redraw()
            self.owner.row_menu(self.rows[index], event.x_root, event.y_root)

    def _on_wheel(self, event) -> None:
        self.offset -= int(event.delta / 120 * self.row_h)
        self._clamp()
        self.hover = self._index_at(event.y)
        self.hover_action = None
        self._redraw()


class Hud:
    """A small notice over everything. It stays mapped at alpha 0 and is never activated,
    so showing it can't take the focus from the program the user is pasting into."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root, bg=SURFACE)
        self.win.title(f"{APP_NAME} aviso")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True, "-alpha", 0.0)
        self.dot = tk.Label(self.win, bg=SURFACE)
        self.dot.pack(side="left", padx=(app.px(14), 0))
        self.label = tk.Label(self.win, bg=SURFACE, fg=TEXT, font=app.f_ui)
        self.label.pack(side="left", padx=(app.px(9), app.px(16)), pady=app.px(10))
        self.win.geometry("1x1+0+0")
        self.win.update()
        win32.style_overlay(int(self.win.wm_frame(), 16), LINE_STRONG)
        self._job = None

    def show(self, text: str, kind: str) -> None:
        self.dot.configure(image=self.app.paint.dot(HUD_COLORS.get(kind, ACCENT_FG), self.app.px(8)))
        self.label.configure(text=text)
        self.win.update_idletasks()
        width, height = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x = (self.win.winfo_screenwidth() - width) // 2
        y = self.win.winfo_screenheight() - height - self.app.px(90)
        self.win.geometry(f"{width}x{height}+{x}+{y}")
        self.win.lift()
        self.win.attributes("-alpha", 0.97)
        if self._job:
            self.win.after_cancel(self._job)
        self._job = self.win.after(1500, lambda: self._fade(0.97))

    def _fade(self, alpha: float) -> None:
        alpha -= 0.16
        self.win.attributes("-alpha", max(alpha, 0.0))
        self._job = self.win.after(25, lambda: self._fade(alpha)) if alpha > 0 else None

    def destroy(self) -> None:
        if self._job:
            self.win.after_cancel(self._job)
        self.win.destroy()


class QuickPaste:
    """The small list that opens where you are typing: type to filter, Enter pastes into the
    program you were in, Esc closes. Tab switches between the history and the repertoire."""

    SOURCES = (("history", "Histórico"), ("saved", "Repertório"))
    VISIBLE_ROWS = 7

    def __init__(self, app: "App"):
        self.app = app
        self.win: tk.Toplevel | None = None
        self.source = "history"

    def is_open(self) -> bool:
        return self.win is not None and bool(self.win.winfo_exists())

    def toggle(self) -> None:
        if self.is_open():
            self.close()
        else:
            self.open()

    def open(self) -> None:
        app, px = self.app, self.app.px
        win = self.win = tk.Toplevel(app.root, bg=BG)
        win.withdraw()
        win.title(f"{APP_NAME} colar rápido")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=px(8), pady=px(8))
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x")
        self.pills = {}
        for key, label in self.SOURCES:
            self.pills[key] = Btn(top, app, lambda k=key: self.set_source(k), text=label, kind="tab_off", font=app.f_tab)
            self.pills[key].pack(side="left", padx=(0, px(2)))
        tk.Label(top, text="Tab troca", bg=BG, fg=FAINT, font=app.f_small).pack(side="right", padx=(0, px(4)))
        box = RoundBox(body, app, icon=I_SEARCH, focus_color=LINE_STRONG)
        box.pack(fill="x", pady=(px(6), px(4)))
        self.query = tk.StringVar(win)
        self.entry = tk.Entry(box, textvariable=self.query, bg=SURFACE, fg=TEXT, insertbackground=TEXT, bd=0,
                              relief="flat", font=app.f_ui, highlightthickness=0, selectbackground=SELECT_TEXT,
                              selectforeground=TEXT)
        box.hold(self.entry)
        self.rows = RowList(body, app, owner=self)
        self.rows.configure(height=self.rows.row_h * self.VISIBLE_ROWS, width=px(380))
        self.rows.pack(fill="both", expand=True)
        self.footer = tk.Label(body, text="Enter cola · Shift+Enter sem formatação · Esc fecha", bg=BG, fg=FAINT,
                               font=app.f_small, anchor="w")
        self.footer.pack(fill="x", padx=px(4), pady=(px(4), 0))
        self.query.trace_add("write", lambda *_args: self.refresh(reset=True))
        entry = self.entry
        entry.bind("<Down>", lambda _e: (self.rows.move_selection(1), "break")[1])
        entry.bind("<Up>", lambda _e: (self.rows.move_selection(-1), "break")[1])
        entry.bind("<Return>", lambda _e: (self.paste_selected(), "break")[1])
        entry.bind("<Shift-Return>", lambda _e: (self.paste_selected(plain=True), "break")[1])
        entry.bind("<Tab>", lambda _e: (self.set_source("saved" if self.source == "history" else "history"), "break")[1])
        win.bind("<Escape>", lambda _e: self.close())
        win.bind("<FocusOut>", lambda _e: win.after(100, self._close_if_left))
        self.set_source(self.source)
        win.update_idletasks()
        self._place()
        win.deiconify()
        win.update()
        win32.style_popup(int(win.wm_frame(), 16), LINE_STRONG)
        win.lift()
        win.focus_force()
        entry.focus_force()
        win.after(700, self._watch)

    def _watch(self) -> None:
        """Closes the list once another program is in front, even if Windows never let it
        take the focus (so no focus-out or Esc would ever reach it)."""
        if not self.is_open():
            return
        if not win32.is_own_window(win32.user32.GetForegroundWindow()):
            self.close()
        else:
            self.win.after(400, self._watch)

    def _place(self) -> None:
        """Under the text cursor when the program shows where it is, else under the mouse;
        above it instead when there is no room below."""
        win, px = self.win, self.app.px
        width, height = win.winfo_reqwidth(), win.winfo_reqheight()
        point = win32.caret_point(self.app.engine.target) or win32.cursor_point()
        left, top, right, bottom = win32.work_area_at(*point)
        x, y = point[0], point[1] + px(6)
        if y + height > bottom:
            y = point[1] - height - px(24)
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _close_if_left(self) -> None:
        """Clicking anywhere else, in another program or in the main window, closes it."""
        if not self.is_open():
            return
        focus = self.win.focus_get()
        if focus is None or focus.winfo_toplevel() is not self.win:
            self.close()

    def close(self) -> None:
        if self.is_open():
            self.win.destroy()
        self.win = None

    def set_source(self, key: str) -> None:
        self.source = key
        for name, pill in self.pills.items():
            pill.set(kind="tab_on" if name == key else "tab_off")
        self.refresh(reset=True)

    def refresh(self, reset: bool = False) -> None:
        if not self.is_open():
            return
        query, store = self.query.get(), self.app.store
        if self.source == "history":
            rows = [item_row(item, "history") for item in store.history(query)]
            empty, icon = ("Nada copiado ainda", TEXT), I_HISTORY
        else:
            rows = [item_row(item, "saved") for item in store.saved(query)]
            empty, icon = ("Repertório vazio", TEXT), I_STAR
        if reset:
            self.rows.reset_view()
        self.rows.set_rows(rows, (("Nada encontrado", DIM),) if query else (empty,), I_SEARCH if query else icon)
        if self.rows.rows and self.rows.selected is None:
            self.rows.move_selection(0)

    def paste_selected(self, plain: bool = False) -> None:
        row = self.rows.current()
        if row is not None:
            self.activate(row, plain)

    def activate(self, row: Row, plain: bool = False) -> None:
        self.close()
        self.app.engine.call(self.app.engine.paste_item, row.table, row.key, plain)

    # The list's other callbacks: nothing to do here.
    def row_action(self, _action, _row) -> None:
        pass

    def row_menu(self, _row, _x, _y) -> None:
        pass

    def reorder(self, _row, _index) -> None:
        pass

    def show_hint(self, _text) -> None:
        pass


class App:
    TABS = (("history", "Histórico"), ("saved", "Repertório"), ("lists", "Listas"))

    def __init__(self, store: Store, engine: Engine, events: queue.SimpleQueue):
        self.store, self.engine, self.events = store, engine, events
        self.theme = resolve_theme(store.get("theme", "auto"))
        use_theme(self.theme)
        win32.set_menu_theme(self.theme == "dark")
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        # The sizes Windows asks for at this scale too (24 and 48 at 150%), so it never shrinks one.
        sizes = sorted({48, 32, 16, *win32.icon_sizes()}, reverse=True)
        self._icons = [tk.PhotoImage(master=self.root, data=win32.icon_png(APP_ICON_COLOR, size), format="png")
                       for size in sizes]  # kept: Tk drops an image nothing refers to
        self.root.iconphoto(True, *self._icons)
        self.scale = self.root.winfo_fpixels("1i") / 96

        families = set(tkfont.families())
        icon_family = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        self.f_ui = tkfont.Font(family="Segoe UI", size=10)
        self.f_small = tkfont.Font(family="Segoe UI", size=9)
        self.f_title = tkfont.Font(family="Segoe UI Semibold", size=12)
        self.f_tab = tkfont.Font(family="Segoe UI Semibold", size=10)
        self.f_icon = tkfont.Font(family=icon_family, size=10)
        self.f_icon_big = tkfont.Font(family=icon_family, size=18)
        self.paint = Painter(self.root)

        self.tab = "history"
        self.hidden_hint_shown = store.get("hidden_hint") == "1"
        self.topmost = store.get("topmost", "1") == "1"
        self.warning: str | None = None
        self._hint: str | None = None
        self._search_job = None
        self._settings: tk.Toplevel | None = None
        self._shown = False  # opened with Windows, the window waits in the tray until asked for

        self._build()
        self.hud = Hud(self)
        self.quick = QuickPaste(self)
        self._bind_keys()
        self._place_window()
        self.root.attributes("-topmost", self.topmost)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.set_tab("history")
        self._sync_mode()
        self.root.after(40, self._pump)
        self.root.after(60_000, self._tick)

    def px(self, value: float) -> int:
        return round(value * self.scale)

    # ---- layout -------------------------------------------------------------

    def _build(self) -> None:
        root, px = self.root, self.px
        status = tk.Frame(root, bg=BG)
        status.pack(side="bottom", fill="x", padx=px(8), pady=(px(3), px(4)))
        self.pause_btn = Btn(status, self, self._toggle_pause, icon=I_PAUSE, fg=DIM)
        self.pause_btn.pack(side="right")  # before the text: a long one gets cut, not the button
        self.clear_btn = Btn(status, self, self._clear_history, icon=I_BROOM, hint="Apagar todo o histórico",
                             kind="ghost_danger")
        self.status_dot = tk.Label(status, bg=BG)
        self.status_dot.pack(side="left", padx=(px(6), px(7)))
        self.status = tk.Label(status, bg=BG, fg=DIM, font=self.f_small, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        tk.Frame(root, bg=LINE, height=1).pack(side="bottom", fill="x")

        header = tk.Frame(root, bg=BG)
        header.pack(side="top", fill="x", padx=px(8), pady=(px(6), px(2)))
        self.pin_btn = Btn(header, self, self._toggle_pin, icon=I_PINNED if self.topmost else I_PIN,
                           hint="Manter sempre visível", fg=ACCENT_FG if self.topmost else DIM)
        self.pin_btn.pack(side="right")
        Btn(header, self, self.open_settings, icon=I_SETTINGS, hint="Configurações", fg=DIM).pack(
            side="right", padx=(0, px(2)))
        self.tab_btns = {}
        for key, title in self.TABS:
            button = Btn(header, self, lambda k=key: self.set_tab(k), text=title, kind="tab_off", font=self.f_tab)
            button.pack(side="left", padx=(0, px(2)))
            self.tab_btns[key] = button

        self.search_bar = tk.Frame(root, bg=BG)
        box = RoundBox(self.search_bar, self, icon=I_SEARCH, focus_color=LINE_STRONG)
        box.pack(fill="x", padx=px(8), pady=(px(6), px(4)))
        self.search_var = tk.StringVar()
        self.search = tk.Entry(box, textvariable=self.search_var, bg=SURFACE, fg=TEXT, insertbackground=TEXT, bd=0,
                               relief="flat", font=self.f_ui, highlightthickness=0, selectbackground=SELECT_TEXT,
                               selectforeground=TEXT)
        box.hold(self.search)
        self.placeholder = tk.Label(box, text="Buscar", font=self.f_ui, bg=SURFACE, fg=FAINT, bd=0, padx=0, pady=0)
        self._show_placeholder()
        self.placeholder.bind("<Button-1>", lambda _e: self.search.focus_set())
        self.search_var.trace_add("write", self._on_search)

        self.saved_bar = tk.Frame(root, bg=BG)
        Btn(self.saved_bar, self, self.new_saved, icon=I_ADD, text="Novo item", kind="outline").pack(
            side="left", padx=px(8), pady=(0, px(4)))

        self.list_bar = tk.Frame(root, bg=BG)
        top = tk.Frame(self.list_bar, bg=BG)
        top.pack(fill="x", padx=px(8), pady=(px(6), px(4)))
        Btn(top, self, self._delete_list, icon=I_DELETE, hint="Apagar esta lista", kind="ghost_danger").pack(
            side="right")
        Btn(top, self, self._rename_list, icon=I_EDIT, hint="Renomear esta lista", fg=DIM).pack(side="right")
        Btn(top, self, self._new_list, icon=I_ADD, hint="Nova lista", fg=DIM).pack(side="right")
        self.list_btn = Btn(top, self, self._list_menu, icon=I_CHEVRON, text=" ", hint="Trocar de lista", kind="outline",
                            icon_right=True)
        self.list_btn.pack(side="left")
        bottom = tk.Frame(self.list_bar, bg=BG)
        bottom.pack(fill="x", padx=px(8), pady=(0, px(4)))
        gap = px(4)
        Btn(bottom, self, self._clear_list, icon=I_BROOM, hint="Apagar os itens desta lista", kind="ghost_danger").pack(
            side="right")
        self.undo_btn = Btn(bottom, self, lambda: self.engine.call(self.engine.undo_removal), icon=I_UNDO,
                            hint="Devolver à lista o último item colado", fg=DIM)
        self.undo_btn.pack(side="right")
        self.record_btn = Btn(bottom, self, lambda: self._toggle_mode(RECORD), icon=I_RECORD, text="Gravar",
                              hint="Cada Ctrl+C entra nesta lista", kind="outline", icon_fg=RED)
        self.record_btn.pack(side="left", padx=(0, gap))
        self.paste_btn = Btn(bottom, self, lambda: self._toggle_mode(PASTE), icon=I_PLAY, text="Colar",
                             hint="Cada Ctrl+V cola o próximo item e o tira da lista", kind="outline", icon_fg=GREEN)
        self.paste_btn.pack(side="left", padx=(0, gap))
        self.order_btn = Btn(bottom, self, self._toggle_order, icon=I_DOWN, text="Em ordem",
                             hint="Inverter a ordem de colagem", kind="outline")
        self.order_btn.pack(side="left")

        self.rows = RowList(root, self)
        self.rows.pack(side="top", fill="both", expand=True, pady=(px(2), px(2)))
        self._build_lock()

    def _build_lock(self) -> None:
        """What covers the window while the password hasn't been typed."""
        px = self.px
        self.lock_frame = tk.Frame(self.root, bg=BG)
        inner = tk.Frame(self.lock_frame, bg=BG)
        inner.place(relx=0.5, rely=0.42, anchor="center")
        tk.Label(inner, text=I_LOCK, font=self.f_icon_big, bg=BG, fg=DIM).pack()
        tk.Label(inner, text=f"O {APP_NAME} está travado", font=self.f_title, bg=BG, fg=TEXT).pack(pady=(px(10), 0))
        tk.Label(inner, text="Nada é guardado até você destravar.", font=self.f_small, bg=BG, fg=DIM).pack(
            pady=(px(4), px(14)))
        box, self.lock_entry = _field(self, inner, width=px(240))
        self.lock_entry.configure(show="•")
        box.pack()
        self.lock_error = tk.Label(inner, text="", font=self.f_small, bg=BG, fg=RED)
        self.lock_error.pack(pady=(px(6), 0))
        Btn(inner, self, self._unlock, text="Destravar", kind="primary").pack(pady=(px(6), 0))
        Btn(inner, self, self._forgot_password, text="Esqueci a senha", kind="ghost", fg=DIM).pack(pady=(px(8), 0))
        self.lock_entry.bind("<Return>", lambda _e: (self._unlock(), "break")[1])

    def show_locked(self) -> None:
        """Covers the window and closes anything that was showing saved text."""
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel) and child is not self.hud.win:
                child.destroy()
        self.quick.close()
        self.rows.set_rows([], (), I_LOCK)
        self.lock_error.configure(text="")
        self.lock_frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.lock_frame.lift()
        if self._on_view():
            self.lock_entry.focus_force()
        self._sync_status()

    def _unlock(self) -> None:
        password = self.lock_entry.get()
        self.lock_entry.delete(0, "end")
        if not password:
            return
        try:
            opened = self.store.unlock(password)
        except LockedError:
            self.lock_error.configure(text="A chave não abre nesta conta do Windows.")
            return
        if not opened:
            self.lock_error.configure(text="Senha errada.")
            return
        self._after_unlock()

    def _after_unlock(self) -> None:
        self.lock_frame.place_forget()
        self.engine.call(self.engine.unlocked)
        self.engine.pick_active_list()
        self.refresh()
        self._sync_mode()
        self._focus_input()

    def _forgot_password(self) -> None:
        def start_over() -> None:
            self.store.wipe()
            self._after_unlock()
            self.hud.show("Tudo apagado: começando do zero", "info")

        open_delete_notice(self, "many", "Apagar tudo e começar de novo?", start_over, silenceable=False,
                           detail="O histórico, o repertório e as listas são apagados de vez. Sem a senha, "
                                  "não há como abrir o que está guardado.")

    def _place_window(self) -> None:
        root = self.root
        root.minsize(self.px(340), self.px(360))
        geometry = self.store.get("geometry")
        if not geometry or not self._on_screen(geometry):
            width, height = self.px(360), self.px(560)
            x = root.winfo_screenwidth() - width - self.px(24)
            y = root.winfo_screenheight() - height - self.px(90)
            geometry = f"{width}x{height}+{x}+{y}"
        root.geometry(geometry)

    def _on_screen(self, geometry: str) -> bool:
        """A saved position can be off-screen after a monitor is unplugged, and opening
        the window out there would lose it."""
        # Tk writes a negative position as "+-1500" (a monitor left of or above the main one).
        # A "-" without the "+" counts from the right or bottom edge, and the app never saves that.
        match = re.fullmatch(r"\d+x\d+\+(-?\d+)\+(-?\d+)", geometry)
        if not match:
            return False
        x, y = int(match[1]), int(match[2])
        left, top, right, bottom = win32.virtual_screen()
        return left <= x <= right - self.px(80) and top <= y <= bottom - self.px(40)

    def _bind_keys(self) -> None:
        root = self.root
        root.bind("<Up>", lambda _e: self.rows.move_selection(-1))
        root.bind("<Down>", lambda _e: self.rows.move_selection(1))
        root.bind("<Return>", self._on_enter)
        root.bind("<Escape>", self._on_escape)
        root.bind("<Delete>", self._on_delete_key)
        for number, (key, _title) in enumerate(self.TABS, start=1):
            root.bind(f"<Control-Key-{number}>", lambda _e, k=key: self.set_tab(k))

    # ---- window -------------------------------------------------------------

    def first_show(self) -> None:
        """Mapped invisible first, so the title bar never flashes white."""
        self._shown = True
        root = self.root
        root.attributes("-alpha", 0.0)
        root.deiconify()
        root.update()
        self._style_titlebar(root)
        root.attributes("-alpha", 1.0)
        root.lift()
        root.focus_force()
        self._focus_input()
        if self.store.get("privacy_notice") != "hide":
            open_privacy_notice(self, lambda accepted: self.engine.call(self.engine.close_notice, accepted))

    def _style_titlebar(self, window) -> None:
        win32.style_titlebar(int(window.wm_frame(), 16), BG, LINE, TEXT, dark=self.theme == "dark")

    def apply_theme(self) -> None:
        """Follows the setting, or Windows when it is on automatic. The window is rebuilt in
        the new colors; while a dialog is open that waits, so nothing typed in it is lost."""
        theme = resolve_theme(self.store.get("theme", "auto"))
        if theme == self.theme:
            return
        if any(isinstance(w, tk.Toplevel) and w is not self.hud.win for w in self.root.winfo_children()):
            self.root.after(1000, self.apply_theme)
            return
        self.theme = theme
        use_theme(theme)
        win32.set_menu_theme(theme == "dark")
        query = self.search_var.get()
        self.quick.close()
        self.hud.destroy()
        for child in self.root.winfo_children():
            child.destroy()
        self.root.configure(bg=BG)
        self._build()
        self.hud = Hud(self)
        self.search_var.set(query)
        self.set_tab(self.tab)
        self._sync_mode()
        self._style_titlebar(self.root)

    def open_settings(self, page: str = "general") -> None:
        if self.store.locked:
            self.show()
            return
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.lift()
        else:
            self._settings = open_settings(self, page)

    def show(self) -> None:
        if not self._shown:
            self.first_show()
            self.refresh()
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._focus_input()
        self.refresh()

    def hide(self) -> None:
        self._save_geometry()
        self.root.withdraw()
        if not self.hidden_hint_shown:
            self.hidden_hint_shown = True
            self.store.set("hidden_hint", "1")
            shortcut = self.engine.hotkey_label("window")
            self.hud.show(f"Continua rodando na bandeja · {shortcut} abre" if shortcut
                          else "Continua rodando na bandeja: o ícone de lá abre a janela", "info")

    def _on_view(self) -> bool:
        return self.root.state() in ("normal", "zoomed")

    def toggle(self, source: str) -> None:
        if not self._on_view():  # closed to the tray, or minimized
            self.show()
        elif source == "hotkey" and not win32.is_own_window(win32.user32.GetForegroundWindow()):
            self.show()
        else:
            self.hide()

    def quit(self) -> None:
        self._save_geometry()
        self.engine.stop()
        self.engine.join(timeout=2)
        self.root.destroy()

    def _save_geometry(self) -> None:
        if self.root.state() == "normal":
            self.store.set("geometry", self.root.geometry())

    def _toggle_pin(self) -> None:
        self.topmost = not self.topmost
        self.store.set("topmost", "1" if self.topmost else "0")
        self.root.attributes("-topmost", self.topmost)
        self.pin_btn.set(icon=I_PINNED if self.topmost else I_PIN, fg=ACCENT_FG if self.topmost else DIM)

    def _focus_input(self) -> None:
        if self.tab == "lists":
            self.rows.focus_set()
        else:
            self.search.focus_set()

    # ---- events from the engine ---------------------------------------------

    def _pump(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def _handle(self, event: tuple) -> None:
        name = event[0]
        if name == "history":
            if self.tab == "history":
                self.refresh()
        elif name == "list":
            if self.tab == "lists":
                self.refresh()
            self._sync_mode()
        elif name == "mode":
            if self.engine.mode != OFF and self.tab != "lists":
                self.set_tab("lists")
            self._sync_mode()
        elif name == "hud":
            self.hud.show(event[1], event[2])
        elif name == "show":
            self.show()
        elif name == "toggle":
            self.toggle(event[1])
        elif name == "warn":
            self.warning = event[1]
            self._sync_status()
        elif name == "settings":
            self.show()
            self.open_settings()
        elif name == "quick":
            if self.store.locked:
                self.show()
            else:
                self.quick.toggle()
        elif name == "locked":
            self.show_locked()
        elif name == "hotkeys":
            self._sync_status()
            if self.tab == "lists":
                self.refresh()
        elif name == "theme":
            self.apply_theme()
        elif name == "quit":
            self.quit()

    def _tick(self) -> None:
        if self._on_view():  # keeps "há 5 min" honest
            self.refresh()
        self.root.after(60_000, self._tick)

    # ---- tabs and rows ------------------------------------------------------

    def set_tab(self, key: str) -> None:
        self.tab = key
        for tab_key, button in self.tab_btns.items():
            button.set(kind="tab_on" if tab_key == key else "tab_off")
        for bar in (self.search_bar, self.saved_bar, self.list_bar):
            bar.pack_forget()
        if key == "lists":
            self.list_bar.pack(side="top", fill="x", before=self.rows)
        else:
            self.search_bar.pack(side="top", fill="x", before=self.rows)
            if key == "saved":
                self.saved_bar.pack(side="top", fill="x", before=self.rows)
        self.clear_btn.pack_forget()
        if key == "history":
            self.clear_btn.pack(side="right", before=self.pause_btn)
        self.rows.reset_view()
        self.refresh()
        self._sync_status()
        self._focus_input()

    def _show_placeholder(self) -> None:
        self.placeholder.place(x=self.px(32), rely=0.5, anchor="w")

    def _on_search(self, *_args) -> None:
        if self.search_var.get():
            self.placeholder.place_forget()
        else:
            self._show_placeholder()
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(120, self._apply_search)

    def _apply_search(self) -> None:
        self._search_job = None
        self.rows.reset_view()
        self.refresh()

    def refresh(self) -> None:
        if self.store.locked:
            self.show_locked()
            return
        if self.tab == "history":
            self._show_history()
        elif self.tab == "saved":
            self._show_saved()
        else:
            self._show_list()

    def _show_history(self) -> None:
        query = self.search_var.get()
        saved = self.store.saved_hashes()
        rows = []
        for item in self.store.history(query):
            star = (I_STAR_ON, "save", "Já está no repertório") if item["text_hash"] in saved else \
                (I_STAR, "save", "Salvar no repertório")
            rows.append(item_row(item, "history", actions=((I_DELETE, "delete", "Apagar de vez do histórico"),
                                                            (I_ADD, "to_list", "Adicionar à lista atual"), star)))
        empty = (("Nada encontrado", DIM),) if query else (
            ("Nada copiado ainda", TEXT),
            ("O que você copiar com Ctrl+C aparece aqui: texto, imagens e arquivos. Dois cliques colam no programa "
             "onde você estava.", DIM))
        self.rows.set_rows(rows, empty, I_SEARCH if query else I_HISTORY)

    def _show_saved(self) -> None:
        query = self.search_var.get()
        rows = []
        for item in self.store.saved(query):
            actions = ((I_DELETE, "delete", "Apagar de vez do repertório"),)
            if item["kind"] == clips.TEXT:
                actions += ((I_EDIT, "edit", "Editar"),)
            rows.append(item_row(item, "saved", actions=actions + ((I_ADD, "to_list", "Adicionar à lista atual"),)))
        empty = (("Nada encontrado", DIM),) if query else (
            ("Repertório vazio", TEXT),
            ("Clique na estrela de um item do histórico ou crie um item novo. O que está aqui não sai sozinho.", DIM))
        self.rows.set_rows(rows, empty, I_SEARCH if query else I_STAR)

    def _show_list(self) -> None:
        rows = []
        for number, item in enumerate(self.store.list_items(self.engine.active_list), start=1):
            actions = ((I_DELETE, "delete", "Apagar de vez da lista"),)
            if item["kind"] == clips.TEXT:
                actions += ((I_EDIT, "edit", "Editar"),)
            row = item_row(item, "list_items", actions=actions, badge=str(number), accent=number == 1)
            if number == 1:
                row.meta = "próximo · " + row.meta
            rows.append(row)
        empty = (
            ("Lista vazia", TEXT),
            ("Clique em Gravar e copie o que quiser com Ctrl+C", DIM, 1),
            ("Clique em Colar e use Ctrl+V: cada um cola o próximo item e o tira da lista", DIM, 2),
        )
        cycle = self.engine.hotkey_label("cycle")
        if cycle:
            empty += ((f"{cycle} alterna entre gravar, colar e desligar", FAINT),)
        self.rows.set_rows(rows, empty, I_LIST, reorderable=True)

    # ---- row actions --------------------------------------------------------

    def activate(self, row: Row, plain: bool = False) -> None:
        if row.table == "list_items":
            self.engine.call(self.engine.paste_list_item, row.key, plain)
        else:
            self.engine.call(self.engine.paste_item, row.table, row.key, plain)

    def row_action(self, action: str, row: Row) -> None:
        if action in ("save", "to_list"):
            clip = self.store.clip(row.table, row.key)
            if clip is None:
                self.hud.show("Não consegui abrir este item", "warn")
            elif action == "save":
                self.store.add_saved(clip)
                self.hud.show("Salvo no repertório", "info")
            else:
                self.store.add_list_item(self.engine.active_list, clip)
                self.hud.show(f"Adicionado a “{self.engine.list_name()}”", "info")
            self.refresh()
            self._sync_mode()
        elif action == "edit":
            self._edit(row)
        elif action == "delete":
            self._delete(row)
        elif action == "paste":
            self.activate(row)
        elif action == "paste_plain":
            self.activate(row, plain=True)
        elif action == "copy":
            self.engine.call(self.engine.copy_item, row.table, row.key)

    def confirm_delete(self, kind: str, message: str, on_yes) -> None:
        """kind is "one" or "many": each notice can be turned off on its own, so silencing
        the one for single items doesn't make the small buttons wipe a whole list unasked."""
        if self.store.get(f"confirm_{kind}") == "no":
            on_yes()
        else:
            open_delete_notice(self, kind, message, on_yes)

    def _delete(self, row: Row) -> None:
        def delete() -> None:
            if row.table == "list_items":
                self.engine.call(self.engine.delete_list_item, row.key)
                return
            if row.table == "history":
                self.store.delete_history(row.key)
            else:
                self.store.delete_saved(row.key)
            if row.kind == clips.TEXT:
                self.engine.call(self.engine.forget, {row.text})
            self.refresh()

        preview = row.title if len(row.title) <= 60 else row.title[:60].rstrip() + "…"
        self.confirm_delete("one", f"Apagar “{preview}”?", delete)

    def _forget_old_version(self, old: str, new: str) -> None:
        """An edit overwrites the old text for good, so it leaves the clipboard like a delete."""
        if old != new:
            self.engine.call(self.engine.forget, {old})

    def _edit(self, row: Row) -> None:
        if row.kind != clips.TEXT:
            return
        if row.table == "saved":
            item = next((i for i in self.store.saved() if i["id"] == row.key), None)
            name = item["title"] if item else ""

            def save(text: str, title: str) -> None:
                self.store.update_saved(row.key, text, title)
                self._forget_old_version(row.text, text)
                self.refresh()

            open_editor(self, "Editar item", row.text, save, name=name)
        elif row.table == "list_items":
            def save(text: str, _title) -> None:
                self.store.update_list_item(row.key, text)
                self._forget_old_version(row.text, text)
                self.refresh()

            open_editor(self, "Editar item da lista", row.text, save)

    def new_saved(self) -> None:
        def save(text: str, title: str) -> None:
            self.store.add_saved(text, title)
            self.refresh()

        open_editor(self, "Novo item do repertório", "", save, name="")

    def row_menu(self, row: Row, x: int, y: int) -> None:
        menu = self._menu()
        menu.add_command(label="Colar", command=lambda: self.activate(row))
        if row.kind == clips.TEXT:
            menu.add_command(label="Colar sem formatação", command=lambda: self.activate(row, plain=True))
        menu.add_command(label="Copiar", command=lambda: self.row_action("copy", row))
        menu.add_separator()
        if row.table == "history":
            menu.add_command(label="Salvar no repertório", command=lambda: self.row_action("save", row))
        if row.table != "list_items":
            menu.add_command(label=f"Adicionar a “{self.engine.list_name()}”", command=lambda: self.row_action("to_list", row))
        if row.table != "history" and row.kind == clips.TEXT:
            menu.add_command(label="Editar", command=lambda: self._edit(row))
        menu.add_separator()
        menu.add_command(label="Apagar de vez", command=lambda: self._delete(row))
        self._popup(menu, x, y)

    def reorder(self, row: Row, new_index: int) -> None:
        self.store.move_item(self.engine.active_list, row.key, new_index)
        self.refresh()

    def _menu(self) -> tk.Menu:
        return tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=TEXT, activebackground=SELECTED, activeforeground=TEXT,
                       bd=0, relief="flat", font=self.f_ui)

    @staticmethod
    def _popup(menu: tk.Menu, x: int, y: int) -> None:
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ---- keyboard -----------------------------------------------------------

    def _on_enter(self, _event) -> None:
        if self.store.locked:
            return
        row = self.rows.current()
        if row is None and self.rows.rows:
            row = self.rows.rows[0]
        if row is not None:
            self.activate(row)

    def _on_escape(self, _event) -> None:
        if self.search_var.get():
            self.search_var.set("")
        else:
            self.hide()

    def _on_delete_key(self, _event) -> None:
        if self.store.locked or (self.root.focus_get() is self.search and self.search_var.get()):
            return
        row = self.rows.current()
        if row is not None:
            self._delete(row)

    # ---- lists --------------------------------------------------------------

    def _toggle_mode(self, mode: str) -> None:
        self.engine.call(self.engine.set_mode, OFF if self.engine.mode == mode else mode)

    def _toggle_order(self) -> None:
        current = self.store.get_list(self.engine.active_list)
        if current is None:  # its name could not be decrypted
            return
        self.store.set_reversed(self.engine.active_list, not current["reversed"])
        self.refresh()
        self._sync_mode()

    def _list_menu(self) -> None:
        menu = self._menu()
        for row in self.store.lists():
            active = "● " if row["id"] == self.engine.active_list else "    "
            menu.add_command(label=f"{active}{row['name']}  ({row['size']})",
                             command=lambda list_id=row["id"]: self.engine.call(self.engine.set_active_list, list_id))
        menu.add_separator()
        menu.add_command(label="Nova lista…", command=self._new_list)
        self._popup(menu, self.list_btn.winfo_rootx(), self.list_btn.winfo_rooty() + self.list_btn.winfo_height())

    def _new_list(self) -> None:
        def create(name: str) -> None:
            self.engine.call(self.engine.set_active_list, self.store.create_list(name))

        ask_name(self, "Nova lista", f"Lista {len(self.store.lists()) + 1}", create)

    def _rename_list(self) -> None:
        def rename(name: str) -> None:
            self.store.rename_list(self.engine.active_list, name)
            self._sync_mode()

        ask_name(self, "Renomear lista", self.engine.list_name(), rename)

    def _delete_list(self) -> None:
        doomed = self.engine.active_list

        def delete() -> None:
            others = [row["id"] for row in self.store.lists() if row["id"] != doomed]
            next_id = others[0] if others else self.store.create_list("Lista 1")
            self.engine.call(self.engine.delete_list, doomed, next_id)

        self.confirm_delete("many", f"Apagar a lista “{self.engine.list_name()}” e os itens dela?", delete)

    def _clear_list(self) -> None:
        list_id = self.engine.active_list
        size = self.store.list_size(list_id)
        if size:
            self.confirm_delete("many", f"Apagar {count(size, 'item', 'itens')} da lista “{self.engine.list_name()}”?",
                                lambda: self.engine.call(self.engine.clear_list, list_id))

    def _clear_history(self) -> None:
        items = self.store.history()
        if not items:
            return
        texts = {item["text"] for item in items if item["kind"] == clips.TEXT}

        def delete() -> None:
            self.store.clear_history()
            self.engine.call(self.engine.forget, texts)
            self.refresh()

        self.confirm_delete("many", f"Apagar todo o histórico ({count(len(items), 'item', 'itens')})? "
                                    "O repertório e as listas continuam.", delete)

    def _toggle_pause(self) -> None:
        self.engine.call(self.engine.set_paused, not self.engine.paused)

    # ---- status -------------------------------------------------------------

    def _sync_mode(self) -> None:
        mode = self.engine.mode
        self.record_btn.set(kind="record_on" if mode == RECORD else "outline",
                            text="Gravando" if mode == RECORD else "Gravar")
        self.paste_btn.set(kind="paste_on" if mode == PASTE else "outline",
                           text="Colando" if mode == PASTE else "Colar")
        current = None if self.store.locked else self.store.get_list(self.engine.active_list)
        size = self.store.list_size(self.engine.active_list)
        if current:
            self.list_btn.set(text=f"{current['name']} · {size}")
            self.order_btn.set(icon=I_UP if current["reversed"] else I_DOWN,
                               text="Inversa" if current["reversed"] else "Em ordem")
        self.undo_btn.set(fg=TEXT if self.engine.undo else FAINT)
        self.pause_btn.set(icon=I_PLAY if self.engine.paused else I_PAUSE, fg=AMBER if self.engine.paused else DIM,
                           hint="Retomar captura" if self.engine.paused else "Pausar captura")
        self._sync_status()

    def _sync_status(self) -> None:
        if self._hint:
            text, color = self._hint, TEXT
        elif self.store.locked:
            text, color = "Travado: nada é guardado até destravar", AMBER
        elif self.warning:
            text, color = self.warning, AMBER
        elif self.engine.paused:
            text, color = "Captura pausada", AMBER
        elif self.engine.mode == RECORD:
            text, color = "Gravando: cada Ctrl+C entra na lista", RED
        elif self.engine.mode == PASTE:
            text, color = "Colando: cada Ctrl+V cola o próximo", GREEN
        else:
            quick = self.engine.hotkey_label("quick")
            text, color = (f"Capturando · {quick} cola rápido" if quick else "Capturando"), DIM
        self.status.configure(text=text, fg=color)
        self.status_dot.configure(image=self.paint.dot(FAINT if self._hint else (ACCENT if color == DIM else color), self.px(7)))

    def show_hint(self, text: str | None) -> None:
        self._hint = text
        self._sync_status()

    def run(self, hidden: bool = False) -> None:
        """hidden: opened with Windows, so it waits in the tray. Not while the privacy notice
        still has to be read, since nothing is captured until it is."""
        if not hidden or self.store.get("privacy_notice") != "hide" or self.store.locked:
            self.first_show()
        self.root.mainloop()


def _dialog(app: App, title: str, resizable: bool = False) -> tk.Toplevel:
    win = tk.Toplevel(app.root, bg=BG)
    win.withdraw()
    win.title(title)
    win.attributes("-topmost", True)
    win.transient(app.root)  # which also keeps it off the taskbar
    win.resizable(resizable, resizable)
    return win


def _open_dialog(app: App, win: tk.Toplevel, width: int, height: int | None, focus) -> None:
    """Without a height, the dialog is as tall as its contents need."""
    previous = win.grab_current()  # a dialog opened over another gives the grab back on closing
    if previous is not None:
        def give_back(event) -> None:
            if event.widget is win and previous.winfo_exists():
                previous.grab_set()
                previous.focus_force()

        win.bind("<Destroy>", give_back, add="+")
    if height is None:
        win.update_idletasks()
        height = win.winfo_reqheight()
    x = app.root.winfo_rootx() + (app.root.winfo_width() - width) // 2
    y = app.root.winfo_rooty() + app.px(40)
    left, top, right, bottom = win32.virtual_screen()
    margin = app.px(24)  # the window's frame is drawn outside width and height
    x = max(left, min(x, right - width - margin))
    y = max(top, min(y, bottom - height - app.px(60)))
    win.geometry(f"{width}x{height}+{x}+{y}")
    win.deiconify()
    win.update()
    app._style_titlebar(win)
    win.lift()
    win.grab_set()
    focus.focus_force()


def _footer(app: App, win: tk.Toplevel, on_save, save_label: str = "Salvar", danger: bool = False,
            cancel: bool = True) -> None:
    footer = tk.Frame(win, bg=BG)
    footer.pack(side="bottom", fill="x", padx=app.px(20), pady=(app.px(16), app.px(18)))
    Btn(footer, app, on_save, text=save_label, kind="danger" if danger else "primary").pack(side="right")
    if cancel:
        Btn(footer, app, win.destroy, text="Cancelar", kind="outline").pack(side="right", padx=(0, app.px(8)))


def _checkbox(app: App, parent, text: str, checked: bool, wrap: int) -> tk.BooleanVar:
    var = tk.BooleanVar(parent, value=checked)
    size = app.px(16)
    tk.Checkbutton(parent, text="  " + text, variable=var, indicatoron=False, compound="left",
                   image=app.paint.checkbox(False, size, ACCENT, ON_ACCENT, CHECK_LINE),
                   selectimage=app.paint.checkbox(True, size, ACCENT, ON_ACCENT, CHECK_LINE),
                   bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT, selectcolor=BG, font=app.f_ui,
                   anchor="w", justify="left", bd=0, relief="flat", offrelief="flat", overrelief="flat",
                   highlightthickness=0, cursor="hand2", wraplength=wrap).pack(
        fill="x", padx=app.px(20), pady=(app.px(6), 0))
    return var


def _text(app: App, parent, text: str, wrap: int, color: str | None = None, font=None, top: int = 8) -> tk.Label:
    label = tk.Label(parent, text=text, bg=BG, fg=color or TEXT, font=font or app.f_ui, anchor="w", justify="left",
                     wraplength=wrap)
    label.pack(fill="x", padx=app.px(20), pady=(app.px(top), 0))
    return label


def _bullet(app: App, parent, text: str, wrap: int) -> None:
    row = tk.Frame(parent, bg=BG)
    row.pack(fill="x", padx=app.px(20), pady=(app.px(10), 0))
    tk.Label(row, image=app.paint.dot(ACCENT, app.px(6)), bg=BG).pack(side="left", anchor="n", pady=(app.px(7), 0))
    tk.Label(row, text=text, bg=BG, fg=TEXT, font=app.f_ui, anchor="w", justify="left",
             wraplength=wrap - app.px(16)).pack(side="left", fill="x", padx=(app.px(10), 0))


def _choice(app: App, parent, options: tuple, value: str) -> list:
    """Pills in a rounded box, the chosen one lit like a tab. Returns a one-item list that
    holds the chosen key as the user clicks."""
    chosen = [value if value in dict(options) else options[0][0]]
    box = RoundBox(parent, app, pad_x=3, pad_y=3, radius=10)
    row = tk.Frame(box, bg=SURFACE)
    buttons = {}

    def pick(key: str) -> None:
        chosen[0] = key
        for other, button in buttons.items():
            button.set(kind="tab_on" if other == key else "tab_off")

    for key, label in options:
        buttons[key] = Btn(row, app, lambda k=key: pick(k), text=label, kind="tab_off")
        buttons[key].pack(side="left")
    pick(chosen[0])
    box.hold(row)
    row.update_idletasks()
    box.configure(width=row.winfo_reqwidth() + 2 * box.pad_x, height=row.winfo_reqheight() + 2 * box.pad_y)
    box.pack(anchor="w", padx=app.px(20), pady=(app.px(8), 0))
    return chosen


def _rule(app: App, parent, top: int = 16) -> None:
    tk.Frame(parent, bg=LINE, height=1).pack(fill="x", padx=app.px(20), pady=(app.px(top), 0))


def _field(app: App, parent, value: str = "", width: int | None = None, justify: str = "left") -> tuple[RoundBox, tk.Entry]:
    box = RoundBox(parent, app, width=width)
    entry = tk.Entry(box, bg=SURFACE, fg=TEXT, insertbackground=TEXT, bd=0, relief="flat", font=app.f_ui,
                     highlightthickness=0, justify=justify, selectbackground=SELECT_TEXT, selectforeground=TEXT)
    entry.insert(0, value)
    box.hold(entry)
    return box, entry


def open_privacy_notice(app: App, on_close) -> None:
    """on_close(True) for "Entendi" (or Enter), on_close(False) when closed any other way."""
    width = app.px(440)
    wrap = width - app.px(40)
    days, max_items = app.store.history_limits()
    win = _dialog(app, "Sua privacidade")

    def finish(accepted: bool) -> None:
        if accepted and hide.get():
            app.store.set("privacy_notice", "hide")
        win.destroy()
        on_close(accepted)

    _footer(app, win, lambda: finish(True), "Entendi", cancel=False)
    _text(app, win, "Antes de usar, um cuidado com a sua privacidade", wrap, font=app.f_title, top=18)
    _text(app, win, f"O {APP_NAME} guarda o que você copia para você colar depois. Isso fica criptografado neste "
                    "computador e só abre na sua conta do Windows.", wrap, DIM)
    for line in (
            "Evite copiar senhas, dados de cartão e documentos. Se precisar, pause a captura antes.",
            "Nenhum app consegue bloquear tudo: um programa malicioso aberto no PC pode ler o que você copia na "
            "hora da cópia e abrir o que está guardado nesta conta.",
            f"O histórico se apaga sozinho depois de {count(days, 'dia', 'dias')} ou {count(max_items, 'item', 'itens')}. "
            "O repertório e as listas ficam até você apagar. Dá para mudar os limites nas configurações."):
        _bullet(app, win, line, wrap)
    _rule(app, win)
    _text(app, win, "A captura só começa depois que você clicar em Entendi. Fechar o aviso de outro jeito deixa "
                    "a captura pausada.", wrap, DIM, app.f_small, top=12)
    hide = _checkbox(app, win, "Não mostrar este aviso de novo", False, wrap)
    win.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    win.bind("<Return>", lambda _e: finish(True))
    win.bind("<Escape>", lambda _e: finish(False))
    _open_dialog(app, win, width, None, win)


def open_delete_notice(app: App, kind: str, message: str, on_yes, silenceable: bool = True,
                       detail: str = "Não dá para recuperar depois.") -> None:
    width = app.px(380)
    wrap = width - app.px(40)
    win = _dialog(app, "Apagar de vez")

    def confirm() -> None:
        if stop is not None and stop.get():
            app.store.set(f"confirm_{kind}", "no")
        win.destroy()
        on_yes()

    _footer(app, win, confirm, "Apagar de vez", danger=True)
    _text(app, win, message, wrap, font=app.f_tab, top=18)
    _text(app, win, detail, wrap, DIM, top=6)
    tk.Frame(win, bg=BG, height=app.px(6)).pack()
    stop = _checkbox(app, win, "Não perguntar de novo", False, wrap) if silenceable else None
    win.bind("<Return>", lambda _e: confirm())
    win.bind("<Escape>", lambda _e: win.destroy())
    _open_dialog(app, win, width, None, win)


def hotkey_problem(mods: int, vk: int) -> str | None:
    """Why a shortcut can't be used, in words for the user, or None if it can."""
    function_key = 0x70 <= vk <= 0x87
    if not mods and not function_key:
        return "Use junto com Ctrl, Alt, Shift ou Win, ou escolha uma tecla F."
    if mods == win32.MOD_SHIFT and not function_key:
        return "Só Shift não basta: seria o mesmo que escrever uma letra maiúscula."
    if mods == win32.MOD_CONTROL and vk in (0x41, 0x43, 0x56, 0x58, 0x59, 0x5A):
        return "Esse atalho é usado por quase todo programa (copiar, colar, desfazer…)."
    if mods == win32.MOD_ALT and vk in (0x09, 0x73):
        return "Esse atalho é do próprio Windows."
    return None


SETTINGS_PAGES = (("general", "Geral"), ("keys", "Atalhos"), ("privacy", "Privacidade"), ("security", "Segurança"))


def _program_names(text: str) -> list[str]:
    """What the user typed in the ignored programs box: one file name per line, ".exe" added
    when missing, in lower case, without repeats."""
    names = []
    for line in text.splitlines():
        name = line.strip().strip('"').rsplit("\\", 1)[-1].lower()
        if name and not name.endswith(".exe"):
            name += ".exe"
        if name and name not in names:
            names.append(name)
    return names


def _append_line(text_box: tk.Text, line: str) -> None:
    current = text_box.get("1.0", "end-1c")
    text_box.insert("end", ("\n" if current and not current.endswith("\n") else "") + line)


def open_settings(app: App, page: str = "general") -> tk.Toplevel:
    """Pages behind pills at the top. Salvar applies all of them at once; Cancelar none."""
    store, engine = app.store, app.engine
    width = app.px(460)
    wrap = width - app.px(40)
    days, max_items = store.history_limits()
    keys = dict(engine.hotkeys)  # edited here, applied on Salvar
    capturing = [None]  # the shortcut waiting for a new combination
    win = _dialog(app, "Configurações")

    def show_page(key: str) -> None:
        for name, frame in pages.items():
            frame.pack_forget()
            pills[name].set(kind="tab_on" if name == key else "tab_off")
        pages[key].pack(fill="both", expand=True)

    # ---- shortcuts ----------------------------------------------------------

    def combo_text(name: str) -> str:
        return win32.hotkey_text(*keys[name]) if keys[name] else "Nenhum"

    def start_capture(name: str) -> None:
        if capturing[0]:
            stop_capture()
        capturing[0] = name
        key_buttons[name].set(text="Aperte o atalho…", kind="tab_on")
        engine.call(engine.suspend_hotkeys, True)
        error.configure(text="")
        win.focus_force()

    def stop_capture() -> None:
        name, capturing[0] = capturing[0], None
        if name:
            key_buttons[name].set(text=combo_text(name), kind="outline")
            engine.call(engine.suspend_hotkeys, False)

    def on_key(event):
        name = capturing[0]
        if not name:
            return None
        vk = event.keycode  # on Windows, Tk's keycode is the virtual key
        if vk in win32.MODIFIER_KEYS:
            return "break"
        mods = win32.held_modifiers()
        if vk == 0x1B and not mods:  # Esc
            stop_capture()
        elif vk in (0x08, 0x2E) and not mods:  # Backspace or Delete: turn it off
            keys[name] = None
            stop_capture()
        elif problem := hotkey_problem(mods, vk):
            error.configure(text=problem)
        else:
            keys[name] = (mods, vk)
            stop_capture()
        return "break"

    def restore_defaults() -> None:
        stop_capture()
        for name, _what, default in HOTKEYS:
            keys[name] = default
            key_buttons[name].set(text=combo_text(name))

    # ---- saving -------------------------------------------------------------

    def fail(page_key: str, message: str) -> None:
        show_page(page_key)
        error.configure(text=message)

    def save() -> None:
        stop_capture()
        try:
            new_days, new_max = int(days_entry.get().strip()), int(max_entry.get().strip())
        except ValueError:
            fail("privacy", "Use só números inteiros nos limites do histórico.")
            return
        if not 1 <= new_days <= MAX_DAYS:
            fail("privacy", f"Os dias precisam ficar entre 1 e {MAX_DAYS}.")
            return
        if not 1 <= new_max <= MAX_ITEMS:
            fail("privacy", f"Os itens precisam ficar entre 1 e {MAX_ITEMS}.")
            return
        combos = [(name, combo) for name, combo in keys.items() if combo]
        for index, (name, combo) in enumerate(combos):
            twin = next((other for other, again in combos[index + 1:] if again == combo), None)
            if twin:
                what = dict((n, w) for n, w, _d in HOTKEYS)
                fail("keys", f"“{what[name]}” e “{what[twin]}” estão com o mesmo atalho.")
                return
        if start_with_windows.get() != autostart.is_enabled():
            try:
                autostart.set_enabled(start_with_windows.get())
            except OSError:
                fail("general", "O Windows não deixou mudar a abertura automática.")
                return

        def apply() -> None:  # all or nothing: canceling the deletion below saves none of it
            store.set("privacy_notice", "show" if show_privacy.get() else "hide")
            store.set("confirm_one", "yes" if ask_one.get() else "no")
            store.set("confirm_many", "yes" if ask_many.get() else "no")
            store.set("keep_formatting", "1" if keep_formatting.get() else "0")
            if lock_with_windows is not None:
                store.set("lock_with_windows", "1" if lock_with_windows.get() else "0")
            store.set("ignore_apps_on", "1" if ignore_on.get() else "0")
            store.set_secret("ignore_apps", "\n".join(_program_names(ignore_text.get("1.0", "end"))))
            store.set_history_limits(new_days, new_max)
            expiring = store.expiring_history()
            removed = store.prune_history()
            if expiring:
                engine.call(engine.forget, expiring)
            if keys != engine.hotkeys:
                engine.call(engine.set_hotkeys, dict(keys))
            store.set("theme", theme[0])
            win.destroy()
            app.apply_theme()
            app.refresh()
            app.hud.show(f"Configurações salvas · {count(removed, 'item apagado', 'itens apagados')}"
                         if removed else "Configurações salvas", "info")

        doomed = store.count_beyond_limits(new_days, new_max)
        if doomed:
            app.confirm_delete("many", f"Com esses limites, {count(doomed, 'item', 'itens')} do histórico "
                                       f"{'será apagado' if doomed == 1 else 'serão apagados'} agora.", apply)
        else:
            apply()

    _footer(app, win, save)
    error = tk.Label(win, text="", bg=BG, fg=RED, font=app.f_small, anchor="w", justify="left", wraplength=wrap)
    error.pack(side="bottom", fill="x", padx=app.px(20))

    nav = tk.Frame(win, bg=BG)
    nav.pack(fill="x", padx=app.px(16), pady=(app.px(14), 0))
    pills = {}
    for key, label in SETTINGS_PAGES:
        pills[key] = Btn(nav, app, lambda k=key: show_page(k), text=label, kind="tab_off", font=app.f_tab)
        pills[key].pack(side="left", padx=(0, app.px(2)))
    holder = tk.Frame(win, bg=BG)
    holder.pack(fill="both", expand=True)
    pages = {key: tk.Frame(holder, bg=BG) for key, _label in SETTINGS_PAGES}

    general = pages["general"]
    _text(app, general, "Aparência", wrap, font=app.f_tab, top=12)
    theme = _choice(app, general, THEMES, store.get("theme", "auto"))
    _text(app, general, "Automático segue o modo claro ou escuro do Windows.", wrap, DIM, app.f_small, top=8)
    _rule(app, general)
    _text(app, general, "Início", wrap, font=app.f_tab, top=14)
    start_with_windows = _checkbox(app, general, "Abrir com o Windows", autostart.is_enabled(), wrap)
    _text(app, general, "Abre na bandeja, sem mostrar a janela, para a captura funcionar desde que você entra.",
          wrap, DIM, app.f_small, top=6)
    _rule(app, general)
    _text(app, general, "Colagem", wrap, font=app.f_tab, top=14)
    keep_formatting = _checkbox(app, general, "Colar com a formatação original", store.get("keep_formatting") != "0",
                                wrap)
    _text(app, general, "Negrito, links e tabelas vão junto. Para uma vez só, sem formatação: botão direito no item, "
                        "ou Shift+Enter no colar rápido.", wrap, DIM, app.f_small, top=6)
    _rule(app, general)
    _text(app, general, "Avisos", wrap, font=app.f_tab, top=14)
    show_privacy = _checkbox(app, general, "Mostrar o aviso de privacidade ao abrir",
                             store.get("privacy_notice") != "hide", wrap)
    ask_one = _checkbox(app, general, "Confirmar antes de apagar um item", store.get("confirm_one") != "no", wrap)
    ask_many = _checkbox(app, general, "Confirmar antes de apagar vários itens", store.get("confirm_many") != "no", wrap)

    shortcuts = pages["keys"]
    _text(app, shortcuts, "Clique num atalho e aperte a combinação nova. Esc cancela, Backspace desliga o atalho.",
          wrap, DIM, app.f_small, top=12)
    key_buttons = {}
    for name, what, _default in HOTKEYS:
        row = tk.Frame(shortcuts, bg=BG)
        row.pack(fill="x", padx=app.px(20), pady=(app.px(10), 0))
        tk.Label(row, text=what, bg=BG, fg=TEXT, font=app.f_ui, anchor="w").pack(side="left")
        key_buttons[name] = Btn(row, app, lambda n=name: start_capture(n), text=combo_text(name), kind="outline")
        key_buttons[name].pack(side="right")
    Btn(shortcuts, app, restore_defaults, text="Restaurar os padrões", kind="ghost").pack(
        anchor="w", padx=app.px(14), pady=(app.px(12), 0))
    _text(app, shortcuts, "No modo Colar da lista, o Ctrl+V continua sendo o Ctrl+V.", wrap, DIM, app.f_small, top=8)

    privacy = pages["privacy"]

    def number_row(before: str, value: int, after: str) -> tk.Entry:
        row = tk.Frame(privacy, bg=BG)
        row.pack(fill="x", padx=app.px(20), pady=(app.px(8), 0))
        tk.Label(row, text=before, bg=BG, fg=TEXT, font=app.f_ui).pack(side="left")
        box, entry = _field(app, row, str(value), width=app.px(64), justify="center")
        box.pack(side="left", padx=app.px(8))
        tk.Label(row, text=after, bg=BG, fg=TEXT, font=app.f_ui).pack(side="left")
        return entry

    _text(app, privacy, "Histórico", wrap, font=app.f_tab, top=12)
    days_entry = number_row("Apagar o que tiver mais de", days, "dias")
    max_entry = number_row("Guardar no máximo", max_items, "itens")
    _text(app, privacy, "Passou do limite, é apagado de vez. O repertório e as listas não são afetados.",
          wrap, DIM, app.f_small, top=8)
    _rule(app, privacy)
    _text(app, privacy, "Programas ignorados", wrap, font=app.f_tab, top=14)
    ignore_on = _checkbox(app, privacy, "Não guardar o que for copiado destes programas",
                          store.get("ignore_apps_on") == "1", wrap)
    names_box = RoundBox(privacy, app, pad_y=6)
    ignore_text = tk.Text(names_box, bg=SURFACE, fg=TEXT, insertbackground=TEXT, bd=0, relief="flat", font=app.f_ui,
                          wrap="none", height=4, highlightthickness=0, padx=0, pady=0, selectbackground=SELECT_TEXT,
                          selectforeground=TEXT)
    ignore_text.insert("1.0", store.get_secret("ignore_apps"))
    names_box.hold(ignore_text)
    names_box.configure(height=app.f_ui.metrics("linespace") * 4 + app.px(14))
    names_box.pack(fill="x", padx=app.px(20), pady=(app.px(8), 0))

    def add_open_program() -> None:
        menu = app._menu()
        current = set(_program_names(ignore_text.get("1.0", "end")))
        for name in win32.open_programs():
            if name not in current:
                menu.add_command(label=name, command=lambda n=name: _append_line(ignore_text, n))
        if menu.index("end") is None:  # nothing added
            menu.add_command(label="Nenhum outro programa aberto", state="disabled")
        App._popup(menu, adder.winfo_rootx(), adder.winfo_rooty() + adder.winfo_height())

    adder = Btn(privacy, app, add_open_program, icon=I_ADD, text="Adicionar programa aberto", kind="ghost")
    adder.pack(anchor="w", padx=app.px(14), pady=(app.px(6), 0))
    _text(app, privacy, "Um por linha, pelo nome do arquivo (por exemplo, banco.exe). Desligado, a lista fica "
                        "guardada mas não vale.", wrap, DIM, app.f_small, top=4)

    security = pages["security"]
    _text(app, security, "Criptografia", wrap, font=app.f_tab, top=12)
    _text(app, security, "O que fica salvo é criptografado e só abre na sua conta do Windows. O que você apaga é "
                         "sobrescrito no arquivo.", wrap, DIM, app.f_small, top=6)
    _rule(app, security)
    _text(app, security, "Senha", wrap, font=app.f_tab, top=14)
    lock_with_windows = None

    def reopen_on_security() -> None:
        win.destroy()
        app.open_settings("security")

    if store.has_password:
        _text(app, security, f"O que está guardado só abre com a senha. O {APP_NAME} pede a senha ao abrir, e "
                             "enquanto estiver travado nada é guardado.", wrap, DIM, app.f_small, top=6)
        lock_with_windows = _checkbox(app, security, "Travar quando o Windows bloquear",
                                      store.get("lock_with_windows") != "0", wrap)
        buttons = tk.Frame(security, bg=BG)
        buttons.pack(fill="x", padx=app.px(20), pady=(app.px(12), 0))
        Btn(buttons, app, lambda: change_password(app, reopen_on_security), text="Trocar a senha…",
            kind="outline").pack(side="left")
        Btn(buttons, app, lambda: remove_password(app, reopen_on_security), text="Tirar a senha…",
            kind="outline").pack(side="left", padx=(app.px(8), 0))
        Btn(buttons, app, lambda: engine.call(engine.lock), text="Travar agora", kind="ghost").pack(side="right")
    else:
        _text(app, security, "Com uma senha, o que está guardado só abre depois de digitá-la, mesmo para um programa "
                             "aberto na sua conta do Windows. Se você esquecer a senha, não há como recuperar o que "
                             "está guardado.", wrap, DIM, app.f_small, top=6)
        Btn(security, app, lambda: create_password(app, reopen_on_security), text="Criar senha…", kind="outline").pack(
            anchor="w", padx=app.px(20), pady=(app.px(12), 0))

    # Every page as tall as the tallest, so switching doesn't make the window jump.
    win.update_idletasks()
    holder.configure(height=max(frame.winfo_reqheight() for frame in pages.values()) + app.px(8))
    holder.pack_propagate(False)
    show_page(page if page in pages else "general")

    win.bind("<KeyPress>", on_key)
    win.bind("<Return>", lambda event: on_key(event) if capturing[0] else save())
    win.bind("<Escape>", lambda event: on_key(event) if capturing[0] else win.destroy())
    win.bind("<Destroy>", lambda event: event.widget is win and capturing[0] and engine.call(engine.suspend_hotkeys, False),
             add="+")
    _open_dialog(app, win, width, None, win)
    return win


def ask_passwords(app: App, title: str, intro: str, labels: tuple, on_ok, button: str = "OK") -> None:
    """Password fields; on_ok(*values) returns an error to show, or None to close."""
    width = app.px(380)
    wrap = width - app.px(40)
    win = _dialog(app, title)
    entries = []

    def save() -> None:
        problem = on_ok(*(entry.get() for entry in entries))
        if problem:
            error.configure(text=problem)
            for entry in entries:
                entry.delete(0, "end")
            entries[0].focus_set()
        else:
            win.destroy()

    _footer(app, win, save, button)
    error = tk.Label(win, text="", bg=BG, fg=RED, font=app.f_small, anchor="w", justify="left", wraplength=wrap)
    error.pack(side="bottom", fill="x", padx=app.px(20))
    _text(app, win, intro, wrap, DIM, app.f_small, top=16)
    for label in labels:
        _text(app, win, label, wrap, DIM, app.f_small, top=12)
        box, entry = _field(app, win)
        entry.configure(show="•")
        box.pack(fill="x", padx=app.px(20), pady=(app.px(4), 0))
        entries.append(entry)
    win.bind("<Return>", lambda _e: save())
    win.bind("<Escape>", lambda _e: win.destroy())
    _open_dialog(app, win, width, None, entries[0])


def _new_password_problem(new: str, again: str) -> str | None:
    if len(new) < MIN_PASSWORD:
        return f"Use pelo menos {MIN_PASSWORD} caracteres."
    if new != again:
        return "As duas senhas não são iguais."
    return None


def create_password(app: App, done) -> None:
    def apply(new: str, again: str) -> str | None:
        problem = _new_password_problem(new, again)
        if problem:
            return problem
        app.store.set_password(new)
        app.hud.show("Senha criada", "info")
        done()
        return None

    ask_passwords(app, "Criar senha", "Guarde bem esta senha: se você esquecer, não há como recuperar o que está "
                                      "guardado. Só dá para apagar tudo e começar de novo.",
                  ("Senha", "Repita a senha"), apply, "Criar senha")


def change_password(app: App, done) -> None:
    def apply(current: str, new: str, again: str) -> str | None:
        if not app.store.check_password(current):
            return "A senha atual não confere."
        problem = _new_password_problem(new, again)
        if problem:
            return problem
        app.store.set_password(new)
        app.hud.show("Senha trocada", "info")
        done()
        return None

    ask_passwords(app, "Trocar a senha", "A senha nova vale a partir de agora.",
                  ("Senha atual", "Senha nova", "Repita a senha nova"), apply, "Trocar")


def remove_password(app: App, done) -> None:
    def apply(current: str) -> str | None:
        if not app.store.check_password(current):
            return "A senha não confere."
        app.store.remove_password()
        app.hud.show("Senha tirada: só a sua conta do Windows protege os dados agora", "info")
        done()
        return None

    ask_passwords(app, "Tirar a senha", "Sem a senha, o que está guardado volta a abrir só com a sua conta do "
                                        "Windows, como antes.", ("Senha atual",), apply, "Tirar a senha")


def open_editor(app: App, title: str, text: str, on_save, name: str | None = None) -> None:
    win = _dialog(app, title, resizable=True)
    name_entry = None

    def save() -> None:
        body = text_box.get("1.0", "end-1c")
        if not body.strip():
            return
        title_value = name_entry.get().strip() if name_entry else ""
        win.destroy()
        on_save(body, title_value)

    _footer(app, win, save)
    if name is not None:
        _text(app, win, "Título (opcional)", app.px(360), DIM, app.f_small, top=14)
        name_box, name_entry = _field(app, win, name)
        name_box.pack(fill="x", padx=app.px(20), pady=(app.px(4), 0))
        _text(app, win, "Texto", app.px(360), DIM, app.f_small, top=12)
    box = RoundBox(win, app, pad_y=8)
    text_box = tk.Text(box, bg=SURFACE, fg=TEXT, insertbackground=TEXT, bd=0, relief="flat", font=app.f_ui, wrap="word",
                       highlightthickness=0, padx=0, pady=0, undo=True, selectbackground=SELECT_TEXT,
                       selectforeground=TEXT)
    text_box.insert("1.0", text)
    box.hold(text_box)
    box.pack(fill="both", expand=True, padx=app.px(20), pady=(app.px(4 if name is not None else 18), 0))
    # On the text box too, returning "break": otherwise Tk's own Return binding adds a line first.
    text_box.bind("<Control-Return>", lambda _e: (save(), "break")[1])
    win.bind("<Control-Return>", lambda _e: save())
    win.bind("<Escape>", lambda _e: win.destroy())
    _open_dialog(app, win, app.px(420), app.px(360), text_box)


def ask_name(app: App, title: str, value: str, on_ok) -> None:
    win = _dialog(app, title)

    def save() -> None:
        name = entry.get().strip()
        if name:
            win.destroy()
            on_ok(name)

    _footer(app, win, save, "OK")
    _text(app, win, "Nome", app.px(300), DIM, app.f_small, top=14)
    box, entry = _field(app, win, value)
    box.pack(fill="x", padx=app.px(20), pady=(app.px(4), 0))
    entry.select_range(0, "end")
    win.bind("<Return>", lambda _e: save())
    win.bind("<Escape>", lambda _e: win.destroy())
    _open_dialog(app, win, app.px(340), None, entry)


def _setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(data_dir() / "clipapp.log", maxBytes=500_000, backupCount=1,
                                                   encoding="utf-8")
    level = logging.DEBUG if os.environ.get("CLIPAPP_DEBUG") else logging.INFO
    logging.basicConfig(level=level, handlers=[handler], format="%(created).3f %(levelname)s %(name)s: %(message)s")
    sys.excepthook = lambda *exc: log.critical("unhandled exception", exc_info=exc)


RUNNING_MUTEX = f"Local\\{APP_NAME}.Running"  # one fixed name the installer checks before updating files


def main() -> None:
    _setup_logging()
    win32.set_dpi_aware()
    win32.set_app_id(f"{APP_NAME}.Desktop")
    # Named after the data folder, so a test copy can run next to the real one.
    mutex_name = "Local\\ClipApp." + str(data_dir()).replace("\\", "_").replace(":", "")
    mutex = win32.kernel32.CreateMutexW(None, True, mutex_name)
    if ctypes.get_last_error() == win32.ERROR_ALREADY_EXISTS:
        signal_running_instance()
        return
    running = win32.kernel32.CreateMutexW(None, False, RUNNING_MUTEX)  # kept until the app exits
    path = data_dir() / "clips.db"
    try:
        store = Store(path)
    except LockedError:
        log.exception("the saved key does not open on this Windows account, or is damaged")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, "Não consegui abrir seus dados: a chave só funciona na conta do Windows em que "
                                       "eles foram criados, ou foi danificada.\n\n"
                                       f"Se não precisar mais deles, apague {path} para começar do zero.")
        return
    events: queue.SimpleQueue = queue.SimpleQueue()
    engine = Engine(store, events.put)
    engine.start()
    if not engine.ready.wait(5):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, f"Não consegui iniciar a captura. Detalhes em {data_dir() / 'clipapp.log'}")
        store.close()
        return
    autostart.refresh()
    app = App(store, engine, events)
    tk.Tk.report_callback_exception = lambda _self, *exc: log.error("window callback failed", exc_info=exc)
    try:
        app.run(hidden=autostart.TRAY_FLAG in sys.argv[1:])
    finally:
        store.close()
        del mutex, running
