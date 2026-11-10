"""Release Title shape, shared by the V4 lint and the item model (#459).

Ruling on #459 (2026-10-22): the program name is appended — "Title | Program"
— except for the programs in ``PREFIX_PROGRAMS``, which prepend it for
analytics scripting. Length limits measure the title part; ``full_max``
caps the whole string.
"""

from __future__ import annotations

from typing import Optional

TITLE_SEPARATOR = " | "
PREFIX_PROGRAMS: tuple[str, ...] = ("Wisconsin Life",)


def split_title(title: str) -> tuple[str, Optional[str]]:
    """Split a Release Title into (title part, program name or None).

    "Wisconsin Life | Title" -> ("Title", "Wisconsin Life");
    "Title | Program" -> ("Title", "Program"); no separator -> (title, None).
    """
    for program in PREFIX_PROGRAMS:
        prefix = program + TITLE_SEPARATOR
        if title.startswith(prefix):
            return title[len(prefix) :], program
    if TITLE_SEPARATOR in title:
        part, program = title.rsplit(TITLE_SEPARATOR, 1)
        return part, program
    return title, None
