"""The voice that reads the commentary.

Piper (github.com/rhasspy/piper) is a natural-sounding voice that runs offline. The program (about 25 MB)
and a voice (about 60 MB) are downloaded once into the models folder, like the speech model, and checked
against the checksums below. The built-in voices were trained on public-domain recordings, so reels made
with them can be monetized. Macs (Piper's Mac build is broken) and languages without a Piper voice use
the computer's own voice: the Windows voices, `say` on a Mac, espeak-ng on Linux.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .config import MODELS_DIR
from .media import StopRequested, Tools, popen_kwargs

log = logging.getLogger("brainrot")

PIPER_URL = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
PIPER_BUILDS = {  # (archive, sha256)
    "windows-amd64": ("piper_windows_amd64.zip", "f3c58906402b24f3a96d92145f58acba6d86c9b5db896d207f78dc80811efcea"),
    "linux-x86_64": ("piper_linux_x86_64.tar.gz", "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992"),
    "linux-aarch64": ("piper_linux_aarch64.tar.gz", "fea0fd2d87c54dbc7078d0f878289f404bd4d6eea6e7444a77835d1537ab88eb"),
}
VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/{revision}/"
VOICES_REVISION = "c10ece1aade47bb51c153c893d14e5bf8e5b7117"
# Trained on LibriVox (public domain) or the speaker's own recordings: (Piper name, model sha256, settings sha256).
VOICES = {
    "norman": ("en_US-norman-medium", "b9739443232a80a59c7d18810dd856899bf16a7964725f5ab81ea49b1351cb71",
               "6c2db7f558a4a8deb9fe822583c1c5105f6c4e834dd0f9de8ad17a888ee9fe1d"),
    "john": ("en_US-john-medium", "789c6c875726e627ddee93d51d8727859abe9c091c3d141591f4b83c2072e988",
             "af60f177b6b550f3d7a302720c0fb89e7f94a82b5dca464775ef63b1c69ba09a"),
    "bryce": ("en_US-bryce-medium", "dc9caa6c313199ffb5ac698b6e542fa6cba388aeaf2731e25262e33b9810aef1",
              "7ceb1bc4af6d4e41b6d1edbb86c67e91e01eaa71f66db4cd0ae92ac704d415be"),
    "kristin": ("en_US-kristin-medium", "5849957f929cbf720c258f8458692d6103fff2f0e3d3b19c8259474bb06a18d4",
                "5681426d4aead22195de70531eeeeddb46493cfaffc5764b2ea3db73428b651c"),
}
DESCRIPTIONS = {"norman": "male", "john": "male, deeper", "bryce": "male, younger", "kristin": "female",
                "system": "the computer's own voice"}
PIPER_VOICE_RE = re.compile(r"^([a-z]{2,3})_([A-Z]{2})-([A-Za-z0-9_]+)-(x_low|low|medium|high)$")
# The Microsoft C++ runtime piper.exe needs. Most PCs have it; the app carries a copy for those that don't.
WINDOWS_RUNTIME = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")

_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$lang = $env:BRAINROT_VOICE_LANG
if ($lang) {
  $v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq $lang } | Select-Object -First 1
  if (-not $v) { exit 3 }
  $s.SelectVoice($v.VoiceInfo.Name)
}
$s.Rate = [int]$env:BRAINROT_VOICE_RATE
$s.SetOutputToWaveFile($env:BRAINROT_VOICE_OUT)
$s.Speak([IO.File]::ReadAllText($env:BRAINROT_VOICE_TEXT, [Text.Encoding]::UTF8))
$s.Dispose()
"""


class VoiceError(Exception):
    """No voice could say it (the reel is then made without the voiceover)."""


def valid_voice(name: str) -> bool:
    return name in VOICES or name == "system" or bool(PIPER_VOICE_RE.match(name))


def piper_build(system: str = "", machine: str = "") -> str:
    """Which Piper download runs on this computer ('' = none: Macs and 32-bit systems)."""
    system = system or ("windows" if os.name == "nt" else sys.platform)
    machine = (machine or platform.machine()).lower()
    if system == "windows":
        return "windows-amd64" if machine in ("amd64", "x86_64", "arm64") else ""  # ARM runs it emulated
    if system.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
    return ""


def voice_language(name: str) -> str:
    """'en' for the built-in voices, 'de' for de_DE-thorsten-medium, '' for the system voice (any language)."""
    if name in VOICES:
        return "en"
    match = PIPER_VOICE_RE.match(name)
    return match.group(1) if match else ""


def download(url: str, dest: Path, sha256: str = "", should_stop: Callable[[], bool] | None = None) -> Path:
    """Download to dest (via a .part file, so a broken download is never used), checking the checksum."""
    from .uploads.http import USER_AGENT

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp, open(partial, "wb") as out:  # noqa: S310 - fixed https URLs
            while True:
                if should_stop is not None and should_stop():
                    raise StopRequested()
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if sha256 and digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise VoiceError(f"the download of {dest.name} was damaged (it will be tried again next time)")
    os.replace(partial, dest)
    return dest


