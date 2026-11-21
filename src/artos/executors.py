"""Job executors: how an :class:`ExperimentSpec` becomes a running job.

An executor never decides *whether* to submit; :mod:`artos.workflow` does that
through the store's idempotency ledger. Executors only submit, poll, cancel,
and — crucially — *recover*: given a spec whose submission may or may not have
happened before a crash, report the external job if it exists. Each executor
leaves a durable marker keyed by the idempotency key so that recovery does not
depend on the controller having recorded the external ID.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import ExperimentSpec
from .utils import atomic_write_json, read_json, utc_now

MARKER = "artos_job.json"
STARTUP_GRACE_SECONDS = 120


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


@dataclass
class JobStatus:
    state: str  # submitted | running | succeeded | failed | cancelled | unknown
    return_code: int | None = None
    note: str | None = None
    runtime_seconds: float | None = None
    detail: dict[str, Any] | None = None


class Executor(Protocol):
    name: str

    def submit(self, spec: ExperimentSpec, key: str, timeout_seconds: float | None) -> str: ...
    def poll(self, external_id: str, spec: ExperimentSpec) -> JobStatus: ...
    def cancel(self, external_id: str, spec: ExperimentSpec) -> None: ...
    def recover(self, spec: ExperimentSpec, key: str) -> str | None: ...


from .procutil import kill_tree, pid_alive  # noqa: E402,F401  (re-exported)


# ---------------------------------------------------------------------------
# Local executor
# ---------------------------------------------------------------------------
class LocalExecutor:
    """Runs ``spec.command`` detached under :mod:`artos.job_runner`.

    The runner writes ``<output_dir>/artos_job.json`` with its PID at start and
    the return code at exit, enforces the wall-clock limit, and survives the
    controller exiting. The marker *is* the external job record, so recovery
    after a crash reads it instead of resubmitting.
    """

    name = "local"

    def __init__(self, python: str | None = None):
        # The runner is ARTOS code and must run under the controller's interpreter;
        # the *job's* interpreter is whatever spec.command names.
        self.python = python or sys.executable

    def _marker(self, spec: ExperimentSpec) -> Path:
        return Path(spec.output_dir) / MARKER

    def submit(self, spec: ExperimentSpec, key: str, timeout_seconds: float | None) -> str:
        if not spec.command:
            raise ValueError("local executor requires spec.command")
        out = Path(spec.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        marker = self._marker(spec)
        existing = read_json(marker)
        if existing and existing.get("idempotency_key") == key:
            state = existing.get("state")
            live_start = state == "starting" and (_age_seconds(existing.get("created_at")) or 0) <= STARTUP_GRACE_SECONDS
            live_run = state == "running" and pid_alive(existing.get("pid"))
            if state == "succeeded" or live_start or live_run:
                return existing["external_id"]
            # Stale marker from a previous attempt: archive it, then submit afresh.
            with (out / "artos_job.previous.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(existing, sort_keys=True) + "\n")
        external_id = f"local-{key}-{int(time.time())}"
        atomic_write_json(marker, {
            "idempotency_key": key, "external_id": external_id, "state": "starting",
            "command": spec.command, "created_at": utc_now(),
        })
        runner = [self.python, "-m", "artos.job_runner", "--marker", str(marker), "--log", str(out / "job.log")]
        if timeout_seconds:
            runner += ["--timeout", str(float(timeout_seconds))]
        runner += ["--", *spec.command]
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))
        kwargs: dict[str, Any] = {"cwd": spec.working_dir or str(out), "env": env,
                                  "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        launcher_log = out / "runner_launch.log"
        kwargs["stderr"] = launcher_log.open("ab")
        try:
            subprocess.Popen(runner, **kwargs)
        finally:
            kwargs["stderr"].close()
        return external_id

    def poll(self, external_id: str, spec: ExperimentSpec) -> JobStatus:
        marker = read_json(self._marker(spec)) or {}
        if marker.get("external_id") != external_id:
            return JobStatus("unknown", note="marker missing or belongs to another submission")
        state = marker.get("state")
        if state in {"succeeded", "failed", "cancelled"}:
            return JobStatus(state, marker.get("return_code"), marker.get("note"), marker.get("runtime_seconds"), marker)
        if state == "starting":
            created = marker.get("created_at")
            age = _age_seconds(created)
            if age is not None and age > STARTUP_GRACE_SECONDS:
                tail = ""
                launch_log = Path(spec.output_dir) / "runner_launch.log"
                if launch_log.exists():
                    tail = launch_log.read_text(encoding="utf-8", errors="replace")[-300:].strip()
                return JobStatus("failed", note=f"runner never started (marker created {created}) {tail}".strip())
            return JobStatus("submitted", note=f"runner starting since {created}")
        if state == "running":
            if pid_alive(marker.get("pid")):
                return JobStatus("running", detail=marker)
            # Re-read: the runner may have finished between the two reads.
            marker = read_json(self._marker(spec)) or {}
            if marker.get("state") in {"succeeded", "failed", "cancelled"}:
                return JobStatus(marker["state"], marker.get("return_code"), marker.get("note"), marker.get("runtime_seconds"), marker)
            return JobStatus("failed", note="runner process vanished without writing an exit status")
        return JobStatus("unknown", note=f"unrecognised marker state {state!r}")

    def cancel(self, external_id: str, spec: ExperimentSpec) -> None:
        marker = read_json(self._marker(spec)) or {}
        if marker.get("external_id") != external_id:
            return
        for pid in (marker.get("child_pid"), marker.get("pid")):
            if pid and pid_alive(pid):
                kill_tree(int(pid))
        marker.update(state="cancelled", note="cancelled by ARTOS controller", finished_at=utc_now())
        atomic_write_json(self._marker(spec), marker)

    def recover(self, spec: ExperimentSpec, key: str) -> str | None:
        marker = read_json(self._marker(spec))
        if marker and marker.get("idempotency_key") == key:
            return marker.get("external_id")
        return None

    def never_started(self, external_id: str | None, spec: ExperimentSpec) -> bool:
        """True when this submission's runner never recorded a PID (no work executed)."""
        marker = read_json(self._marker(spec)) or {}
        return bool(external_id) and marker.get("external_id") == external_id and not marker.get("pid")


