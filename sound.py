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

Pitch is a resample: playing the clip back "faster" raises its pitch (and
shortens it) with no extra dependency, at the cost of also shifting duration --
a fine trade for a one-shot alert tone, where a formant-preserving shift would
need real DSP for no audible benefit.
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
_transformed_cache: tuple[Path, int, int, bytes] | None = None

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


def _decode_samples(frames: bytes, sampwidth: int) -> list:
    if sampwidth == 1:
        return list(frames)
    if sampwidth in (2, 4):
        code = "h" if sampwidth == 2 else "i"
        count = len(frames) // sampwidth
        return list(struct.unpack(f"<{count}{code}", frames))
    count = len(frames) // 3
    return [int.from_bytes(frames[i * 3:i * 3 + 3], "little", signed=True) for i in range(count)]


def _encode_samples(samples: list, sampwidth: int) -> bytes:
    if sampwidth == 1:
        return bytes(max(0, min(255, round(s))) for s in samples)
    if sampwidth in (2, 4):
        code = "h" if sampwidth == 2 else "i"
        limit = (1 << (sampwidth * 8 - 1)) - 1
        clamped = [max(-limit - 1, min(limit, round(s))) for s in samples]
        return struct.pack(f"<{len(clamped)}{code}", *clamped)
    limit = (1 << 23) - 1
    out = bytearray(len(samples) * 3)
    for i, s in enumerate(samples):
        value = max(-limit - 1, min(limit, round(s)))
        out[i * 3:i * 3 + 3] = value.to_bytes(3, "little", signed=True)
    return bytes(out)


def _shift_pitch(frames: bytes, sampwidth: int, nchannels: int, factor: float) -> bytes:
    samples = _decode_samples(frames, sampwidth)
    frame_count = len(samples) // nchannels
    if frame_count < 2:
        return frames
    last = frame_count - 1
    new_frame_count = max(1, round(frame_count / factor))
    channels = [samples[c::nchannels] for c in range(nchannels)]
    out = [0.0] * (new_frame_count * nchannels)
    for c, channel in enumerate(channels):
        for i in range(new_frame_count):
            pos = min(i * factor, last)
            lo = int(pos)
            hi = min(lo + 1, last)
            frac = pos - lo
            out[i * nchannels + c] = channel[lo] * (1 - frac) + channel[hi] * frac
    return _encode_samples(out, sampwidth)


def _transformed_wav_bytes(path: Path, volume: int, pitch: int) -> bytes:
    """The whole WAV file with pitch and volume applied -- cached by
    (path, volume, pitch) since a rule firing repeatedly at an unchanged
    setting shouldn't redo this work every time."""
    global _transformed_cache
    if _transformed_cache is not None and _transformed_cache[:3] == (path, volume, pitch):
        return _transformed_cache[3]

    with wave.open(str(path), "rb") as wav_in:
        params = wav_in.getparams()
        frames = wav_in.readframes(params.nframes)

    if pitch != 100:
        frames = _shift_pitch(frames, params.sampwidth, params.nchannels, pitch / 100.0)
    frames = _scale_frames(frames, params.sampwidth, volume / 100.0)

    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_out:
        frame_size = params.sampwidth * params.nchannels
        wav_out.setparams(params._replace(nframes=len(frames) // frame_size))
        wav_out.writeframes(frames)
    data = buffer.getvalue()

    _transformed_cache = (path, volume, pitch, data)
    return data


def play(path: Path, volume: int = 100, pitch: int = 100) -> None:
    if volume <= 0:
        return
    if not path.is_file():
        log.warning("notification sound not found: %s", path)
        return

    transformed = volume < 100 or pitch != 100

    if sys.platform == "win32":
        import winsound

        try:
            if not transformed:
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            else:
                winsound.PlaySound(_transformed_wav_bytes(path, volume, pitch), winsound.SND_MEMORY | winsound.SND_ASYNC)
        except OSError as error:
            log.warning("could not play notification sound: %s", error)
        return

    play_path = path
    if transformed:
        app_paths.cache_dir().mkdir(parents=True, exist_ok=True)
        _SCRATCH_PATH.write_bytes(_transformed_wav_bytes(path, volume, pitch))
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
