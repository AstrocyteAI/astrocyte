"""Turn an agent transcript (JSONL) into conversation turns to capture.

Claude Code's format is described first; Antigravity's further down.

What is kept, per the transcript structure measured on Claude Code 2.1:

* **User prompts** — ``type == "user"`` lines with string content whose
  ``origin.kind`` is ``"human"`` (older transcripts lack ``origin``; untagged
  plain text counts as human). Task notifications, command wrappers and tool
  results are not the user speaking.
* **Assistant prose** — ``text`` blocks only. ``thinking`` is internal and
  ``tool_use`` / ``tool_result`` are bulky and can carry file contents.
* **Files the turn touched** — the paths of the assistant's file tools
  (``Read``, ``Edit``, ``Write``, ``MultiEdit``, ``NotebookEdit``), not their
  contents: what ties a captured turn to the code it was about.

Hook output (including memories *we* inject) lives in separate ``attachment``
lines, so it is never re-captured: there is no feedback loop.

Reading is incremental from a byte offset. The returned offset never passes a
half-written line or a turn whose answer has not arrived, so the next read
resumes exactly where this one stopped — no turn is lost or captured twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Long messages are clipped: memory wants the gist, not a 40 KB paste.
MAX_USER_CHARS = 2_000
MAX_ASSISTANT_CHARS = 4_000

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_WRAPPER = re.compile(r"^\s*<(command-name|command-message|command-args|local-command-[a-z]+|task-notification)>")


@dataclass
class Turn:
    user: str
    assistant: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    files: list[str] = field(default_factory=list)  # touched, in first-touch order

    def render(self) -> str:
        """Conversation-engine input (``**role**: text``)."""
        answer = _clip("\n\n".join(self.assistant), MAX_ASSISTANT_CHARS)
        return f"**user**: {_clip(self.user, MAX_USER_CHARS)}\n\n**assistant**: {answer}"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _human_prompt(line: dict) -> str | None:
    if line.get("type") != "user" or line.get("isSidechain") or line.get("isMeta"):
        return None
    content = (line.get("message") or {}).get("content")
    if isinstance(content, list):  # text blocks typed by a human; tool results are not
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    origin = line.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    if _WRAPPER.match(content):
        return None
    text = _REMINDER.sub("", content).strip()
    return text or None


def _assistant_text(line: dict) -> list[str]:
    if line.get("type") != "assistant" or line.get("isSidechain"):
        return []
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]


_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}


def _touched_files(line: dict) -> list[str]:
    """Paths the assistant's file tools name in this line (Claude Code)."""
    if line.get("type") != "assistant" or line.get("isSidechain"):
        return []
    content = (line.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    found = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and (key := _FILE_TOOLS.get(block.get("name"))):
            path = (block.get("input") or {}).get(key)
            if isinstance(path, str) and path:
                found.append(path)
    return found


# ── Antigravity (transcript.jsonl under brain/<conversation>/) ───────────
#
# Steps, measured on agy 1.2 and the Antigravity app: the user's message is a
# USER_INPUT step whose content wraps the text in <USER_REQUEST> (followed by
# <ADDITIONAL_METADATA> the app adds); replies are PLANNER_RESPONSE steps with
# string content (tool-calling ones have none). Context our hooks inject is an
# EPHEMERAL_MESSAGE step, so it is never re-captured.

_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def antigravity_prompt(line: dict) -> str | None:
    if line.get("type") != "USER_INPUT" or line.get("source") not in (None, "USER_EXPLICIT"):
        return None
    content = line.get("content")
    if not isinstance(content, str):
        return None
    m = _USER_REQUEST.search(content)
    text = (m.group(1) if m else content).strip()
    return text or None


# File tools by name, and the argument holding the path. The envelope —
# PLANNER_RESPONSE ``tool_calls: [{"name", "args"}]`` with each argument value
# JSON-encoded as a string — is measured on agy 1.2; the tool names and their
# path arguments come from Antigravity's hook documentation (no file-tool
# call has been recorded here yet).
_ANTIGRAVITY_FILE_TOOLS = {"view_file": "AbsolutePath", "write_to_file": "TargetFile",
                           "replace_file_content": "TargetFile", "multi_replace_file_content": "TargetFile"}


def _antigravity_files(line: dict) -> list[str]:
    if line.get("type") != "PLANNER_RESPONSE" or not isinstance(line.get("tool_calls"), list):
        return []
    found = []
    for call in line["tool_calls"]:
        if not isinstance(call, dict) or not (key := _ANTIGRAVITY_FILE_TOOLS.get(call.get("name"))):
            continue
        value = (call.get("args") or {}).get(key)
        if isinstance(value, str) and value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, str) and value:
            found.append(value)
    return found


def _antigravity_reply(line: dict) -> list[str]:
    if line.get("type") != "PLANNER_RESPONSE":
        return []
    content = line.get("content")
    return [content] if isinstance(content, str) and content.strip() else []


def read_new_turns(path: str | Path, offset: int = 0, *, antigravity: bool = False) -> tuple[list[Turn], int]:
    """Complete turns after ``offset``, and the offset to resume from."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return [], offset
    if offset > size:  # transcript was rewritten or truncated; start over
        offset = 0
    with p.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()

    turns: list[Turn] = []
    current: Turn | None = None
    current_start = offset  # where the in-progress turn began
    resume = offset
    pos = offset
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break  # half-written line: leave it for the next read
        line_start, pos = pos, pos + len(raw)  # pos only ever covers complete lines
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            resume = pos if current is None else resume
            continue
        if not isinstance(line, dict):
            continue
        prompt = antigravity_prompt(line) if antigravity else _human_prompt(line)
        if prompt is not None:
            if current is not None and current.assistant:
                turns.append(current)
                resume = line_start
            when = line.get("created_at") if antigravity else line.get("timestamp")
            current, current_start = Turn(user=prompt, started_at=_parse_time(when)), line_start
            continue
        texts = _antigravity_reply(line) if antigravity else _assistant_text(line)
        if texts and current is not None:
            current.assistant.extend(texts)
        if current is not None:
            touched = _antigravity_files(line) if antigravity else _touched_files(line)
            current.files.extend(f for f in touched if f not in current.files)
        if current is None:
            resume = pos  # nothing pending: safe to move past this line

    if current is not None and current.assistant:
        # Stop fires when the assistant has finished, so the last turn is done.
        turns.append(current)
        resume = pos
    elif current is not None:
        resume = current_start  # prompt without an answer yet: re-read next time
    return turns, resume
