"""Recovery, duplicate-job prevention, bounded retries, cancellation, budgets, and evaluation."""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

from artos.evaluation import evaluate_run
from artos.executors import LocalExecutor, MockExecutor
from artos.store import LeaseHeld
from artos.workflow import ExperimentController, write_plan

from helpers import Root, advance_to_frozen, contract_for, plan, write_metrics


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Root()
        self.run = advance_to_frozen(self.root)
        self.root.store.transition(self.run["run_id"], "primary_running", config=self.root.config)

    def tearDown(self) -> None:
        self.root.close()

    def controller(self, executor) -> ExperimentController:
        return ExperimentController(self.root.store, self.run["run_id"], {executor.name: executor}, self.root.config)

    def test_repeated_steps_never_duplicate_jobs(self) -> None:
        specs = plan(self.root, self.run)
        ex = MockExecutor()
        first = self.controller(ex).step()
        self.assertEqual(len(first.submitted), 4)
        for _ in range(3):
            again = self.controller(ex).step()
            self.assertEqual(again.submitted, [])
        self.assertEqual(set(ex.submissions.values()), {1})
        self.assertEqual(len(ex.submissions), len(specs))

    def test_resume_with_fresh_controller_and_executor_does_not_resubmit(self) -> None:
        plan(self.root, self.run)
        ledger = self.root.tmp / "mock_ledger.json"
        self.controller(MockExecutor(ledger=ledger)).step()
        # Simulate a process restart: new store connection, new executor instance.
        self.root.store.close()
        from artos.store import Store
        self.root.store = Store(self.root.tmp / "state" / "artos.sqlite")
        ex2 = MockExecutor(ledger=ledger)
        report = self.controller(ex2).step()
        self.assertEqual(report.submitted, [])
        self.assertEqual(set(ex2.submissions.values()), {1})

    def test_crash_between_claim_and_record_recovers_without_resubmitting(self) -> None:
        specs = plan(self.root, self.run)
        ex = MockExecutor()
        key = specs[0].idempotency_key()
        # Claim and submit, then "crash" before the controller records the external id.
        self.root.store.claim_job(key, self.run["run_id"], specs[0].model_dump(mode="json"),
                                  {"output_dir": specs[0].output_dir}, 2)
        ex.submit(specs[0], key, None)
        self.assertEqual(self.root.store.get_job(key)["state"], "submitting")
        report = self.controller(ex).step()
        self.assertIn(key, report.recovered)
        self.assertEqual(ex.submissions[key], 1)

    def test_crash_before_executor_saw_submission_submits_once(self) -> None:
        specs = plan(self.root, self.run)
        ex = MockExecutor()
        key = specs[0].idempotency_key()
        self.root.store.claim_job(key, self.run["run_id"], specs[0].model_dump(mode="json"),
                                  {"output_dir": specs[0].output_dir}, 2)
        report = self.controller(ex).step()
        self.assertEqual(ex.submissions[key], 1)
        self.assertEqual(self.root.store.get_job(key)["attempt"], 1)
        self.assertIn(key, report.retried + report.submitted)

    def test_unknown_after_crash_blocks_until_resolved(self) -> None:
        specs = plan(self.root, self.run)
        ex = MockExecutor()
        key = specs[0].idempotency_key()
        self.root.store.claim_job(key, self.run["run_id"], specs[0].model_dump(mode="json"), {"output_dir": specs[0].output_dir}, 2)
        self.root.store.update_job(key, state="unknown_after_crash")
        report = self.controller(ex).step()
        self.assertIn(key, report.blocked)
        self.assertNotIn(key, ex.submissions)

    def test_failed_jobs_retry_within_bound(self) -> None:
        plan(self.root, self.run)
        ex = MockExecutor(outcomes={"cand": ["failed", "failed", "failed"]})
        c = self.controller(ex)
        c.step()          # submit all
        c.step()          # poll: cand fails
        r = c.step()      # retry cand (attempt 2)
        self.assertEqual(len(r.retried), 2)
        c.step()          # poll: fails again
        r = c.step()      # attempts exhausted (max_attempts_per_job=2)
        self.assertEqual(len(r.exhausted), 2)
        self.assertEqual(max(ex.submissions.values()), 2)

    def test_cancellation_cancels_live_jobs_and_blocks_new_ones(self) -> None:
        plan(self.root, self.run)

        class Slow(MockExecutor):
            def poll(self, external_id, spec):
                from artos.executors import JobStatus
                return JobStatus("running")

        ex = Slow()
        c = self.controller(ex)
        c.step()
        self.root.store.request_cancel(self.run["run_id"], "test")
        r = c.step()
        self.assertTrue(all(s == "cancelled" for s in r.states.values()))
        self.assertEqual(len(ex.cancelled), 4)
        self.root.store.transition(self.run["run_id"], "cancelled", config=self.root.config)

    def test_budget_limits_submission(self) -> None:
        # Separate run whose contract allows 1 CPU-hour: 4 jobs x 0.5 h -> only 2 may start.
        root2 = Root()
        try:
            run2 = advance_to_frozen(root2, contract_factory=lambda r: contract_for(
                r, compute_budget={"cpu_hours": 1.0, "gpu_hours": 0}))
            root2.store.transition(run2["run_id"], "primary_running", config=root2.config)
            plan(root2, run2)
            r = ExperimentController(root2.store, run2["run_id"], {"mock": MockExecutor()}, root2.config).step()
            self.assertEqual(len(r.submitted), 2)
            self.assertEqual(len(r.deferred), 2)
            events = [e for e in root2.store.events(run2["run_id"]) if e["event_type"] == "budget_blocked"]
            self.assertEqual(len(events), 2)
        finally:
            root2.close()

    def test_gpu_jobs_need_explicit_approval_above_limit(self) -> None:
        root2 = Root()
        try:
            run2 = advance_to_frozen(root2, contract_factory=lambda r: contract_for(
                r, compute_budget={"cpu_hours": 0, "gpu_hours": 100}))
            root2.store.transition(run2["run_id"], "primary_running", config=root2.config)
            from artos.models import ExperimentSpec
            spec = ExperimentSpec(run_id=run2["run_id"], contract_sha256=run2["contract_sha256"], variant_id="cand", seed=1,
                                  executor="mock", output_dir=str(root2.tmp / "o"), estimated_gpu_hours=5.0)
            write_plan(run2, [spec])
            r = ExperimentController(root2.store, run2["run_id"], {"mock": MockExecutor()}, root2.config).step()
            self.assertEqual(r.submitted, [])
            reasons = [e["payload"]["reason"] for e in root2.store.events(run2["run_id"]) if e["event_type"] == "budget_blocked"]
            self.assertTrue(any("explicit approval" in x for x in reasons))
        finally:
            root2.close()

    def test_plan_is_append_only(self) -> None:
        specs = plan(self.root, self.run)
        with self.assertRaisesRegex(ValueError, "append-only"):
            write_plan(self.root.store.get_run(self.run["run_id"]), specs[:2])

    def test_lease_prevents_two_controllers(self) -> None:
        owner = self.root.store.acquire_lease(self.run["run_id"], "controller-a")
        with self.assertRaises(LeaseHeld):
            self.root.store.acquire_lease(self.run["run_id"], "controller-b")
        self.root.store.release_lease(self.run["run_id"], owner)
        self.root.store.acquire_lease(self.run["run_id"], "controller-b")

    def test_deterministic_evaluation_and_verdict(self) -> None:
        specs = plan(self.root, self.run)
        ex = MockExecutor()
        c = self.controller(ex)
        c.step()
        for s in specs:
            write_metrics(s, {"base": 1.0, "cand": 0.9}[s.variant_id] + 0.001 * s.seed)
        c.step()
        result = evaluate_run(self.root.store, self.run["run_id"])
        self.assertEqual(result.verdicts["H1"], "supported")
        rule = result.decisions["rules"]["R1"]
        self.assertEqual(rule["seed_agreement"], 1.0)
        self.root.store.transition(self.run["run_id"], "primary_complete", config=self.root.config)

    def test_evaluation_unsupported_and_inconclusive(self) -> None:
        specs = plan(self.root, self.run)
        c = self.controller(MockExecutor())
        c.step()
        for s in specs:
            write_metrics(s, {"base": 1.0, "cand": 1.1}[s.variant_id])
        c.step()
        self.assertEqual(evaluate_run(self.root.store, self.run["run_id"]).verdicts["H1"], "unsupported")
        # Mixed seeds: one improves, one worsens -> inconclusive.
        write_metrics(specs[2], 0.8)  # cand seed 1
        write_metrics(specs[3], 1.2)  # cand seed 2
        self.assertEqual(evaluate_run(self.root.store, self.run["run_id"]).verdicts["H1"], "inconclusive")

    def test_missing_metrics_are_inconclusive(self) -> None:
        plan(self.root, self.run)
        c = self.controller(MockExecutor())
        c.step()
        c.step()
        self.assertEqual(evaluate_run(self.root.store, self.run["run_id"]).verdicts["H1"], "inconclusive")


