from __future__ import annotations

import json
from pathlib import Path

from .models import AuditFindings, AuditGate, Independence, load_artifact, parse_artifact, write_artifact


def create_audit_template(run_dir: Path) -> tuple[Path, Path]:
    json_path = Path(run_dir) / "reviews" / "audit_findings.json"
    markdown_path = Path(run_dir) / "10_adversarial_audit.md"
    if not json_path.exists():
        write_artifact(json_path, AuditFindings(
            run_id=Path(run_dir).name,
            independence=Independence(),
            checks={
                "data_identity_and_versions": "not_checked",
                "sample_counts_and_filters": "not_checked",
                "time_space_units_and_sign": "not_checked",
                "leakage_and_circularity": "not_checked",
                "baselines_nulls_and_uncertainty": "not_checked",
                "table_figure_text_consistency": "not_checked",
            },
            gate=AuditGate.not_ready,
        ))
    if not markdown_path.exists():
        markdown_path.write_text(
            "# Adversarial audit\n\n"
            "Status: **not checked**\n\n"
            "Do not label this work independently reproduced until the audit record identifies "
            "the independently recomputed results and methods.\n",
            encoding="utf-8",
        )
    return json_path, markdown_path


def normalize_agent_audit(payload: dict) -> tuple[dict, list[str]]:
    """Coerce common agent layout variants into the audit schema. Values are never changed.

    - ``checks`` given as a list of ``{check_id|name, ...}`` becomes a dict keyed by id;
    - a finding's ``evidence`` given as a string becomes a one-element list;
    - a finding's ``repair`` / ``recommendation`` text moves into ``resolution`` (prefixed).
    Severity, status, gate and independence claims are left exactly as written.
    """
    data = json.loads(json.dumps(payload))
    notes: list[str] = []
    checks = data.get("checks")
    if isinstance(checks, list):
        as_dict = {}
        for i, item in enumerate(checks):
            key = (item.get("check_id") or item.get("name") or f"check_{i}") if isinstance(item, dict) else f"check_{i}"
            as_dict[str(key)] = item
        data["checks"] = as_dict
        notes.append(f"checks: list of {len(checks)} converted to dict keyed by check_id")
    for f in data.get("findings", []) or []:
        if isinstance(f.get("evidence"), str):
            f["evidence"] = [f["evidence"]]
            notes.append(f"{f.get('finding_id')}: evidence string wrapped in list")
        for extra in ("repair", "recommendation", "suggested_repair"):
            if extra in f:
                text = f.pop(extra)
                f["resolution"] = (f.get("resolution") + " | " if f.get("resolution") else "") + f"proposed repair: {text}"
                notes.append(f"{f.get('finding_id')}: '{extra}' moved to resolution")
    return data, notes


def adopt_audit(run_dir: Path, audit_path: Path, *, normalize: bool = False) -> Path:
    """Validate an adversary's audit file and copy it to the canonical location."""
    notes: list[str] = []
    if normalize:
        raw = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        raw, notes = normalize_agent_audit(raw)
        audit, upgrade = parse_artifact("audit_findings", raw)
        notes += upgrade
    else:
        audit, notes = load_artifact(Path(audit_path), "audit_findings")
    assert isinstance(audit, AuditFindings)
    if audit.run_id != Path(run_dir).name:
        raise ValueError(f"audit is for run {audit.run_id}, not {Path(run_dir).name}")
    out = Path(run_dir) / "reviews" / "audit_findings.json"
    write_artifact(out, audit)
    if notes:
        (out.with_suffix(".normalization.json")).write_text(json.dumps({"source": str(audit_path), "notes": notes}, indent=2),
                                                            encoding="utf-8")
    return out


def audit_summary(audit: AuditFindings) -> dict:
    return {
        "gate": audit.gate.value,
        "blocking": [f.finding_id for f in audit.blocking_findings],
        "independently_reproduced": audit.independently_reproduced,
        "recomputed": audit.independence.headline_results_independently_recomputed,
    }
