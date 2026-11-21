from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .audit import adopt_audit, audit_summary, create_audit_template
from .config import find_root, load_config, projects_dir, state_dir
from .hypotheses import (
    aggregate_judge_panel,
    create_blind_ranking_packet,
    create_hypothesis_template,
    rank_scored_hypotheses,
)
from .inventory import export_catalog, refresh_compute, refresh_data_catalog, register_path
from .models import Route, RouteDecision, export_json_schemas, load_artifact, write_artifact
from .models import utc_now as utc_dt
from .reporting import build_artifact_manifest, create_report_skeleton
from .runs import create_run, freeze_contract, snapshot_inventories
from .sessions import inspect_session, launch_claude_session, write_adversary_prompt
from .store import Store


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _context(args: argparse.Namespace) -> tuple[Path, dict[str, Any], Path, Path]:
    root = find_root(args.root)
    config = load_config(root)
    state = state_dir(root, config)
    projects = projects_dir(root, config)
    state.mkdir(parents=True, exist_ok=True)
    projects.mkdir(parents=True, exist_ok=True)
    return root, config, state, projects


def _store(state: Path) -> Store:
    return Store(state / "artos.sqlite")


def _stale(path: Path, ttl_hours: float) -> bool:
    if not path.exists():
        return True
    return (time.time() - path.stat().st_mtime) > ttl_hours * 3600


def _configured_roots(root: Path, values: list[str]) -> list[Path]:
    paths = []
    for value in values:
        path = Path(value).expanduser()
        paths.append(path if path.is_absolute() else root / path)
    return paths


def _ensure_inventories(root: Path, config: dict[str, Any], state: Path, store: Store) -> dict[str, Any]:
    inventory = config["inventory"]
    refreshed: dict[str, Any] = {}
    compute_latest = state / "compute" / "latest.json"
    if _stale(compute_latest, float(inventory["compute_ttl_hours"])):
        refreshed["compute"] = refresh_compute(root, state)
    data_last_scan = state / "data" / "last_scan.json"
    if _stale(data_last_scan, float(inventory["data_incremental_ttl_hours"])):
        configured = list(inventory.get("data_roots", [])) + list(inventory.get("artifact_roots", []))
        roots = _configured_roots(root, list(dict.fromkeys(configured)))
        refreshed["data"] = refresh_data_catalog(
            store, state, roots, int(inventory["max_scan_files"]), int(inventory["hash_file_limit_mb"]))
    elif not (state / "data" / "catalog.json").exists():
        refreshed["catalog"] = export_catalog(store, state)
    return refreshed