# ---------------------------------------------------------------------------
# Mock executor (tests and dry runs)
# ---------------------------------------------------------------------------
class MockExecutor:
    """Deterministic in-process executor. Counts submissions per key."""

    name = "mock"

    def __init__(self, outcomes: dict[str, list[str]] | None = None, ledger: Path | None = None):
        self.outcomes = outcomes or {}
        self.ledger = ledger
        self.submissions: dict[str, int] = {}
        self.polls: dict[str, int] = {}
        self.cancelled: list[str] = []
        self._load()

    def _load(self) -> None:
        if self.ledger and self.ledger.exists():
            data = json.loads(self.ledger.read_text())
            self.submissions = data.get("submissions", {})

    def _save(self) -> None:
        if self.ledger:
            atomic_write_json(self.ledger, {"submissions": self.submissions})

    def submit(self, spec: ExperimentSpec, key: str, timeout_seconds: float | None) -> str:
        self.submissions[key] = self.submissions.get(key, 0) + 1
        self._save()
        Path(spec.output_dir).mkdir(parents=True, exist_ok=True)
        atomic_write_json(Path(spec.output_dir) / MARKER, {"idempotency_key": key, "external_id": f"mock-{key}-{self.submissions[key]}"})
        return f"mock-{key}-{self.submissions[key]}"

    def poll(self, external_id: str, spec: ExperimentSpec) -> JobStatus:
        key = external_id.rsplit("-", 1)[0].removeprefix("mock-")
        attempt = int(external_id.rsplit("-", 1)[1])
        sequence = self.outcomes.get(spec.variant_id, ["succeeded"])
        outcome = sequence[min(attempt - 1, len(sequence) - 1)]
        self.polls[key] = self.polls.get(key, 0) + 1
        return JobStatus(outcome, 0 if outcome == "succeeded" else 1, runtime_seconds=0.0)

    def cancel(self, external_id: str, spec: ExperimentSpec) -> None:
        self.cancelled.append(external_id)

    def recover(self, spec: ExperimentSpec, key: str) -> str | None:
        marker = read_json(Path(spec.output_dir) / MARKER)
        if marker and marker.get("idempotency_key") == key:
            return marker["external_id"]
        return None


