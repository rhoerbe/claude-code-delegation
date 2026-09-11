"""The fleet dashboard: JSON model in, markdown out (ADR-0008).

**Metadata is system-wide, content is scoped.** Every roster entry contributes
its handle, model, effort, claim graph, working tree, status and cost to one
view. What a session is *doing* — a dispatcher's goal, a worker's last task —
appears only for the single handle passed as `scope`, so no rendered artifact
ever holds two clients' working material. That constraint lives in `build()`:
`ccd_dashboard.transcript` hands back content for every session whether or not
it is wanted, exactly as ADR-0008 warns.

**Read-only.** There is no stop, retire or redirect here and there is not meant
to be: writing would need an authorization story, and ADR-0006 leaves no
principals to write one against. Control stays in the participant sessions,
where a human is already attached.

The JSON model is ephemeral by default — printed, not stored. `write_json`
exists for the case that wants a file, and **refuses any path inside a git
working tree**, which is what stops one client's content being committed into
another client's repository.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import transcript as tx

SCHEMA = "ccd.dashboard/1"


class WriteRefused(Exception):
    """A JSON write was aimed somewhere it must not go."""


# ----------------------------------------------------------------------
# model
# ----------------------------------------------------------------------


def _role(handle: str, roster: Iterable[dict]) -> str:
    """Dispatcher or worker, read off the claim graph (ADR-0007).

    A dispatcher is a handle that holds at least one claim — the same test the
    broker itself uses to decide whose `send` to refuse. Nothing declares a
    role, so nothing here can be out of step with what the broker enforces.
    """
    return "dispatcher" if any(e.get("owner") == handle for e in roster) else "worker"


def _repo_root(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def _usd(by_model: dict, rates: Optional[dict]) -> Optional[float]:
    """Price a session's tokens, or None when we cannot price it honestly.

    Rates are USD per million tokens, per model, supplied by the caller — this
    repo ships none, because pricing is provider- and deployment-specific and a
    stale table baked in here would be worse than an empty cost column. One
    unpriced model makes the whole figure None rather than an undercount.
    """
    if not rates or not by_model:
        return None
    total = 0.0
    for model, used in by_model.items():
        if not any(used.values()):
            continue        # a model that burned nothing needs no price
        rate = rates.get(model)
        if not isinstance(rate, dict):
            return None
        for key, tokens in used.items():
            price = rate.get(key)
            if price is None:
                return None
            total += (tokens / 1_000_000.0) * float(price)
    return round(total, 4)


def build(roster: list, scope: Optional[str] = None, *,
          root: Optional[Path] = None, rates: Optional[dict] = None,
          now: Optional[float] = None) -> dict:
    """Build the dashboard model from a `roster` reply.

    `scope` selects the one handle whose content is included. Every other
    entry carries metadata only — `content` is explicitly `null`, not omitted,
    so a reader can tell "not shown here" from "nothing to show".
    """
    roster = [e for e in roster if isinstance(e, dict)]
    known = {e.get("handle") for e in roster}
    sessions = []
    for entry in sorted(roster, key=lambda e: str(e.get("handle") or "")):
        handle = entry.get("handle")
        cwd = entry.get("cwd") or None
        located = tx.locate(cwd, entry.get("session") or None, root=root)
        facts = tx.parse(located.path) if located else tx.Facts()

        session = {
            "handle": handle,
            "role": _role(str(handle), roster),
            # "model" is the *resolved* id (e.g. claude-sonnet-5, or an
            # OpenRouter slug) — usually still empty today, since nothing
            # upstream of the manifest/launcher work (#13 phase 2/4) can
            # supply it yet. No "slot" alongside it: the roster tried that
            # (fable/opus/sonnet/haiku, what this key briefly called "model"
            # before broker 1.3 stopped overloading that name) and dropped
            # it again before release — a live probe showed naming a model
            # directly reaches it exactly as well as a slot alias.
            "model": entry.get("model") or None,
            "effort": entry.get("effort") or None,
            "owner": entry.get("owner") or None,
            "tree": {
                "cwd": cwd,
                "repo": _repo_root(cwd),
                "branch": facts.branch,
            },
            "transcript": {
                "session": entry.get("session") or None,
                "path": str(located.path) if located else None,
                "resolved_by": located.resolved_by,
            },
            "status": facts.status,
            "last_activity": facts.last_activity,
            "cost": {
                "turns": facts.turns,
                "tokens": facts.tokens or {},
                "by_model": facts.by_model,
                "usd": _usd(facts.by_model, rates),
            },
            # Filled in below for the scoped handle only.
            "content": None,
        }
        if scope is not None and handle == scope:
            session["content"] = _content(session["role"], facts)
        sessions.append(session)

    model = {
        "schema": SCHEMA,
        "generated_at": _iso(now if now is not None else time.time()),
        "scope": scope,
        "sessions": sessions,
    }
    if scope is not None and scope not in known:
        model["scope_error"] = f"no announced handle '{scope}'"
    return model


def _content(role: str, facts: tx.Facts) -> Optional[dict]:
    """One bounded line, role-dependent (ADR-0008, "What content is").

    A dispatcher's first substantive message is the goal it was given; a
    worker's last received task is what it is on. Widening this to recent
    turns or transcript excerpts is a deliberate later decision, not a drift.
    A worker with no task yet falls back to its own first message rather than
    showing nothing, and says so in `kind`.
    """
    if role == "dispatcher":
        text, kind = facts.first_message, "goal"
    else:
        text, kind = facts.last_task, "last-task"
        if not text:
            text, kind = facts.first_message, "goal"
    if not text:
        return None
    # Tag the line being shown, not whatever the parser happened to find
    # first: a worker's last task and the goal it started from are often
    # about different issues.
    return {"kind": kind, "text": text, "issue": tx.issue_tag(text)}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# render
# ----------------------------------------------------------------------


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def _ago(stamp: Optional[str], now: Optional[float] = None) -> str:
    if not stamp:
        return "—"
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    delta = (now if now is not None else time.time()) - when.timestamp()
    if delta < 0:
        delta = 0
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta // 60)}m ago"
    if delta < 172800:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _tree(session: dict) -> str:
    tree = session["tree"]
    cwd, repo, branch = tree["cwd"], tree["repo"], tree["branch"]
    if not cwd:
        return "—"
    name = Path(repo or cwd).name or cwd
    home = str(Path.home())
    if not repo and cwd.startswith(home):
        name = "~" + cwd[len(home):]
    return f"{name}@{branch}" if branch else name


def _cost(session: dict) -> str:
    cost = session["cost"]
    total = (cost.get("tokens") or {}).get("total")
    if not total:
        return "—"
    text = _tokens(total)
    if cost.get("usd") is not None:
        text += f" / ${cost['usd']:.2f}"
    return text


def render_markdown(model: dict, now: Optional[float] = None) -> str:
    """Render the model for reading. Content appears only under `## Scope`."""
    sessions = model["sessions"]
    lines = [f"# ccd fleet — {len(sessions)} session"
             f"{'' if len(sessions) == 1 else 's'} "
             f"({model['generated_at']})", ""]

    if not sessions:
        lines.append("_(no handles announced)_")
        lines.append("")
    else:
        lines.append("| handle | role | model/effort | owner | status "
                     "| last activity | cost | tree |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for session in sessions:
            tier = "/".join(x for x in (session["model"], session["effort"]) if x)
            lines.append(
                "| `{handle}` | {role} | {tier} | {owner} | {status} "
                "| {ago} | {cost} | {tree} |".format(
                    handle=session["handle"],
                    role=session["role"],
                    tier=tier or "—",
                    owner=f"`{session['owner']}`" if session["owner"] else "—",
                    status=session["status"],
                    ago=_ago(session["last_activity"], now),
                    cost=_cost(session),
                    tree=_tree(session),
                ))
        lines.append("")

    scope = model.get("scope")
    if model.get("scope_error"):
        lines += [f"> **{model['scope_error']}** — content not shown.", ""]
    elif scope is None:
        lines += ["_Metadata is system-wide; content is scoped. "
                  "Pass `--scope <handle>` for one session's current line._", ""]
    else:
        selected = next((s for s in sessions if s["handle"] == scope), None)
        lines.append(f"## Scope: `{scope}`" +
                     (f" ({selected['role']})" if selected else ""))
        lines.append("")
        content = (selected or {}).get("content")
        if not content:
            lines.append("_No content in this session's transcript "
                         "(never announced a cwd, no transcript, or nothing said yet)._")
        else:
            label = "Goal" if content["kind"] == "goal" else "Last task"
            issue = f" ({content['issue']})" if content.get("issue") else ""
            lines.append(f"**{label}**{issue} — {content['text']}")
        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# write
# ----------------------------------------------------------------------


def git_worktree_containing(path: Path) -> Optional[Path]:
    """The nearest ancestor of `path` that is a git working tree, if any.

    `.git` as a directory is an ordinary clone; `.git` as a *file* is a
    worktree or submodule and counts just as much. Checked on the resolved
    path so a symlink or `..` cannot walk around it.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    start = resolved if resolved.is_dir() else resolved.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def write_json(model: dict, path: str) -> Path:
    """Write the JSON model to `path`, refusing to land it in a git tree.

    The JSON is a machine-readable aggregate of several sessions' material, so
    the realistic leak is not dramatic: it is this file being committed into
    whichever repository happened to be the working directory. Refusing the
    write is the mechanism ADR-0008 names for that, and it is deliberately
    dumb — no allowlist, no override flag.
    """
    target = Path(path).expanduser()
    tree = git_worktree_containing(target)
    if tree is not None:
        raise WriteRefused(
            f"refusing to write inside the git working tree at {tree}: "
            f"the dashboard aggregates several sessions' material and must "
            f"not be committable into one of their repositories (ADR-0008). "
            f"Choose a path outside any repository — {os.environ.get('TMPDIR', '/tmp')} "
            f"or a state directory."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model, indent=2, sort_keys=False) + "\n",
                      encoding="utf-8")
    return target


def load_rates(path: Optional[str]) -> Optional[dict]:
    """Read a USD-per-million-tokens table: {model: {input, output, ...}}."""
    if not path:
        return None
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("rate table must be a JSON object of model -> rates")
    return data


def roster_from_reply(reply: Any) -> list:
    """Pull the worker list out of a `roster` RPC reply."""
    if not isinstance(reply, dict):
        raise ValueError("roster reply must be a JSON object")
    if not reply.get("ok", True):
        raise ValueError(str(reply.get("reason") or "roster failed"))
    workers = reply.get("workers")
    if workers is None and isinstance(reply.get("roster"), dict):
        workers = list(reply["roster"].values())
    if not isinstance(workers, list):
        raise ValueError("roster reply has no 'workers' list")
    return workers
