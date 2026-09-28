"""Reader for opencode session JSON files.

The opencode session format is defined in
`opencode_reference/src/session/message-v2.ts`. Each session is a
sequence of messages with parts (text, tool-call, tool-result).
For our purposes we only need:

- session id
- per-message role (user / assistant)
- text content (concatenated text parts)
- tool-call name + argv (assistant messages only)
- tool-result stdout / stderr (assistant messages only)

Everything else is dropped at this boundary. The adapter is
intentionally permissive: unknown fields are ignored, not rejected.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class OpencodeToolCall:
    name: str
    argv: List[str] = field(default_factory=list)


@dataclass
class OpencodeMessage:
    """A single opencode message after adapter-side normalisation."""

    message_id: str
    role: str  # "user" | "assistant"
    text: str = ""
    tool_call: Optional[OpencodeToolCall] = None
    tool_stdout: str = ""
    tool_stderr: str = ""
    timestamp: float = 0.0

    def to_lingyi_event(self, session_id: str) -> Dict[str, Any]:
        """Convert to the shape `KylinAgentMemory.store()` expects."""
        parts = [self.text]
        if self.tool_call:
            parts.append(
                f"[tool_call] {self.tool_call.name} argv={self.tool_call.argv}"
            )
        if self.tool_stdout:
            parts.append(f"[stdout] {self.tool_stdout[:1024]}")
        if self.tool_stderr:
            parts.append(f"[stderr] {self.tool_stderr[:512]}")
        body = " | ".join(p for p in parts if p)
        return {
            "content": body,
            "tool": self.tool_call.name if self.tool_call else "",
            "argv": self.tool_call.argv if self.tool_call else [],
            "stdout": self.tool_stdout,
            "stderr": self.tool_stderr,
            "returncode": 0 if not self.tool_stderr else 1,
            "latency_ms": 0.0,
            "session_id": session_id,
        }


@dataclass
class OpencodeSession:
    session_id: str
    messages: List[OpencodeMessage] = field(default_factory=list)


def _coerce_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _coerce_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for v in value:
        if isinstance(v, str):
            out.append(v)
        else:
            out.append(str(v))
    return out


def _parse_message(raw: Dict[str, Any]) -> OpencodeMessage:
    """Parse a single opencode `Info` row into our flat shape."""
    role = _coerce_str(raw.get("role"), "assistant")
    mid = _coerce_str(raw.get("id"), "")
    ts_raw = raw.get("time", {})
    if isinstance(ts_raw, dict):
        ts = float(ts_raw.get("created", 0) or 0)
    elif isinstance(ts_raw, (int, float)):
        ts = float(ts_raw)
    else:
        ts = 0.0

    parts = raw.get("parts", [])
    text_parts: List[str] = []
    tool_call: Optional[OpencodeToolCall] = None
    tool_stdout = ""
    tool_stderr = ""

    for part in parts:
        ptype = _coerce_str(part.get("type"), "")
        if ptype == "text":
            text_parts.append(_coerce_str(part.get("text"), ""))
        elif ptype == "tool":
            # opencode tool calls have a `name` and either `args` or
            # `input`. We accept either.
            name = _coerce_str(part.get("name"), "")
            args = _coerce_list(part.get("args")) or _coerce_list(part.get("input"))
            tool_call = OpencodeToolCall(name=name, argv=args)
        elif ptype == "tool_result":
            tool_stdout = _coerce_str(part.get("output"), "")
            tool_stderr = _coerce_str(part.get("error"), "")
        # other part types (file, patch, agent, step-start, etc.) are
        # ignored on purpose — they are formatting sugar.

    return OpencodeMessage(
        message_id=mid,
        role=role,
        text="\n".join(t for t in text_parts if t),
        tool_call=tool_call,
        tool_stdout=tool_stdout,
        tool_stderr=tool_stderr,
        timestamp=ts,
    )


def read_session_json(path: str | Path) -> OpencodeSession:
    """Parse a single opencode session JSON file.

    Accepts either:
      - a single Info row (one message), or
      - an array of Info rows (a full session), or
      - an object with a `messages` key.
    """
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))

    if isinstance(raw, list):
        messages_raw = raw
        session_id = _coerce_str(raw[0].get("sessionID"), "") if raw else ""
    elif isinstance(raw, dict):
        if "messages" in raw and isinstance(raw["messages"], list):
            messages_raw = raw["messages"]
            session_id = _coerce_str(raw.get("id"), "")
        else:
            messages_raw = [raw]
            session_id = _coerce_str(raw.get("sessionID"), "")
    else:
        raise ValueError(f"unrecognised opencode session format: {type(raw)}")

    if not session_id:
        session_id = "oc_" + p.stem

    messages = [_parse_message(m) for m in messages_raw if isinstance(m, dict)]
    return OpencodeSession(session_id=session_id, messages=messages)