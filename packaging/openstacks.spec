# PyInstaller spec for the OpenStacks front-end (one-folder, windowed).
#   pyinstaller --noconfirm packaging/openstacks.spec      (run from the repo root)
# packaging/assemble.py then adds qemu/, patches/, tools/ and docs next to the exe.
import os
import sys

ROOT = os.path.abspath(os.getcwd())
FRONTEND = os.path.join(ROOT, "frontend")
BRAND = os.path.join(ROOT, "brand")

# SDL2 for gamepads (frontend/openstacks/controls/gamepad.py loads it with ctypes, from the
# bundle first): the pysdl2-dll wheel's SDL2.dll on Windows, the build machine's libSDL2
# elsewhere (it links only libc/libm and dlopens its video/input backends).
def _sdl2():
    if sys.platform == "win32":
        try:
            import sdl2dll
            p = os.path.join(sdl2dll.get_dllpath(), "SDL2.dll")
            return [(p, ".")] if os.path.exists(p) else []
        except ImportError:
            return []
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib"):
        p = os.path.join(d, "libSDL2-2.0.so.0")
        if os.path.exists(p):
            return [(os.path.realpath(p), ".")]
    print("warning: libSDL2-2.0.so.0 not found: no bundled gamepad support")
    return []


a = Analysis(
    [os.path.join(FRONTEND, "run_player.py")],
    pathex=[FRONTEND],
    binaries=_sdl2(),
    datas=[(os.path.join(BRAND, d), os.path.join("brand", d)) for d in ("png", "ico", "svg")],
    # image patches (patches/*.py) are loaded from disk at install time and import these
    hiddenimports=["gzip", "hashlib", "urllib.request", "zipfile", "importlib.util", "truststore"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia",
              "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf"],
    noarchive=False,
)
# Qt pieces the widgets UI never loads (software OpenGL, Quick/QML, PDF, translations):
# about 45 MB.
_DROP = ("opengl32sw.dll", "qt6quick", "qt6qml", "qt6pdf", "qpdf.dll", "qt6virtualkeyboard",
         "qt6opengl.dll", "qt6network.dll", "qt6svgwidgets")
a.binaries = [b for b in a.binaries if not any(d in b[0].lower().replace("\\", "/").split("/")[-1]
                                                 for d in _DROP)]
a.datas = [d for d in a.datas if "/translations/" not in d[0].replace("\\", "/").lower()]
pyz = PYZ(a.pure)
WIN = sys.platform == "win32"          # icon + version resource: Windows executables only
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="OpenStacks",
    icon=os.path.join(BRAND, "ico", "openstacks.ico") if WIN else None,
    console=False,
    version=os.path.join(ROOT, "packaging", "version_info.txt")
    if WIN and os.path.exists(os.path.join(ROOT, "packaging", "version_info.txt")) else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="OpenStacks")
