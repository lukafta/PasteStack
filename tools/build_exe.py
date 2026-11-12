"""Builds the app for people without Python: dist/PasteStack/PasteStack.exe, a zip of it,
and the installer dist/PasteStack-<version>-setup.exe.

Compiled to native code with Nuitka (no Python bytecode inside to decompile). The installer
is per-user (no administrator rights) and needs Inno Setup 6; its script is tools/PasteStack.iss.

    py -m pip install nuitka              (the first build downloads a C compiler, Zig)
    py tools/build_exe.py                 everything
    py tools/build_exe.py --no-installer  skip the setup.exe
    py tools/build_exe.py --installer-only  just the setup.exe, from dist/PasteStack
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipapp import APP_NAME, VERSION, win32  # noqa: E402
from clipapp.engine import APP_ICON_COLOR, WINDOW_CLASS, WM_QUIT_REQUEST  # noqa: E402
from clipapp.ui import RUNNING_MUTEX  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP = DIST / APP_NAME
ICON = BUILD / f"{APP_NAME}.ico"
INNO_SCRIPT = ROOT / "tools" / f"{APP_NAME}.iss"
DOTTED_VERSION = VERSION + ".0" * (3 - VERSION.count("."))  # Windows wants a.b.c.d
SKIP_IMPORTS = ["unittest", "pydoc", "PIL", "doctest", "test"]

README = """{name} {version}
{line}

1. Instale com {name}-{version}-setup.exe (cria o atalho no Menu Iniciar e tem
   desinstalador), ou extraia esta pasta onde quiser e abra {name}.exe.
2. Na primeira vez, leia o aviso de privacidade e clique em Entendi.

Atalhos (dá para mudar em Configurações > Atalhos):
  Ctrl+Shift+V       cola rápido, onde você está escrevendo
  Ctrl+Shift+Espaço  abre e fecha a janela
  Ctrl+Shift+Q       grava, cola e desliga as listas

Fechar a janela deixa o {name} na área de notificação, perto do relógio, ainda
capturando. Para sair: botão direito no ícone de lá > Sair.

Se o Windows mostrar "O Windows protegeu o computador", clique em
"Mais informações" > "Executar assim mesmo" (o app ainda não é assinado).

Seus dados ficam em %APPDATA%\\ClipApp, criptografados para a sua conta do Windows.
Desinstalar o {name} não apaga esses dados.

Requer Windows 10 ou 11, 64 bits.
"""


def make_ico(path: Path) -> Path:
    """The logo as an .ico: one PNG per size, which Windows Vista and later read."""
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    images = [win32.icon_png(APP_ICON_COLOR, size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, png in zip(sizes, images):
        side = 0 if size >= 256 else size  # 0 means 256 in an .ico
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + data)
    return path


def build_nuitka() -> Path:
    out = BUILD / "nuitka"
    cmd = [
        sys.executable, "-m", "nuitka", str(ROOT / f"{APP_NAME}.pyw"),
        "--standalone", "--zig", "--assume-yes-for-downloads", "--enable-plugin=tk-inter",
        "--python-flag=no_asserts", "--windows-console-mode=disable", f"--windows-icon-from-ico={ICON}",
        f"--company-name={APP_NAME}", f"--product-name={APP_NAME}", f"--file-version={DOTTED_VERSION}",
        f"--product-version={DOTTED_VERSION}", f"--file-description={APP_NAME} - Ctrl+C e Ctrl+V melhorados",
        f"--copyright={APP_NAME}", "--include-package=clipapp",
        *[f"--nofollow-import-to={module}" for module in SKIP_IMPORTS],
        f"--output-dir={out}", f"--output-filename={APP_NAME}.exe",
    ]
    print("compiling with Nuitka (several minutes)...", flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    return out / f"{APP_NAME}.dist"


def package(built: Path) -> None:
    shutil.rmtree(APP, ignore_errors=True)
    shutil.copytree(built, APP)
    (APP / "LEIA-ME.txt").write_text(README.format(name=APP_NAME, version=VERSION, line="=" * (len(APP_NAME) + 1 + len(VERSION))),
                                     encoding="utf-8")
    archive = DIST / f"{APP_NAME}-{VERSION}-win64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in APP.rglob("*"):
            z.write(path, Path(APP_NAME) / path.relative_to(APP))
    size = sum(p.stat().st_size for p in APP.rglob("*") if p.is_file())
    print(f"built {APP / (APP_NAME + '.exe')} ({size / 1e6:.0f} MB unpacked)")
    print(f"zip   {archive} ({archive.stat().st_size / 1e6:.0f} MB)")


def _iscc() -> Path | None:
    """Inno Setup's command-line compiler (per-user winget install, or Program Files)."""
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for base in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs", os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramFiles")):
        if base and (Path(base) / "Inno Setup 6" / "ISCC.exe").exists():
            return Path(base) / "Inno Setup 6" / "ISCC.exe"
    return None


def build_installer() -> Path | None:
    iscc = _iscc()
    if not iscc:
        print("Inno Setup 6 isn't installed, so no setup.exe this time. "
              "Install it with: winget install JRSoftware.InnoSetup --scope user")
        return None
    if not (APP / f"{APP_NAME}.exe").exists():
        raise SystemExit(f"{APP / (APP_NAME + '.exe')} is missing: build the app first")
    setup = DIST / f"{APP_NAME}-{VERSION}-setup.exe"
    setup.unlink(missing_ok=True)
    defines = {"AppName": APP_NAME, "AppVersion": VERSION, "AppVersionDotted": DOTTED_VERSION, "AppDir": APP,
               "OutputDir": DIST, "AppIcon": ICON, "AppUserModelId": f"{APP_NAME}.Desktop",
               "AppMutex": RUNNING_MUTEX, "EngineClass": WINDOW_CLASS, "QuitMessage": WM_QUIT_REQUEST}
    print("building the installer...", flush=True)
    subprocess.run([str(iscc), "/Q", *[f"/D{k}={v}" for k, v in defines.items()], str(INNO_SCRIPT)], check=True)
    print(f"setup {setup} ({setup.stat().st_size / 1e6:.0f} MB)")
    return setup


def main() -> None:
    make_ico(ICON)
    if "--installer-only" in sys.argv:
        build_installer()
        return
    package(build_nuitka())
    if "--no-installer" not in sys.argv:
        build_installer()


if __name__ == "__main__":
    main()
