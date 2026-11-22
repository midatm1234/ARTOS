"""Export ARTOS runs as an Open Knowledge Format (OKF v0.3) bundle and a knowledge graph.

Every validated artifact becomes an OKF concept document: markdown with YAML
frontmatter whose ``type`` names the concept (``Research Question``,
``Hypothesis``, ``Evidence``, ``Analysis Contract``, ``Experiment``,
``Evaluation``, ``Audit Finding``, ``Decision``, ``Run``). Relationships are
bundle-relative markdown links, which OKF treats as directed edges. Because OKF
defers typed relations, the relation name is carried in the surrounding prose
and in the ``x-artos-relations`` extension key, and every edge is also written
to ``graph.json`` (nodes + typed edges) for graph tooling.

Trust fields follow OKF v0.2+:

- ``generated`` is ``{by: "artos/<version>", at: ...}``.
- ``sources`` points at the exact artifact file and its sha256.
- ``verified`` is added only when the run's audit independently recomputed
  results, with the auditor session as the actor. A machine audit is not
  ``human:`` verification.
- ``status`` mirrors the scientific status (``proposed``, ``supported``,
  ``unsupported``, ``inconclusive``, ``open``, ``accepted``, …).

Export is deterministic for the same artifacts and timestamp, never reads
outside the run directory, and never edits a run. Multiple runs can export into
one bundle: evidence and datasets with the same identity merge into one node,
so repeated research accumulates into a shared knowledge base.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .gates import paths as run_paths
from .models import (
    ArtifactError,
    AuditFindings,
    EvaluationResult,
    FrozenContract,
    HypothesisSet,
    RankingResult,
    ResearchQuestion,
    RouteDecision,
    load_artifact,
)
from .utils import atomic_write_text, sha256_file

OKF_VERSION = "0.3"
PRODUCER = f"artos/{__version__}"


def _slug(value: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (s[:limit].strip("-")) or "item"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)  # JSON strings are valid YAML 1.2 scalars


def _yaml(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:\n{_yaml(v, indent + 1)}")
            else:
                lines.append(f"{pad}{k}: {_yaml(v, 0) if isinstance(v, (dict, list)) else _yaml_scalar(v)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                first, *rest = _yaml(item, indent + 1).split("\n")
                lines.append(f"{pad}- {first.strip()}" + ("\n" + "\n".join(rest) if rest else ""))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return _yaml_scalar(value)


class Bundle:
    """Accumulates concept documents and typed edges, then writes an OKF bundle."""

    def __init__(self, at: str):
        self.at = at
        self.docs: dict[str, dict[str, Any]] = {}   # bundle path -> {frontmatter, body, relations}
        self.edges: list[dict[str, str]] = []

    def node(self, path: str, frontmatter: dict[str, Any], body: str) -> str:
        if path in self.docs:
            # Merge: keep the first body, union sources, tags and relations.
            existing = self.docs[path]
            fm = existing["frontmatter"]
            for key in ("sources", "tags"):
                merged = list(fm.get(key, []))
                for item in frontmatter.get(key, []):
                    if item not in merged:
                        merged.append(item)
                if merged:
                    fm[key] = merged
            return path
        self.docs[path] = {"frontmatter": frontmatter, "body": body, "relations": []}
        return path

    def edge(self, source: str, relation: str, target: str, note: str = "") -> None:
        record = {"source": source, "relation": relation, "target": target}
        if record not in self.edges:
            self.edges.append(record)
            self.docs[source]["relations"].append((relation, target, note))

    def write(self, out: Path, title: str, description: str, license_id: str | None) -> list[Path]:
        written: list[Path] = []
        sections: dict[str, list[str]] = {}
        for path, doc in sorted(self.docs.items()):
            fm = dict(doc["frontmatter"])
            rels = doc["relations"]
            if rels:
                fm["x-artos-relations"] = [{"relation": r, "target": t} for r, t, _ in rels]
            lines = ["---", _yaml(fm), "---", "", doc["body"].rstrip(), ""]
            if rels:
                lines += ["# Relations", ""]
                for relation, target, note in rels:
                    tdoc = self.docs.get(target, {}).get("frontmatter", {})
                    label = tdoc.get("title", target)
                    lines.append(f"- **{relation}** [{label}]({target})" + (f": {note}" if note else ""))
                lines.append("")
            srcs = fm.get("sources") or []
            if srcs:
                lines += ["# Citations", ""]
                for i, s in enumerate(srcs, 1):
                    lines.append(f"[{i}] `{s.get('resource')}`" + (f" (sha256 `{s['id'][7:23]}…`)" if str(s.get('id', '')).startswith('sha256:') else ""))
                lines.append("")
            target = out / path.lstrip("/")
            atomic_write_text(target, "\n".join(lines))
            written.append(target)
            section = path.strip("/").split("/")[0]
            sections.setdefault(section, []).append(f"* [{fm.get('title', path)}]({path}) - {fm.get('description', '')}")
        index = ["---", _yaml({"okf_version": OKF_VERSION, "title": title, "description": description,
                                **({"license": license_id} if license_id else {})}), "---", "",
                 f"# {title}", "", description, ""]
        for section, items in sorted(sections.items()):
            index += [f"## {section.replace('-', ' ').title()}", "", *items, ""]
        atomic_write_text(out / "index.md", "\n".join(index))
        graph = {"okf_version": OKF_VERSION, "generated": {"by": PRODUCER, "at": self.at},
                 "nodes": [{"id": p, "type": d["frontmatter"]["type"], "title": d["frontmatter"].get("title"),
                            "status": d["frontmatter"].get("status")} for p, d in sorted(self.docs.items())],
                 "edges": self.edges}
        atomic_write_text(out / "graph.json", json.dumps(graph, indent=2, ensure_ascii=False) + "\n")
        written += [out / "index.md", out / "graph.json"]
        log = out / "log.md"
        entry = f"## {self.at[:10]}\n\n**Update**: exported {len(self.docs)} concepts and {len(self.edges)} relations ({PRODUCER}).\n\n"
        previous = log.read_text(encoding="utf-8").split("\n", 2)[2] if log.exists() else ""
        atomic_write_text(log, "# Knowledge Bundle Log\n\n" + entry + previous)
        written.append(log)
        return written


def _source(path: Path, run_dir: Path, at: str) -> dict[str, Any]:
    return {"resource": f"artos-run://{run_dir.name}/{path.relative_to(run_dir).as_posix()}",
            "id": "sha256:" + sha256_file(path), "title": path.name, "last_modified": at}


def _base(type_: str, title: str, description: str, at: str, status: str | None, tags: Iterable[str],
          sources: list[dict[str, Any]], resource: str | None = None, **extra: Any) -> dict[str, Any]:
    fm: dict[str, Any] = {"type": type_, "title": title, "description": description}
    if resource:
        fm["resource"] = resource
    fm["tags"] = sorted(set(tags))
    if status:
        fm["status"] = status
    fm["generated"] = {"by": PRODUCER, "at": at}
    fm["sources"] = sources
    fm.update({k: v for k, v in extra.items() if v is not None})
    return fm


def _load_optional(path: Path, kind: str):
    if not path.exists():
        return None
    try:
        return load_artifact(path, kind)[0]
    except ArtifactError:
        return None


def add_run(bundle: Bundle, store, run_id: str) -> dict[str, int]:
    """Add one run's artifacts to the bundle. Returns counts per concept type."""
    run = store.get_run(run_id)
    rd = Path(run["run_dir"])
    p = run_paths(rd)
    at = bundle.at
    counts: dict[str, int] = {}

    def count(t: str) -> None:
        counts[t] = counts.get(t, 0) + 1

    rid = _slug(run_id, 80)
    run_node = f"/runs/{rid}.md"
    q: ResearchQuestion | None = _load_optional(p["question"], "research_question")  # type: ignore[assignment]
    route: RouteDecision | None = _load_optional(p["route"], "route_decision")  # type: ignore[assignment]
    hset: HypothesisSet | None = _load_optional(p["hypotheses"], "hypothesis_set")  # type: ignore[assignment]
    ranking: RankingResult | None = _load_optional(p["ranking"], "ranking_result")  # type: ignore[assignment]
    frozen: FrozenContract | None = _load_optional(p["contract_frozen"], "frozen_contract")  # type: ignore[assignment]
    evaluation: EvaluationResult | None = _load_optional(p["evaluation"], "evaluation_result")  # type: ignore[assignment]
    audit: AuditFindings | None = _load_optional(p["audit"], "audit_findings")  # type: ignore[assignment]

    verified = None
    if audit is not None and audit.independently_reproduced:
        verified = [{"by": f"process:artos-adversary/{audit.auditor_session or 'session'}", "at": at,
                     "x-artos-method": audit.independence.recomputation_method}]

    bundle.node(run_node, _base(
        "Run", run["title"], f"ARTOS research run ({run['status']}).", at, run["status"], ["artos", "run"],
        [_source(p["question"], rd, at)] if p["question"].exists() else [],
        **{"x-artos-run-id": run_id, "x-artos-route": run.get("route"),
           "x-artos-contract-sha256": run.get("contract_sha256")}),
        f"Research run `{run_id}` in project `{run['project']}`.\n\n"
        f"- Status: **{run['status']}**\n- Route: {run.get('route') or 'not routed'}"
        + (f" (rationale: {route.rationale})" if route is not None else "") + "\n"
        f"- Repair rounds: {run['repair_rounds']}\n")
    count("Run")

    if q is not None:
        qnode = f"/questions/{_slug(q.mother_question)}-{q.mother_question_sha256[:8]}.md"
        bundle.node(qnode, _base(
            "Research Question", q.mother_question[:90], "Mother research question, recorded separately from derived hypotheses.",
            at, None, ["question"], [_source(p["question"], rd, at)],
            **{"x-artos-mother-question-sha256": q.mother_question_sha256}),
            f"{q.mother_question}\n\nThe verbatim source ({q.source_type.value}) is stored with the run and hashed "
            f"(`{q.source_sha256[:16]}…`). It is untrusted research input, not instructions.\n")
        bundle.edge(run_node, "investigates", qnode)
        count("Research Question")
    ranked = {r.hypothesis_id: r for r in (ranking.ranked if ranking else [])}
    evid_nodes: dict[str, str] = {}
    if hset is not None:
        for ev in hset.evidence:
            enode = f"/evidence/{_slug(ev.citation)}.md"
            bundle.node(enode, _base(
                "Evidence", ev.title, ev.claim[:200], at, "verified" if ev.verified else "unverified",
                ["evidence", ev.claim_type.value], [_source(p["hypotheses"], rd, at)],
                resource=ev.url or None,
                **{"x-artos-citation": ev.citation, "x-artos-claim-type": ev.claim_type.value,
                   "x-artos-verification": ev.verification_method}),
                f"{ev.first_author} ({ev.year}){', ' + ev.venue if ev.venue else ''}. *{ev.title}*. `{ev.citation}`\n\n"
                f"Claim recorded ({ev.claim_type.value.replace('_', ' ')}): {ev.claim}\n")
            evid_nodes[ev.evidence_id] = enode
            count("Evidence")
        for h in hset.hypotheses:
            hnode = f"/hypotheses/{rid}-{_slug(h.hypothesis_id, 20)}.md"
            r = ranked.get(h.hypothesis_id)
            verdict = evaluation.verdicts.get(h.hypothesis_id) if evaluation else None
            status = verdict or (r.verdict if r else h.status.value)
            body = (f"{h.statement}\n\n# Mechanism\n\n{h.mechanism}\n\n# Expected benefit\n\n{h.expected_benefit}\n\n"
                    f"# Implementation cost\n\n{h.implementation_cost}\n\n# Falsification\n\n{h.falsification}\n")
            if r:
                body += f"\n# Blind ranking\n\nMean score {r.mean_score:.3f} from {r.judges} blind judges; panel verdict **{r.verdict}**.\n"
            if verdict:
                body += f"\n# Evaluation\n\nPre-registered evaluation verdict: **{verdict}**.\n"
            bundle.node(hnode, _base(
                "Hypothesis", h.statement[:90], f"{h.role} hypothesis in run {run_id}.", at, status,
                ["hypothesis", h.role], [_source(p["hypotheses"], rd, at)],
                **{"x-artos-hypothesis-id": h.hypothesis_id, "x-artos-role": h.role}), body)
            bundle.edge(run_node, "proposes", hnode)
            if q is not None:
                bundle.edge(hnode, "addresses", qnode)
            for e in h.supporting_evidence:
                if e in evid_nodes:
                    bundle.edge(hnode, "supported_by", evid_nodes[e])
            for e in h.conflicting_evidence:
                if e in evid_nodes:
                    bundle.edge(hnode, "conflicts_with", evid_nodes[e])
            count("Hypothesis")

    if frozen is not None:
        c = frozen.contract
        cnode = f"/contracts/{_slug(c.contract_id)}-{frozen.contract_sha256[:8]}.md"
        body = (f"Frozen and hashed before testing: `{frozen.contract_sha256}`.\n\n# Stopping rule\n\n{c.stopping_rule}\n\n"
                "# Splits\n\n" + ("\n".join(f"- {s.name}: {s.start} to {s.end} ({s.purpose})" for s in c.splits) or "- not specified") +
                "\n\n# Leakage controls\n\n" + ("\n".join(f"- {x}" for x in c.leakage_controls) or "- not specified") +
                "\n\n# Acceptance rules\n\n| rule | hypothesis | group | candidate vs reference | metric | mode | threshold |\n|---|---|---|---|---|---|---|\n" +
                "\n".join(f"| {r.rule_id} | {r.hypothesis_id} | {r.group} | {r.candidate} vs {r.reference} | {r.variable}/{r.metric} | {r.mode} | {r.threshold} |"
                          for r in c.acceptance_rules) +
                "\n\n# Interpretation limits\n\n" + ("\n".join(f"- {x}" for x in c.interpretation_limits) or "- none recorded") + "\n")
        bundle.node(cnode, _base("Analysis Contract", c.contract_id, "Pre-registered analysis plan, frozen before testing.", at,
                                 "frozen", ["contract"], [_source(p["contract_frozen"], rd, at)],
                                 **{"x-artos-contract-sha256": frozen.contract_sha256, "x-artos-seeds": list(c.seeds)}), body)
        bundle.edge(run_node, "governed_by", cnode)
        for hid in c.selected_hypothesis_ids:
            hnode = f"/hypotheses/{rid}-{_slug(hid, 20)}.md"
            if hnode in bundle.docs:
                bundle.edge(cnode, "tests", hnode)
        count("Analysis Contract")
        for ds in c.datasets:
            dnode = f"/datasets/{_slug(ds.name)}.md"
            bundle.node(dnode, _base("Dataset", ds.name, f"Dataset used as {ds.role}.", at, None, ["dataset"],
                                     [_source(p["contract_frozen"], rd, at)], resource=ds.path,
                                     **{"x-artos-version": ds.version, "x-artos-fingerprint": ds.sha256}),
                        f"Role: {ds.role}\n\nVersion: {ds.version or 'not recorded'}\n\nVariables: {', '.join(ds.variables) or 'not recorded'}\n")
            bundle.edge(cnode, "uses_dataset", dnode)
            count("Dataset")

    jobs = store.jobs_for_run(run_id)
    for job in jobs:
        spec = job["spec"]
        xnode = f"/experiments/{rid}-{_slug(spec['variant_id'], 30)}-s{spec['seed']}.md"
        rec = job["record"]
        bundle.node(xnode, _base(
            "Experiment", f"{spec['variant_id']} seed {spec['seed']}", f"Executed job ({job['state']}).", at, job["state"],
            ["experiment", spec["variant_id"]], [], **{"x-artos-idempotency-key": job["idempotency_key"],
                                                         "x-artos-code-refs": spec.get("code_refs") or None,
                                                         "x-artos-attempts": job["attempt"]}),
            f"Variant `{spec['variant_id']}`, seed {spec['seed']}, executor `{spec['executor']}`.\n\n"
            f"- State: {job['state']}\n- Runtime (s): {rec.get('runtime_seconds')}\n- Output dir: `{spec['output_dir']}`\n"
            + (f"- Output hashes: {len(rec.get('outputs') or {})} files\n" if rec.get("outputs") else ""))
        bundle.edge(run_node, "executed", xnode)
        if frozen is not None:
            bundle.edge(xnode, "planned_under", f"/contracts/{_slug(frozen.contract.contract_id)}-{frozen.contract_sha256[:8]}.md")
        count("Experiment")

    if evaluation is not None:
        vnode = f"/evaluations/{rid}.md"
        rules = evaluation.decisions.get("rules", {})
        body = ("Computed deterministically by `" + evaluation.computed_by + "`.\n\n| rule | seed-mean measure | agreement | status |\n|---|---|---|---|\n" +
                "\n".join(f"| {k} | {v.get('seed_mean_measure')} | {v.get('seed_agreement')} | {v.get('status')} |" for k, v in rules.items()) +
                "\n\nVerdicts: " + ", ".join(f"{k}: **{v}**" for k, v in evaluation.verdicts.items()) + "\n")
        bundle.node(vnode, _base("Evaluation", f"Evaluation of {run['title']}", "Pre-registered rules applied per paired seed.", at,
                                 None, ["evaluation"], [_source(p["evaluation"], rd, at)],
                                 **({"verified": verified} if verified else {})), body)
        bundle.edge(run_node, "evaluated_by", vnode)
        for hid, verdict in evaluation.verdicts.items():
            hnode = f"/hypotheses/{rid}-{_slug(hid, 20)}.md"
            if hnode in bundle.docs:
                bundle.edge(vnode, verdict, hnode, "pre-registered verdict")
        count("Evaluation")

    if audit is not None:
        anode = f"/audits/{rid}.md"
        ind = audit.independence
        bundle.node(anode, _base(
            "Audit", f"Adversarial audit of {run['title']}", f"Gate {audit.gate.value}.", at, audit.gate.value, ["audit"],
            [_source(p["audit"], rd, at)], **({"verified": verified} if verified else {})),
            f"Gate: **{audit.gate.value}**.\n\nIndependently reproduced: **{audit.independently_reproduced}**"
            + (f" — {ind.recomputation_method}" if ind.recomputation_method else "") + "\n\n"
            + ("Recomputed:\n\n" + "\n".join(f"- {x}" for x in ind.headline_results_independently_recomputed) + "\n"
               if ind.headline_results_independently_recomputed else ""))
        bundle.edge(run_node, "audited_by", anode)
        if evaluation is not None:
            bundle.edge(anode, "reviews", f"/evaluations/{rid}.md")
        count("Audit")
        for f in audit.findings:
            fnode = f"/findings/{rid}-{_slug(f.finding_id, 20)}.md"
            bundle.node(fnode, _base("Audit Finding", f"{f.finding_id} ({f.severity.value})", f.description[:200], at,
                                     f.status.value, ["finding", f.severity.value], [_source(p["audit"], rd, at)]),
                        f"{f.description}\n\n- Severity: {f.severity.value}\n- Status: {f.status.value}\n"
                        + (f"- Resolution: {f.resolution}\n" if f.resolution else "")
                        + ("\n# Evidence\n\n" + "\n".join(f"- `{e}`" for e in f.evidence) + "\n" if f.evidence else ""))
            bundle.edge(anode, "raised", fnode)
            count("Audit Finding")

    for ev in store.events(run_id):
        if ev["event_type"] != "decision_record":
            continue
        d = ev["payload"]
        dnode = f"/decisions/{rid}-{_slug(d.get('decision_id', str(ev['seq'])), 40)}.md"
        bundle.node(dnode, _base("Decision", d.get("decision", "")[:90], (d.get("rationale") or "")[:200], at, None,
                                 ["decision"], [], **{"x-artos-actor": d.get("actor"), "x-artos-event-hash": ev["hash"]}),
                    f"**Decision.** {d.get('decision')}\n\n**Rationale.** {d.get('rationale')}\n\n"
                    + ("**Evidence.**\n\n" + "\n".join(f"- `{e}`" for e in d.get("evidence", [])) + "\n" if d.get("evidence") else ""))
        bundle.edge(run_node, "decided", dnode)
        count("Decision")
    return counts


