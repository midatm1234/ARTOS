"""Detached runner for local jobs: records PID, enforces a wall-clock limit,
and writes the exit status into the job marker.

    python -m artos.job_runner --marker M --log L [--timeout S] -- cmd args...
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .procutil import kill_tree
from .utils import atomic_write_json, read_json, utc_now


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--marker", required=True, type=Path)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--timeout", type=float)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    marker = read_json(args.marker) or {}
    if marker.get("state") == "cancelled":
        return 143
    started = time.monotonic()
    marker.update(state="running", pid=os.getpid(), started_at=utc_now(), host=socket.gethostname())
    atomic_write_json(args.marker, marker)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("ab") as log:
        log.write(f"[artos-runner] START {utc_now()} cmd={command!r}\n".encode())
        log.flush()
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=os.getcwd())
        marker["child_pid"] = proc.pid
        atomic_write_json(args.marker, marker)
        note = None
        while True:
            try:
                rc = proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                current = read_json(args.marker) or {}
                if current.get("state") == "cancelled":
                    kill_tree(proc.pid)
                    rc, note = 143, "cancelled"
                    break
                if args.timeout and time.monotonic() - started > args.timeout:
                    kill_tree(proc.pid)
                    proc.wait(timeout=30)
                    rc, note = 124, f"wall-clock limit {args.timeout:.0f}s exceeded"
                    break
        runtime = time.monotonic() - started
        log.write(f"[artos-runner] EXIT {utc_now()} rc={rc} runtime_s={runtime:.1f} note={note}\n".encode())
    marker = read_json(args.marker) or marker
    final = "cancelled" if note == "cancelled" or marker.get("state") == "cancelled" else ("succeeded" if rc == 0 else "failed")
    marker.update(state=final, return_code=rc, finished_at=utc_now(), runtime_seconds=round(runtime, 3), note=note or marker.get("note"))
    atomic_write_json(args.marker, marker)
    return int(rc)


if __name__ == "__main__":
    sys.exit(main())
