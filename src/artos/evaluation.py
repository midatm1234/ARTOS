"""Deterministic evaluation against a frozen contract.

Reads each succeeded job's ``metrics.json`` (``{"metrics": {variable: {metric:
value}}}``), applies the contract's pre-registered acceptance rules per seed
(paired by seed), aggregates across seeds, and writes an
:class:`EvaluationResult`. The verdict policy is fixed by the contract; this
module performs arithmetic and records every intermediate number.
"""

from __future__ import annotations

import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .gates import paths, verify_frozen_contract
from .models import AcceptanceRule, EvaluationResult, MetricValue, write_artifact
from .models import utc_now as utc_dt
from .store import Store
from .utils import read_json


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def rule_measure(rule: AcceptanceRule, cand: float, ref: float) -> tuple[float | None, bool]:
    if rule.mode == "rel_improve":
        if abs(ref) < 1e-12:
            return None, False
        m = (abs(ref) - abs(cand)) / abs(ref)
        return m, m >= rule.threshold
    if rule.mode == "rel_degrade":
        if abs(ref) < 1e-12:
            return None, False
        m = (abs(cand) - abs(ref)) / abs(ref)
        return m, m <= rule.threshold
    if rule.mode == "abs_degrade":
        m = ref - cand
        return m, m <= rule.threshold
    if rule.mode == "toward_one_improve":
        m = abs(ref - 1.0) - abs(cand - 1.0)
        return m, m >= rule.threshold
    raise ValueError(rule.mode)


def collect_metrics(store: Store, run_id: str) -> tuple[list[MetricValue], dict[str, Any]]:
    values: list[MetricValue] = []
    cost: dict[str, Any] = {}
    for job in store.jobs_for_run(run_id):
        if job["state"] != "succeeded":
            continue
        spec = job["spec"]
        data = read_json(Path(spec["output_dir"]) / "metrics.json") or {}
        for variable, metrics in (data.get("metrics") or {}).items():
            for name, value in metrics.items():
                values.append(MetricValue(variant_id=spec["variant_id"], seed=spec["seed"], variable=variable,
                                          metric=name, value=_finite(value), split=data.get("split", "test")))
        key = f"{spec['variant_id']}/seed{spec['seed']}"
        cost[key] = {k: data.get(k) for k in ("train_seconds", "eval_seconds", "peak_memory_bytes", "parameters", "temporal_parameters", "device")}
    return values, cost


def evaluate_run(store: Store, run_id: str) -> EvaluationResult:
    run = store.get_run(run_id)
    frozen = verify_frozen_contract(run)
    contract = frozen.contract
    values, cost = collect_metrics(store, run_id)
    table: dict[tuple[str, int | None, str, str], float | None] = {
        (v.variant_id, v.seed, v.variable, v.metric): v.value for v in values}
    seeds = sorted(contract.seeds) or sorted({v.seed for v in values if v.seed is not None})
    policy = contract.decision_policy

    rule_results: dict[str, Any] = {}
    for rule in contract.acceptance_rules:
        per_seed = []
        for seed in seeds:
            c = table.get((rule.candidate, seed, rule.variable, rule.metric))
            r = table.get((rule.reference, seed, rule.variable, rule.metric))
            if c is None or r is None:
                per_seed.append({"seed": seed, "status": "missing", "candidate": c, "reference": r})
                continue
            m, ok = rule_measure(rule, c, r)
            per_seed.append({"seed": seed, "candidate": c, "reference": r,
                             "measure": None if m is None else round(m, 6), "status": "pass" if ok else "fail"})
        complete = [p for p in per_seed if p["status"] != "missing"]
        cands = [p["candidate"] for p in complete]
        refs = [p["reference"] for p in complete]
        if complete:
            m_mean, ok_mean = rule_measure(rule, mean(cands), mean(refs))
            measures = [p["measure"] for p in complete if p["measure"] is not None]
            agreement = sum(p["status"] == "pass" for p in complete) / len(seeds) if seeds else 0.0
        else:
            m_mean, ok_mean, measures, agreement = None, False, [], 0.0
        status = ("missing" if len(complete) < len(seeds) else
                  "pass" if ok_mean and agreement >= policy.min_seed_agreement else "fail")
        rule_results[rule.rule_id] = {
            "hypothesis_id": rule.hypothesis_id, "group": rule.group, "candidate": rule.candidate,
            "reference": rule.reference, "variable": rule.variable, "metric": rule.metric, "mode": rule.mode,
            "threshold": rule.threshold, "per_seed": per_seed, "seed_mean_measure": None if m_mean is None else round(m_mean, 6),
            "seed_measure_sd": round(pstdev(measures), 6) if len(measures) > 1 else None,
            "seed_agreement": round(agreement, 4), "status": status,
            # Sign of the effect irrespective of threshold: did the candidate move the right way?
            "direction_ok_fraction": round(sum((p["measure"] or 0) > 0 if rule.mode in {"rel_improve", "toward_one_improve"}
                                               else (p["measure"] or 0) <= 0 for p in complete) / len(complete), 4) if complete else None,
        }

    verdicts: dict[str, str] = {}
    for hid in contract.selected_hypothesis_ids:
        rules = [r for r in rule_results.values() if r["hypothesis_id"] == hid]
        if not rules or any(r["status"] == "missing" for r in rules):
            verdicts[hid] = "inconclusive"
            continue
        primary = [r for r in rules if r["group"] == "primary"]
        if all(r["status"] == "pass" for r in rules):
            verdicts[hid] = "supported"
        elif primary and all((r["direction_ok_fraction"] or 0) < 0.5 for r in primary):
            verdicts[hid] = "unsupported"
        else:
            verdicts[hid] = "inconclusive"

    result = EvaluationResult(
        run_id=run_id,
        contract_sha256=frozen.contract_sha256,
        computed_by="artos.evaluation.evaluate_run (deterministic, contract acceptance_rules)",
        computed_at=utc_dt(),
        values=values,
        decisions={"rules": rule_results, "policy": policy.model_dump(), "seeds": seeds},
        verdicts=verdicts,  # type: ignore[arg-type]
        cost=cost,
    )
    write_artifact(paths(Path(run["run_dir"]))["evaluation"], result)
    store.record_event("evaluation_written", {"verdicts": verdicts}, run_id)
    return result


def render_markdown(result: EvaluationResult) -> str:
    lines = ["# Evaluation (deterministic)", "", f"Contract `{result.contract_sha256[:16]}`; computed by `{result.computed_by}`.", "",
             "| rule | hyp | group | cand vs ref | var/metric | seed-mean measure | sd | agreement | status |",
             "|---|---|---|---|---|---|---|---|---|"]
    for rid, r in result.decisions.get("rules", {}).items():
        lines.append(f"| {rid} | {r['hypothesis_id']} | {r['group']} | {r['candidate']} vs {r['reference']} | "
                     f"{r['variable']}/{r['metric']} | {r['seed_mean_measure']} (≥/≤ {r['threshold']}) | {r['seed_measure_sd']} | "
                     f"{r['seed_agreement']} | {r['status']} |")
    lines += ["", "Verdicts: " + ", ".join(f"**{k}: {v}**" for k, v in result.verdicts.items())]
    return "\n".join(lines) + "\n"
