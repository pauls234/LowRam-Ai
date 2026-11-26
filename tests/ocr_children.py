"""Stand-ins for the text-recognition child process (``ocr._child``) – run in a real spawned process.

Kept apart from the tests so the child only imports this small module.
"""

from __future__ import annotations

import os
import time


def talker(source, kind, pages, conn):
    """Answers like Vision would: two pages, then done."""
    conn.send(("pages", 2))
    conn.send(("page", 1, "eins"))
    conn.send(("page", 2, "zwei"))
    conn.send(("done",))
    conn.close()


def sleeper(source, kind, pages, conn):
    """Starts, then hangs on its first page."""
    conn.send(("pages", 1))
    time.sleep(60)


def crasher(source, kind, pages, conn):
    """Dies in the middle of the work (like a framework crash on a damaged file)."""
    conn.send(("pages", 1))
    os._exit(3)


def hog(source, kind, pages, conn):
    """Takes more and more memory (like a crafted file) without answering."""
    conn.send(("pages", 1))
    blocks = []
    for _ in range(12):
        block = bytearray(50 * 1024 * 1024)
        for offset in range(0, len(block), 4096):  # really in RAM, not only reserved
            block[offset] = 1
        blocks.append(block)
        time.sleep(0.2)
    time.sleep(60)
