"""Deterministic scientific gates checked on every run transition.

The state machine says which transitions exist; these gates say whether the
evidence on disk allows one *now*. Every gate reads validated artifacts from
the run directory and raises :class:`GateError` with an actionable message.
Nothing here asks a language model anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import (
    ArtifactError,
    AuditFindings,
    AuditGate,
    FrozenContract,
    HypothesisSet,
    RankingPacket,
    RankingResult,
    RouteDecision,
    canonical_sha256,
    load_artifact,
)
from .utils import sha256_file


class GateError(ValueError):
    """A scientific gate refused a transition."""


# Canonical artifact locations within a run directory.
def paths(run_dir: Path) -> dict[str, Path]:
    run_dir = Path(run_dir)
    return {
        "question": run_dir / "questions" / "research_question.json",
        "route": run_dir / "04_route_decision.json",
        "hypotheses": run_dir / "questions" / "hypotheses.json",
        "packet": run_dir / "questions" / "ranking_packet.json",
        "judges": run_dir / "questions" / "judges",
        "ranking": run_dir / "questions" / "ranking.json",
        "contract_frozen": run_dir / "contract" / "contract.frozen.json",
        "evaluation": run_dir / "evaluation" / "evaluation.json",
        "audit": run_dir / "reviews" / "audit_findings.json",
        "final_summary": run_dir / "12_final_summary.md",
        "work": run_dir / "work",
    }


def _load(path: Path, kind: str):
    if not path.exists():
        raise GateError(f"required artifact missing: {path}")
    try:
        artifact, _ = load_artifact(path, kind)
    except ArtifactError as exc:
        raise GateError(str(exc)) from exc
    return artifact


def verify_frozen_contract(run: dict[str, Any]) -> FrozenContract:
    p = paths(Path(run["run_dir"]))["contract_frozen"]
    frozen: FrozenContract = _load(p, "frozen_contract")
    if not run.get("contract_sha256"):
        raise GateError("run has no recorded contract hash; freeze the contract first")
    if frozen.contract_sha256 != run["contract_sha256"]:
        raise GateError(
            f"frozen contract on disk ({frozen.contract_sha256[:12]}) differs from the hash recorded "
            f"in the run store ({run['contract_sha256'][:12]}); the contract was modified after freezing"
        )
    return frozen


def _check_blind_packet(packet: RankingPacket, hypotheses: HypothesisSet) -> None:
    if packet.hypothesis_set_sha256 != canonical_sha256(hypotheses.model_dump(mode="json")):
        raise GateError("ranking packet was built from a different hypothesis set than the one on disk")
    raw = packet.model_dump(mode="json")
    text = repr(raw)
    for leaked in ("proposer_rank", "proposer_notes"):
        if leaked in text:
            raise GateError(f"ranking packet leaks proposer field {leaked!r}")
    proposer_order = [h.statement for h in hypotheses.hypotheses]
    packet_order = [c.statement for c in packet.cards]
    if len(proposer_order) > 2 and packet_order == proposer_order:
        raise GateError("ranking packet preserves the proposer's hypothesis order; reshuffle with a different seed")


def check_ranking(run: dict[str, Any], config: dict[str, Any]) -> RankingResult:
    p = paths(Path(run["run_dir"]))
    hypotheses: HypothesisSet = _load(p["hypotheses"], "hypothesis_set")
    if hypotheses.mother_question_sha256 != run["mother_question_sha256"]:
        raise GateError("hypothesis set refers to a different mother question")
    roles = {h.role for h in hypotheses.hypotheses}
    if len(hypotheses.hypotheses) < 2 or roles == {"control"}:
        raise GateError("at least two competing hypotheses are required")
    packet: RankingPacket = _load(p["packet"], "ranking_packet")
    _check_blind_packet(packet, hypotheses)
    ranking: RankingResult = _load(p["ranking"], "ranking_result")
    packet_hash = canonical_sha256(packet.model_dump(mode="json"))
    if ranking.packet_sha256 != packet_hash:
        raise GateError("ranking result was computed from a different packet")
    research = config.get("research", {})
    minimum = int(research.get("minimum_hypothesis_judges", 1))
    if len(set(ranking.judge_ids)) < minimum:
        raise GateError(f"{len(set(ranking.judge_ids))} blind judge(s) scored the packet; configuration requires {minimum}")
    if research.get("require_independent_ranking", True) and hypotheses.proposer in ranking.judge_ids:
        raise GateError(f"proposer {hypotheses.proposer!r} also judged its own hypotheses")
    return ranking


def check_audit_for_completion(run: dict[str, Any], config: dict[str, Any]) -> AuditFindings | None:
    p = paths(Path(run["run_dir"]))
    required = bool(config.get("research", {}).get("require_adversarial_audit", True)) and run.get("route") != "direct"
    if not p["audit"].exists():
        if required:
            raise GateError("adversarial audit is required before reporting/completion; none on disk")
        return None
    audit: AuditFindings = _load(p["audit"], "audit_findings")
    blocking = audit.blocking_findings
    if blocking:
        listed = ", ".join(f"{f.finding_id}({f.severity.value})" for f in blocking)
        raise GateError(f"unresolved critical/major audit findings block finalization: {listed}")
    if audit.gate in {AuditGate.not_ready, AuditGate.repair_required}:
        raise GateError(f"audit gate is {audit.gate.value!r}")
    return audit


REPRODUCTION_CLAIM = re.compile(r"independent(ly)?\s+(reproduc|replicat|recomput)", re.IGNORECASE)
NEGATED_CLAIM = re.compile(r"(not|never|no)\b[^.\n]{0,60}independent(ly)?\s+(reproduc|replicat|recomput)", re.IGNORECASE)


def check_reproduction_language(text: str, audit: AuditFindings | None) -> None:
    """Refuse "independently reproduced" wording unless an audit recomputed results."""
    claims = [m for m in REPRODUCTION_CLAIM.finditer(text)]
    if not claims:
        return
    if audit is not None and audit.independently_reproduced:
        return
    for match in claims:
        window = text[max(0, match.start() - 80): match.end()]
        if not NEGATED_CLAIM.search(window):
            raise GateError(
                "final summary claims independent reproduction, but the audit record lists no "
                "independently recomputed results with a recomputation method"
            )


def check_role_isolation(store, run_id: str) -> None:
    sessions = store.sessions_for_run(run_id)
    by_role: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        by_role.setdefault(s["role"], []).append(s)
    primary_dirs = {Path(s["work_dir"]).resolve() for s in by_role.get("primary", [])}
    adversary_dirs = {Path(s["work_dir"]).resolve() for s in by_role.get("adversary", [])}
    if primary_dirs & adversary_dirs:
        raise GateError("primary and adversary sessions share a writable work directory")
    for a in adversary_dirs:
        for p in primary_dirs:
            if a in p.parents or p in a.parents:
                raise GateError(f"adversary work dir {a} is nested with primary work dir {p}")
    primary_names = {s["tmux_name"] for s in by_role.get("primary", []) if s.get("tmux_name")}
    adversary_names = {s["tmux_name"] for s in by_role.get("adversary", []) if s.get("tmux_name")}
    if primary_names & adversary_names:
        raise GateError("primary and adversary share a tmux session")


def check_transition_gate(run: dict[str, Any], requested: str, config: dict[str, Any], store) -> dict[str, Any]:
    """Raise :class:`GateError` unless ``requested`` is scientifically allowed."""
    run_dir = Path(run["run_dir"])
    p = paths(run_dir)
    research = config.get("research", {})
    evidence: dict[str, Any] = {"enforced": True}

    if run.get("cancel_requested") and requested not in {"cancelled", "failed"}:
        raise GateError("cancellation was requested for this run; only 'cancelled' or 'failed' are allowed")

    if requested.startswith("routed_"):
        route: RouteDecision = _load(p["route"], "route_decision")
        if f"routed_{route.route.value}" != requested:
            raise GateError(f"route decision says {route.route.value!r} but transition requested {requested!r}")
        evidence["route_rationale_sha256"] = canonical_sha256(route.rationale)

    elif requested == "hypotheses_ranked":
        ranking = check_ranking(run, config)
        evidence["judges"] = sorted(set(ranking.judge_ids))

    elif requested == "contract_frozen":
        frozen = verify_frozen_contract(run)
        evidence["contract_sha256"] = frozen.contract_sha256
        if p["ranking"].exists():
            ranking: RankingResult = _load(p["ranking"], "ranking_result")
            ranked_ids = {r.hypothesis_id for r in ranking.ranked}
            missing = set(frozen.contract.selected_hypothesis_ids) - ranked_ids
            if missing:
                raise GateError(f"contract selects hypotheses that were never ranked: {sorted(missing)}")

    elif requested == "primary_running":
        frozen = verify_frozen_contract(run)
        evidence["contract_sha256"] = frozen.contract_sha256
        if run["status"] == "repair_required":
            limit = int(research.get("maximum_repair_rounds", 2))
            if int(run["repair_rounds"]) >= limit:
                raise GateError(f"repair round limit reached ({limit}); escalate to a human")

    elif requested == "primary_complete":
        verify_frozen_contract(run)
        jobs = store.jobs_for_run(run["run_id"])
        live = [j["idempotency_key"] for j in jobs if j["state"] not in {"succeeded", "failed", "cancelled"}]
        if live:
            raise GateError(f"jobs are not terminal: {live}")
        if jobs and not any(j["state"] == "succeeded" for j in jobs):
            raise GateError("no job succeeded")
        if not p["evaluation"].exists():
            raise GateError("primary_complete requires a deterministic evaluation result")
        evaluation = _load(p["evaluation"], "evaluation_result")
        if evaluation.contract_sha256 != run["contract_sha256"]:
            raise GateError("evaluation was computed against a different contract")

    elif requested == "audit_running":
        check_role_isolation(store, run["run_id"])

    elif requested == "audit_complete":
        check_role_isolation(store, run["run_id"])
        audit: AuditFindings = _load(p["audit"], "audit_findings")
        if audit.blocking_findings:
            raise GateError("audit has unresolved critical/major findings; transition to repair_required instead")
        if audit.gate in {AuditGate.not_ready, AuditGate.repair_required}:
            raise GateError(f"audit gate is {audit.gate.value!r}")
        if not audit.independence.separate_agent_session:
            raise GateError("audit record does not state that it ran in a separate agent session")
        evidence["independently_reproduced"] = audit.independently_reproduced

    elif requested == "reporting":
        if run["status"] != "routed_direct":
            check_audit_for_completion(run, config)

    elif requested == "complete":
        audit = check_audit_for_completion(run, config)
        if not p["final_summary"].exists():
            raise GateError(f"final summary missing: {p['final_summary']}")
        text = p["final_summary"].read_text(encoding="utf-8")
        check_reproduction_language(text, audit)
        evidence["final_summary_sha256"] = sha256_file(p["final_summary"])

    return evidence
