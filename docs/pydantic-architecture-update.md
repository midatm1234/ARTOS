# ARTOS v0.2: Pydantic-based orchestration architecture

This update replaces the v0.1 public skeleton, whose core modules raised
`NotImplementedError`, with a working orchestration runtime. That runtime is built
on typed, validated artifacts, a durable store and deterministic scientific gates.
It is domain-independent: ARTOS orchestrates research runs, and domain tools plug
in as executors.

Related documents: [architecture decision record](architecture-decision.md) (framework
comparison) and [running and resuming a workflow](running-and-resuming.md) (full
command reference).

## 1. Purpose and the role of Pydantic

ARTOS coordinates several agents: a proposer, blind judges, a primary investigator
and an adversary. It also coordinates long-running jobs. Every handoff between them
is a file that the next stage acts on. In v0.1 these files were free-form JSON, so
a malformed or manipulated artifact could pass silently.

Pydantic v2 is the **contract layer at every trust boundary**:

- **Agent outputs are validated before use.** A judge's scores, an adversary's
  audit and a proposer's hypotheses are parsed into strict models. Unknown fields,
  out-of-range scores, missing falsification criteria and contradictory states
  (for example, an audit marked `pass` while a major finding is still `open`) are
  rejected with a message that names the file.
- **Scientific rules live in validators.** Verified evidence must state how it was
  verified. A hypothesis set needs at least two competing hypotheses and may cite
  only evidence that exists. A judge that saw the proposer's ranking is invalid. A
  research question's hashes must match its verbatim text.
- **Hashes are canonical.** Contracts and experiment specs are hashed from their
  validated, sorted JSON form, so key order and formatting do not change a hash.
- **Configuration is typed.** `artos.json` is validated on load. Unknown top-level
  sections and invalid values (such as a negative budget) are rejected.
- **Schemas are exportable.** `artos schemas export` writes JSON Schema for every
  artifact, for use by agents and other tools.

Pydantic is used only for data and validation. Workflow control is plain,
deterministic Python over SQLite. The [ADR](architecture-decision.md) explains why
PydanticAI/pydantic-graph and LangGraph were not adopted for that role.

## 2. Main components

```
                 ┌──────────────────────────── artos CLI ────────────────────────────┐
  source ─► runs.py ─► gates.py ◄── store.py (SQLite: runs, events, jobs, leases, sessions)
                 │          ▲                     ▲
  hypotheses.py ─┤          │                     │
  (blind packet, │     models.py (Pydantic artifacts, versioned, legacy upgrade)
   ranking)      │          │                     │
  sessions.py ───┤          │                workflow.py (reconcile controller)
  (isolated      │          │                     │
   agent dirs)   │     evaluation.py         executors.py ─► job_runner.py / MCP server
  audit.py ──────┘     (contract rules)      (local | mcp | mock)
```

| Module | Responsibility |
|---|---|
| `models.py` | Pydantic artifact models, `schema_version = 2`, canonical hashing, legacy upgrade, JSON Schema export |
| `config.py` | Typed `artos.json` (`project`, `inventory`, `agents`, `research`, `safety`, `execution`, `approvals`) |
| `store.py` | SQLite (WAL, `BEGIN IMMEDIATE`). Runs and transitions, a hash-chained append-only event log, the job ledger keyed by idempotency key, controller leases, sessions, resources |
| `state_machine.py` | The declared transition table (unchanged from v0.1) |
| `gates.py` | Deterministic checks run on every transition, before the state changes |
| `runs.py` | Run creation (verbatim source and hashes first), inventory snapshots, contract freezing |
| `hypotheses.py` | Blind ranking packets, judge-panel aggregation, deterministic ranking |
| `sessions.py`, `session_worker.py` | Agent sessions in tmux with per-role writable directories, frozen prompts, budget limits, and adversary inputs that exclude the primary agent's interpretation |
| `workflow.py` | Resumable experiment controller: submit, poll, recover, retry, cancel, budgets, concurrency |
| `executors.py`, `job_runner.py`, `procutil.py`, `mcp_client.py` | `local` (detached subprocess, wall-clock limit), `mcp` (any MCP server over stdio) and `mock` executors |
| `evaluation.py` | Applies the contract's pre-registered acceptance rules per seed and derives verdicts |
| `audit.py` | Audit template, adoption of an adversary's findings (with optional layout normalization), gate summary |
| `inventory.py` | Compute snapshot and a file-level data catalog |
| `knowledge.py` | Export runs as an OKF v0.3 knowledge bundle (concept documents, typed relations, `graph.json`) and validate conformance; see [knowledge-base.md](knowledge-base.md) |

