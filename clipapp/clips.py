"""What one copy holds: text (with its formatting, when the program gave any), an image,
or files. Plus the conversions that need no Windows calls: packing the extra formats for
the database, the image's thumbnail, and the file list's clipboard layout."""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field

TEXT, IMAGE, FILES = "text", "image", "files"
THUMB_SIZE = (96, 64)  # the most a thumbnail takes, in pixels


@dataclass
class Clip:
    kind: str
    # The text itself; for an image, a description ("Imagem 1920 × 1080"); for files, their
    # paths one per line. It is what gets searched.
    text: str
    # "html" and "rtf" next to a text, "dib" for an image: the bytes Windows had.
    formats: dict = field(default_factory=dict)
    thumb: bytes | None = None  # a small PNG of an image

    @classmethod
    def plain(cls, text: str) -> "Clip":
        return cls(TEXT, text)

    @property
    def paths(self) -> list[str]:
        return self.text.split("\n") if self.kind == FILES else []

    def identity(self) -> str:
        """What makes two copies the same one. Text alone for text, so the same words copied
        from two programs (one with formatting, one without) stay a single history item."""
        if self.kind == TEXT:
            return self.text
        digest = hashlib.sha256(self.formats.get("dib", b"")).hexdigest() if self.kind == IMAGE else ""
        return f"\0{self.kind}\0{self.text}\0{digest}"


def pack(formats: dict) -> bytes:
    """The extra formats as one compressed blob: name, size and bytes for each."""
    parts = []
    for name, data in formats.items():
        label = name.encode("ascii")
        parts.append(struct.pack("<B", len(label)) + label + struct.pack("<I", len(data)) + data)
    return zlib.compress(b"".join(parts), 6)


def unpack(blob: bytes) -> dict:
    data, formats, at = zlib.decompress(blob), {}, 0
    while at < len(data):
        size = data[at]
        name = data[at + 1:at + 1 + size].decode("ascii")
        at += 1 + size
        (length,) = struct.unpack_from("<I", data, at)
        at += 4
        formats[name] = data[at:at + length]
        at += length
    return formats


# ---- files ---------------------------------------------------------------------

def hdrop(paths: list[str]) -> bytes:
    """The CF_HDROP layout Explorer reads: a DROPFILES header, then the paths in UTF-16,
    each ending in a zero, with one more zero at the end."""
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # files start at byte 20; fWide = 1
    return header + "".join(f"{path}\0" for path in paths).encode("utf-16-le") + b"\0\0"


def paths_from_hdrop(data: bytes) -> list[str]:
    if len(data) < 20:
        return []
    offset, _x, _y, _nc, wide = struct.unpack_from("<IiiII", data)
    body = data[offset:]
    text = body.decode("utf-16-le", "replace") if wide else body.decode("mbcs", "replace")
    return [path for path in text.split("\0") if path]


def describe_files(paths: list[str]) -> str:
    names = [path.rstrip("\\").rsplit("\\", 1)[-1] for path in paths]
    shown = ", ".join(names[:2])
    return shown + (f" e mais {len(names) - 2}" if len(names) > 2 else "")


# ---- images --------------------------------------------------------------------

def dib_size(dib: bytes) -> tuple[int, int] | None:
    if len(dib) < 40:
        return None
    width, height = struct.unpack_from("<ii", dib, 4)
    return abs(width), abs(height)


def _dib_pixels(dib: bytes):
    """(width, height, bits per pixel, where the pixels start, row size, bottom-up), or None
    for a kind of bitmap this doesn't read (palettes, compression)."""
    if len(dib) < 40:
        return None
    header_size, width, height, _planes, bits, compression = struct.unpack_from("<IiiHHI", dib)
    colors_used = struct.unpack_from("<I", dib, 32)[0]
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        return None
    start = header_size + (12 if compression == 3 and header_size == 40 else 0) + colors_used * 4
    row = (width * bits + 31) // 32 * 4
    if start + row * abs(height) > len(dib):
        return None
    return width, abs(height), bits, start, row, height > 0


def thumbnail(dib: bytes, png) -> bytes | None:
    """A small PNG of the image, averaging the pixels each thumbnail pixel covers.
    `png` turns (rgba, width, height) into PNG bytes."""
    layout = _dib_pixels(dib)
    if layout is None:
        return None
    width, height, bits, start, row, bottom_up = layout
    scale = min(THUMB_SIZE[0] / width, THUMB_SIZE[1] / height, 1.0)
    out_w, out_h = max(1, round(width * scale)), max(1, round(height * scale))
    step = bits // 8
    pixels = bytearray()
    for ty in range(out_h):
        for tx in range(out_w):
            red = green = blue = 0
            samples = 0
            for sy in (0.25, 0.75):
                for sx in (0.25, 0.75):
                    x = min(width - 1, int((tx + sx) * width / out_w))
                    y = min(height - 1, int((ty + sy) * height / out_h))
                    source_row = height - 1 - y if bottom_up else y
                    at = start + source_row * row + x * step
                    blue += dib[at]
                    green += dib[at + 1]
                    red += dib[at + 2]
                    samples += 1
            pixels += bytes((red // samples, green // samples, blue // samples, 255))
    return png(bytes(pixels), out_w, out_h)


def describe_image(dib: bytes) -> str:
    size = dib_size(dib)
    return f"Imagem {size[0]} × {size[1]}" if size else "Imagem"
