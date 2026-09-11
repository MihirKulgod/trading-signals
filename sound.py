"""
Plays a short notification sound, without adding a new dependency.

Windows (the real target -- this is where the app actually runs for users)
gets it for free via the stdlib winsound module. Non-Windows only needs to
work well enough for development: it shells out to whatever's on PATH.
Either way, starting a new sound always stops whatever's still playing, so
several rules firing close together are never audible on top of each other.

Volume is applied by scaling the WAV's own PCM samples rather than relying on
a player's volume flag -- winsound and aplay/afplay don't have one, and this
way every backend behaves identically. Deliberately not audioop (deprecated
since 3.11, gone in 3.13): plain struct/bytes math instead.
"""

import shutil
import struct
import subprocess
import sys
import threading
import wave
from io import BytesIO
from pathlib import Path

import app_paths
from app_logging import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_process: subprocess.Popen | None = None
_scaled_cache: tuple[Path, int, bytes] | None = None

_SCRATCH_PATH = app_paths.cache_dir() / "notification_scaled.wav"


def _scale_frames(frames: bytes, sampwidth: int, factor: float) -> bytes:
    if sampwidth == 1:
        # Unsigned, centered at 128.
        return bytes(max(0, min(255, 128 + round((b - 128) * factor))) for b in frames)
    if sampwidth in (2, 4):
        code = "h" if sampwidth == 2 else "i"
        count = len(frames) // sampwidth
        samples = struct.unpack(f"<{count}{code}", frames)
        limit = (1 << (sampwidth * 8 - 1)) - 1
        scaled = [max(-limit - 1, min(limit, round(s * factor))) for s in samples]
        return struct.pack(f"<{count}{code}", *scaled)
    # 24-bit: no native struct code, so each 3-byte sample is handled by hand.
    limit = (1 << 23) - 1
    out = bytearray(len(frames))
    for offset in range(0, len(frames), 3):
        value = int.from_bytes(frames[offset:offset + 3], "little", signed=True)
        value = max(-limit - 1, min(limit, round(value * factor)))
        out[offset:offset + 3] = value.to_bytes(3, "little", signed=True)
    return bytes(out)


def _scaled_wav_bytes(path: Path, volume: int) -> bytes:
    """The whole WAV file, every sample scaled by volume/100 -- cached by
    (path, volume) since a rule firing repeatedly at an unchanged volume
    shouldn't redo this work every time."""
    global _scaled_cache
    if _scaled_cache is not None and _scaled_cache[:2] == (path, volume):
        return _scaled_cache[2]

    with wave.open(str(path), "rb") as wav_in:
        params = wav_in.getparams()
        frames = wav_in.readframes(params.nframes)

    scaled_frames = _scale_frames(frames, params.sampwidth, volume / 100.0)

    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_out:
        wav_out.setparams(params)
        wav_out.writeframes(scaled_frames)
    data = buffer.getvalue()

    _scaled_cache = (path, volume, data)
    return data


def play(path: Path, volume: int = 100) -> None:
    if volume <= 0:
        return
    if not path.is_file():
        log.warning("notification sound not found: %s", path)
        return

    if sys.platform == "win32":
        import winsound

        try:
            if volume >= 100:
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            else:
                winsound.PlaySound(_scaled_wav_bytes(path, volume), winsound.SND_MEMORY | winsound.SND_ASYNC)
        except OSError as error:
            log.warning("could not play notification sound: %s", error)
        return

    play_path = path
    if volume < 100:
        app_paths.cache_dir().mkdir(parents=True, exist_ok=True)
        _SCRATCH_PATH.write_bytes(_scaled_wav_bytes(path, volume))
        play_path = _SCRATCH_PATH

    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            _process.terminate()
        player = next((p for p in ("paplay", "aplay", "afplay") if shutil.which(p)), None)
        if player is None:
            log.warning("no audio player found (tried paplay/aplay/afplay); skipping notification sound")
            return
        _process = subprocess.Popen([player, str(play_path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
