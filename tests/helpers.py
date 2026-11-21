"""Shared fixtures: a disposable ARTOS root and a run advanced through its gates."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from artos.config import load_config
from artos.hypotheses import aggregate_judge_panel, create_blind_ranking_packet, rank_scored_hypotheses
from artos.models import (
    AnalysisContract,
    CardScore,
    ExperimentSpec,
    HypothesisSet,
    JudgeScores,
    Route,
    RouteDecision,
    canonical_sha256,
    load_artifact,
    write_artifact,
)
from artos.models import utc_now as utc_dt
from artos.runs import create_run, freeze_contract
from artos.store import Store
from artos.workflow import write_plan

REPO = Path(__file__).resolve().parents[1]


class Root:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="artos-test-"))
        config = json.loads((REPO / "artos.example.json").read_text(encoding="utf-8"))
        config["research"]["minimum_hypothesis_judges"] = 2
        config["research"]["minimum_codex_judges"] = 0
        (self.tmp / "artos.json").write_text(json.dumps(config), encoding="utf-8")
        self.config = load_config(self.tmp)
        self.store = Store(self.tmp / "state" / "artos.sqlite")

    def close(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def hypothesis_set(run: dict[str, Any]) -> HypothesisSet:
    return HypothesisSet.model_validate({
        "run_id": run["run_id"],
        "mother_question_sha256": run["mother_question_sha256"],
        "proposer": "proposer-agent",
        "evidence": [{
            "evidence_id": "E1", "citation": "doi:10.0000/synthetic.0001", "title": "A synthetic source used only in tests",
            "first_author": "Example", "year": 2020, "verified": True, "verification_method": "synthetic fixture",
            "claim": "treatment A changed the outcome in a prior setting", "claim_type": "published_finding", "relation": "supports",
        }],
        "hypotheses": [
            {"hypothesis_id": "H1", "statement": "Treatment A reduces error relative to control B",
             "mechanism": "A adds information B lacks", "expected_benefit": "lower error",
             "implementation_cost": "low", "falsification": "A is no better than B beyond seed noise",
             "supporting_evidence": ["E1"], "proposer_rank": 1, "proposer_notes": "my favourite"},
            {"hypothesis_id": "H2", "statement": "A cheaper variant of A is sufficient",
             "mechanism": "the extra component of A is redundant", "expected_benefit": "same as H1 at lower cost",
             "implementation_cost": "low", "falsification": "full A beats the cheaper variant", "proposer_rank": 2},
            {"hypothesis_id": "H0", "statement": "Neither variant changes the outcome",
             "mechanism": "no causal signal", "expected_benefit": "none", "implementation_cost": "none",
             "falsification": "any variant beats the control", "role": "null", "proposer_rank": 3},
        ],
    })


def contract_for(run: dict[str, Any], **extra: Any) -> AnalysisContract:
    payload = {
        "contract_id": "c1",
        "mother_question_sha256": run["mother_question_sha256"],
        "selected_hypothesis_ids": ["H1"],
        "datasets": [{"name": "synthetic", "role": "target", "version": "v1"}],
        "metrics": [{"name": "rmse", "definition": "root mean square error", "direction": "lower", "group": "primary"}],
        "null_controls": [{"name": "nohistory", "definition": "same capacity, no history"}],
        "stopping_rule": "fixed budget",
        "seeds": [1, 2],
        "variants": [
            {"variant_id": "base", "description": "baseline", "role": "baseline"},
            {"variant_id": "cand", "description": "candidate", "role": "candidate"},
        ],
        "acceptance_rules": [{
            "rule_id": "R1", "hypothesis_id": "H1", "group": "primary", "candidate": "cand", "reference": "base",
            "variable": "y", "metric": "rmse", "mode": "rel_improve", "threshold": 0.05}],
        "compute_budget": {"cpu_hours": 10, "gpu_hours": 0, "max_attempts_per_job": 2},
    }
    payload.update(extra)
    return AnalysisContract.model_validate(payload)


def judge(run_dir: Path, judge_id: str, favour: str) -> Path:
    packet_path = run_dir / "questions" / "ranking_packet.json"
    packet, _ = load_artifact(packet_path, "ranking_packet")
    key = json.loads((run_dir / "questions" / "ranking_key.json").read_text())["card_to_hypothesis"]
    cards = []
    for card in packet.cards:  # type: ignore[attr-defined]
        s = 0.9 if key[card.card_id] == favour else 0.4
        cards.append(CardScore(card_id=card.card_id, scores={c: s for c in packet.criteria},  # type: ignore[attr-defined]
                               verdict="accept", rationale="scored from card content only"))
    js = JudgeScores(run_id=run_dir.name, judge_id=judge_id, judge_context="separate claude -p session, packet only",
                     packet_sha256=canonical_sha256(packet.model_dump(mode="json")), cards=cards)
    path = run_dir / "work" / f"{judge_id}.json"
    write_artifact(path, js)
    return path


def advance_to_frozen(root: Root, contract: AnalysisContract | None = None, contract_factory=None) -> dict[str, Any]:
    store, config = root.store, root.config
    run = create_run(store, root.tmp / "projects", "Does treatment A reduce error relative to control B?", "question", "demo", "demo")
    run_dir = Path(run["run_dir"])
    store.transition(run["run_id"], "inventory_ready", config=config)
    store.transition(run["run_id"], "oriented", config=config)
    write_artifact(run_dir / "04_route_decision.json", RouteDecision(
        run_id=run["run_id"], route=Route.hypothesis, rationale="competing mechanisms need a discriminating test",
        decided_at=utc_dt(), decided_by="test"))
    store.transition(run["run_id"], "routed_hypothesis", config=config)
    write_artifact(run_dir / "questions" / "hypotheses.json", hypothesis_set(run))
    create_blind_ranking_packet(run_dir / "questions" / "hypotheses.json", seed=7)
    j1, j2 = judge(run_dir, "judge-a", "H1"), judge(run_dir, "judge-b", "H2")
    aggregate_judge_panel(run_dir / "questions" / "ranking_packet.json", [j1, j2],
                          run_dir / "questions" / "hypotheses_scored_panel.json", 2, 0)
    rank_scored_hypotheses(run_dir / "questions" / "hypotheses_scored_panel.json", run_dir / "questions")
    store.transition(run["run_id"], "hypotheses_ranked", config=config)
    current = store.get_run(run["run_id"])
    contract = contract or (contract_factory(current) if contract_factory else contract_for(current))
    contract_path = run_dir / "contract" / "contract.json"
    write_artifact(contract_path, contract)
    digest = freeze_contract(store, run["run_id"], contract_path)
    store.transition(run["run_id"], "contract_frozen", {"sha256": digest}, config=config)
    return store.get_run(run["run_id"])


def plan(root: Root, run: dict[str, Any], executor: str = "mock", command_for=None) -> list[ExperimentSpec]:
    specs = []
    for variant in ("base", "cand"):
        for seed in (1, 2):
            out = Path(run["run_dir"]) / "jobs" / f"{variant}-s{seed}"
            specs.append(ExperimentSpec(
                run_id=run["run_id"], contract_sha256=run["contract_sha256"], variant_id=variant, seed=seed,
                executor=executor, output_dir=str(out), estimated_cpu_hours=0.5,
                command=command_for(variant, seed, out) if command_for else []))
    write_plan(run, specs)
    return specs


def write_metrics(spec: ExperimentSpec, rmse: float) -> None:
    out = Path(spec.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps({"metrics": {"y": {"rmse": rmse}}, "split": "test"}), encoding="utf-8")
