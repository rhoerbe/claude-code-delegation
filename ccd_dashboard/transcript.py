"""Reading a participant session's own transcript (ADR-0008).

The dashboard's working tree, status, cost and content all come from here —
never from the broker, which stays content-free and owes a delivered message
nothing (ADR-0003). The broker's only contribution is the correlation: a
handle's self-asserted `cwd` and `session` (see `ccd_broker.broker`), from
which the transcript file is named deterministically:

    <root>/<cwd with '/' and '.' replaced by '-'>/<session>.jsonl

`root` is Claude Code's own transcript directory (`$CLAUDE_CONFIG_DIR/projects`,
default `~/.claude/projects`) — the one Claude-Code-shaped assumption in this
repo, sanctioned by ADR-0008 and overridable for tests and other layouts.

A participant that announced neither field, or that runs on a backend keeping
no such transcript at all, simply has no transcript: every field sourced from
one then reads "unknown", which is the honest answer rather than a guess.

Note the parser deliberately returns more than any one view displays — the
first message, the last task and an issue tag come back whether or not the
caller wants them. ADR-0008 flags this: keeping content out of an unscoped
view is a property the *renderer* maintains (see `ccd_dashboard.dashboard`),
not one this module enforces.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: One bounded line, per ADR-0008 ("What content is"). Longer than a terminal
#: row on purpose — the renderer, not the parser, decides where to cut.
CONTENT_MAX_CHARS = 400

#: Wrappers Claude Code puts around its own machinery. A user record that is
#: only one of these is not the human's goal, so `first_message` skips it.
_MACHINERY_PREFIXES = (
    "<local-command-",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-memory-input>",
    "Caveat:",
)

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_ISSUE_RE = re.compile(r"(?:issues/|#)(\d{1,6})\b")

#: A `ccd recv` *invocation* in a Bash command — at a command position, after
#: an optional path and optional environment assignments. Deliberately not a
#: bare search for the two words: a session working on ccd itself writes them
#: in comments and heredocs all day, and every such command would otherwise
#: read as a parked worker.
_RECV_RE = re.compile(
    r"(?:^|[\n;&|(])\s*(?:\w+=\S*\s+)*(?:[\w./~+-]*/)?ccd\s+recv\b", re.M)

#: What `ccd recv` prints when it did not deliver a message.
_RECV_FAILURE_PREFIX = "ccd recv:"


def transcript_root() -> Path:
    """Where Claude Code keeps per-project transcript directories."""
    override = os.environ.get("CCD_TRANSCRIPT_ROOT")
    if override:
        return Path(override)
    config = os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
    return Path(config) / "projects"


def encode_project_dir(cwd: str) -> str:
    """Claude Code's project-directory name for a working directory.

    `/home/a/devl/x` -> `-home-a-devl-x`; a dot becomes a dash too, which is
    why `.../.claude/worktrees/...` lands as `...--claude-worktrees-...`.
    """
    return cwd.replace("/", "-").replace(".", "-")


@dataclass
class Located:
    """Which transcript belongs to a handle, and how confidently."""

    path: Optional[Path] = None
    #: "session-id" — named by the announced session, exact.
    #: "mtime"      — newest transcript in the announced tree, a guess.
    #: None         — nothing to read.
    resolved_by: Optional[str] = None

    def __bool__(self) -> bool:
        return self.path is not None


def locate(cwd: Optional[str], session: Optional[str],
           root: Optional[Path] = None) -> Located:
    """Find the transcript for an announced (cwd, session) pair.

    With a session id this is exact. Without one it falls back to the most
    recently modified transcript in that working tree — a guess, and flagged
    as such, because two sessions in one tree are indistinguishable from here.
    """
    if not cwd:
        return Located()
    root = root or transcript_root()
    project = root / encode_project_dir(cwd)
    if session:
        path = project / f"{session}.jsonl"
        if path.is_file():
            return Located(path, "session-id")
        # An announced session id that has no file is not a reason to guess a
        # different session's transcript: say nothing rather than the wrong
        # thing.
        return Located()
    if not project.is_dir():
        return Located()
    candidates = sorted(
        (p for p in project.glob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return Located()
    return Located(candidates[0], "mtime")


@dataclass
class Facts:
    """Everything one transcript can say about its session."""

    #: "parked" (blocked in `ccd recv` — the ADR-0002 idle state), "working"
    #: (a tool call outstanding, or a prompt not yet answered), "idle" (turn
    #: finished, nothing pending), "unknown" (no transcript).
    status: str = "unknown"
    last_activity: Optional[str] = None
    branch: Optional[str] = None
    models: list = field(default_factory=list)
    #: The most recently reported model/effort (claude-code-delegation#13) —
    #: `model` from the latest assistant record's own `message.model`, same
    #: source as `models` but just the current one; `effort` from that
    #: record's top-level `effort` field, Claude Code's post-downgrade
    #: report of what actually served the turn. Both are the "observed"
    #: half of drift detection (`ccd ls`): compared against a handle's
    #: *declared* slot/effort/model, never written back anywhere.
    model: Optional[str] = None
    effort: Optional[str] = None
    turns: int = 0
    tokens: dict = field(default_factory=dict)
    #: The same tokens split per model, so a price table can be applied to a
    #: session that changed model mid-run.
    by_model: dict = field(default_factory=dict)
    #: Content — see the module docstring: returned whether wanted or not.
    first_message: Optional[str] = None
    last_task: Optional[str] = None
    #: The issue reference in `first_message`, if it names one. Use
    #: `issue_tag()` on whichever line is actually being shown.
    issue: Optional[str] = None


def _text_blocks(content: Any) -> list:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b.get("text") or "" for b in content
            if isinstance(b, dict) and b.get("type") == "text"]


def _clean(text: str) -> str:
    text = _SYSTEM_REMINDER_RE.sub("", text)
    return " ".join(text.split())


def _is_machinery(text: str) -> bool:
    return not text or text.startswith(_MACHINERY_PREFIXES)


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") or "" for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts)
    return ""


def parse(path: Path) -> Facts:
    """Read one transcript into the facts the dashboard reports.

    Whole-file, single pass: the first user message is at the head and the
    pending-tool state is at the tail, so there is nothing to be gained by
    reading one end only. Cost sums *every* assistant record including
    sidechains (a subagent's tokens are billed to this session); status and
    content ignore sidechains, which are not what the session is doing at the
    top level.
    """
    facts = Facts(status="idle")
    tokens = {
        "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
    }
    models: list = []
    by_model: dict = {}
    pending: dict = {}          # tool_use_id -> True if it is a `ccd recv`
    last_record_role: Optional[str] = None

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return Facts()

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue

            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            message = rec.get("message")
            if not isinstance(message, dict):
                continue

            if rec.get("timestamp"):
                facts.last_activity = rec["timestamp"]
            if rec.get("gitBranch"):
                facts.branch = rec["gitBranch"]

            content = message.get("content")

            if rtype == "assistant":
                model = message.get("model")
                if model:
                    facts.model = str(model)
                # Claude Code's own post-downgrade report of what effort
                # actually served this turn (hosting#131's verification: a
                # real transcript records it top-level, alongside
                # `message`/`timestamp`/`gitBranch` — not nested inside
                # `message` the way `model` is).
                if rec.get("effort"):
                    facts.effort = str(rec["effort"])
                usage = message.get("usage")
                if isinstance(usage, dict):
                    facts.turns += 1
                    used = {
                        "input": int(usage.get("input_tokens") or 0),
                        "output": int(usage.get("output_tokens") or 0),
                        "cache_read": int(
                            usage.get("cache_read_input_tokens") or 0),
                        "cache_creation": int(
                            usage.get("cache_creation_input_tokens") or 0),
                    }
                    for key, value in used.items():
                        tokens[key] += value
                    per = by_model.setdefault(str(model or "unknown"),
                                              dict.fromkeys(used, 0))
                    for key, value in used.items():
                        per[key] += value
                if model and model not in models:
                    models.append(model)

            if rec.get("isSidechain"):
                continue

            if rtype == "assistant":
                last_record_role = "assistant"
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        command = ""
                        args = block.get("input")
                        if isinstance(args, dict):
                            command = str(args.get("command") or "")
                        pending[block.get("id")] = bool(
                            block.get("name") == "Bash"
                            and _RECV_RE.search(command))
                continue

            # user record
            results = [b for b in content
                       if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            for block in results:
                was_recv = pending.pop(block.get("tool_use_id"), False)
                if was_recv and not block.get("is_error"):
                    task = _clean(_tool_result_text(block.get("content")))
                    # A timeout or an unreachable broker is not a task, and
                    # some harnesses hand stderr back without `is_error`.
                    if task and not task.startswith(_RECV_FAILURE_PREFIX):
                        facts.last_task = task[:CONTENT_MAX_CHARS]
            if results:
                # A tool result is the harness answering the model, not the
                # human speaking, so it neither starts a turn nor counts as
                # the session's goal.
                continue

            # `isMeta` is deliberately *not* a filter here. It marks the
            # caveat/command boilerplate — which the prefix test below drops
            # on its own — but it also marks a message injected by a peer
            # session, and for a dispatcher started that way the injected
            # message is precisely the goal it was given.
            last_record_role = "user"
            if facts.first_message is None:
                for raw in _text_blocks(content):
                    text = _clean(raw)
                    if _is_machinery(text):
                        continue
                    facts.first_message = text[:CONTENT_MAX_CHARS]
                    break

    facts.tokens = dict(tokens, total=sum(tokens.values()))
    facts.by_model = by_model
    facts.models = models
    if any(pending.values()):
        facts.status = "parked"
    elif pending:
        facts.status = "working"
    elif last_record_role == "user":
        facts.status = "working"
    elif last_record_role == "assistant":
        facts.status = "idle"
    else:
        facts.status = "unknown"

    facts.issue = issue_tag(facts.first_message)
    return facts


def issue_tag(text: Optional[str]) -> Optional[str]:
    """The first issue reference in `text` ("#11", ".../issues/11"), if any."""
    match = _ISSUE_RE.search(text or "")
    return "#" + match.group(1) if match else None
