#!/usr/bin/env python3
"""
Claude Code -> Langfuse hook

"""

import json
import os
import re
import sys
import time
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# --- Langfuse import (fail-open) ---
try:
    from langfuse import Langfuse
except Exception:
    sys.exit(0)

# --- Paths ---
STATE_DIR = Path.home() / ".claude" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
MAX_CHARS = int(os.environ.get("CC_LANGFUSE_MAX_CHARS", "20000"))

# ----------------- Logging -----------------
def _log(level: str, message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")
    except Exception:
        # Never block
        pass

def debug(msg: str) -> None:
    if DEBUG:
        _log("DEBUG", msg)

def info(msg: str) -> None:
    _log("INFO", msg)

def warn(msg: str) -> None:
    _log("WARN", msg)

def error(msg: str) -> None:
    _log("ERROR", msg)

# ----------------- State locking (best-effort) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl  # Unix only
            deadline = time.time() + self.timeout_s
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        break
                    time.sleep(0.05)
        except Exception:
            # If locking isn't available, proceed without it.
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")

def state_key(session_id: str, transcript_path: str) -> str:
    # stable key even if session_id collides
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def session_trace_id(session_id: str) -> str:
    """Derive a stable Langfuse trace_id from session_id so all turns land in one trace."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"claude-code-session:{session_id}").hex

# ----------------- Hook payload -----------------
def read_hook_payload() -> Dict[str, Any]:
    """
    Claude Code hooks pass a JSON payload on stdin.
    This script tolerates missing/empty stdin by returning {}.
    """
    try:
        data = sys.stdin.read()
        if not data.strip():
            return {}
        return json.loads(data)
    except Exception:
        return {}

def extract_session_and_transcript(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[Path]]:
    """
    Tries a few plausible field names; exact keys can vary across hook types/versions.
    Prefer structured values from stdin over heuristics.
    """
    session_id = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("session", {}).get("id")
    )

    transcript = (
        payload.get("transcriptPath")
        or payload.get("transcript_path")
        or payload.get("transcript", {}).get("path")
    )

    if transcript:
        try:
            transcript_path = Path(transcript).expanduser().resolve()
        except Exception:
            transcript_path = None
    else:
        transcript_path = None

    return session_id, transcript_path

# ----------------- Transcript parsing helpers -----------------
def get_content(msg: Dict[str, Any]) -> Any:
    if not isinstance(msg, dict):
        return None
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")

def get_role(msg: Dict[str, Any]) -> Optional[str]:
    # Claude Code transcript lines commonly have type=user/assistant OR message.role
    t = msg.get("type")
    if t in ("user", "assistant"):
        return t
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"):
            return r
    return None

def is_tool_result(msg: Dict[str, Any]) -> bool:
    role = get_role(msg)
    if role != "user":
        return False
    content = get_content(msg)
    if isinstance(content, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
    return False

def iter_tool_results(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out

def iter_tool_uses(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out

def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": orig_len, "kept_len": len(head), "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()}

def get_model(msg: Dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("model") or "claude"
    return "claude"

def get_message_id(msg: Dict[str, Any]) -> Optional[str]:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None

# Extract usage from message
def get_usage_details(msg: Dict[str, Any]) -> Dict[str, int]:
    m = msg.get("message")
    if isinstance(m, dict):
        usage = m.get("usage")
        if isinstance(usage, dict):
            return {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
    return {}

# Classify tool type
def _classify_tool(tool_name: str) -> str:
    claude_code_builtins = {
        "Read", "Write", "Edit", "Bash", "Glob", "Grep", "LS", "MultiEdit",
        "NotebookEdit", "TodoWrite", "TodoRead", "WebFetch", "WebSearch",
        "CronCreate", "CronDelete", "CronList", "Monitor", "PushNotification",
        "RemoteTrigger", "ScheduleWakeup",
    }
    if tool_name == "Skill":
        return "skill"
    if tool_name in ("Agent", "Task"):
        return "agent"
    if tool_name in claude_code_builtins:
        return "claude-code"
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        server = parts[1] if len(parts) >= 2 else "unknown"
        return f"mcp-{server}"
    return "unknown"


def _obs_display_name(tc: Dict[str, Any]) -> str:
    """Return a human-readable span name for a tool call."""
    tool_name = tc["name"]
    tool_type = tc["type"]
    inp = tc.get("input") or {}

    if tool_type == "skill":
        skill = inp.get("skill", "unknown") if isinstance(inp, dict) else "unknown"
        return f"Skill: {skill}"

    if tool_type == "agent":
        desc = ""
        if isinstance(inp, dict):
            desc = (inp.get("description", "") or inp.get("prompt", ""))[:80]
        return f"Agent: {desc}" if desc else "Agent"

    if tool_type.startswith("mcp-"):
        server = tool_type[4:]
        parts = tool_name.split("__", 2)
        fn_name = parts[2] if len(parts) > 2 else tool_name
        return f"MCP [{server}]: {fn_name}"

    return f"Tool: {tool_name} [{tool_type}]"

# ----------------- Subagent stitching helpers -----------------
def _extract_agent_id(tool_result: Optional[str]) -> Optional[str]:
    """
    The Agent tool result includes a trailing line: 'agentId: <hex>'.
    Extract that hex ID so we can locate the subagent transcript.
    """
    if not tool_result:
        return None
    m = re.search(r'\bagentId:\s*([0-9a-f]+)', tool_result)
    return m.group(1) if m else None


def _subagent_jsonl_path(transcript_path: Path, session_id: str, agent_id: str) -> Path:
    """
    Deterministic path: <transcript_dir>/<session_id>/subagents/agent-<agent_id>.jsonl
    """
    return transcript_path.parent / session_id / "subagents" / f"agent-{agent_id}.jsonl"


def _read_all_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read an entire JSONL file at once (used for complete subagent transcripts)."""
    msgs: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                continue
    except Exception as e:
        debug(f"_read_all_jsonl failed for {path}: {e}")
    return msgs


# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: Dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, ss: SessionState) -> Tuple[List[Dict[str, Any]], SessionState]:
    """
    Reads only new bytes since ss.offset. Keeps ss.buffer for partial last line.
    Returns parsed JSON lines (best-effort) and updated state.
    """
    if not transcript_path.exists():
        return [], ss

    try:
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return [], ss

    if not chunk:
        return [], ss

    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")

    combined = ss.buffer + text
    lines = combined.split("\n")
    # last element may be incomplete
    ss.buffer = lines[-1]
    ss.offset = new_offset

    msgs: List[Dict[str, Any]] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except Exception:
            continue

    return msgs, ss

# ----------------- Turn assembly -----------------
@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, Any]