def export_bundle(store, run_ids: list[str], out: Path, *, title: str, description: str,
                  license_id: str | None = None, at: str | None = None) -> dict[str, Any]:
    stamp = at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    bundle = Bundle(stamp)
    totals: dict[str, int] = {}
    for run_id in run_ids:
        for k, v in add_run(bundle, store, run_id).items():
            totals[k] = totals.get(k, 0) + v
    files = bundle.write(Path(out), title, description, license_id)
    return {"bundle": str(out), "okf_version": OKF_VERSION, "concepts": len(bundle.docs), "relations": len(bundle.edges),
            "by_type": totals, "files": len(files)}


def validate_bundle(root: Path) -> dict[str, Any]:
    """OKF v0.3 conformance: every frontmatter parses as a mapping with a non-empty ``type``;
    ``index.md``/``log.md`` follow their rules. Also reports broken bundle-relative links (allowed, but listed)."""
    root = Path(root)
    errors, warnings, concepts = [], [], 0
    try:
        import yaml  # optional: stricter YAML check when available
    except ImportError:
        yaml = None
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root).as_posix()
        text = md.read_text(encoding="utf-8")
        has_fm = text.startswith("---\n")
        if md.name == "log.md" and has_fm:
            errors.append(f"{rel}: log.md must not have frontmatter")
            continue
        if md.name == "index.md":
            if has_fm and rel != "index.md":
                errors.append(f"{rel}: only the root index.md may have frontmatter")
            continue
        if md.name == "log.md":
            continue
        if not has_fm:
            warnings.append(f"{rel}: no frontmatter (not a concept)")
            continue
        block = text[4:text.find("\n---", 4)]
        if yaml is not None:
            try:
                fm = yaml.safe_load(block)
            except yaml.YAMLError as exc:
                errors.append(f"{rel}: frontmatter is not valid YAML ({exc})")
                continue
            if not isinstance(fm, dict) or not str(fm.get("type") or "").strip():
                errors.append(f"{rel}: frontmatter must be a mapping with a non-empty type")
                continue
        elif not re.search(r"^type:\s*\S", block, re.M):
            errors.append(f"{rel}: missing type")
            continue
        concepts += 1
        for link in re.findall(r"\]\((/[^)#\s]+\.md)\)", text):
            if not (root / link.lstrip("/")).exists():
                warnings.append(f"{rel}: broken link {link}")
    return {"conformant": not errors, "okf_version": OKF_VERSION, "concepts": concepts, "errors": errors, "warnings": warnings}
