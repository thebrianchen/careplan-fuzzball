"""OpenAI-compatible chat client (OpenRouter, vLLM, any /v1/chat/completions server) plus a
scripted fake for tests.

tool_mode="native" uses the API's tool calling. tool_mode="json" asks the model to emit
{"tool": ..., "arguments": {...}} in plain text, for providers without tool support.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict | None
    raw_arguments: str
    parse_error: str | None = None


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall]
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class ModelProfile:
    name: str
    model: str
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    temperature: float = 0.6
    top_p: float = 0.95
    max_tokens: int = 4096
    tool_mode: str = "native"         # native | json | raw (client renders the chat template, parses tool XML itself)
    quantization: str | None = None
    extra_body: dict = field(default_factory=dict)
    template_dir: str | None = None   # raw mode: directory with the model's tokenizer + chat_template.jinja
    base_url_env: str | None = None   # if this env var is set, it replaces base_url (a Fuzzball service name or endpoint URL)


def _parse_args(raw: str) -> tuple[dict | None, str | None]:
    try:
        val = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return None, f"arguments were not valid JSON: {e}"
    if not isinstance(val, dict):
        return None, "arguments must be a JSON object"
    return val, None


def _json_tool_prompt(tools: list[dict]) -> str:
    lines = ["You can call tools. To call one, reply with ONLY a JSON object, nothing else:",
             '{"tool": "<name>", "arguments": {...}}', "One call per reply. Available tools:"]
    for t in tools:
        f = t["function"]
        lines.append(f"- {f['name']}: {f['description']} Parameters (JSON Schema): {json.dumps(f['parameters'])}")
    return "\n".join(lines)


def _first_json_object(text: str) -> str | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    cand = text[start:i + 1]
                    try:
                        json.loads(cand)
                        return cand
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class ChatClient:
    def __init__(self, profile: ModelProfile, timeout: int = 300, retries: int = 6):
        self.p = profile
        self.timeout = timeout
        self.retries = retries
        self.key = os.environ.get(profile.api_key_env, "")

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        msgs = messages
        body: dict[str, Any] = {"model": self.p.model, "temperature": self.p.temperature, "top_p": self.p.top_p,
                                "max_tokens": self.p.max_tokens, **self.p.extra_body}
        if self.p.tool_mode == "native":
            if tools:                                       # vLLM rejects an empty tools list with tool_choice
                body["tools"] = tools
                body["tool_choice"] = "auto"
        else:
            msgs = [{"role": "system", "content": _json_tool_prompt(tools)}] + _flatten_for_json_mode(messages)
        body["messages"] = msgs
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        url = self.p.base_url.rstrip("/") + "/chat/completions"
        last_err = None
        for attempt in range(self.retries):
            t0 = time.time()
            try:
                r = requests.post(url, json=body, headers=headers, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504, 529):
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    retry_after = r.headers.get("Retry-After", "")
                    time.sleep(float(retry_after) if retry_after.isdigit() else min(60, 5 * 2 ** attempt))
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:500]}")
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                last_err = str(e)
                time.sleep(2 ** attempt * 3)
                continue
            return self._to_reply(data, int((time.time() - t0) * 1000))
        raise RuntimeError(f"model call failed after {self.retries} attempts: {last_err}")

    def chat_stream(self, messages: list[dict], tools: list[dict], on_delta: Callable[[int, str, str], None]) -> Reply:
        """Like chat(), but streams. on_delta(tool_index, tool_name, argument_fragment) is called as tool-call
        arguments arrive; plain content arrives as on_delta(-1, "", text). Retries only before the first byte;
        falls back to chat() in json tool mode."""
        if self.p.tool_mode != "native":
            return self.chat(messages, tools)
        body: dict[str, Any] = {"model": self.p.model, "temperature": self.p.temperature, "top_p": self.p.top_p,
                                "max_tokens": self.p.max_tokens, **self.p.extra_body, "messages": messages,
                                "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        url = self.p.base_url.rstrip("/") + "/chat/completions"
        last_err = None
        for attempt in range(self.retries):
            t0 = time.time()
            try:
                r = requests.post(url, json=body, headers=headers, timeout=self.timeout, stream=True)
                if r.status_code in (429, 500, 502, 503, 504, 529):
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:500]}")
                content, calls, usage, model = "", {}, {}, None
                for line in r.iter_lines():
                    if not line.startswith(b"data: ") or line == b"data: [DONE]":
                        continue
                    d = json.loads(line[6:])
                    usage = d.get("usage") or usage
                    model = d.get("model") or model
                    for ch in d.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            content += delta["content"]
                            on_delta(-1, "", delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            c["id"] = tc.get("id") or c["id"]
                            fn = tc.get("function") or {}
                            c["name"] = fn.get("name") or c["name"]
                            frag = fn.get("arguments") or ""
                            if frag:
                                c["args"] += frag
                                on_delta(tc.get("index", 0), c["name"], frag)
            except (requests.RequestException, ValueError) as e:
                last_err = str(e)
                time.sleep(2 ** attempt * 3)
                continue
            msg = {"content": content, "tool_calls": [{"id": c["id"] or f"call_{i}", "function": {"name": c["name"], "arguments": c["args"]}}
                                                      for i, c in sorted(calls.items())]}
            return self._to_reply({"choices": [{"message": msg}], "usage": usage, "model": model},
                                  int((time.time() - t0) * 1000))
        raise RuntimeError(f"model call failed after {self.retries} attempts: {last_err}")

    def _to_reply(self, data: dict, latency_ms: int) -> Reply:
        if "choices" not in data or not data["choices"]:
            raise RuntimeError(f"bad response: {json.dumps(data)[:300]}")
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        calls = []
        if self.p.tool_mode == "native":
            for i, tc in enumerate(msg.get("tool_calls") or []):
                raw = tc.get("function", {}).get("arguments", "")
                args, err = _parse_args(raw)
                calls.append(ToolCall(tc.get("id") or f"call_{i}", tc["function"]["name"], args, raw, err))
        else:
            js = _first_json_object(content)
            if js:
                obj = json.loads(js)
                if isinstance(obj, dict) and "tool" in obj:
                    args = obj.get("arguments", {})
                    calls.append(ToolCall("call_0", obj["tool"], args if isinstance(args, dict) else None,
                                          json.dumps(args), None if isinstance(args, dict) else "arguments must be an object"))
        return Reply(content, calls, data.get("usage", {}) or {}, latency_ms, data)


class RawClient(ChatClient):
    """Raw-completions client: renders the model's chat template locally and parses Nemotron's `<tool_call>` XML
    itself (careplan.rawtools), so tool-call arguments stream character by character. Needs the tokenizer files
    (`template_dir`) and a server exposing /v1/completions for the same model."""

    def __init__(self, profile: ModelProfile, timeout: int = 300, retries: int = 6):
        super().__init__(profile, timeout, retries)
        from transformers import AutoTokenizer      # tokenizer-only; no torch needed
        if not profile.template_dir:
            raise ValueError(f"profile {profile.name}: raw tool mode needs template_dir")
        self.tok = AutoTokenizer.from_pretrained(profile.template_dir)

    def render(self, messages: list[dict], tools: list[dict]) -> str:
        from .rawtools import prepare_messages
        kw = dict(self.p.extra_body.get("chat_template_kwargs") or {})
        return self.tok.apply_chat_template(prepare_messages(messages), tools=tools or None, tokenize=False,
                                            add_generation_prompt=True, **kw)

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        return self.chat_stream(messages, tools, lambda i, n, f: None)

    def chat_stream(self, messages: list[dict], tools: list[dict], on_delta: Callable[[int, str, str], None]) -> Reply:
        from .rawtools import ToolCallTextParser, type_arguments
        body = {k: v for k, v in self.p.extra_body.items() if k != "chat_template_kwargs"}
        body.update({"model": self.p.model, "prompt": self.render(messages, tools), "temperature": self.p.temperature,
                     "top_p": self.p.top_p, "max_tokens": self.p.max_tokens, "stream": True,
                     "stream_options": {"include_usage": True}})
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        url = self.p.base_url.rstrip("/") + "/completions"
        last_err = None
        for attempt in range(self.retries):
            t0 = time.time()
            parser = ToolCallTextParser(on_delta)
            usage, model = {}, None
            try:
                r = requests.post(url, json=body, headers=headers, timeout=self.timeout, stream=True)
                if r.status_code in (429, 500, 502, 503, 504, 529):
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:500]}")
                for line in r.iter_lines():
                    if not line.startswith(b"data: ") or line == b"data: [DONE]":
                        continue
                    d = json.loads(line[6:])
                    usage = d.get("usage") or usage
                    model = d.get("model") or model
                    for ch in d.get("choices") or []:
                        if ch.get("text"):
                            parser.feed(ch["text"])
            except (requests.RequestException, ValueError) as e:
                last_err = str(e)
                time.sleep(2 ** attempt * 3)
                continue
            content, parsed = parser.result()
            calls = []
            for i, c in enumerate(parsed):
                args, err = type_arguments(c, tools)
                calls.append(ToolCall(f"call_{i}", c.name, args, json.dumps(args) if args is not None else json.dumps(c.params), err))
            return Reply(content.strip(), calls, usage or {}, int((time.time() - t0) * 1000), {"model": model, "raw_tool_xml": True})
        raise RuntimeError(f"model call failed after {self.retries} attempts: {last_err}")


def make_client(profile: ModelProfile, **kw) -> ChatClient:
    return RawClient(profile, **kw) if profile.tool_mode == "raw" else ChatClient(profile, **kw)


def _flatten_for_json_mode(messages: list[dict]) -> list[dict]:
    """Rewrite tool-call history into plain user/assistant text for json tool mode."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "user", "content": f"Tool result for {m.get('name', 'tool')}:\n{m['content']}"})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            tc = m["tool_calls"][0]["function"]
            out.append({"role": "assistant", "content": json.dumps({"tool": tc["name"], "arguments": json.loads(tc["arguments"] or "{}")})})
        else:
            out.append({k: v for k, v in m.items() if k in ("role", "content")})
    return out


class FakeClient:
    """Scripted client for tests. `script` is a function (messages, tools) -> Reply, or a list of Replies."""

    def __init__(self, script: Callable[[list[dict], list[dict]], Reply] | list[Reply]):
        self.script = script
        self.i = 0
        self.p = ModelProfile(name="fake", model="fake-model", base_url="", api_key_env="")

    def chat(self, messages, tools) -> Reply:
        if callable(self.script):
            return self.script(messages, tools)
        r = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return r


def fake_call(name: str, args: dict, cid: str | None = None) -> ToolCall:
    raw = json.dumps(args)
    return ToolCall(cid or f"call_{name}", name, args, raw)