# ---------------------------------------------------------------------------
# doctor / inventory / data
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    import pydantic

    root, config, state, projects = _context(args)
    checks: dict[str, Any] = {
        "artos_version": __version__,
        "root": str(root),
        "configuration": str(root / "artos.json"),
        "state_dir": str(state),
        "projects_dir": str(projects),
        "python": {"path": sys.executable, "version": sys.version.split()[0]},
        "tmux": {"path": shutil.which("tmux")},
        "claude": {"path": shutil.which("claude")},
        "configured_models": {role: config["agents"][f"{role}_model"] for role in ("ranker", "primary", "adversary")},
        "allow_model_fallback": bool(config["agents"].get("allow_model_fallback", False)),
        "pydantic": {"version": pydantic.VERSION, "ok": int(pydantic.VERSION.split(".")[0]) >= 2},
    }
    for tool in ("tmux", "claude"):
        path = checks[tool]["path"]
        if path:
            command = [path, "-V"] if tool == "tmux" else [path, "--version"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
                checks[tool]["version"] = (result.stdout or result.stderr).strip()
                checks[tool]["ok"] = result.returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                checks[tool]["ok"] = False
                checks[tool]["error"] = str(exc)
        else:
            checks[tool]["ok"] = False
    with _store(state) as store:
        checks["store"] = {"path": str(state / "artos.sqlite"), "event_chain_ok": store.verify_event_chain()}
    mcp = config["execution"].get("mcp") or {}
    checks["mcp_executor"] = {"configured": bool(mcp.get("command")), "command": mcp.get("command")}
    checks["core_ok"] = bool(checks["pydantic"]["ok"] and checks["store"]["event_chain_ok"])
    checks["agents_ok"] = bool(checks["tmux"]["ok"] and checks["claude"]["ok"])
    checks["ok"] = bool(checks["core_ok"] and checks["agents_ok"])
    _json(checks)
    return 0 if checks["ok"] else 1


def cmd_inventory_refresh(args: argparse.Namespace) -> int:
    root, config, state, _ = _context(args)
    inventory = config["inventory"]
    with _store(state) as store:
        result: dict[str, Any] = {}
        if args.compute or not args.data_root:
            result["compute"] = refresh_compute(root, state)
        if args.data_root is not None:
            roots = [Path(value) for value in args.data_root]
            if not roots:
                configured = list(inventory.get("data_roots", [])) + list(inventory.get("artifact_roots", []))
                roots = _configured_roots(root, list(dict.fromkeys(configured)))
            result["data"] = refresh_data_catalog(
                store, state, roots, int(inventory["max_scan_files"]), int(inventory["hash_file_limit_mb"]))
        _json(result)
    return 0


def cmd_inventory_show(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        latest = state / "compute" / "latest.json"
        _json({"compute": json.loads(latest.read_text()) if latest.exists() else None,
               "resources": store.list_resources(args.kind)})
    return 0


def cmd_data_register(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    inventory = config["inventory"]
    with _store(state) as store:
        resource = register_path(store, args.path, int(inventory["hash_file_limit_mb"]), int(inventory["max_scan_files"]), args.kind)
        export_catalog(store, state)
    _json(resource)
    return 0


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
def cmd_run_create(args: argparse.Namespace) -> int:
    root, config, state, projects = _context(args)
    if args.email:
        source_text = args.email.read_text(encoding="utf-8", errors="replace")
        source_type = "email"
    elif args.source_file:
        source_text = args.source_file.read_text(encoding="utf-8", errors="replace")
        source_type = args.source_type
    else:
        source_text = args.question
        source_type = "question"
    with _store(state) as store:
        record = create_run(store, projects, source_text, source_type, args.project, args.title, args.mother_question)
        refreshed = _ensure_inventories(root, config, state, store)
        copied = snapshot_inventories(Path(record["run_dir"]), state)
        store.transition(record["run_id"], "inventory_ready",
                         {"inventory_snapshots": copied, "inventories_refreshed": sorted(refreshed)}, config=config)
        record = store.get_run(record["run_id"])
    _json({"run": record, "inventory_snapshots": copied, "inventories_refreshed": sorted(refreshed)})
    return 0


def cmd_run_list(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        _json(store.list_runs(args.limit))
    return 0


def cmd_run_show(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        run = store.get_run(args.run_id)
        sessions = []
        for session in store.sessions_for_run(args.run_id):
            spec = Path(session["spec_path"])
            sessions.append(inspect_session(spec) if spec.exists() else session)
        jobs = [{k: j[k] for k in ("idempotency_key", "variant_id", "seed", "state", "attempt")} for j in store.jobs_for_run(args.run_id)]
    _json({"run": run, "sessions": sessions, "jobs": jobs})
    return 0


def cmd_run_transition(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        store.transition(args.run_id, args.status, {"reason": args.reason} if args.reason else {}, config=config)
        _json(store.get_run(args.run_id))
    return 0


def cmd_run_route(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        run = store.get_run(args.run_id)
        decision = RouteDecision(run_id=args.run_id, route=Route(args.route), rationale=args.rationale,
                                 decided_at=utc_dt(), decided_by=args.decided_by)
        write_artifact(Path(run["run_dir"]) / "04_route_decision.json", decision)
        if run["status"] == "inventory_ready":
            store.transition(args.run_id, "oriented", {"orientation": "recorded with route decision"}, config=config)
        store.transition(args.run_id, f"routed_{args.route}", {"rationale": args.rationale}, config=config)
        _json(store.get_run(args.run_id))
    return 0


def cmd_run_snapshot(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        run = store.get_run(args.run_id)
    _json({"copied": snapshot_inventories(Path(run["run_dir"]), state)})
    return 0


def cmd_run_freeze(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        digest = freeze_contract(store, args.run_id, args.contract)
        if store.get_run(args.run_id)["status"] == "hypotheses_ranked":
            store.transition(args.run_id, "contract_frozen", {"sha256": digest}, config=config)
        status = store.get_run(args.run_id)["status"]
    _json({"run_id": args.run_id, "contract_sha256": digest, "status": status})
    return 0


def cmd_run_cancel(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    reason = args.reason or "user request"
    with _store(state) as store:
        store.request_cancel(args.run_id, reason)
        report = None
        if store.jobs_for_run(args.run_id):
            report = _controller(store, args.run_id, config).step(submit=False).as_dict()
        store.transition(args.run_id, "cancelled", {"reason": reason}, config=config)
        _json({"run": store.get_run(args.run_id), "jobs": report})
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        _json({"chain_ok": store.verify_event_chain(), "events": store.events(args.run_id)})
    return 0


def _run_dir(store: Store, run_id: str) -> Path:
    return Path(store.get_run(run_id)["run_dir"])


# ---------------------------------------------------------------------------
# hypotheses
# ---------------------------------------------------------------------------
def cmd_hypotheses_init(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    count = args.count or int(config["research"]["default_hypothesis_count"])
    with _store(state) as store:
        path = create_hypothesis_template(_run_dir(store, args.run_id), count)
    _json({"path": str(path), "count": count})
    return 0


def cmd_hypotheses_packet(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        run_dir = _run_dir(store, args.run_id)
    path = create_blind_ranking_packet(run_dir / "questions" / "hypotheses.json", args.seed)
    _json({"path": str(path), "seed": args.seed, "note": "ranking_key.json must not be given to judges"})
    return 0


def cmd_hypotheses_aggregate(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        run_dir = _run_dir(store, args.run_id)
    research = config["research"]
    packet_path = args.packet or run_dir / "questions" / "ranking_packet.json"
    output_path = args.output or run_dir / "questions" / "hypotheses_scored_panel.json"
    path = aggregate_judge_panel(packet_path, args.judge, output_path,
                                 int(research.get("minimum_hypothesis_judges", 4)), int(research.get("minimum_codex_judges", 2)))
    _json({"path": str(path), "judges": len(args.judge)})
    return 0


def cmd_hypotheses_rank(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        run_dir = _run_dir(store, args.run_id)
        input_path = args.input or run_dir / "questions" / "hypotheses_scored_panel.json"
        json_path, markdown_path = rank_scored_hypotheses(input_path, run_dir / "questions")
        if store.get_run(args.run_id)["status"] in {"routed_hybrid", "routed_hypothesis"}:
            store.transition(args.run_id, "hypotheses_ranked", {"ranking": str(json_path)}, config=config)
    _json({"json": str(json_path), "markdown": str(markdown_path)})
    return 0


# ---------------------------------------------------------------------------
# plan / jobs / evaluate
# ---------------------------------------------------------------------------
def cmd_plan_write(args: argparse.Namespace) -> int:
    from .models import ExperimentSpec
    from .workflow import write_plan

    _, _, state, _ = _context(args)
    payload = json.loads(args.plan.read_text(encoding="utf-8"))
    with _store(state) as store:
        run = store.get_run(args.run_id)
        if not run.get("contract_sha256"):
            raise ValueError("freeze the analysis contract before writing an experiment plan")
        specs = [ExperimentSpec.model_validate({**s, "run_id": args.run_id, "contract_sha256": run["contract_sha256"]})
                 for s in payload["experiments"]]
        path = write_plan(run, specs)
    _json({"plan": str(path), "experiments": [{"variant": s.variant_id, "seed": s.seed, "key": s.idempotency_key()} for s in specs]})
    return 0


def _controller(store: Store, run_id: str, config: dict[str, Any]):
    from .executors import build_executor
    from .workflow import ExperimentController, load_plan

    names = {s.executor for s in load_plan(store.get_run(run_id))}
    return ExperimentController(store, run_id, {n: build_executor(n, config) for n in names}, config)


def cmd_jobs_step(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        if store.get_run(args.run_id)["status"] == "contract_frozen" and not args.dry_run:
            store.transition(args.run_id, "primary_running", {"controller": "jobs step"}, config=config)
        controller = _controller(store, args.run_id, config)
        if args.wait:
            report = controller.run_until_terminal(float(config["execution"]["poll_seconds"]), args.max_wall_seconds,
                                                   takeover=args.takeover)
        else:
            report = controller.step(submit=not args.dry_run)
        _json(report.as_dict())
    return 0


def cmd_jobs_status(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        _json([{k: j[k] for k in ("idempotency_key", "variant_id", "seed", "executor", "state", "attempt", "max_attempts", "external_job_id")}
               | {"note": j["record"].get("note"), "output_dir": j["spec"]["output_dir"]} for j in store.jobs_for_run(args.run_id)])
    return 0


def cmd_jobs_resolve(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        job = store.get_job(args.key)
        if not job:
            raise KeyError(args.key)
        if job["state"] not in {"unknown_after_crash", "submitting"}:
            raise ValueError(f"job {args.key} is {job['state']}; only crash-ambiguous jobs can be resolved by hand")
        changes: dict[str, Any] = {"state": args.state, "note": f"resolved by user: {args.reason}"}
        if args.external_job_id:
            changes["external_job_id"] = args.external_job_id
        _json(store.update_job(args.key, **changes))
    return 0


def cmd_jobs_requeue(args: argparse.Namespace) -> int:
    from .workflow import requeue_unstarted

    _, config, state, _ = _context(args)
    with _store(state) as store:
        controller = _controller(store, args.run_id, config)
        _json({"requeued": requeue_unstarted(store, args.run_id, controller.executors, args.reason)})
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from .evaluation import evaluate_run, render_markdown

    _, config, state, _ = _context(args)
    with _store(state) as store:
        result = evaluate_run(store, args.run_id)
        run = store.get_run(args.run_id)
        out = Path(run["run_dir"]) / "evaluation"
        (out / "evaluation.md").write_text(render_markdown(result), encoding="utf-8")
        if run["status"] == "primary_running" and args.complete_primary:
            store.transition(args.run_id, "primary_complete", {"verdicts": result.verdicts}, config=config)
        _json({"verdicts": result.verdicts, "evaluation": str(out / "evaluation.json"), "status": store.get_run(args.run_id)["status"]})
    return 0


# ---------------------------------------------------------------------------
# sessions / audit / report / schemas
# ---------------------------------------------------------------------------
def cmd_session_launch(args: argparse.Namespace) -> int:
    root, config, state, _ = _context(args)
    with _store(state) as store:
        spec = launch_claude_session(root, config, store, args.run_id, args.role, args.prompt, args.model, dry_run=args.dry_run)
    _json({"session": spec["session_name"], "tmux": spec["tmux_name"], "model": spec["model"],
           "work_dir": spec["work_dir"], "watch": f"tmux attach-session -r -t {spec['tmux_name']}",
           "log": spec["log_path"], "started": not args.dry_run})
    return 0


def cmd_session_status(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        sessions = store.sessions_for_run(args.run_id)
    _json([inspect_session(Path(s["spec_path"])) if Path(s["spec_path"]).exists() else s for s in sessions])
    return 0


def cmd_session_watch(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        matches = [item for item in store.sessions_for_run(args.run_id) if item["role"] == args.role]
    if not matches:
        raise KeyError(f"No {args.role} session for {args.run_id}")
    os.execvp("tmux", ["tmux", "attach-session", "-r", "-t", matches[-1]["tmux_name"]])
    return 0


def cmd_audit_init(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        run_dir = _run_dir(store, args.run_id)
    json_path, markdown_path = create_audit_template(run_dir)
    prompt = write_adversary_prompt(run_dir)
    _json({"json": str(json_path), "markdown": str(markdown_path), "adversary_prompt": str(prompt)})
    return 0


def cmd_audit_adopt(args: argparse.Namespace) -> int:
    _, config, state, _ = _context(args)
    with _store(state) as store:
        run = store.get_run(args.run_id)
        path = adopt_audit(Path(run["run_dir"]), args.audit, normalize=args.normalize)
        audit, _ = load_artifact(path, "audit_findings")
        summary = audit_summary(audit)  # type: ignore[arg-type]
        if run["status"] == "audit_running":
            target = "repair_required" if summary["blocking"] or summary["gate"] == "repair_required" else "audit_complete"
            store.transition(args.run_id, target, {"audit": summary}, config=config)
        _json({"audit": str(path), **summary, "run_status": store.get_run(args.run_id)["status"]})
    return 0


def cmd_report_init(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        run_dir = _run_dir(store, args.run_id)
    _json({"created": [str(path) for path in create_report_skeleton(run_dir)]})
    return 0


def cmd_report_manifest(args: argparse.Namespace) -> int:
    _, _, state, _ = _context(args)
    with _store(state) as store:
        run_dir = _run_dir(store, args.run_id)
    _json({"manifest": str(build_artifact_manifest(run_dir))})
    return 0


def cmd_schemas_export(args: argparse.Namespace) -> int:
    _json({"written": [str(p) for p in export_json_schemas(args.directory)]})
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artos", description="ARTOS research orchestrator")
    parser.add_argument("--root", type=Path, help="ARTOS project root")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check ARTOS, store, Claude, and tmux readiness")
    doctor.set_defaults(func=cmd_doctor)

    inventory = sub.add_parser("inventory", help="Manage compute and data inventories")
    inventory_sub = inventory.add_subparsers(dest="inventory_command", required=True)
    refresh = inventory_sub.add_parser("refresh")
    refresh.add_argument("--compute", action="store_true")
    refresh.add_argument("--data-root", action="append", default=None)
    refresh.set_defaults(func=cmd_inventory_refresh)
    show = inventory_sub.add_parser("show")
    show.add_argument("--kind", action="append")
    show.set_defaults(func=cmd_inventory_show)

    data = sub.add_parser("data", help="Register data or artifacts")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    register = data_sub.add_parser("register")
    register.add_argument("path", type=Path)
    register.add_argument("--kind")
    register.set_defaults(func=cmd_data_register)

    run = sub.add_parser("run", help="Create and manage research runs")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    create = run_sub.add_parser("create")
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--question")
    source.add_argument("--email", type=Path)
    source.add_argument("--source-file", type=Path)
    create.add_argument("--source-type", default="other", choices=("question", "email", "manuscript", "slides", "other"))
    create.add_argument("--project")
    create.add_argument("--title")
    create.add_argument("--mother-question", help="Interpreted mother question, recorded separately from the verbatim source")
    create.set_defaults(func=cmd_run_create)
    listing = run_sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=25)
    listing.set_defaults(func=cmd_run_list)
    show_run = run_sub.add_parser("show")
    show_run.add_argument("run_id")
    show_run.set_defaults(func=cmd_run_show)
    transition = run_sub.add_parser("transition")
    transition.add_argument("run_id")
    transition.add_argument("status")
    transition.add_argument("--reason")
    transition.set_defaults(func=cmd_run_transition)
    route = run_sub.add_parser("route", help="Record a route decision (with rationale) and transition")
    route.add_argument("run_id")
    route.add_argument("route", choices=("direct", "hybrid", "hypothesis"))
    route.add_argument("--rationale", required=True)
    route.add_argument("--decided-by", default="orchestrator")
    route.set_defaults(func=cmd_run_route)
    snapshot = run_sub.add_parser("snapshot-inventory")
    snapshot.add_argument("run_id")
    snapshot.set_defaults(func=cmd_run_snapshot)
    freeze = run_sub.add_parser("freeze-contract")
    freeze.add_argument("run_id")
    freeze.add_argument("contract", type=Path)
    freeze.set_defaults(func=cmd_run_freeze)
    cancel = run_sub.add_parser("cancel", help="Request cancellation, cancel live jobs, mark the run cancelled")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason")
    cancel.set_defaults(func=cmd_run_cancel)
    events = run_sub.add_parser("events", help="Show the hash-chained event log")
    events.add_argument("run_id", nargs="?")
    events.set_defaults(func=cmd_events)

    hypotheses = sub.add_parser("hypotheses", help="Prepare and rank hypotheses")
    hypotheses_sub = hypotheses.add_subparsers(dest="hypotheses_command", required=True)
    init_h = hypotheses_sub.add_parser("init")
    init_h.add_argument("run_id")
    init_h.add_argument("--count", type=int)
    init_h.set_defaults(func=cmd_hypotheses_init)
    packet = hypotheses_sub.add_parser("packet")
    packet.add_argument("run_id")
    packet.add_argument("--seed", type=int, default=20260816)
    packet.set_defaults(func=cmd_hypotheses_packet)
    aggregate = hypotheses_sub.add_parser("aggregate")
    aggregate.add_argument("run_id")
    aggregate.add_argument("--packet", type=Path)
    aggregate.add_argument("--judge", action="append", type=Path, required=True)
    aggregate.add_argument("--output", type=Path)
    aggregate.set_defaults(func=cmd_hypotheses_aggregate)
    rank = hypotheses_sub.add_parser("rank")
    rank.add_argument("run_id")
    rank.add_argument("--input", type=Path)
    rank.set_defaults(func=cmd_hypotheses_rank)

    plan = sub.add_parser("plan", help="Experiment plans bound to a frozen contract")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_write = plan_sub.add_parser("write")
    plan_write.add_argument("run_id")
    plan_write.add_argument("plan", type=Path)
    plan_write.set_defaults(func=cmd_plan_write)

    jobs = sub.add_parser("jobs", help="Idempotent job submission, polling, cancellation, and recovery")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    step = jobs_sub.add_parser("step", help="Reconcile once (or --wait); safe to re-run after any interruption")
    step.add_argument("run_id")
    step.add_argument("--wait", action="store_true")
    step.add_argument("--dry-run", action="store_true", help="Poll and recover only; never submit")
    step.add_argument("--max-wall-seconds", type=float)
    step.add_argument("--takeover", action="store_true", help="Take the controller lease if its owner process on this host is dead")
    step.set_defaults(func=cmd_jobs_step)
    jstatus = jobs_sub.add_parser("status")
    jstatus.add_argument("run_id")
    jstatus.set_defaults(func=cmd_jobs_status)
    resolve = jobs_sub.add_parser("resolve", help="Resolve a crash-ambiguous job after checking the executor by hand")
    resolve.add_argument("key")
    resolve.add_argument("--state", required=True, choices=("submitted", "failed", "cancelled", "succeeded"))
    resolve.add_argument("--external-job-id")
    resolve.add_argument("--reason", required=True)
    resolve.set_defaults(func=cmd_jobs_resolve)

    requeue = jobs_sub.add_parser("requeue-unstarted", help="Reset attempts of failed jobs whose runner never started")
    requeue.add_argument("run_id")
    requeue.add_argument("--reason", required=True)
    requeue.set_defaults(func=cmd_jobs_requeue)

    evaluate = sub.add_parser("evaluate", help="Deterministic evaluation against the frozen contract")
    evaluate.add_argument("run_id")
    evaluate.add_argument("--complete-primary", action="store_true", help="Transition to primary_complete if gates pass")
    evaluate.set_defaults(func=cmd_evaluate)

    session = sub.add_parser("session", help="Launch and monitor tmux agents")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    launch = session_sub.add_parser("launch")
    launch.add_argument("run_id")
    launch.add_argument("--role", choices=("ranker", "primary", "adversary", "judge"), required=True)
    launch.add_argument("--prompt", type=Path, required=True)
    launch.add_argument("--model")
    launch.add_argument("--dry-run", action="store_true", help="Create the isolated work dir and spec without starting tmux")
    launch.set_defaults(func=cmd_session_launch)
    status = session_sub.add_parser("status")
    status.add_argument("run_id")
    status.set_defaults(func=cmd_session_status)
    watch = session_sub.add_parser("watch")
    watch.add_argument("run_id")
    watch.add_argument("--role", choices=("ranker", "primary", "adversary", "judge"), required=True)
    watch.set_defaults(func=cmd_session_watch)

    audit = sub.add_parser("audit", help="Adversarial audit records")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_init = audit_sub.add_parser("init")
    audit_init.add_argument("run_id")
    audit_init.set_defaults(func=cmd_audit_init)
    audit_adopt_p = audit_sub.add_parser("adopt", help="Validate an adversary's audit file and apply the gate")
    audit_adopt_p.add_argument("run_id")
    audit_adopt_p.add_argument("audit", type=Path)
    audit_adopt_p.add_argument("--normalize", action="store_true",
                               help="Coerce agent layout variants (list checks, string evidence); values unchanged, notes recorded")
    audit_adopt_p.set_defaults(func=cmd_audit_adopt)

    report = sub.add_parser("report", help="Create reporting artifacts")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_init = report_sub.add_parser("init")
    report_init.add_argument("run_id")
    report_init.set_defaults(func=cmd_report_init)
    manifest = report_sub.add_parser("manifest")
    manifest.add_argument("run_id")
    manifest.set_defaults(func=cmd_report_manifest)

    schemas = sub.add_parser("schemas", help="Export JSON Schemas generated from the Pydantic models")
    schemas_sub = schemas.add_subparsers(dest="schemas_command", required=True)
    export = schemas_sub.add_parser("export")
    export.add_argument("directory", type=Path, nargs="?", default=Path("schemas"))
    export.set_defaults(func=cmd_schemas_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, KeyError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"artos: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