def _extract(archive: Path, folder: Path) -> None:
    """Unpack a zip or tar.gz, refusing anything that would land outside the folder."""
    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(folder)  # zipfile drops "..", drive letters and leading slashes itself
            return
        _extract_tar(archive, folder)
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        raise VoiceError(f"the voice download couldn't be unpacked ({exc})") from exc


def _extract_tar(archive: Path, folder: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(folder, filter="data")
            return
        root = folder.resolve()
        for member in tf.getmembers():
            target = (folder / member.name).resolve()
            if root not in target.parents and target != root:
                raise VoiceError(f"unexpected file in the voice download: {member.name}")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve() if member.issym() else (folder / member.linkname).resolve()
                if root not in link.parents:
                    raise VoiceError(f"unexpected link in the voice download: {member.name}")
            elif not (member.isfile() or member.isdir()):
                raise VoiceError(f"unexpected file in the voice download: {member.name}")
        tf.extractall(folder)  # noqa: S202 - checked above


def _copy_windows_runtime(folder: Path) -> None:
    """Put the C++ runtime next to piper.exe if this PC may not have it (the app carries a copy)."""
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    places = [Path(p) for p in (getattr(sys, "_MEIPASS", ""), sys.base_prefix, os.path.dirname(sys.executable)) if p]
    for name in WINDOWS_RUNTIME:
        if (folder / name).exists() or (system / name).exists():
            continue
        for place in places:
            found = [p for p in place.glob("*.dll") if p.name.lower() == name] if place.is_dir() else []
            if found:
                shutil.copyfile(found[0], folder / name)
                break


class Voice:
    """Says text into a WAV file with the voice from commentary.voice (downloaded the first time)."""

    def __init__(self, name: str, speed: float, tools: Tools, folder: Path = MODELS_DIR,
                 should_stop: Callable[[], bool] | None = None):
        self.name = name
        self.speed = speed
        self.tools = tools
        self.folder = folder
        self.should_stop = should_stop
        self._piper_broken = ""  # why Piper can't run here (then the computer's voice is used)
        self._download_failed = 0.0  # when the last download failed (tried again an hour later)

    # ------------------------------------------------------------------ what speaks

    def engine_for(self, language: str) -> str:
        """'piper' or 'system' for a reel in this language ('' = unknown)."""
        lang = (language or "").lower().split("-")[0].split("_")[0]
        if self.name == "system" or not piper_build() or self._piper_broken:
            return "system"
        if self._download_failed and time.time() - self._download_failed < 3600 and not self.piper_ready():
            return "system"
        if lang and lang != voice_language(self.name):
            return "system"  # e.g. a Spanish clip with the English voice
        return "piper"

    def describe(self, language: str = "") -> str:
        if self.engine_for(language) == "piper":
            return f"{self.name} ({DESCRIPTIONS.get(self.name, 'Piper voice')})"
        return "the computer's own voice"

    def speak(self, text: str, language: str, out: Path) -> Path:
        """Say text into out (a WAV file). Raises VoiceError if no voice can say it."""
        text = " ".join(text.split())
        if not text:
            raise VoiceError("nothing to say")
        out.parent.mkdir(parents=True, exist_ok=True)
        if self.engine_for(language) == "piper":
            try:
                exe, model = self._install_piper(), self._install_voice()
            except StopRequested:
                raise
            except (OSError, VoiceError) as exc:  # no internet, a blocked site...: try again in an hour
                self._download_failed = time.time()
                log.warning("Couldn't download the voice (%s); using the computer's own voice this time.", exc)
            else:
                try:
                    return self._piper(exe, model, text, out)
                except (OSError, subprocess.SubprocessError, VoiceError) as exc:
                    self._piper_broken = str(exc)
                    log.warning("The natural voice couldn't start (%s); using the computer's own voice instead.", exc)
        return self._system(text, language, out)

    # ------------------------------------------------------------------ Piper

    def piper_ready(self) -> bool:
        build = piper_build()
        return bool(build) and self._piper_exe(build).is_file() and (self.name not in VOICES or self._voice_files()[0].is_file())

    def _piper_exe(self, build: str) -> Path:
        return self.folder / "piper" / build / "piper" / ("piper.exe" if os.name == "nt" else "piper")

    def _install_piper(self) -> Path:
        build = piper_build()
        exe = self._piper_exe(build)
        if exe.is_file():
            return exe
        archive_name, sha = PIPER_BUILDS[build]
        log.info("Downloading the voice program (about 25 MB, only this once)...")
        home = self.folder / "piper"
        archive = download(PIPER_URL + archive_name, home / archive_name, sha, self.should_stop)
        staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=home))
        try:
            _extract(archive, staging)
            if os.name == "nt":
                _copy_windows_runtime(staging / "piper")
            staging.chmod(0o755)
            target = home / build
            shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            archive.unlink(missing_ok=True)
        if os.name != "nt":
            exe.chmod(exe.stat().st_mode | 0o111)
        return exe

    def _voice_files(self) -> tuple[Path, Path]:
        voice = VOICES[self.name][0] if self.name in VOICES else self.name
        base = self.folder / "voices" / voice
        return base.with_suffix(".onnx"), base.with_name(voice + ".onnx.json")

    def _install_voice(self) -> Path:
        model, settings = self._voice_files()
        if model.is_file() and settings.is_file():
            return model
        if self.name in VOICES:
            voice, model_sha, settings_sha = VOICES[self.name]
            base = VOICES_URL.format(revision=VOICES_REVISION)
        else:
            voice, model_sha, settings_sha = self.name, "", ""
            base = VOICES_URL.format(revision="main")
        lang_region, speaker, quality = voice.split("-")
        url = f"{base}{lang_region.split('_')[0]}/{lang_region}/{speaker}/{quality}/{voice}"
        log.info("Downloading the voice '%s' (about 60 MB, only this once)...", voice)
        download(url + ".onnx.json", settings, settings_sha, self.should_stop)
        download(url + ".onnx", model, model_sha, self.should_stop)
        return model

    def _piper(self, exe: Path, model: Path, text: str, out: Path) -> Path:
        cmd = [str(exe), "--model", str(model), "--output_file", str(out),
               "--length_scale", f"{1 / max(0.5, self.speed):.3f}", "--sentence_silence", "0.2"]
        out.unlink(missing_ok=True)
        proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=300, cwd=str(exe.parent),
                              **popen_kwargs())
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
            raise VoiceError(f"Piper: {detail[0][-300:]}")
        return out

    # ------------------------------------------------------------------ the computer's own voice

    def _system(self, text: str, language: str, out: Path) -> Path:
        lang = (language or "").lower().split("-")[0].split("_")[0]
        with tempfile.TemporaryDirectory() as tmp:
            text_file = Path(tmp) / "text.txt"
            text_file.write_text(text, encoding="utf-8")
            if os.name == "nt":
                self._windows(text_file, lang, out)
            elif sys.platform == "darwin":
                self._mac(text_file, lang, out, Path(tmp))
            else:
                self._espeak(text_file, lang, out)
        if not out.is_file() or out.stat().st_size < 1000:
            raise VoiceError("the computer's voice made no sound")
        return out

    def _windows(self, text_file: Path, lang: str, out: Path) -> None:
        env = dict(os.environ, BRAINROT_VOICE_LANG=lang if lang and lang != "en" else "",
                   BRAINROT_VOICE_RATE=str(max(-10, min(10, round((self.speed - 1) * 10)))),
                   BRAINROT_VOICE_OUT=str(out), BRAINROT_VOICE_TEXT=str(text_file))
        script = base64.b64encode(_POWERSHELL.encode("utf-16-le")).decode("ascii")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        proc = subprocess.run([str(powershell) if powershell.is_file() else "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-EncodedCommand", script], capture_output=True, timeout=300, env=env, **popen_kwargs())
        if proc.returncode == 3:
            raise VoiceError(f"Windows has no voice for this language ({lang}): add one in Settings > Time & language > "
                             "Speech, or set commentary.voice to a Piper voice for it")
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
            raise VoiceError(f"Windows' voice failed: {detail[0][-300:]}")

    def _mac(self, text_file: Path, lang: str, out: Path, tmp: Path) -> None:
        cmd = ["say", "-r", str(round(180 * self.speed)), "-f", str(text_file), "-o", str(tmp / "voice.aiff")]
        if lang and lang != "en":
            listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=60).stdout
            voice = next((line.split()[0] for line in listing.splitlines()
                          if re.search(rf"\s{lang}[_-]", line) and line.split()), "")
            if not voice:
                raise VoiceError(f"this Mac has no voice for this language ({lang})")
            cmd[1:1] = ["-v", voice]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        subprocess.run([self.tools.ffmpeg, "-y", "-v", "error", "-i", str(tmp / "voice.aiff"), str(out)], check=True,
                       capture_output=True, timeout=300)

    def _espeak(self, text_file: Path, lang: str, out: Path) -> None:
        program = shutil.which("espeak-ng") or shutil.which("espeak")
        if not program:
            raise VoiceError("no voice program found (install espeak-ng, or use a Piper voice)")
        cmd = [program, "-s", str(round(170 * self.speed)), "-f", str(text_file), "-w", str(out)]
        if lang:
            cmd[1:1] = ["-v", lang]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise VoiceError(f"espeak: {(proc.stderr or b'').decode('utf-8', 'replace').strip()[-300:]}")
