"""TTY-aware terminal colours (see DESIGN_TOKENS.md).

Colour is secondary encoding only: the status word is always printed.
Colour is enabled only when the stream is a TTY and NO_COLOR is unset/empty.
"""

from __future__ import annotations

import os
from typing import TextIO

RED, YELLOW, GREEN, DIM = "31", "33", "32", "2"

_STATUS = {"FAIL": RED, "CRITICAL": RED, "WARN": YELLOW, "HIGH": YELLOW, "PASS": GREEN, "INFO": GREEN}


def color_enabled(stream: TextIO, force_off: bool = False) -> bool:
    if force_off or os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def status(self, word: str) -> str:
        return self.paint(word, _STATUS.get(word, DIM))

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)