class LocalExecutorTest(unittest.TestCase):
    """Real subprocesses: detached runner, marker-based recovery, timeouts."""

    def setUp(self) -> None:
        self.root = Root()
        self.run = advance_to_frozen(self.root)
        self.root.store.transition(self.run["run_id"], "primary_running", config=self.root.config)

    def tearDown(self) -> None:
        self.root.close()

    def _wait(self, controller, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            r = controller.step()
            if r.all_terminal:
                return r
            time.sleep(0.5)
        self.fail(f"jobs not terminal: {r.states}")

    def test_local_jobs_run_and_resume_does_not_resubmit(self) -> None:
        def command(variant, seed, out):
            code = (f"import json,pathlib; p=pathlib.Path(r'{out}'); p.mkdir(parents=True, exist_ok=True);"
                    f"(p/'metrics.json').write_text(json.dumps({{'metrics': {{'y': {{'rmse': {1.0 if variant == 'base' else 0.8} }}}}}}))")
            return [sys.executable, "-c", code]

        plan(self.root, self.run, executor="local", command_for=command)
        ex = LocalExecutor()
        c = ExperimentController(self.root.store, self.run["run_id"], {"local": ex}, self.root.config)
        first = c.step()
        self.assertEqual(len(first.submitted), 4)
        markers = {s: json.loads((Path(j["spec"]["output_dir"]) / "artos_job.json").read_text())["external_id"]
                   for s, j in ((j["idempotency_key"], j) for j in self.root.store.jobs_for_run(self.run["run_id"]))}
        report = self._wait(c)
        self.assertTrue(all(s == "succeeded" for s in report.states.values()))
        # A brand-new controller after "restart" reuses the same external jobs.
        c2 = ExperimentController(self.root.store, self.run["run_id"], {"local": LocalExecutor()}, self.root.config)
        r2 = c2.step()
        self.assertEqual(r2.submitted, [])
        for key, ext in markers.items():
            self.assertEqual(self.root.store.get_job(key)["external_job_id"], ext)
        self.assertEqual(evaluate_run(self.root.store, self.run["run_id"]).verdicts["H1"], "supported")

    def test_wall_clock_limit_fails_job(self) -> None:
        config = dict(self.root.config)
        config["execution"] = dict(config["execution"], job_timeout_seconds=2)
        plan(self.root, self.run, executor="local",
             command_for=lambda v, s, o: [sys.executable, "-c", "import time; time.sleep(60)"])
        c = ExperimentController(self.root.store, self.run["run_id"], {"local": LocalExecutor()}, config)
        c.step()
        report = self._wait(c, 90)
        self.assertTrue(all(s in {"failed", "submitted"} or s == "failed" for s in report.states.values()))
        notes = [j["record"].get("note") or "" for j in self.root.store.jobs_for_run(self.run["run_id"])]
        self.assertTrue(any("wall-clock" in n for n in notes))


if __name__ == "__main__":
    unittest.main()


class RunnerIsolationTest(unittest.TestCase):
    def test_job_runner_does_not_import_pydantic(self) -> None:
        """The detached runner may start under a job interpreter that lacks ARTOS deps."""
        import subprocess
        src = str(Path(__file__).resolve().parents[1] / "src")
        code = f"import sys; sys.path.insert(0, r'{src}'); import artos.job_runner; print('pydantic' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "False")


class LeaseTakeoverTest(unittest.TestCase):
    def test_takeover_only_when_owner_dead(self) -> None:
        import os
        import socket
        root = Root()
        try:
            run = advance_to_frozen(root)
            store = root.store
            dead = f"{socket.gethostname()}:999999"
            store.acquire_lease(run["run_id"], dead)
            with self.assertRaises(LeaseHeld):
                store.acquire_lease(run["run_id"], "new")
            store.acquire_lease(run["run_id"], "new", takeover_dead=True)
            store.release_lease(run["run_id"], "new")
            alive = f"{socket.gethostname()}:{os.getpid()}"
            store.acquire_lease(run["run_id"], alive)
            with self.assertRaises(LeaseHeld):
                store.acquire_lease(run["run_id"], "other", takeover_dead=True)
            self.assertTrue(any(e["event_type"] == "lease_taken_over" for e in store.events(run["run_id"])))
        finally:
            root.close()


class ConcurrencyRetryTest(unittest.TestCase):
    def test_wait_loop_runs_retries_deferred_by_concurrency(self) -> None:
        root = Root()
        try:
            run = advance_to_frozen(root)
            root.store.transition(run["run_id"], "primary_running", config=root.config)
            plan(root, run)
            config = dict(root.config)
            config["execution"] = dict(config["execution"], max_concurrent_jobs=1)
            ex = MockExecutor(outcomes={"cand": ["failed", "succeeded"], "base": ["succeeded"]})
            report = ExperimentController(root.store, run["run_id"], {"mock": ex}, config).run_until_terminal(poll_seconds=0)
            self.assertTrue(report.all_terminal)
            states = {j["variant_id"] + str(j["seed"]): (j["state"], j["attempt"]) for j in root.store.jobs_for_run(run["run_id"])}
            self.assertTrue(all(s == "succeeded" for s, _ in states.values()), states)
            self.assertEqual(states["cand1"][1], 2)
        finally:
            root.close()


class StaleMarkerTest(unittest.TestCase):
    def test_retry_after_unstarted_runner_launches_and_does_not_burn_attempts(self) -> None:
        from artos.utils import atomic_write_json
        from artos.workflow import requeue_unstarted
        root = Root()
        try:
            run = advance_to_frozen(root)
            root.store.transition(run["run_id"], "primary_running", config=root.config)

            def command(variant, seed, out):
                code = (f"import json,pathlib; p=pathlib.Path(r'{out}'); "
                        f"(p/'metrics.json').write_text(json.dumps({{'metrics': {{'y': {{'rmse': 1.0}}}}}}))")
                return [sys.executable, "-c", code]

            specs = plan(root, run, executor="local", command_for=command)
            key = specs[0].idempotency_key()
            root.store.claim_job(key, run["run_id"], specs[0].model_dump(mode="json"), {"output_dir": specs[0].output_dir}, 2)
            Path(specs[0].output_dir).mkdir(parents=True, exist_ok=True)
            atomic_write_json(Path(specs[0].output_dir) / "artos_job.json", {
                "idempotency_key": key, "external_id": "local-old", "state": "starting", "created_at": "2000-01-01T00:00:00Z"})
            root.store.update_job(key, state="failed", attempt=2, external_job_id="local-old")
            ex = LocalExecutor()
            self.assertEqual(requeue_unstarted(root.store, run["run_id"], {"local": ex}, "test"), [key])
            c = ExperimentController(root.store, run["run_id"], {"local": ex}, root.config)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if c.step().all_terminal:
                    break
                time.sleep(0.5)
            job = root.store.get_job(key)
            self.assertEqual(job["state"], "succeeded")
            self.assertNotEqual(job["external_job_id"], "local-old")
            self.assertTrue((Path(specs[0].output_dir) / "artos_job.previous.jsonl").exists())
            self.assertEqual(requeue_unstarted(root.store, run["run_id"], {"local": ex}, "again"), [])
        finally:
            root.close()


class MCPExecutorTest(unittest.TestCase):
    """Generic MCP executor against a minimal stdio MCP server (no external dependencies)."""

    SERVER = r'''
import json, sys
jobs, by_req = {}, {}
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        out = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
    else:
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        if name == "submit":
            rid = args.get("client_request_id")
            jid = by_req.get(rid) or f"j{len(jobs) + 1}"
            jobs[jid] = "done"; by_req[rid] = jid
            res = {"job_id": jid}
        elif name == "get_job_status":
            res = {"status": "done", "return_code": 0}
        elif name == "find_job_by_client_request_id":
            res = {"status": "not_found"}
        else:
            res = {"status": "ok"}
        out = {"content": [{"type": "text", "text": json.dumps(res)}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": out}), flush=True)
'''

    def test_submit_poll_and_request_id(self) -> None:
        from artos.executors import MCPExecutor
        from artos.models import ExperimentSpec
        tmp = Path(self.id().replace(".", "_"))
        server = Root()
        try:
            script = server.tmp / "fake_mcp.py"
            script.write_text(self.SERVER, encoding="utf-8")
            ex = MCPExecutor([sys.executable, str(script)])
            spec = ExperimentSpec(run_id="r", contract_sha256="a" * 64, variant_id="v", seed=0, executor="mcp",
                                  output_dir=str(server.tmp / "o"), tool="submit", tool_args={"x": 1})
            key = spec.idempotency_key()
            job = ex.submit(spec, key, None)
            self.assertEqual(job, "j1")
            self.assertEqual(ex.poll(job, spec).state, "succeeded")
            self.assertIsNone(ex.recover(spec, key))
        finally:
            server.close()
        del tmp