# ---------------------------------------------------------------------------
# Generic MCP executor
# ---------------------------------------------------------------------------
DEFAULT_MCP_TOOLS = {
    "status": "get_job_status",
    "cancel": "cancel_job",
    "lookup": "find_job_by_client_request_id",
    "request_id_arg": "client_request_id",
}
DEFAULT_MCP_STATES = {
    "done": "succeeded", "succeeded": "succeeded", "completed": "succeeded",
    "failed": "failed", "error": "failed", "blocked": "failed",
    "cancelled": "cancelled", "canceled": "cancelled",
    "running": "running", "queued": "submitted", "pending": "submitted",
    "waiting_for_gpu": "submitted", "waiting_for_job": "submitted",
}


class MCPExecutor:
    """Submits jobs through any MCP server over stdio.

    The server must expose a submission tool (named per spec in
    ``spec.tool``), a status tool and a cancel tool. For duplicate-free recovery
    the server should also accept an idempotency argument (default
    ``client_request_id``) and offer a lookup tool that returns the job created
    for that ID. Tool names, the argument name and the status vocabulary are
    configurable in ``artos.json`` under ``execution.mcp``.

    Expected JSON results: submission returns ``{"job_id": ...}``; status returns
    ``{"status": <state>, "return_code"?: int, "note"?: str}``; lookup returns
    ``{"status": "ok", "job_id": ...}`` or any other status when not found.
    """

    name = "mcp"

    def __init__(self, command: list[str], tools: dict[str, str] | None = None,
                 states: dict[str, str] | None = None, env: dict[str, str] | None = None, timeout: float = 120):
        self.command = command
        self.tools = {**DEFAULT_MCP_TOOLS, **(tools or {})}
        self.states = {**DEFAULT_MCP_STATES, **(states or {})}
        self.env = env
        self.timeout = timeout

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from .mcp_client import call_tool

        raw = call_tool(self.command, tool, arguments, env=self.env, timeout=self.timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "text", "text": raw}

    def submit(self, spec: ExperimentSpec, key: str, timeout_seconds: float | None) -> str:
        if not spec.tool:
            raise ValueError("mcp executor requires spec.tool (the server's submission tool)")
        args = dict(spec.tool_args)
        if self.tools.get("request_id_arg"):
            args[self.tools["request_id_arg"]] = key
        result = self.call(spec.tool, args)
        if result.get("status") == "error":
            raise RuntimeError(f"{spec.tool}: {result.get('message')}")
        job_id = result.get("job_id") or (result.get("jobs") or [{}])[-1].get("job_id")
        if not job_id:
            raise RuntimeError(f"{spec.tool} returned no job_id: {result}")
        return str(job_id)

    def poll(self, external_id: str, spec: ExperimentSpec) -> JobStatus:
        result = self.call(self.tools["status"], {"job_id": external_id})
        state = self.states.get(str(result.get("status")), "unknown")
        return JobStatus(state, result.get("return_code"), result.get("note"), detail=result)

    def cancel(self, external_id: str, spec: ExperimentSpec) -> None:
        self.call(self.tools["cancel"], {"job_id": external_id})

    def recover(self, spec: ExperimentSpec, key: str) -> str | None:
        lookup, arg = self.tools.get("lookup"), self.tools.get("request_id_arg")
        if not lookup or not arg:
            return None  # cannot prove absence; the controller then blocks rather than resubmits
        result = self.call(lookup, {arg: key})
        return result.get("job_id") if result.get("status") == "ok" else None


def build_executor(name: str, config: dict[str, Any]) -> Executor:
    execution = config.get("execution", {})
    if name == "local":
        return LocalExecutor(execution.get("python"))
    if name == "mock":
        return MockExecutor(ledger=Path(execution["mock_ledger"]) if execution.get("mock_ledger") else None)
    if name == "mcp":
        mcp = execution.get("mcp") or {}
        if not mcp.get("command"):
            raise ValueError("execution.mcp.command is not configured in artos.json")
        return MCPExecutor(list(mcp["command"]), tools=mcp.get("tools"), states=mcp.get("states"),
                           timeout=float(mcp.get("timeout_seconds", 120)))
    raise ValueError(f"unknown executor {name!r}")


def gpu_free_mib() -> list[int] | None:
    """Live free-memory query per GPU; ``None`` when nvidia-smi is unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
