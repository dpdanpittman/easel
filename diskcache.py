"""The last good answer of a slow source, kept on disk so a restarted server never shows a partial picture.

Without it, for a few seconds after each start the first Linear fetch has not answered and the task list would show
only the hand-written items, as if complete. Now Linear's and GitHub's last good results are saved here after every
good fetch and loaded at start, then served with their age until a fresh answer arrives.

Files under settings `server.state_dir`: written to a temp file in the same folder
(mode 600) and renamed into place; never a key or token, only what the screen shows anyway (ticket ids, titles,
states; PR numbers, titles, check states, ticket ids). A missing, unreadable, oversized or malformed file reads as
nothing (the caller then says it is loading). Never raises.
"""
import json
import os
import tempfile

import config

MAX_BYTES = 8 * 1024 * 1024


def path(name):
    return config.state_path(name)


def load(file, max_bytes=MAX_BYTES):
    """The JSON object in the file, or None."""
    try:
        if os.path.getsize(file) > max_bytes:
            return None
        with open(file, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def save(file, doc):
    """Write the object atomically, mode 600. Returns True on success; never raises."""
    tmp = None
    try:
        d = os.path.dirname(file)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cache-", suffix=".tmp", dir=d)   # mkstemp creates it 0600
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
        return True
    except Exception:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
