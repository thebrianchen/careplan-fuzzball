"""Client-side tool-call parsing for Nemotron 3's XML format, so arguments stream character by character.

vLLM's `qwen3_coder` parser (0.27.1) releases a tool-call parameter only when it closes, so a card returned as
one `submit_assessment(items=[...])` call reaches the UI all at once. Here the planner renders the chat template
itself (the model's tokenizer files), calls `/v1/completions`, and parses the generated text:

    <tool_call>
    <function=NAME>
    <parameter=KEY>
    VALUE (string, or JSON for array/object/number/boolean parameters)
    </parameter>
    </function>
    </tool_call>

`ToolCallTextParser.feed()` emits content deltas and, for each parameter, `{"KEY": ` followed by the value as it
arrives, so `careplan.stream.ItemStreamer` sees the same byte stream it would get from a JSON tool call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

OPEN_CALL, OPEN_FUNC, OPEN_PARAM = "<tool_call>", "<function=", "<parameter="
CLOSE_PARAM, CLOSE_FUNC, CLOSE_CALL = "</parameter>", "</function>", "</tool_call>"
HOLD = len("\n" + CLOSE_PARAM)      # never emit the tail that could still turn into a closing tag

OnDelta = Callable[[int, str, str], None]


@dataclass
class ParsedCall:
    name: str
    params: dict[str, str] = field(default_factory=dict)


def _could_prefix(buf: str, tags: tuple[str, ...]) -> bool:
    return any(t.startswith(buf) for t in tags)


class ToolCallTextParser:
    """Incremental parser. feed() with text fragments; result() when the stream ends."""

    def __init__(self, on_delta: OnDelta | None = None):
        self.on_delta = on_delta or (lambda i, n, f: None)
        self.buf = ""
        self.state = "content"
        self.content = ""
        self.calls: list[ParsedCall] = []
        self.cur: ParsedCall | None = None
        self.key = ""
        self.value = ""

    def _emit_content(self, text: str) -> None:
        if text:
            self.content += text
            self.on_delta(-1, "", text)

    def _emit_value(self, text: str) -> None:
        if text and self.cur is not None:
            self.value += text
            self.on_delta(len(self.calls), self.cur.name, text)

    def feed(self, text: str) -> None:
        self.buf += text
        while True:
            if self.state == "content":
                i = self.buf.find(OPEN_CALL)
                if i >= 0:
                    self._emit_content(self.buf[:i])
                    self.buf = self.buf[i + len(OPEN_CALL):]
                    self.state = "call"
                    continue
                keep = 0                        # hold back a possible partial "<tool_call>"
                for k in range(min(len(OPEN_CALL) - 1, len(self.buf)), 0, -1):
                    if OPEN_CALL.startswith(self.buf[-k:]):
                        keep = k
                        break
                self._emit_content(self.buf[:len(self.buf) - keep])
                self.buf = self.buf[len(self.buf) - keep:]
                return
            if self.state == "call":
                s = self.buf.lstrip()
                if s.startswith(OPEN_FUNC):
                    j = s.find(">")
                    if j < 0:
                        return
                    self.cur = ParsedCall(s[len(OPEN_FUNC):j])
                    self.buf = s[j + 1:]
                    self.state = "func"
                    continue
                if _could_prefix(s, (OPEN_FUNC,)) or not s:
                    return
                self.buf = s[1:]                 # stray character inside <tool_call>; skip it
                continue
            if self.state == "func":
                s = self.buf.lstrip()
                if s.startswith(OPEN_PARAM):
                    j = s.find(">")
                    if j < 0:
                        return
                    self.key, self.value = s[len(OPEN_PARAM):j], ""
                    rest = s[j + 1:]
                    if rest.startswith("\n"):
                        rest = rest[1:]
                    elif not rest:               # the newline after ">" may not have arrived yet
                        self.buf = s
                        return
                    self.buf = rest
                    self.state = "param"
                    assert self.cur is not None
                    self.on_delta(len(self.calls), self.cur.name, json.dumps(self.key) + ": ")
                    continue
                if s.startswith(CLOSE_FUNC):
                    s = s[len(CLOSE_FUNC):].lstrip()
                    if s.startswith(CLOSE_CALL):
                        s = s[len(CLOSE_CALL):].lstrip("\n")
                    elif _could_prefix(s, (CLOSE_CALL,)):
                        self.buf = CLOSE_FUNC + s   # wait for the rest of </tool_call>
                        return
                    if self.cur is not None:
                        self.calls.append(self.cur)
                    self.cur = None
                    self.buf = s
                    self.state = "content"
                    continue
                if _could_prefix(s, (OPEN_PARAM, CLOSE_FUNC)) or not s:
                    self.buf = s
                    return
                self.buf = s[1:]
                continue
            if self.state == "param":
                i = self.buf.find(CLOSE_PARAM)
                if i >= 0:
                    seg = self.buf[:i]
                    if seg.endswith("\n"):
                        seg = seg[:-1]
                    self._emit_value(seg)
                    assert self.cur is not None
                    self.cur.params[self.key] = self.value
                    self.buf = self.buf[i + len(CLOSE_PARAM):]
                    self.state = "func"
                    continue
                safe = len(self.buf) - HOLD
                if safe > 0:
                    self._emit_value(self.buf[:safe])
                    self.buf = self.buf[safe:]
                return

    def result(self) -> tuple[str, list[ParsedCall]]:
        """Flush at end of stream: unterminated text becomes content or the last parameter value."""
        if self.state == "content":
            self._emit_content(self.buf)
        elif self.state == "param" and self.cur is not None:
            self._emit_value(self.buf.rstrip("\n"))
            self.cur.params[self.key] = self.value
            self.calls.append(self.cur)
        elif self.cur is not None and self.cur.params:
            self.calls.append(self.cur)
        self.buf, self.cur = "", None
        return self.content, self.calls


def type_arguments(call: ParsedCall, tools: list[dict]) -> tuple[dict | None, str | None]:
    """Convert the XML string values to the JSON types the tool schema declares (what vLLM's parser does)."""
    props: dict[str, Any] = {}
    for t in tools:
        f = t.get("function", {})
        if f.get("name") == call.name:
            props = f.get("parameters", {}).get("properties", {}) or {}
    out: dict[str, Any] = {}
    for k, v in call.params.items():
        typ = props.get(k, {}).get("type")
        types = set(typ) if isinstance(typ, list) else {typ}
        if types & {"object", "array", "integer", "number", "boolean"} or k not in props:
            try:
                out[k] = json.loads(v)
                continue
            except json.JSONDecodeError as e:
                if k in props and not types & {"string"}:
                    return None, f"parameter {k!r} is not valid JSON: {e}"
        out[k] = v
    return out, None


def prepare_messages(messages: list[dict]) -> list[dict]:
    """Chat templates expect tool-call arguments as objects, not JSON strings (vLLM converts them the same way)."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m = dict(m)
            calls = []
            for c in m["tool_calls"]:
                c = json.loads(json.dumps(c))
                a = c.get("function", {}).get("arguments")
                if isinstance(a, str):
                    try:
                        c["function"]["arguments"] = json.loads(a or "{}")
                    except json.JSONDecodeError:
                        c["function"]["arguments"] = {"_raw": a}
                calls.append(c)
            m["tool_calls"] = calls
        out.append(m)
    return out