### Data models

| Kind | Model | Key guarantees |
|---|---|---|
| `research_question` | `ResearchQuestion` | Verbatim source and interpreted mother question are stored separately, each with a verified SHA-256 |
| `route_decision` | `RouteDecision` | `direct`, `hybrid` or `hypothesis`, with a recorded rationale |
| `hypothesis_set` | `HypothesisSet`, `Hypothesis`, `LiteratureEvidence` | ≥ 2 hypotheses; mechanism, expected benefit, cost and falsification required; evidence typed as published finding, proposed idea or local measurement |
| `ranking_packet` | `RankingPacket`, `BlindCard` | Opaque card IDs, shuffled order, no proposer fields |
| `judge_scores` | `JudgeScores`, `CardScore` | Scores in [0, 1]; bound to the hash of one specific packet |
| `ranking_result` | `RankingResult` | Arithmetic over judges; records the judge IDs |
| `analysis_contract` | `AnalysisContract` | Datasets, splits, variants, seeds, metrics, controls, leakage controls, `acceptance_rules`, `decision_policy`, compute budget |
| `frozen_contract` | `FrozenContract` | Content hash checked on load |
| `experiment_spec` | `ExperimentSpec` | Bound to the contract hash; `idempotency_key()` is derived from its full content |
| `execution_record` | `ExecutionRecord` | Job state, attempts, code refs, environment, output hashes, runtime |
| `evaluation_result` | `EvaluationResult` | Every metric value, every rule outcome per seed, and the verdicts |
| `audit_findings` | `AuditFindings`, `AuditFinding` | Severity and status; a gate consistent with blocking findings; `independently_reproduced` true only when recomputed results and a method are both named |
| `decision_record` | `DecisionRecord` | Who decided what, why, and on what evidence |

## 3. Agent orchestration workflow

```
intake ─► inventory ─► orientation ─► route ─┬─ direct ─────────────────────────────► reporting
                                             └─ hypothesis/hybrid
                                                 proposer writes hypothesis_set
                                                 └─► blind packet ─► N judges (separate contexts)
                                                     └─► ranking ─► [gate] hypotheses_ranked
                                                         └─► contract frozen + hashed ─► [gate]
                                                             └─► plan (specs bound to contract hash)
                                                                 └─► controller: submit/poll/recover
                                                                     └─► deterministic evaluation ─► [gate]
                                                                         └─► adversary (separate session + dir)
                                                                             ├─► repair_required ─► (bounded) repair
                                                                             └─► audit_complete ─► reporting ─► [gate] complete
```

The gates are enforced in code on every `Store.transition`:

| Transition | Refused unless |
|---|---|
| `routed_*` | the route decision on disk matches the requested route |
| `hypotheses_ranked` | the packet matches the hypothesis set, leaks no proposer fields and is not in the proposer's order; there are enough distinct judges; the proposer did not judge |
| `contract_frozen`, `primary_running` | the frozen contract's hash matches the store; every selected hypothesis was ranked; the repair-round limit has not been reached |
| `primary_complete` | every job is terminal and at least one succeeded; an evaluation exists for **this** contract |
| `audit_running`, `audit_complete` | primary and adversary work directories and tmux sessions are disjoint; the audit ran in a separate session; no open critical or major finding |
| `reporting`, `complete` | the audit gate passes (unless the route is direct); the final summary exists; it claims "independently reproduced" only if the audit lists recomputed results and a method |
| any, after `run cancel` | only `cancelled` or `failed` |

**No duplicate jobs.** Before submitting, the controller claims each spec's
idempotency key in SQLite. After any interruption it reconciles:

- a live job is polled;
- an interrupted submission is recovered through the executor's durable marker
  (`local`) or the server's request-ID lookup (`mcp`);
- a job that cannot be resolved is **blocked**, never resubmitted blindly;
- a failure is retried up to `max_attempts_per_job`. A submission whose runner
  never started does not count as an attempt.

A lease ensures only one controller drives a run. A lease is taken over only from
a dead process on the same host.

## 4. Key changes from v0.1

