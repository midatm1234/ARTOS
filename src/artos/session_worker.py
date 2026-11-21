"""Runs inside a tmux session: executes the agent command, streams to a log,
and writes a heartbeat/status file independent of terminal output.

    python -m artos.session_worker <session_spec.json>
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from .utils import atomic_write_json, read_json, utc_now


def _heartbeat(status_path: Path, stop: threading.Event, proc: subprocess.Popen) -> None:
    while not stop.wait(15):
        current = read_json(status_path) or {}
        current.update(heartbeat_at=utc_now(), pid=proc.pid, state="running")
        atomic_write_json(status_path, current)


def run(spec_path: Path) -> int:
    spec = read_json(Path(spec_path))
    if not spec:
        print(f"no session spec at {spec_path}", file=sys.stderr)
        return 2
    status_path = Path(spec["status_path"])
    log_path = Path(spec["log_path"])
    prompt = Path(spec["prompt_path"]).read_text(encoding="utf-8")
    started = time.monotonic()
    atomic_write_json(status_path, {"state": "starting", "started_at": utc_now()})
    with log_path.open("ab") as log:
        proc = subprocess.Popen(spec["command"], cwd=spec["work_dir"], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        stop = threading.Event()
        threading.Thread(target=_heartbeat, args=(status_path, stop, proc), daemon=True).start()
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        rc = proc.wait()
        stop.set()
    atomic_write_json(status_path, {"state": "succeeded" if rc == 0 else "failed", "return_code": rc,
                                    "finished_at": utc_now(), "runtime_seconds": round(time.monotonic() - started, 1)})
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m artos.session_worker <session_spec.json>", file=sys.stderr)
        return 2
    return run(Path(argv[0]))


if __name__ == "__main__":
    sys.exit(main())
