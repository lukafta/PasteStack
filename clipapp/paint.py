"""Smooth rounded shapes for the window.

Tk's canvas draws jagged curves, so anything round is a small anti-aliased image made
here: the corners of a rectangle, a dot, a checkbox. Straight edges stay plain canvas
rectangles, which is what lets one set of corners serve a shape of any size.
"""

from __future__ import annotations

import tkinter as tk

from .win32 import _over, _rounded_rect, png_from_rgba


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _stroke(x: float, y: float, start: tuple, end: tuple, half_width: float) -> float:
    """Coverage of a pixel by a straight stroke with round ends."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    t = max(0.0, min(1.0, ((x - start[0]) * dx + (y - start[1]) * dy) / (dx * dx + dy * dy)))
    distance = ((x - start[0] - t * dx) ** 2 + (y - start[1] - t * dy) ** 2) ** 0.5
    return max(0.0, min(1.0, 0.5 - (distance - half_width)))


class Painter:
    def __init__(self, root: tk.Misc):
        self._root = root
        self._images: dict = {}  # kept: Tk drops an image nothing refers to

    def _image(self, key: tuple, width: int, height: int, shade) -> tk.PhotoImage:
        """shade(x, y) gives a pixel's (r, g, b, alpha 0..1)."""
        image = self._images.get(key)
        if image is None:
            rgba = bytearray(width * height * 4)
            for py in range(height):
                for px in range(width):
                    r, g, b, a = shade(px + 0.5, py + 0.5)
                    i = (py * width + px) * 4
                    rgba[i:i + 4] = bytes((round(r), round(g), round(b), round(a * 255)))
            image = tk.PhotoImage(master=self._root, data=png_from_rgba(bytes(rgba), width, height), format="png")
            self._images[key] = image
        return image

    def _corner(self, radius: int, fill: str | None, border: str | None, which: str) -> tk.PhotoImage:
        fill_rgb = _rgb(fill) if fill else None
        border_rgb = _rgb(border) if border else None
        far = radius * 4 + 4  # a shape big enough that only this corner's curve matters

        def shade(x: float, y: float) -> tuple:
            mx = x if which[1] == "w" else radius - x
            my = y if which[0] == "n" else radius - y
            outer = _rounded_rect(mx, my, 0, 0, far, far, radius, 1)
            if not border_rgb:
                return _over((0.0, 0.0, 0.0, 0.0), fill_rgb, outer)
            inner = _rounded_rect(mx, my, 1, 1, far, far, max(radius - 1, 0), 1)
            if not fill_rgb:
                return (*border_rgb, max(0.0, outer - inner))
            return _over(_over((0.0, 0.0, 0.0, 0.0), border_rgb, outer), fill_rgb, inner)

        return self._image(("corner", radius, fill, border, which), radius, radius, shade)

    def rect(self, canvas: tk.Canvas, x0: int, y0: int, x1: int, y1: int, radius: int,
             fill: str | None = None, border: str | None = None, tags: str = "") -> None:
        """A rounded rectangle from (x0, y0) up to, not including, (x1, y1). The border is one pixel."""
        if not fill and not border:
            return
        r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
        if fill:
            canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline="", tags=tags)
            canvas.create_rectangle(x0, y0 + r, x0 + r, y1 - r, fill=fill, outline="", tags=tags)
            canvas.create_rectangle(x1 - r, y0 + r, x1, y1 - r, fill=fill, outline="", tags=tags)
        if border:
            canvas.create_rectangle(x0 + r, y0, x1 - r, y0 + 1, fill=border, outline="", tags=tags)
            canvas.create_rectangle(x0 + r, y1 - 1, x1 - r, y1, fill=border, outline="", tags=tags)
            canvas.create_rectangle(x0, y0 + r, x0 + 1, y1 - r, fill=border, outline="", tags=tags)
            canvas.create_rectangle(x1 - 1, y0 + r, x1, y1 - r, fill=border, outline="", tags=tags)
        if r:
            for which, x, y in (("nw", x0, y0), ("ne", x1 - r, y0), ("sw", x0, y1 - r), ("se", x1 - r, y1 - r)):
                canvas.create_image(x, y, image=self._corner(r, fill, border, which), anchor="nw", tags=tags)

    def dot(self, color: str, size: int) -> tk.PhotoImage:
        rgb = _rgb(color)
        return self._image(("dot", color, size), size, size,
                           lambda x, y: (*rgb, _rounded_rect(x, y, 0, 0, size, size, size / 2, 1)))

    def checkbox(self, checked: bool, size: int, box: str, mark: str, line: str) -> tk.PhotoImage:
        """An empty rounded box with a `line` border, or one filled with `box` and a `mark` tick."""
        box_rgb, mark_rgb, line_rgb = _rgb(box), _rgb(mark), _rgb(line)
        radius = size * 0.26
        start, bend, end = (size * 0.25, size * 0.52), (size * 0.43, size * 0.70), (size * 0.75, size * 0.33)
        half_width = size * 0.07

        def shade(x: float, y: float) -> tuple:
            outer = _rounded_rect(x, y, 0, 0, size, size, radius, 1)
            if not checked:
                inner = _rounded_rect(x, y, 1, 1, size - 1, size - 1, radius - 1, 1)
                return (*line_rgb, max(0.0, outer - inner))
            tick = max(_stroke(x, y, start, bend, half_width), _stroke(x, y, bend, end, half_width))
            return _over(_over((0.0, 0.0, 0.0, 0.0), box_rgb, outer), mark_rgb, tick)

        return self._image(("checkbox", checked, size, box, mark, line), size, size, shade)