| Area | v0.1 | v0.2 |
|---|---|---|
| Store, runs, hypotheses, sessions, inventory | stubs (`NotImplementedError`) | implemented |
| Artifacts | free-form JSON plus two loose JSON Schemas | 13 versioned Pydantic models; JSON Schema generated from them |
| Transitions | table check only | table check **plus** evidence gates |
| Event history | not implemented | append-only, hash-chained, verifiable (`artos run events`) |
| Jobs | none | idempotent controller with recovery, retries, budgets, GPU approval limit, concurrency, cancellation |
| Evaluation | none | contract-defined acceptance rules per paired seed; verdicts `supported`, `unsupported` or `inconclusive` |
| Config | unvalidated dict | validated model; new `execution` and `approvals` sections |
| Dependencies | none | `pydantic>=2.7` |
| CLI | v0.1 commands (stubs) | same commands, working, plus `run route/cancel/events`, `plan write`, `jobs step/status/resolve/requeue-unstarted`, `evaluate`, `audit adopt`, `schemas export` |

## 5. Setup

```bash
git clone git@github.com:midatm1234/ARTOS.git && cd ARTOS
git checkout ARTOS_pydantic
python -m pip install -e ".[test]"          # Python >= 3.10; installs pydantic>=2.7
cp artos.example.json artos.json
artos doctor                                # core_ok: pydantic + store; agents_ok: tmux + claude
python -m pytest -q                         # 57 tests
```

`core_ok` is all the workflow below needs. `agents_ok` (tmux and the `claude` CLI)
is needed only to launch agent sessions.

## 6. Minimal domain-independent example

The goal: test whether treatment A beats control B, using a toy job that writes
a metric.

```bash
# 1. intake and route
RUN=$(artos run create --question "Does treatment A reduce error relative to control B?" \
        --project demo --title demo | python -c "import json,sys; print(json.load(sys.stdin)['run']['run_id'])")
artos run route $RUN hypothesis --rationale "Two competing explanations need a controlled comparison."
RD=$(artos run show $RUN | python -c "import json,sys; print(json.load(sys.stdin)['run']['run_dir'])")
MQ=$(python -c "import json; print(json.load(open('$RD/questions/research_question.json'))['mother_question_sha256'])")
```

`$RD/questions/hypotheses.json` holds the competing hypotheses, written by the proposer:

```json
{"schema_version": 2, "kind": "hypothesis_set", "run_id": "<RUN>", "mother_question_sha256": "<MQ>",
 "proposer": "proposer", "evidence": [],
 "hypotheses": [
  {"hypothesis_id": "H1", "statement": "Treatment A reduces error versus B", "mechanism": "A adds information",
   "expected_benefit": "lower error", "implementation_cost": "low", "falsification": "A not better than B on 2 of 3 seeds"},
  {"hypothesis_id": "H0", "statement": "A and B do not differ", "mechanism": "no causal signal", "role": "null",
   "expected_benefit": "none", "implementation_cost": "none", "falsification": "A beats B on all seeds"}]}
```

```bash
# 2. blind ranking (each judge in its own context; ranking_key.json is never shown to judges)
artos hypotheses packet $RUN --seed 7
#   ...each judge writes a judge_scores JSON bound to the packet hash...
artos hypotheses aggregate $RUN --judge judge-a.json --judge judge-b.json
artos hypotheses rank $RUN                     # gate -> hypotheses_ranked
```

The contract lists variants `A` and `B`, seeds `[0, 1, 2]`, and one rule:

```json
"acceptance_rules": [{"rule_id": "R1", "hypothesis_id": "H1", "group": "primary", "candidate": "A",
  "reference": "B", "variable": "y", "metric": "error", "mode": "rel_improve", "threshold": 0.05}]
```

```bash
# 3. freeze, plan, run, evaluate
artos run freeze-contract $RUN contract.json   # validated, hashed, read-only
artos plan write $RUN plan.json                # local executor; each job writes <output_dir>/metrics.json:
                                               #   {"metrics": {"y": {"error": 0.91}}}
artos jobs step $RUN --wait                    # safe to interrupt and re-run at any point
artos evaluate $RUN --complete-primary         # -> evaluation/evaluation.{json,md}

# 4. adversarial audit and completion
artos run transition $RUN audit_running
artos audit init $RUN                          # writes the adversary's first-pass prompt
artos session launch $RUN --role adversary --prompt $RD/prompts/adversary_first_pass.md
artos audit adopt $RUN $RD/work/adversary-1/audit_findings.json
artos run transition $RUN reporting
artos run transition $RUN complete             # refused if blocking findings or unearned reproduction claims

# 5. publish to the knowledge base (OKF v0.3)
artos knowledge export kb/ --run-id $RUN
```

A plan entry for the `local` executor:

