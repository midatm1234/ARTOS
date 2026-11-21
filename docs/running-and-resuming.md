# Running and resuming an ARTOS workflow

Run every command from the project root (the directory that contains `artos.json`), or pass `--root`:

```bash
pip install -e .            # or: export PYTHONPATH=<ARTOS>/src
cp artos.example.json artos.json
artos doctor                # core_ok: pydantic + store; agents_ok: tmux + claude
```

## 1. Intake, inventory, route

```bash
artos run create --question "<verbatim request>" --mother-question "<interpreted question>" --project <p>
artos run route <RUN> hypothesis --rationale "<why direct/hybrid/hypothesis>"
```

`run create` saves the source byte for byte with its hash. It refreshes stale inventories according to the TTLs in `artos.json` and snapshots them into `inventory/`.

## 2. Competing hypotheses and blind ranking

1. The proposer writes `questions/hypotheses.json`, a `hypothesis_set` with at least 2 hypotheses and verified evidence. Use `artos hypotheses init` to get a template.
2. Build the blind packet: `artos hypotheses packet <RUN> --seed N`. It strips `proposer_rank`, `proposer_notes` and `status`, relabels cards with opaque IDs and shuffles them. `ranking_key.json` stays with the controller and is never given to judges.
3. Run each judge in its own context. `artos session launch <RUN> --role judge --prompt <p>` creates `work/judge-<n>/`. Add `--dry-run` to register the directory only, then run `claude -p` yourself.
4. Aggregate and rank:
   ```bash
   artos hypotheses aggregate <RUN> --judge work/judge-1/judge_scores.json --judge ...
   artos hypotheses rank <RUN>
   ```
   The `hypotheses_ranked` gate checks the judge count, that the proposer did not judge, and the packet and hypothesis-set hashes.

## 3. Freeze the contract

```bash
artos run freeze-contract <RUN> contract.json
```

The contract is validated, hashed and written read-only to `contract/contract.frozen.json`. A different contract for the same run is refused. Any later change to the frozen file blocks `primary_running` and the transitions after it.

## 4. Plan and run experiments

```bash
artos plan write <RUN> plan.json      # each spec bound to the frozen contract hash
artos jobs step <RUN>                 # reconcile once: submit / poll / recover
artos jobs step <RUN> --wait          # loop until terminal or blocked
artos jobs status <RUN>
```

Every spec has an idempotency key computed from its full content. The controller claims the key in SQLite **before** it submits, so:

| Interrupted at | On resume |
|---|---|
| after claim, before the executor saw the job | submits once; no attempt is consumed |
| after the executor started it, before the external ID was recorded | `recover()` finds the job's marker (local) or `client_request_id` (MCP); **no resubmission** |
| job running when the controller died | polled; never resubmitted |
| ambiguous (`unknown_after_crash`) | **blocked**; check by hand, then `artos jobs resolve <KEY> --state ... --reason ...` |
| job failed | retried up to `compute_budget.max_attempts_per_job` |

Limits checked before every submission:

- the contract's `compute_budget` (CPU-hours, GPU-hours, max jobs);
- `safety.max_gpu_hours_without_approval`, unless `approvals.<RUN>.gpu_hours` in `artos.json` raises it;
- `execution.max_concurrent_jobs`;
- live free GPU memory (`execution.min_free_gpu_mib`) for GPU specs;
- `execution.job_timeout_seconds`, a wall-clock limit enforced by the runner.

If a controller was killed and its lease still blocks you, add `--takeover`. It works only when the lease owner is a dead process on this host.

To cancel: `artos run cancel <RUN> --reason ...`. This cancels live jobs, blocks new submissions and marks the run cancelled.

## 5. Evaluate, audit, report

```bash
artos evaluate <RUN> --complete-primary   # deterministic; contract acceptance rules, per paired seed
artos run transition <RUN> audit_running  # gate: primary/adversary work dirs and tmux sessions disjoint
artos audit init <RUN>                    # template + adversary first-pass prompt (no primary interpretation)
artos session launch <RUN> --role adversary --prompt <RUN_DIR>/prompts/adversary_first_pass.md
artos audit adopt <RUN> work/adversary-1/audit_findings.json   # -> audit_complete or repair_required
artos run transition <RUN> reporting
artos run transition <RUN> complete       # gate: no open critical/major; summary exists; reproduction wording honest
```

`artos run events <RUN>` shows the hash-chained event log and whether the chain verifies.
