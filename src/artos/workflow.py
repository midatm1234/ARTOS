"""Resumable experiment controller.

``ExperimentController.step()`` is a pure reconcile loop over durable state:

1. Read the frozen contract (hash-verified) and the planned experiment specs.
2. For each spec, derive its idempotency key and consult the job ledger:
   * live job          -> poll it, record state;
   * crash-ambiguous   -> ask the executor to *recover* by key; resubmit only
                          if the executor positively reports no prior job;
   * failed, attempts  -> retry (bounded by ``max_attempts_per_job``);
   * no row            -> submit, after budget and GPU checks.
3. Honour cancellation and budget limits before every submission.

Running ``step()`` twice, or after the controller was killed at any point,
never produces a second external job for the same key. That is the property
the tests in ``tests/test_workflow.py`` pin down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .executors import Executor, JobStatus, gpu_free_mib
from .gates import verify_frozen_contract
from .models import ExecutionRecord, ExperimentSpec, JobState
from .models import utc_now as utc_dt
from .store import Store
from .utils import atomic_write_json, read_json, sha256_file


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class StepReport:
    submitted: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)  # deferred only by the concurrency limit
    states: dict[str, str] = field(default_factory=dict)

    @property
    def all_terminal(self) -> bool:
        """Every job is finished and nothing is waiting to be (re)submitted."""
        return (bool(self.states) and not self.deferred
                and all(s in {"succeeded", "failed", "cancelled"} for s in self.states.values()))

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("submitted", "reused", "retried", "recovered", "blocked", "exhausted", "deferred", "queued", "states")} | {
            "all_terminal": self.all_terminal}


def plan_path(run_dir: Path) -> Path:
    return Path(run_dir) / "jobs" / "plan.json"


def load_plan(run: dict[str, Any]) -> list[ExperimentSpec]:
    data = read_json(plan_path(Path(run["run_dir"])))
    if not data:
        raise FileNotFoundError(f"no experiment plan at {plan_path(Path(run['run_dir']))}")
    specs = [ExperimentSpec.model_validate(s) for s in data["experiments"]]
    for s in specs:
        if s.contract_sha256 != run.get("contract_sha256"):
            raise ValueError(f"spec {s.variant_id}/{s.seed} was planned against contract {s.contract_sha256[:12]}, "
                             f"not the frozen contract {str(run.get('contract_sha256'))[:12]}")
    return specs


def write_plan(run: dict[str, Any], specs: list[ExperimentSpec]) -> Path:
    path = plan_path(Path(run["run_dir"]))
    existing = read_json(path)
    payload = {"run_id": run["run_id"], "contract_sha256": run["contract_sha256"],
               "experiments": [s.model_dump(mode="json") for s in specs]}
    if existing and existing != payload:
        old = {ExperimentSpec.model_validate(s).idempotency_key() for s in existing["experiments"]}
        new = {s.idempotency_key() for s in specs}
        if not old <= new:
            raise ValueError("a plan already exists and the new plan drops experiments from it; plans are append-only")
    atomic_write_json(path, payload)
    return path


class ExperimentController:
    def __init__(self, store: Store, run_id: str, executors: dict[str, Executor], config: dict[str, Any]):
        self.store = store
        self.run_id = run_id
        self.executors = executors
        self.config = config

    # -- budget -----------------------------------------------------------
    def _budget_used(self) -> dict[str, float]:
        used = {"cpu_hours": 0.0, "gpu_hours": 0.0, "jobs": 0}
        for job in self.store.jobs_for_run(self.run_id):
            spec = job["spec"]
            attempts = int(job["attempt"])
            used["cpu_hours"] += float(spec.get("estimated_cpu_hours", 0.0)) * attempts
            used["gpu_hours"] += float(spec.get("estimated_gpu_hours", 0.0)) * attempts
            used["jobs"] += attempts
        return used

    def _check_budget(self, spec: ExperimentSpec, contract) -> None:
        budget = contract.compute_budget
        used = self._budget_used()
        if used["cpu_hours"] + spec.estimated_cpu_hours > budget.cpu_hours + 1e-9:
            raise BudgetExceeded(f"CPU budget {budget.cpu_hours} h would be exceeded ({used['cpu_hours']:.2f} h committed)")
        if used["gpu_hours"] + spec.estimated_gpu_hours > budget.gpu_hours + 1e-9:
            raise BudgetExceeded(f"GPU budget {budget.gpu_hours} h would be exceeded ({used['gpu_hours']:.2f} h committed)")
        safety = self.config.get("safety", {})
        approved = float(self.config.get("approvals", {}).get(self.run_id, {}).get("gpu_hours", 0.0))
        limit = max(float(safety.get("max_gpu_hours_without_approval", 2.0)), approved)
        if used["gpu_hours"] + spec.estimated_gpu_hours > limit + 1e-9:
            raise BudgetExceeded(f"GPU use above {limit} h needs explicit approval (artos.json approvals.{self.run_id}.gpu_hours)")
        if budget.max_jobs is not None and used["jobs"] + 1 > budget.max_jobs:
            raise BudgetExceeded(f"job budget {budget.max_jobs} would be exceeded")

    def _gpu_ready(self, spec: ExperimentSpec) -> bool:
        if not spec.requires_gpu:
            return True
        need = int(self.config.get("execution", {}).get("min_free_gpu_mib", 16384))
        free = gpu_free_mib()
        return bool(free) and max(free) >= need

    # -- reconcile --------------------------------------------------------
    def _record(self, spec: ExperimentSpec, key: str, max_attempts: int) -> dict[str, Any]:
        return ExecutionRecord(
            run_id=self.run_id, idempotency_key=key, variant_id=spec.variant_id, seed=spec.seed,
            executor=spec.executor, state=JobState.planned, attempt=0, max_attempts=max_attempts,
            code_refs=spec.code_refs, output_dir=spec.output_dir,
        ).model_dump(mode="json")

    def _apply_status(self, key: str, status: JobStatus, spec: ExperimentSpec) -> str:
        mapping = {"submitted": "submitted", "running": "running", "succeeded": "succeeded",
                   "failed": "failed", "cancelled": "cancelled", "unknown": "unknown_after_crash"}
        state = mapping[status.state]
        changes: dict[str, Any] = {"state": state}
        if status.return_code is not None:
            changes["return_code"] = status.return_code
        if status.runtime_seconds is not None:
            changes["runtime_seconds"] = status.runtime_seconds
        if status.note:
            changes["note"] = status.note
        if state == "failed":
            job = self.store.get_job(key)
            checker = getattr(self.executors.get(spec.executor), "never_started", None)
            if job and checker and checker(job.get("external_job_id"), spec):
                # No work ran: the attempt does not count against max_attempts_per_job.
                changes["attempt"] = max(0, int(job["attempt"]) - 1)
                changes["note"] = (changes.get("note") or "") + " [attempt not counted: runner never started]"
        if state in {"succeeded", "failed", "cancelled"}:
            changes["finished_at"] = utc_dt().isoformat()
            changes["outputs"] = _hash_outputs(Path(spec.output_dir))
            metrics = read_json(Path(spec.output_dir) / "metrics.json") or {}
            if "peak_memory_bytes" in metrics:
                changes["peak_memory_bytes"] = metrics["peak_memory_bytes"]
            if "environment" in metrics:
                changes["environment"] = metrics["environment"]
        self.store.update_job(key, **changes)
        return state

    def step(self, *, submit: bool = True) -> StepReport:
        run = self.store.get_run(self.run_id)
        frozen = verify_frozen_contract(run)
        contract = frozen.contract
        specs = load_plan(run)
        report = StepReport()
        cancelling = bool(run.get("cancel_requested"))
        max_attempts = int(contract.compute_budget.max_attempts_per_job)
        timeout = self.config.get("execution", {}).get("job_timeout_seconds")

        for spec in specs:
            key = spec.idempotency_key()
            executor = self.executors[spec.executor]
            job = self.store.get_job(key)

            # 1. Live or ambiguous jobs: poll / recover, never resubmit blindly.
            if job and job["state"] in {"submitted", "running"}:
                status = executor.poll(job["external_job_id"], spec)
                if cancelling and status.state in {"submitted", "running"}:
                    executor.cancel(job["external_job_id"], spec)
                    status = JobStatus("cancelled", note="run cancellation")
                report.states[key] = self._apply_status(key, status, spec)
                report.reused.append(key)
                continue
            if job and job["state"] in {"submitting", "unknown_after_crash"}:
                external = executor.recover(spec, key)
                if external:
                    self.store.update_job(key, state="submitted", external_job_id=external,
                                          note="recovered after interrupted submission")
                    report.recovered.append(key)
                    report.states[key] = self._apply_status(key, executor.poll(external, spec), spec)
                    continue
                if job["state"] == "unknown_after_crash":
                    report.blocked.append(key)
                    report.states[key] = job["state"]
                    continue
                # 'submitting' with no executor-side trace: the crash happened
                # before submission reached the executor. Safe to submit once.
                # Do not consume an attempt for a submission that never happened.
                job = self.store.update_job(key, state="failed", attempt=max(0, int(job["attempt"]) - 1),
                                            note="submission never reached executor")
            if job and job["state"] == "succeeded":
                report.states[key] = "succeeded"
                report.reused.append(key)
                continue

            if cancelling:
                report.states[key] = job["state"] if job else "planned"
                report.deferred.append(key)
                continue
            if not submit:
                report.states[key] = job["state"] if job else "planned"
                continue

            # 2. Budget and resource checks happen before the claim.
            if job is None or job["state"] in {"failed", "cancelled"}:
                if job is not None and int(job["attempt"]) >= int(job["max_attempts"]):
                    report.exhausted.append(key)
                    report.states[key] = job["state"]
                    continue
                try:
                    self._check_budget(spec, contract)
                except BudgetExceeded as exc:
                    self.store.record_event("budget_blocked", {"key": key, "reason": str(exc)}, self.run_id)
                    report.deferred.append(key)
                    report.states[key] = job["state"] if job else "planned"
                    continue
                if not self._gpu_ready(spec):
                    report.deferred.append(key)
                    report.states[key] = job["state"] if job else "planned"
                    continue
                limit = self.config.get("execution", {}).get("max_concurrent_jobs")
                if limit:
                    live = sum(1 for j in self.store.jobs_for_run(self.run_id)
                               if j["state"] in {"submitting", "submitted", "running"})
                    if live >= int(limit):
                        report.deferred.append(key)
                        report.queued.append(key)
                        report.states[key] = job["state"] if job else "planned"
                        continue

            decision, row = self.store.claim_job(
                key, self.run_id, spec.model_dump(mode="json"), self._record(spec, key, max_attempts), max_attempts)
            if decision in {"existing"}:
                report.reused.append(key)
                report.states[key] = row["state"]
                continue
            if decision in {"blocked", "exhausted"}:
                getattr(report, decision).append(key)
                report.states[key] = row["state"]
                continue
            try:
                external = executor.submit(spec, key, timeout)
            except Exception as exc:  # submission failed cleanly: record and move on
                self.store.update_job(key, state="failed", note=f"submit error: {exc}")
                report.states[key] = "failed"
                continue
            self.store.update_job(key, state="submitted", external_job_id=external, submitted_at=utc_dt().isoformat())
            (report.retried if decision == "retry" else report.submitted).append(key)
            report.states[key] = "submitted"

        self.store.record_event("controller_step", report.as_dict(), self.run_id)
        return report

    def run_until_terminal(self, poll_seconds: float = 5.0, max_wall_seconds: float | None = None, *, takeover: bool = False) -> StepReport:
        owner = self.store.acquire_lease(self.run_id, takeover_dead=takeover)
        started = time.monotonic()
        try:
            while True:
                report = self.step()
                live = any(s in {"submitted", "running"} for s in report.states.values())
                # Stop when everything is terminal, or nothing is running and nothing can
                # progress (deferred by budget/approval, blocked, or exhausted).
                if report.all_terminal:
                    return report
                if not live and not report.submitted and not report.retried and not report.queued:
                    # Nothing running and nothing could start this step (budget, approval,
                    # GPU, blocked or exhausted): waiting would not change anything.
                    return report
                if max_wall_seconds and time.monotonic() - started > max_wall_seconds:
                    return report
                self.store.acquire_lease(self.run_id, owner)
                time.sleep(poll_seconds)
        finally:
            self.store.release_lease(self.run_id, owner)


def requeue_unstarted(store: Store, run_id: str, executors: dict[str, Executor], reason: str) -> list[str]:
    """Reset the attempt counter of failed jobs whose runner never started.

    ``max_attempts_per_job`` bounds *executed* attempts. Jobs that ran (a PID was
    recorded) are never reset. Each reset is recorded as an event.
    """
    reset = []
    for job in store.jobs_for_run(run_id):
        if job["state"] != "failed":
            continue
        spec = ExperimentSpec.model_validate(job["spec"])
        checker = getattr(executors.get(spec.executor), "never_started", None)
        if checker and checker(job.get("external_job_id"), spec):
            store.update_job(job["idempotency_key"], attempt=0, note=f"requeued (never started): {reason}")
            store.record_event("job_requeued_unstarted", {"key": job["idempotency_key"],
                                                          "previous_attempt": job["attempt"], "reason": reason}, run_id)
            reset.append(job["idempotency_key"])
    return reset


def _hash_outputs(directory: Path, limit_mb: int = 256) -> dict[str, str]:
    out: dict[str, str] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.stat().st_size <= limit_mb * 1024 * 1024:
            out[str(path.relative_to(directory)).replace("\\", "/")] = sha256_file(path)
    return out
