# ADR-001: Orchestration architecture for typed research workflows

Status: accepted (2026-11-20). Branch `ARTOS_pydantic`.

## Context

ARTOS v0.1 was a public skeleton. The state machine, configuration, utilities and
reporting worked. The store, runs, hypotheses, inventory and sessions modules were
stubs that raised `NotImplementedError`. The operating contract (`AGENTS.md`) requires:

1. explicit, durable workflow state with gated transitions;
2. competing hypotheses, ranked blind in a separate context;
3. a frozen, hashed analysis contract before testing;
4. primary and adversary agents in separate tmux sessions and writable directories;
5. finalization blocked by open critical or major audit findings;
6. honest reproduction claims;
7. resumable long-running jobs (GPU training that runs for hours to days) that are
   **never submitted twice**;
8. human approval gates on budgets, data acquisition and outbound actions.

Agents already run as separate `claude -p` processes in tmux. Domain tools (for
example model training pipelines) are typically exposed as MCP servers that have their
own job managers.

## Options compared

Every option uses Pydantic models for artifacts. They differ in what owns the
workflow state.

| Requirement | A. ARTOS + Pydantic + SQLite (chosen) | B. PydanticAI + pydantic-graph / Harness | C. LangGraph + Pydantic |
|---|---|---|---|
| Model providers | Any CLI agent (Claude Code today). The provider sits behind `sessions.build_command` | Many providers through PydanticAI models | Many through LangChain chat models |
| Existing agent execution (tmux + `claude -p`) | Native | Would replace the CLI agents with in-process `Agent` runs, or wrap them as tools | Same as B |
| MCP tools | Minimal stdio client (`mcp_client.py`), no dependencies | First-class MCP client | Through `langchain-mcp-adapters` |
| Explicit workflow state | Declared transition table plus deterministic gates | Graph nodes. The v2 docs say *"`pydantic_graph.persistence` was removed in V2"* | `StateGraph` with typed state |
| Persistence / recovery | SQLite WAL, hash-chained events, artifacts on disk | Harness `StepPersistence` saves an agent run's **message history**, not a multi-stage workflow. Durable engines (Temporal, DBOS, Prefect, …) are separate services | Checkpointers (`SqliteSaver`, `PostgresSaver`) per thread |
| Duplicate side effects on resume | Idempotency key per experiment, recorded before submission; executor-side marker or `client_request_id`; ambiguous jobs block | Docs: no automatic dedup. Tool calls interrupted mid-run become `unknown_after_crash` | Durable execution replays from checkpoints. Side effects must be idempotent or wrapped by the user |
| Parallel investigations | Independent runs and jobs; a lease prevents two controllers on one run | Broadcast/map/join in graph v2 | Send/fan-out |
| Observability | Event log, per-job logs and markers, `artos run events` | Logfire / OpenTelemetry | LangSmith |
| Testing | Plain unit tests (52), real subprocess tests | Good (TestModel) | Good |
| Dependency burden | `pydantic` only | `pydantic-ai-slim` → `pydantic-graph`, `httpx2`, `opentelemetry-api`, `genai-prices`, … | `langgraph` → `langchain-core`, `langgraph-checkpoint`, `langgraph-sdk`, `xxhash`, … |
| Maintenance | Small and owned; no framework upgrade churn | 0.x/2.x API churn (graph persistence removed in v2) | Frequent minor releases |

Versions checked on PyPI on 2026-11-20: `pydantic-graph` 2.71.0, `pydantic-ai-slim`
2.71.0, `langgraph` 1.2.24.

## Decision

**A. Extend ARTOS with Pydantic schemas, a SQLite store and a deterministic
reconcile controller.** It is the simplest option that meets every requirement:

- The hardest requirement is #7, no duplicate training jobs after interruption. No
  framework solves it for us. Each one leaves idempotency of external side effects
  to application code, and that is where the work is.
