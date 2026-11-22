---
type: Reference
title: ARTOS knowledge base export (OKF)
description: How ARTOS turns validated research artifacts into an Open Knowledge Format bundle and a typed knowledge graph.
tags: [artos, okf, knowledge-graph, knowledge-base, provenance]
status: experimental
generated:
  by: claude-code/opus-5.5
  at: 2026-11-21T00:00:00Z
---

ARTOS records research as validated artifacts. `artos knowledge export` publishes
those artifacts as an [Open Knowledge Format](https://okf.md/spec) v0.3 bundle,
so every run adds to a knowledge base that other agents and tools can read:

```bash
artos knowledge export kb/                         # all runs into one bundle
artos knowledge export kb/ --run-id <RUN> --title "My project knowledge"
artos knowledge validate kb/                       # OKF v0.3 conformance + broken-link report
```

# What becomes a concept

| OKF `type` | From | Key fields |
|---|---|---|
| `Run` | run store | `status`, route, `x-artos-contract-sha256` |
| `Research Question` | `questions/research_question.json` | mother question, its hash |
| `Hypothesis` | `questions/hypotheses.json` + ranking + evaluation | `status` = evaluation verdict, else panel verdict; mechanism, falsification |
| `Evidence` | hypothesis-set evidence | `status` verified/unverified, `resource` URL, claim type |
| `Analysis Contract` | `contract/contract.frozen.json` | hash, splits, leakage controls, acceptance rules |
| `Dataset` | contract datasets | version, fingerprint, `resource` path |
| `Experiment` | job ledger | state, attempts, idempotency key, code refs |
| `Evaluation` | `evaluation/evaluation.json` | per-rule outcomes, verdicts |
| `Audit`, `Audit Finding` | `reviews/audit_findings.json` | gate, severity, status, resolution |
| `Decision` | `decision_record` events | actor, rationale, evidence, event hash |

Proposer-private fields (`proposer_rank`, `proposer_notes`) and the blind-ranking key are never exported.

# Relations (the graph)

OKF links are untyped and directed, and typed relations are deferred in v0.3.
ARTOS writes each edge in three places:

- as a bundle-relative link under a `# Relations` heading, with the relation name in the prose;
- in the `x-artos-relations` extension key;
- in `graph.json` (`nodes`, `edges: [{source, relation, target}]`), for graph tools.

Relations: `investigates`, `proposes`, `addresses`, `supported_by`,
`conflicts_with`, `governed_by`, `tests`, `uses_dataset`, `executed`,
`planned_under`, `evaluated_by`, the verdicts (`supported`, `unsupported`,
`inconclusive`), `audited_by`, `reviews`, `raised` and `decided`.

When several runs export into one bundle, nodes with the same identity merge:

- evidence is keyed by citation;
- datasets are keyed by name;
- the merged node keeps the `sources` of every run.

As a result, repeated investigations accumulate into a shared knowledge graph
instead of separate reports.

# Trust fields

- `generated: {by: artos/<version>, at}` on every concept.
- `sources`: the exact artifact (`artos-run://<run>/<path>`) with `id: sha256:<digest>`.
- `verified` appears only when the run's audit independently recomputed results.
  Its actor is `process:artos-adversary/...`, never `human:`, because an agent
  audit is not human review.
- `status` carries the scientific state, so OKF consumers can filter, for example
  supported hypotheses only.

# Conformance

`artos knowledge validate` checks the OKF v0.3 rules:

- every concept's frontmatter parses as a YAML mapping with a non-empty `type`;
- `log.md` has no frontmatter;
- only the root `index.md` carries `okf_version` metadata.

It also lists broken bundle-relative links. OKF tolerates them; ARTOS exports should have none.

# Citations

[1] [OKF specification](https://okf.md/spec)
[2] `src/artos/knowledge.py`
[3] `tests/test_knowledge.py`
