"""Schema validation and legacy-artifact compatibility."""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from artos.models import (
    AnalysisContract,
    ArtifactError,
    AuditFindings,
    ExperimentSpec,
    HypothesisSet,
    JudgeScores,
    LiteratureEvidence,
    ResearchQuestion,
    load_artifact,
    parse_artifact,
)
from artos.models import utc_now as utc_dt

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "synthetic-demo-run"
H = "a" * 64


class SchemaTest(unittest.TestCase):
    def test_legacy_example_artifacts_load(self) -> None:
        contract, notes = load_artifact(EXAMPLE / "contract.json", "analysis_contract")
        self.assertIsInstance(contract, AnalysisContract)
        self.assertTrue(any("upgraded" in n for n in notes))
        audit, notes = load_artifact(EXAMPLE / "reviews" / "audit_findings.json", "audit_findings")
        self.assertIsInstance(audit, AuditFindings)
        # The legacy boolean is retained, but it is not trusted as reproduction evidence.
        self.assertFalse(audit.independently_reproduced)
        self.assertTrue(any("not treated as reproduction" in n for n in notes))

    def test_contract_requires_core_fields(self) -> None:
        with self.assertRaises(ArtifactError):
            parse_artifact("analysis_contract", {"contract_id": "x", "mother_question_sha256": H})
        with self.assertRaises(ArtifactError):
            parse_artifact("analysis_contract", {
                "contract_id": "x", "mother_question_sha256": "not-a-hash", "selected_hypothesis_ids": ["H1"],
                "datasets": [{"name": "d", "role": "t"}], "metrics": [{"name": "m", "definition": "d"}],
                "null_controls": [{"name": "n", "definition": "d"}], "stopping_rule": "s"})

    def test_contract_hash_is_order_independent(self) -> None:
        base = {"contract_id": "x", "mother_question_sha256": H, "selected_hypothesis_ids": ["H1"],
                "datasets": [{"name": "d", "role": "t"}], "metrics": [{"name": "m", "definition": "d"}],
                "null_controls": [{"name": "n", "definition": "d"}], "stopping_rule": "s"}
        reordered = dict(reversed(list(base.items())))
        a = AnalysisContract.model_validate(base).content_sha256()
        b = AnalysisContract.model_validate(reordered).content_sha256()
        self.assertEqual(a, b)
        changed = AnalysisContract.model_validate({**base, "stopping_rule": "t"}).content_sha256()
        self.assertNotEqual(a, changed)

    def test_rules_must_reference_declared_variants_and_selected_hypotheses(self) -> None:
        base = {"contract_id": "x", "mother_question_sha256": H, "selected_hypothesis_ids": ["H1"],
                "datasets": [{"name": "d", "role": "t"}], "metrics": [{"name": "m", "definition": "d"}],
                "null_controls": [{"name": "n", "definition": "d"}], "stopping_rule": "s",
                "variants": [{"variant_id": "a", "description": "a", "role": "baseline"}]}
        rule = {"rule_id": "R", "hypothesis_id": "H1", "group": "primary", "candidate": "b", "reference": "a",
                "variable": "v", "metric": "m", "mode": "rel_improve", "threshold": 0.1}
        with self.assertRaises(ValidationError):
            AnalysisContract.model_validate({**base, "acceptance_rules": [rule]})
        with self.assertRaises(ValidationError):
            AnalysisContract.model_validate({**base, "variants": base["variants"] + [{"variant_id": "b", "description": "b", "role": "candidate"}],
                                             "acceptance_rules": [{**rule, "hypothesis_id": "H9"}]})

    def test_question_hashes_are_verified(self) -> None:
        text = "Is X true?"
        digest = hashlib.sha256(text.encode()).hexdigest()
        ResearchQuestion(run_id="r", source_type="question", verbatim_source=text, source_sha256=digest,
                         mother_question=text, mother_question_sha256=digest, received_at=utc_dt())
        with self.assertRaises(ValidationError):
            ResearchQuestion(run_id="r", source_type="question", verbatim_source=text + " edited", source_sha256=digest,
                             mother_question=text, mother_question_sha256=digest, received_at=utc_dt())

    def test_hypothesis_set_requires_competition_and_known_evidence(self) -> None:
        one = {"hypothesis_id": "H1", "statement": "a long enough statement", "mechanism": "mech",
               "expected_benefit": "b", "implementation_cost": "c", "falsification": "a falsifying result"}
        with self.assertRaises(ValidationError):
            HypothesisSet(run_id="r", mother_question_sha256=H, proposer="p", hypotheses=[one])
        with self.assertRaises(ValidationError):
            HypothesisSet(run_id="r", mother_question_sha256=H, proposer="p",
                          hypotheses=[{**one, "supporting_evidence": ["E9"]}, {**one, "hypothesis_id": "H2"}])

    def test_verified_evidence_needs_method(self) -> None:
        with self.assertRaises(ValidationError):
            LiteratureEvidence(evidence_id="E", citation="arXiv:1", title="t", first_author="a", year=2020,
                               verified=True, claim="claims x", claim_type="published_finding", relation="supports")

    def test_judge_that_saw_proposer_ranking_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            JudgeScores(run_id="r", judge_id="j", judge_context="ctx", saw_proposer_ranking=True, packet_sha256=H, cards=[])

    def test_audit_pass_with_open_major_is_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            AuditFindings(run_id="r", gate="pass", findings=[
                {"finding_id": "F1", "severity": "major", "description": "d", "status": "open"}])
        ok = AuditFindings(run_id="r", gate="pass", findings=[
            {"finding_id": "F1", "severity": "major", "description": "d", "status": "repaired"}])
        self.assertEqual(ok.blocking_findings, [])

    def test_idempotency_key_is_stable_and_sensitive(self) -> None:
        spec = ExperimentSpec(run_id="r", contract_sha256=H, variant_id="v", seed=1, executor="mock", output_dir="o")
        self.assertEqual(spec.idempotency_key(), ExperimentSpec.model_validate(json.loads(spec.model_dump_json())).idempotency_key())
        self.assertNotEqual(spec.idempotency_key(), spec.model_copy(update={"seed": 2}).idempotency_key())

    def test_unknown_fields_rejected_on_strict_models(self) -> None:
        with self.assertRaises(ValidationError):
            ExperimentSpec(run_id="r", contract_sha256=H, variant_id="v", seed=1, executor="mock", output_dir="o", surprise=1)


if __name__ == "__main__":
    unittest.main()
