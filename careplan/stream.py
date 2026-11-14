"""Stream the card while the planner writes it.

The planner's final answer is one submit_assessment tool call. ItemStreamer reads that call's JSON
arguments as they arrive and yields each plan item the moment its object closes, so the UI can show
the first flags long before the whole card is done.
"""
from __future__ import annotations

import json
import re


class ItemStreamer:
    """Feed argument fragments; get back each completed item in "items": [...] exactly once."""

    def __init__(self):
        self.buf = ""
        self.pos = 0              # next unscanned index in buf
        self.in_items = False
        self.depth = 0            # brace depth inside the items array (0 = between items)
        self.start = None         # index where the current item object began
        self.in_str = False
        self.esc = False
        self.done = False

    def feed(self, fragment: str) -> list[dict]:
        self.buf += fragment
        out = []
        if not self.in_items:
            m = re.search(r'"items"\s*:\s*\[', self.buf)
            if not m:
                return out
            self.in_items, self.pos = True, m.end()
        i = self.pos
        while i < len(self.buf) and not self.done:
            ch = self.buf[i]
            if self.in_str:
                if self.esc:
                    self.esc = False
                elif ch == "\\":
                    self.esc = True
                elif ch == '"':
                    self.in_str = False
            elif ch == '"':
                self.in_str = True
            elif ch == "{":
                if self.depth == 0:
                    self.start = i
                self.depth += 1
            elif ch == "}":
                self.depth -= 1
                if self.depth == 0 and self.start is not None:
                    try:
                        out.append(json.loads(self.buf[self.start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    self.start = None
            elif ch == "]" and self.depth == 0:
                self.done = True
            i += 1
        self.pos = i
        return out
