"""OKF knowledge-bundle export: conformance, trust fields, typed edges, merging across runs."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from artos.evaluation import evaluate_run
from artos.executors import MockExecutor
from artos.knowledge import export_bundle, validate_bundle
from artos.models import AuditFindings, write_artifact
from artos.workflow import ExperimentController

from helpers import Root, advance_to_frozen, plan, write_metrics

AT = "2026-11-21T00:00:00Z"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text[4:text.find("\n---", 4)])


class KnowledgeExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Root()
        self.run = advance_to_frozen(self.root)
        store, config = self.root.store, self.root.config
        store.transition(self.run["run_id"], "primary_running", config=config)
        specs = plan(self.root, self.run)
        c = ExperimentController(store, self.run["run_id"], {"mock": MockExecutor()}, config)
        c.step()
        for s in specs:
            write_metrics(s, {"base": 1.0, "cand": 0.9}[s.variant_id])
        c.step()
        evaluate_run(store, self.run["run_id"])
        self._audit(recomputed=True)
        store.record_event("decision_record", {"decision_id": "D1", "actor": "orchestrator", "decision": "use 2 seeds",
                                               "rationale": "budget", "evidence": ["contract"]}, self.run["run_id"])
        self.out = self.root.tmp / "kb"

    def _audit(self, recomputed: bool) -> None:
        independence = {"separate_agent_session": True}
        if recomputed:
            independence.update(headline_results_independently_recomputed=["R1"],
                                recomputation_method="numpy reimplementation from saved outputs")
        write_artifact(Path(self.run["run_dir"]) / "reviews" / "audit_findings.json", AuditFindings(
            run_id=self.run["run_id"], gate="pass_with_disclosures", independence=independence,
            findings=[{"finding_id": "F1", "severity": "minor", "description": "toy data", "status": "accepted"}]))

    def tearDown(self) -> None:
        self.root.close()

    def test_bundle_is_okf_conformant_with_typed_graph(self) -> None:
        res = export_bundle(self.root.store, [self.run["run_id"]], self.out, title="KB", description="test bundle", at=AT)
        val = validate_bundle(self.out)
        self.assertTrue(val["conformant"], val["errors"])
        self.assertEqual([w for w in val["warnings"] if "broken link" in w], [])
        for t in ("Run", "Research Question", "Hypothesis", "Evidence", "Analysis Contract", "Dataset", "Experiment",
                  "Evaluation", "Audit", "Audit Finding", "Decision"):
            self.assertIn(t, res["by_type"], t)
        self.assertEqual(_frontmatter(self.out / "index.md")["okf_version"], "0.3")
        self.assertFalse((self.out / "log.md").read_text(encoding="utf-8").startswith("---"))
        graph = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))
        rels = {e["relation"] for e in graph["edges"]}
        self.assertTrue({"proposes", "supported_by", "tests", "executed", "supported", "audited_by", "raised"} <= rels, rels)
        for e in graph["edges"]:
            self.assertTrue((self.out / e["target"].lstrip("/")).exists(), e)
        self.assertIn("## Hypotheses", (self.out / "index.md").read_text(encoding="utf-8"))

    def test_trust_fields(self) -> None:
        export_bundle(self.root.store, [self.run["run_id"]], self.out, title="KB", description="d", at=AT)
        hyp = next((self.out / "hypotheses").glob("*-h1.md"))
        fm = _frontmatter(hyp)
        self.assertEqual(fm["type"], "Hypothesis")
        self.assertEqual(fm["status"], "supported")
        self.assertTrue(fm["generated"]["by"].startswith("artos/"))
        self.assertTrue(fm["sources"][0]["id"].startswith("sha256:"))
        self.assertIn({"relation": "supported_by", "target": "/evidence/doi-10-0000-synthetic-0001.md"}, fm["x-artos-relations"])
        self.assertNotIn("proposer_rank", hyp.read_text(encoding="utf-8"))
        ev = _frontmatter(next((self.out / "evaluations").glob("*.md")))
        self.assertTrue(ev["verified"][0]["by"].startswith("process:"))  # a machine audit is never "human:"

    def test_no_verified_without_recomputation(self) -> None:
        self._audit(recomputed=False)
        export_bundle(self.root.store, [self.run["run_id"]], self.out, title="KB", description="d", at=AT)
        self.assertNotIn("verified", _frontmatter(next((self.out / "evaluations").glob("*.md"))))

    def test_runs_accumulate_into_one_knowledge_base(self) -> None:
        run2 = advance_to_frozen(self.root)
        res = export_bundle(self.root.store, [self.run["run_id"], run2["run_id"]], self.out, title="KB",
                            description="d", at=AT)
        evidence = list((self.out / "evidence").glob("*.md"))
        self.assertEqual(len(evidence), 1)            # the same source merges into one node
        self.assertEqual(res["by_type"]["Run"], 2)
        self.assertEqual(len(_frontmatter(evidence[0])["sources"]), 2)  # provenance from both runs kept
        self.assertEqual(len(list((self.out / "hypotheses").glob("*.md"))), 6)

    def test_validator_rejects_missing_type_and_log_frontmatter(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / "bad.md").write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")
        (self.out / "log.md").write_text("---\ntype: x\n---\n", encoding="utf-8")
        val = validate_bundle(self.out)
        self.assertFalse(val["conformant"])
        self.assertEqual(len(val["errors"]), 2)


if __name__ == "__main__":
    unittest.main()
