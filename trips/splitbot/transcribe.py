#!/usr/bin/env python3
"""Speech to text for voice notes, run locally (no API key; audio never leaves the container).

Uses faster-whisper (Whisper on CPU). The listener calls transcribe() on each
voice note, then deletes the audio; only the text goes on as the message.

  transcribe.py --warm        install faster-whisper if needed and download the model
  transcribe.py FILE          print the transcript of an audio file

SPLITBOT_STT_MODEL picks the model (default "large-v3-turbo": far better with
accents, names and spoken numbers than "small"; ~10 s for a 10 s note on 4 CPUs,
~1.6 GB download once per run). SPLITBOT_STT_LANG fixes the language (default
"en", which covers Singapore English; auto-detection mistakes accented English
for other languages and garbles it). Set it to "auto" to detect per note.
"""

from __future__ import annotations

import fcntl
import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

MODEL = os.environ.get("SPLITBOT_STT_MODEL", "large-v3-turbo")
LANG = os.environ.get("SPLITBOT_STT_LANG", "en")
# PyAV 19 dropped an argument faster-whisper 1.2 still passes to av.open(), so
# every transcription fails with it. Pin until faster-whisper catches up.
PACKAGES = ["faster-whisper==1.2.1", "av>=12,<19"]
MAX_SECONDS = 180          # longer recordings are not transcribed
RETRY_AFTER = 60           # after a failed install or download, try again this much later
LOCK = Path.home() / ".cache" / "splitbot-stt.lock"
_model = None
_failed_at = 0.0


def _install_and_load():
    try:
        import av
        if int(av.__version__.split(".")[0]) >= 19:
            raise ImportError("incompatible PyAV")  # reinstall with the pin below
        from faster_whisper import WhisperModel
    except ImportError:
        # Containers are rebuilt without it; install on first use.
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *PACKAGES],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"pip install failed: {r.stderr.strip()[-300:]}")
        importlib.invalidate_caches()  # let this running process see the new package
        for name in [k for k in sys.modules if k == "av" or k.startswith("av.")]:
            del sys.modules[name]          # drop an already-imported PyAV 19
        from faster_whisper import WhisperModel
    return WhisperModel(MODEL, device="cpu", compute_type="int8")


def _load():
    """The Whisper model, loaded once per process; None if it can't be had right now.

    The warm-up and the listener (or a second bot using this module) may reach here at
    once in a fresh container. A file lock makes them take turns, so the
    second waits for the first's install and download instead of failing on
    a half-installed package or half-downloaded model. A failure is retried
    on a later call, never remembered for good.
    """
    global _model, _failed_at
    if _model is not None:
        return _model
    if time.time() - _failed_at < RETRY_AFTER:
        return None
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # waits while another process installs or downloads
        try:
            _model = _install_and_load()
        except Exception as e:  # noqa: BLE001  (install, download or load failure)
            print(f"transcribe: model unavailable: {e}", file=sys.stderr)
            _failed_at = time.time()
    return _model


def transcribe(path: str, hint: str = "") -> str | None:
    """The spoken text, or None if transcription isn't possible.

    `hint` biases the model toward words it should expect (members' names,
    currency words), which helps with names and amounts.
    """
    model = _load()
    if model is None:
        return None
    try:
        segs, _ = model.transcribe(path, beam_size=5, vad_filter=True,
                                   language=None if LANG == "auto" else LANG,
                                   condition_on_previous_text=False,  # no runaway repeats
                                   initial_prompt=hint or None)
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:  # noqa: BLE001  (corrupt or unsupported audio)
        print(f"transcribe: failed on {path}: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if sys.argv[1:] == ["--warm"]:
        print("transcribe: ready" if _load() is not None else "transcribe: unavailable")
    elif len(sys.argv) == 2:
        print(transcribe(sys.argv[1]) or "")
    else:
        print(__doc__)
        sys.exit(2)
