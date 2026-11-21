"""Scientific gates: blind ranking, frozen contracts, audit blocking, reproduction wording, role isolation."""
from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path

from artos.gates import GateError, check_reproduction_language
from artos.hypotheses import create_blind_ranking_packet
from artos.models import AuditFindings, EvaluationResult, write_artifact
from artos.models import utc_now as utc_dt
from artos.runs import create_run, freeze_contract
from artos.store import StoreError

from helpers import Root, advance_to_frozen, contract_for, judge


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Root()

    def tearDown(self) -> None:
        self.root.close()

    def test_happy_path_reaches_frozen(self) -> None:
        run = advance_to_frozen(self.root)
        self.assertEqual(run["status"], "contract_frozen")
        self.assertTrue(self.root.store.verify_event_chain())

    def test_packet_hides_proposer_ranking(self) -> None:
        run = advance_to_frozen(self.root)
        packet = json.loads((Path(run["run_dir"]) / "questions" / "ranking_packet.json").read_text())
        text = json.dumps(packet)
        self.assertNotIn("proposer_rank", text)
        self.assertNotIn("my favourite", text)
        self.assertNotIn("H1", [c["card_id"] for c in packet["cards"]])

    def test_ranking_requires_enough_blind_judges(self) -> None:
        store, config = self.root.store, dict(self.root.config)
        config["research"] = dict(config["research"], minimum_hypothesis_judges=3)
        run = create_run(store, self.root.tmp / "projects", "q?", "question", "p", "t")
        from helpers import hypothesis_set
        from artos.hypotheses import aggregate_judge_panel, rank_scored_hypotheses
        from artos.models import Route, RouteDecision
        rd = Path(run["run_dir"])
        for s in ("inventory_ready", "oriented"):
            store.transition(run["run_id"], s, config=config)
        write_artifact(rd / "04_route_decision.json", RouteDecision(run_id=run["run_id"], route=Route.hypothesis,
                       rationale="needs a discriminating test", decided_at=utc_dt(), decided_by="t"))
        store.transition(run["run_id"], "routed_hypothesis", config=config)
        write_artifact(rd / "questions" / "hypotheses.json", hypothesis_set(run))
        create_blind_ranking_packet(rd / "questions" / "hypotheses.json", seed=3)
        js = [judge(rd, "a", "H1"), judge(rd, "b", "H2")]
        aggregate_judge_panel(rd / "questions" / "ranking_packet.json", js, rd / "questions" / "panel.json", 2, 0)
        rank_scored_hypotheses(rd / "questions" / "panel.json", rd / "questions")
        with self.assertRaisesRegex(GateError, "requires 3"):
            store.transition(run["run_id"], "hypotheses_ranked", config=config)
        self.assertEqual(store.get_run(run["run_id"])["status"], "routed_hypothesis")

    def test_proposer_cannot_judge(self) -> None:
        store, config = self.root.store, self.root.config
        run = create_run(store, self.root.tmp / "projects", "q?", "question", "p", "t2")
        from helpers import hypothesis_set
        from artos.hypotheses import aggregate_judge_panel, rank_scored_hypotheses
        from artos.models import Route, RouteDecision
        rd = Path(run["run_dir"])
        for s in ("inventory_ready", "oriented"):
            store.transition(run["run_id"], s, config=config)
        write_artifact(rd / "04_route_decision.json", RouteDecision(run_id=run["run_id"], route=Route.hypothesis,
                       rationale="needs a discriminating test", decided_at=utc_dt(), decided_by="t"))
        store.transition(run["run_id"], "routed_hypothesis", config=config)
        write_artifact(rd / "questions" / "hypotheses.json", hypothesis_set(run))
        create_blind_ranking_packet(rd / "questions" / "hypotheses.json", seed=3)
        js = [judge(rd, "proposer-agent", "H1"), judge(rd, "b", "H2")]
        aggregate_judge_panel(rd / "questions" / "ranking_packet.json", js, rd / "questions" / "panel.json", 2, 0)
        rank_scored_hypotheses(rd / "questions" / "panel.json", rd / "questions")
        with self.assertRaisesRegex(GateError, "judged its own"):
            store.transition(run["run_id"], "hypotheses_ranked", config=config)

    def test_route_must_match_decision(self) -> None:
        store, config = self.root.store, self.root.config
        run = create_run(store, self.root.tmp / "projects", "q?", "question", "p", "t3")
        from artos.models import Route, RouteDecision
        for s in ("inventory_ready", "oriented"):
            store.transition(run["run_id"], s, config=config)
        with self.assertRaisesRegex(GateError, "missing"):
            store.transition(run["run_id"], "routed_direct", config=config)
        write_artifact(Path(run["run_dir"]) / "04_route_decision.json", RouteDecision(
            run_id=run["run_id"], route=Route.hypothesis, rationale="needs a discriminating test", decided_at=utc_dt(), decided_by="t"))
        with self.assertRaisesRegex(GateError, "route decision says"):
            store.transition(run["run_id"], "routed_direct", config=config)

    def test_frozen_contract_cannot_be_replaced(self) -> None:
        run = advance_to_frozen(self.root)
        other = contract_for(run, stopping_rule="changed after seeing data")
        path = Path(run["run_dir"]) / "contract" / "contract_v2.json"
        write_artifact(path, other)
        with self.assertRaisesRegex(ValueError, "refusing to replace"):
            freeze_contract(self.root.store, run["run_id"], path)
        with self.assertRaises(StoreError):
            self.root.store.set_contract_hash(run["run_id"], other.content_sha256())

    def test_tampered_frozen_contract_blocks_execution(self) -> None:
        run = advance_to_frozen(self.root)
        frozen = Path(run["run_dir"]) / "contract" / "contract.frozen.json"
        os.chmod(frozen, stat.S_IWRITE | stat.S_IREAD)
        data = json.loads(frozen.read_text())
        data["contract"]["stopping_rule"] = "moved goalposts"
        frozen.write_text(json.dumps(data))
        with self.assertRaisesRegex(GateError, "hash does not match"):
            self.root.store.transition(run["run_id"], "primary_running", config=self.root.config)

    def test_primary_complete_requires_evaluation_for_this_contract(self) -> None:
        run = advance_to_frozen(self.root)
        store = self.root.store
        store.transition(run["run_id"], "primary_running", config=self.root.config)
        with self.assertRaisesRegex(GateError, "evaluation"):
            store.transition(run["run_id"], "primary_complete", config=self.root.config)
        write_artifact(Path(run["run_dir"]) / "evaluation" / "evaluation.json", EvaluationResult(
            run_id=run["run_id"], contract_sha256="b" * 64, computed_by="x", computed_at=utc_dt(), values=[]))
        with self.assertRaisesRegex(GateError, "different contract"):
            store.transition(run["run_id"], "primary_complete", config=self.root.config)

    def _to_audit_running(self):
        run = advance_to_frozen(self.root)
        store, config = self.root.store, self.root.config
        store.transition(run["run_id"], "primary_running", config=config)
        write_artifact(Path(run["run_dir"]) / "evaluation" / "evaluation.json", EvaluationResult(
            run_id=run["run_id"], contract_sha256=run["contract_sha256"], computed_by="x", computed_at=utc_dt(), values=[]))
        store.transition(run["run_id"], "primary_complete", config=config)
        store.transition(run["run_id"], "audit_running", config=config)
        return store.get_run(run["run_id"])

    def test_open_major_finding_blocks_completion(self) -> None:
        run = self._to_audit_running()
        store, config = self.root.store, self.root.config
        audit = AuditFindings(run_id=run["run_id"], gate="repair_required",
                              independence={"separate_agent_session": True},
                              findings=[{"finding_id": "F1", "severity": "major", "description": "leakage", "status": "open"}])
        write_artifact(Path(run["run_dir"]) / "reviews" / "audit_findings.json", audit)
        with self.assertRaisesRegex(GateError, "unresolved critical/major"):
            store.transition(run["run_id"], "audit_complete", config=config)
        store.transition(run["run_id"], "repair_required", config=config)
        # Repair loop is bounded.
        store.transition(run["run_id"], "primary_running", config=config)
        self.assertEqual(store.get_run(run["run_id"])["repair_rounds"], 1)

    def test_repair_rounds_are_bounded(self) -> None:
        run = self._to_audit_running()
        store, config = self.root.store, dict(self.root.config)
        config["research"] = dict(config["research"], maximum_repair_rounds=0)
        store.transition(run["run_id"], "repair_required", config=config)
        with self.assertRaisesRegex(GateError, "repair round limit"):
            store.transition(run["run_id"], "primary_running", config=config)

    def test_reproduction_claim_requires_recomputation(self) -> None:
        no = AuditFindings(run_id="r", gate="pass", independence={"separate_agent_session": True})
        with self.assertRaises(GateError):
            check_reproduction_language("Results were independently reproduced.", no)
        check_reproduction_language("This was not independently reproduced.", no)
        yes = AuditFindings(run_id="r", gate="pass", independence={
            "separate_agent_session": True, "headline_results_independently_recomputed": ["R1"],
            "recomputation_method": "numpy re-implementation of RMSE from saved predictions"})
        check_reproduction_language("Results were independently reproduced.", yes)

    def test_complete_blocked_without_summary_and_with_false_claim(self) -> None:
        run = self._to_audit_running()
        store, config = self.root.store, self.root.config
        audit = AuditFindings(run_id=run["run_id"], gate="pass_with_disclosures", independence={"separate_agent_session": True})
        write_artifact(Path(run["run_dir"]) / "reviews" / "audit_findings.json", audit)
        store.transition(run["run_id"], "audit_complete", config=config)
        store.transition(run["run_id"], "reporting", config=config)
        with self.assertRaisesRegex(GateError, "final summary missing"):
            store.transition(run["run_id"], "complete", config=config)
        (Path(run["run_dir"]) / "12_final_summary.md").write_text("The effect was independently replicated.")
        with self.assertRaisesRegex(GateError, "independent reproduction"):
            store.transition(run["run_id"], "complete", config=config)
        (Path(run["run_dir"]) / "12_final_summary.md").write_text("Second-agent review; not independently reproduced.")
        store.transition(run["run_id"], "complete", config=config)

    def test_shared_work_dir_blocks_audit(self) -> None:
        run = advance_to_frozen(self.root)
        store = self.root.store
        shared = str(Path(run["run_dir"]) / "work" / "shared")
        for role in ("primary", "adversary"):
            store.record_session({"session_name": f"{role}", "run_id": run["run_id"], "role": role, "tmux_name": f"t-{role}",
                                  "spec_path": "x", "work_dir": shared})
        store.transition(run["run_id"], "primary_running", config=self.root.config)
        write_artifact(Path(run["run_dir"]) / "evaluation" / "evaluation.json", EvaluationResult(
            run_id=run["run_id"], contract_sha256=run["contract_sha256"], computed_by="x", computed_at=utc_dt(), values=[]))
        store.transition(run["run_id"], "primary_complete", config=self.root.config)
        with self.assertRaisesRegex(GateError, "share a writable"):
            store.transition(run["run_id"], "audit_running", config=self.root.config)

    def test_invalid_transition_leaves_state_unchanged(self) -> None:
        run = advance_to_frozen(self.root)
        with self.assertRaises(ValueError):
            self.root.store.transition(run["run_id"], "complete", config=self.root.config)
        self.assertEqual(self.root.store.get_run(run["run_id"])["status"], "contract_frozen")

    def test_event_chain_detects_tampering(self) -> None:
        advance_to_frozen(self.root)
        store = self.root.store
        self.assertTrue(store.verify_event_chain())
        store._db.execute("UPDATE events SET payload='{}' WHERE seq=2")
        self.assertFalse(store.verify_event_chain())


if __name__ == "__main__":
    unittest.main()


class AuditNormalizationTest(unittest.TestCase):
    def test_normalization_changes_layout_not_severity(self) -> None:
        from artos.audit import normalize_agent_audit
        raw = {"kind": "audit_findings", "schema_version": 2, "run_id": "r", "gate": "repair_required",
               "checks": [{"check_id": "C1", "result": "pass"}],
               "findings": [{"finding_id": "F1", "severity": "major", "status": "open", "description": "d",
                             "evidence": "file.json", "repair": "fix it"}]}
        data, notes = normalize_agent_audit(raw)
        audit = AuditFindings.model_validate(data)
        self.assertEqual(audit.checks["C1"]["result"], "pass")
        self.assertEqual(audit.findings[0].severity.value, "major")
        self.assertEqual(audit.findings[0].evidence, ["file.json"])
        self.assertIn("fix it", audit.findings[0].resolution)
        self.assertEqual([f.finding_id for f in audit.blocking_findings], ["F1"])
        self.assertEqual(len(notes), 3)
