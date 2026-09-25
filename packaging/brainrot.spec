# PyInstaller build for Brainrot Bot:  pyinstaller packaging/brainrot.spec
# Produces dist/BrainrotBot/ with two programs sharing the same files:
#   BrainrotBot.exe             the app you double-click (setup + menu)
#   BrainrotBot-background.exe  the same app without a window (background mode, start at login)
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "fonts"), "fonts"),
    (os.path.join(ROOT, "assets"), "assets"),  # emoji pictures, face detection model
    (os.path.join(ROOT, "config.yaml"), "."),
]
datas += collect_data_files("faster_whisper")  # voice-activity model used while transcribing
datas += collect_data_files("yt_dlp_ejs")  # YouTube challenge solver scripts (run by the bundled Deno)
datas += collect_data_files("certifi")
for package in ("huggingface_hub", "tqdm", "tokenizers", "faster_whisper", "ctranslate2", "numpy", "av", "onnxruntime", "PyYAML",
                "yt-dlp", "yt-dlp-ejs", "anthropic", "httpx2", "pydantic", "pydantic_core"):
    try:
        datas += copy_metadata(package)
    except Exception:
        pass
binaries = collect_dynamic_libs("ctranslate2")

a = Analysis(
    [os.path.join(ROOT, "brainrot.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=["brainrot_bot.app", "brainrot_bot.wizard", "brainrot_bot.selftest", "yt_dlp_ejs"]
    + collect_submodules("yt_dlp.extractor") + collect_submodules("anthropic.types"),
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PIL", "cryptography", "OpenSSL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = os.path.join(ROOT, "packaging", "icon.ico")
icon = icon if os.path.exists(icon) else None

app = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BrainrotBot", console=True, icon=icon, upx=False)
background = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BrainrotBot-background", console=False, icon=icon, upx=False)
COLLECT(app, background, a.binaries, a.datas, name="BrainrotBot", upx=False)