def build_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    """
    Groups incremental transcript rows into turns:
    user (non-tool-result) -> assistant messages -> (tool_result rows, possibly interleaved)
    Uses:
    - assistant message dedupe by message.id (latest row wins)
    - tool results dedupe by tool_use_id (latest wins)
    """
    turns: List[Turn] = []
    current_user: Optional[Dict[str, Any]] = None

    # assistant messages for current turn:
    assistant_order: List[str] = []             # message ids in order of first appearance (or synthetic)
    assistant_latest: Dict[str, Dict[str, Any]] = {}  # id -> latest msg

    tool_results_by_id: Dict[str, Any] = {}     # tool_use_id -> content

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_latest, tool_results_by_id, turns
        if current_user is None:
            return
        if not assistant_latest:
            return
        assistants = [assistant_latest[mid] for mid in assistant_order if mid in assistant_latest]
        turns.append(Turn(user_msg=current_user, assistant_msgs=assistants, tool_results_by_id=dict(tool_results_by_id)))

    for msg in messages:
        role = get_role(msg)

        # tool_result rows show up as role=user with content blocks of type tool_result
        if is_tool_result(msg):
            for tr in iter_tool_results(get_content(msg)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = tr.get("content")
            continue

        if role == "user":
            # new user message -> finalize previous turn
            flush_turn()

            # start a new turn
            current_user = msg
            assistant_order = []
            assistant_latest = {}
            tool_results_by_id = {}
            continue

        if role == "assistant":
            if current_user is None:
                # ignore assistant rows until we see a user message
                continue

            mid = get_message_id(msg) or f"noid:{len(assistant_order)}"
            if mid not in assistant_latest:
                assistant_order.append(mid)
            assistant_latest[mid] = msg
            continue

        # ignore unknown rows

    # flush last
    flush_turn()
    return turns

# ----------------- Langfuse emit -----------------
def _tool_calls_from_assistants(assistant_msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for am in assistant_msgs:
        for tu in iter_tool_uses(get_content(am)):
            tid = tu.get("id") or ""
            tool_name = tu.get("name") or "unknown"
            calls.append({
                "id": str(tid),
                "name": tool_name,
                "type": _classify_tool(tool_name),
                "input": tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {},
            })
    return calls


def _emit_tool_spans(langfuse: Langfuse, tool_calls: List[Dict[str, Any]], emitted_agents: Set[str], transcript_path: Path, session_id: str) -> None:
    """Emit all tool calls as child spans of the currently-active span."""
    for tc in tool_calls:
        in_obj = tc["input"]
        if isinstance(in_obj, str):
            in_obj, in_meta = truncate_text(in_obj)
        else:
            in_meta = None

        obs_name = _obs_display_name(tc)

        meta: Dict[str, Any] = {
            "tool_name": tc["name"],
            "tool_type": tc["type"],
            "tool_id": tc["id"],
            "input_meta": in_meta,
            "output_meta": tc.get("output_meta"),
        }
        if tc["type"] == "skill" and isinstance(tc.get("input"), dict):
            meta["skill_name"] = tc["input"].get("skill")
            meta["skill_args"] = tc["input"].get("args")
        elif tc["type"] == "agent" and isinstance(tc.get("input"), dict):
            meta["agent_description"] = tc["input"].get("description")
            meta["agent_subagent_type"] = tc["input"].get("subagent_type")
        elif tc["type"].startswith("mcp-"):
            meta["mcp_server"] = tc["type"][4:]
            meta["mcp_function"] = (
                tc["name"].split("__", 2)[2]
                if tc["name"].count("__") >= 2
                else tc["name"]
            )

        with langfuse.start_as_current_span(
            name=obs_name,
            input=in_obj,
            output=tc.get("output"),
            metadata=meta,
        ):
            # For Agent calls: emit subagent transcript as nested child turns
            if tc["type"] == "agent":
                agent_id = _extract_agent_id(tc.get("output", ""))
                if agent_id:
                    sub_path = _subagent_jsonl_path(transcript_path, session_id, agent_id)
                    _emit_subagent_turns(langfuse, agent_id, sub_path, emitted_agents)


def _emit_subagent_turns(langfuse: Langfuse, agent_id: str, subagent_path: Path, emitted_agents: Set[str]) -> None:
    """
    Read and emit the subagent transcript as nested child spans of the active Agent span.
    Skips if the agent was already emitted this session (idempotent across hook firings).
    """
    if agent_id in emitted_agents:
        debug(f"Subagent {agent_id} already emitted, skipping")
        return
    if not subagent_path.exists():
        debug(f"Subagent transcript not found: {subagent_path}")
        emitted_agents.add(agent_id)
        return

    msgs = _read_all_jsonl(subagent_path)
    turns = build_turns(msgs)
    emitted_agents.add(agent_id)

    if not turns:
        debug(f"Subagent {agent_id}: no turns parsed")
        return

    for i, turn in enumerate(turns, start=1):
        try:
            _emit_single_subagent_turn(langfuse, agent_id, i, turn, emitted_agents, subagent_path)
        except Exception as e:
            debug(f"Subagent {agent_id} turn {i} emit failed: {e}")

    debug(f"Emitted {len(turns)} turns for subagent {agent_id}")


def _emit_single_subagent_turn(
    langfuse: Langfuse,
    agent_id: str,
    turn_num: int,
    turn: "Turn",
    emitted_agents: Set[str],
    subagent_path: Path,
) -> None:
    """Emit one subagent turn as a child span within the active Agent span."""
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    last_assistant = turn.assistant_msgs[-1]
    assistant_text_raw = extract_text(get_content(last_assistant))
    assistant_text, assistant_text_meta = truncate_text(assistant_text_raw)

    model = get_model(turn.assistant_msgs[0])
    usage_details = get_usage_details(last_assistant)
    tool_calls = _tool_calls_from_assistants(turn.assistant_msgs)

    for c in tool_calls:
        if c["id"] and c["id"] in turn.tool_results_by_id:
            out_raw = turn.tool_results_by_id[c["id"]]
            out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
            out_trunc, out_meta = truncate_text(out_str)
            c["output"] = out_trunc
            c["output_meta"] = out_meta
        else:
            c["output"] = None

    with langfuse.start_as_current_span(
        name=f"Subagent Turn {turn_num}",
        input={"role": "user", "content": user_text},
        metadata={"agent_id": agent_id, "turn_number": turn_num, "user_text": user_text_meta},
    ):
        with langfuse.start_as_current_generation(
            name="Claude Response",
            model=model,
            input={"role": "user", "content": user_text},
            output={"role": "assistant", "content": assistant_text},
            usage_details=usage_details if usage_details else None,
            metadata={"assistant_text": assistant_text_meta, "tool_count": len(tool_calls)},
        ):
            pass

        # subagent_path parent is <session_id>/subagents/ — the parent transcript dir is two levels up
        parent_transcript_dir = subagent_path.parent.parent.parent
        # session_id is the directory name one level up from subagents/
        sub_session_id = subagent_path.parent.parent.name
        synthetic_transcript = parent_transcript_dir / f"{sub_session_id}.jsonl"
        _emit_tool_spans(langfuse, tool_calls, emitted_agents, synthetic_transcript, sub_session_id)


def emit_turn(langfuse: Langfuse, session_id: str, trace_id: str, turn_num: int, turn: Turn, transcript_path: Path, emitted_agents: Set[str]) -> None:
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    last_assistant = turn.assistant_msgs[-1]
    assistant_text_raw = extract_text(get_content(last_assistant))
    assistant_text, assistant_text_meta = truncate_text(assistant_text_raw)

    model = get_model(turn.assistant_msgs[0])
    usage_details = get_usage_details(last_assistant)
    tool_calls = _tool_calls_from_assistants(turn.assistant_msgs)

    for c in tool_calls:
        if c["id"] and c["id"] in turn.tool_results_by_id:
            out_raw = turn.tool_results_by_id[c["id"]]
            out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
            out_trunc, out_meta = truncate_text(out_str)
            c["output"] = out_trunc
            c["output_meta"] = out_meta
        else:
            c["output"] = None

    with langfuse.start_as_current_span(
        name=f"Claude Code - Turn {turn_num}",
        trace_context={"trace_id": trace_id},
        input={"role": "user", "content": user_text},
        metadata={
            "source": "claude-code",
            "session_id": session_id,
            "turn_number": turn_num,
            "transcript_path": str(transcript_path),
            "user_text": user_text_meta,
        },
    ):
        langfuse.update_current_trace(
            session_id=session_id,
            name=f"Claude Code Session",
        )

        with langfuse.start_as_current_generation(
            name="Claude Response",
            model=model,
            input={"role": "user", "content": user_text},
            output={"role": "assistant", "content": assistant_text},
            usage_details=usage_details if usage_details else None,
            metadata={"assistant_text": assistant_text_meta, "tool_count": len(tool_calls)},
        ):
            pass

        _emit_tool_spans(langfuse, tool_calls, emitted_agents, transcript_path, session_id)

# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Hook started")

    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        return 0

    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("CC_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"

    if not public_key or not secret_key:
        return 0

    payload = read_hook_payload()
    session_id, transcript_path = extract_session_and_transcript(payload)

    if not session_id or not transcript_path:
        # No structured payload; fail open (do not guess)
        debug("Missing session_id or transcript_path from hook payload; exiting.")
        return 0

    if not transcript_path.exists():
        debug(f"Transcript path does not exist: {transcript_path}")
        return 0

    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(session_id, str(transcript_path))
            ss = load_session_state(state, key)

            # emitted_agents: tracks which subagent IDs have been stitched into Langfuse
            # stored globally (agentIds are unique per invocation)
            emitted_agents: Set[str] = set(state.get("emitted_subagents", []))

            msgs, ss = read_new_jsonl(transcript_path, ss)
            if not msgs:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            turns = build_turns(msgs)
            if not turns:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            trace_id = session_trace_id(session_id)
            emitted = 0
            for t in turns:
                emitted += 1
                turn_num = ss.turn_count + emitted
                try:
                    emit_turn(langfuse, session_id, trace_id, turn_num, t, transcript_path, emitted_agents)
                except Exception as e:
                    debug(f"emit_turn failed: {e}")

            ss.turn_count += emitted
            write_session_state(state, key, ss)
            state["emitted_subagents"] = list(emitted_agents)
            save_state(state)

        try:
            langfuse.flush()
        except Exception:
            pass

        dur = time.time() - start
        info(f"Processed {emitted} turns in {dur:.2f}s (session={session_id})")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

    finally:
        try:
            langfuse.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
