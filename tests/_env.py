"""Imported first by every test module: the suite runs with tests/easel.test.toml, never a real settings file."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["EASEL_CONFIG"] = os.path.join(HERE, "easel.test.toml")
sys.path.insert(0, os.path.dirname(HERE))


def notes_of(cfg):
    """Older tests wrote a small JSON file naming the notes file ({"notes": path}); the sources now take the notes
    path itself. A missing or odd file gives a path that does not exist (the source then has no notes file)."""
    import json
    try:
        with open(cfg, encoding="utf-8") as f:
            return json.load(f)["notes"]
    except (OSError, ValueError, KeyError, TypeError):
        return os.path.join(os.path.dirname(cfg) or ".", "no-such-notes-file.md")