- ARTOS workflows are long-lived (days), human-gated and file-centric. The unit of
  recovery is a **job and an artifact**, not an LLM message history, so
  checkpointing graph state per LLM step adds little.
- Isolation requirements (#2, #4) favor separate OS processes and directories,
  which ARTOS already uses. In-process agent graphs share memory and a working
  directory by default.
- One new dependency (pydantic ≥ 2).

**Where PydanticAI fits later.** If ARTOS gains in-process agents (for example,
cheap blind judges through an API instead of `claude -p`), use PydanticAI `Agent`
with `output_type=JudgeScores` inside a single node. The ARTOS models are already
Pydantic, so they can serve directly as structured outputs. The workflow controller
does not change.

## What was built

| Module | Role |
|---|---|
| `models.py` | Versioned (`schema_version: 2`) artifacts covering the research question, route, literature evidence, hypotheses, blind packet and cards, judge scores, ranking, analysis contract (splits, variants, seeds, leakage controls, acceptance rules, decision policy), frozen contract, experiment spec, execution record, evaluation, audit and decision record. `load_artifact` upgrades v0.1 JSON. |
| `store.py` | SQLite WAL with `BEGIN IMMEDIATE` transactions. Hash-chained events, a job ledger keyed by idempotency key, controller leases (taken over only from a dead local owner), a cancel flag, and a write-once contract hash. |
| `gates.py` | Deterministic checks on every transition: route matches its decision; ranking is blind, comes from enough judges and excludes the proposer; frozen contract hash matches the store; `primary_complete` requires terminal jobs and an evaluation for **this** contract; role isolation before audit; audit blocking; wording about reproduction. |
| `workflow.py` | Reconcile loop. Polls live jobs. Recovers ambiguous submissions by key and never resubmits blindly. Retries failures up to `max_attempts_per_job`. Enforces budgets, the GPU approval limit, concurrency and cancellation. Plans are append-only. |
| `executors.py`, `job_runner.py`, `procutil.py` | `local` executor (detached, portable runner, wall-clock limit, durable marker), `mock` executor, and a generic `mcp` executor (configurable tool names; idempotent through a request-ID argument). |
| `evaluation.py` | Applies the contract's acceptance rules per paired seed, aggregates across seeds, and records every intermediate number. The verdict policy comes from the contract. |
| `sessions.py` | Isolated `work/<role>-<n>/` directories, frozen prompt copies, a budget-locked `claude -p` command (no fallback model), and adversary first-pass inputs that exclude the primary agent's interpretation. |

## Compatibility

- Every v0.1 CLI command still exists with the same arguments. New commands:
  `run route|cancel|events`, `plan write`, `jobs step|status|resolve`,
  `evaluate`, `audit adopt`, `schemas export`.
- The `artos.json` layout is unchanged. It is now validated, and unknown top-level
  sections are rejected. New optional `execution` and `approvals` sections.
- v0.1 JSON (the synthetic example) loads with in-memory upgrade notes. A legacy
  boolean `recomputed_via_independent_path` is kept but **not** trusted as evidence
  of reproduction.
- `Store.transition` now enforces gates by default. Tests can pass
  `enforce_gates=False`; the CLI never does.
- `artos doctor` now reports `core_ok` (pydantic and store) separately from
  `agents_ok` (tmux and claude).

## Migration plan (incremental)

1. **Done.** Typed artifacts, store, gates, controller, local/mock executors, CLI.
2. **Done.** Generic `mcp` executor. A domain MCP server becomes a duplicate-safe
   ARTOS backend once it accepts a request-ID argument and offers a lookup tool.
3. **Next.** Add domain integrations as separate packages or repositories. They
   plug in through `executor: mcp` or `executor: local`; none belong in the core.
4. **Next.** Launch adversary sessions automatically from `audit init`, then
   generate the independent-recomputation task (roadmap v0.4).
5. **Optional.** In-process blind judges through PydanticAI structured output.
