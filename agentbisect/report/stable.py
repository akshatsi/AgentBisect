"""Idempotent writes: leave a saved file alone when only volatile fields would change.

Volatile fields are the time a file was made (``created_at``, the generation time in the study
page and draft), how long an oracle run took (``duration_s``), and the timing text pytest prints
in an output tail ("1 passed in 0.34s"). Everything else is a real change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

VOLATILE_KEYS = frozenset({"created_at", "duration_s"})
_SECONDS = re.compile(r"\d+(?:\.\d+)?s\b")


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_SECONDS.sub("Ns", v) if k == "output_tail" and isinstance(v, str) else _strip(v))
            for k, v in value.items()
            if k not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def same_json(old: str, new: str) -> bool:
    """True when two JSON texts differ only in volatile fields."""
    try:
        return _strip(json.loads(old)) == _strip(json.loads(new))
    except ValueError:
        return False


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def write_json_stable(path: Path, text: str) -> bool:
    """Write ``text`` unless ``path`` holds JSON equal to it apart from volatile fields.

    Returns True when it wrote.
    """
    old = read_text(path)
    if old is not None and same_json(old, text):
        return False
    path.write_text(text, encoding="utf-8")
    return True


def write_text_stable(path: Path, text: str) -> bool:
    """Write ``text`` unless ``path`` already holds exactly that. Returns True when it wrote."""
    if read_text(path) == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True
