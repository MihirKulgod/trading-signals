"""
Plays a short notification sound, without adding a new dependency.

Windows (the real target -- this is where the app actually runs for users)
gets it for free via the stdlib winsound module. Non-Windows only needs to
work well enough for development: it shells out to whatever's on PATH.
Either way, starting a new sound always stops whatever's still playing, so
several rules firing close together are never audible on top of each other.
"""

import shutil
import subprocess
import sys
import threading
from pathlib import Path

from app_logging import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_process: subprocess.Popen | None = None


def play(path: Path) -> None:
    if not path.is_file():
        log.warning("notification sound not found: %s", path)
        return

    if sys.platform == "win32":
        import winsound

        try:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except OSError as error:
            log.warning("could not play notification sound: %s", error)
        return

    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            _process.terminate()
        player = next((p for p in ("paplay", "aplay", "afplay") if shutil.which(p)), None)
        if player is None:
            log.warning("no audio player found (tried paplay/aplay/afplay); skipping notification sound")
            return
        _process = subprocess.Popen([player, str(path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
