from __future__ import annotations

import sys
import time


def debug(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[fdedup][{timestamp}] {message}", file=sys.stdout, flush=True)