```json
{"experiments": [{"variant_id": "A", "seed": 0, "executor": "local", "output_dir": "<RD>/jobs/A-s0",
  "command": ["python", "my_experiment.py", "--variant", "A", "--seed", "0", "--out", "<RD>/jobs/A-s0"],
  "estimated_cpu_hours": 0.1}]}
```

To run jobs through an MCP server instead, set `"executor": "mcp"`, `"tool"` and
`"tool_args"` in the plan, and configure the server once in `artos.json`:

```json
"execution": {"mcp": {"command": ["python", "my_server.py"],
                      "tools": {"status": "get_job_status", "cancel": "cancel_job",
                                "lookup": "find_job_by_client_request_id",
                                "request_id_arg": "client_request_id"}}}
```

## 7. Migration requirements

- **Dependency.** Install `pydantic>=2.7` (`pip install -e .`).
- **`artos.json`.** The existing layout keeps working. Validation now rejects
  unknown top-level sections and invalid values. Optional new sections:
  `execution` (python, `job_timeout_seconds`, `poll_seconds`, `min_free_gpu_mib`,
  `max_concurrent_jobs`, `mcp`) and `approvals` (`{"<run_id>": {"gpu_hours": N}}`).
- **Existing JSON artifacts.** v0.1 files (no `schema_version`) load through
  `load_artifact`, which upgrades them in memory and returns upgrade notes. A
  legacy boolean `recomputed_via_independent_path` is kept, but it no longer counts
  as evidence of independent reproduction.
- **Transitions.** `artos run transition` now enforces gates. Scripts that jumped
  between states without the required artifacts will be refused; they must write
  the artifacts the gate names.
- **Executors.** Use `local`, `mcp` or `mock`. Domain-specific executors belong
  outside the core.
- **New store.** v0.1 had no working store, so there is nothing to migrate. A new
  `state/artos.sqlite` is created on first use.

## 8. Validation (this update)

- `python -m pytest -q`: 52 passed, on Python 3.14.6 (Windows 11) and Python
  3.12.16. The tests cover:
  - schema validation and legacy upgrade;
  - every gate: blind ranking, proposer exclusion, judge count, route mismatch, frozen
    contract tampering and replacement, evaluation bound to its contract, audit
    blocking, repair-round limit, reproduction wording, role isolation;
  - invalid transitions and event-chain tamper detection;
  - duplicate-job prevention across restarts;
  - crash recovery before and after the executor saw a submission;
  - blocking of unresolvable jobs, bounded retries, cancellation, budgets and GPU approval;
  - concurrency queueing, lease takeover, stale-marker handling;
  - a real subprocess with a wall-clock timeout, and an MCP round trip against an
    in-test stdio server.
- `artos doctor` and a CLI smoke test (`run create` → `inventory_ready`) on a fresh root.
- The full workflow has also been run end to end on a real multi-seed experiment,
  including blind judges and a two-round adversarial audit. Those domain-specific
  materials are kept outside this repository.

## 9. Current limitations and remaining work

- **Agent launch is partly manual.** `session launch` creates isolated directories
  and tmux sessions for `claude -p`, but it does not yet decide when to launch
  judges or the adversary, or collect their outputs automatically. Only the
  `claude` provider has a launcher.
- **Permission scoping for agents** is left to the operator. Claude Code matches
  file writes against `Edit(...)` permission rules, so a narrowly scoped adversary
  needs an `Edit(<work_dir>/audit_findings.json)` rule.
- **Write isolation** is checked, not enforced. ARTOS verifies that directories are
  disjoint, but an agent's permissions decide what it can actually write. Use
  disposable worktrees or sandboxing for strict isolation (roadmap v0.3).
- **Independent recomputation** depends on the adversary supplying it. ARTOS
  records and gates on it but does not yet generate the recomputation task
  (roadmap v0.4).
- **Inventory is file-level.** NetCDF, Zarr and similar metadata extraction is not
  done (roadmap v0.2 item).
- **Reporting** writes Markdown skeletons and a hash manifest only. Slide
  production is not implemented (roadmap v0.5).
- **Evaluation rules** are paired relative or absolute thresholds with seed
  agreement. There is no built-in bootstrap or significance test yet. Relative
  thresholds become noise-sensitive when the reference value is small.
- **The MCP executor** needs a server that accepts a request-ID argument and offers
  a lookup tool for duplicate-free recovery. Without them, interrupted submissions
  are blocked for manual resolution (`artos jobs resolve`).
- **The `local` executor** runs on one host. It has no multi-host scheduling.
