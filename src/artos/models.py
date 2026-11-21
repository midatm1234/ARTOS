"""Typed, versioned ARTOS research artifacts.

Every artifact that crosses a trust boundary (agent output, frozen contract,
job record, evaluation, audit) is validated by one of these models before the
runtime acts on it. Models are Pydantic v2 and carry an explicit
``schema_version`` so stored JSON can be migrated rather than reinterpreted.

Compatibility: JSON written by ARTOS v0.1 (no ``schema_version``, looser
contract and audit layouts) is accepted by :func:`load_artifact`, which
upgrades it in memory and records that it did so. See ``docs/schemas.md``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 2

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON used for every hash ARTOS computes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class ArtosModel(BaseModel):
    """Base: strict about unknown keys unless a model opts out."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=False)


class VersionedArtifact(ArtosModel):
    schema_version: int = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Question, route, hypotheses
# ---------------------------------------------------------------------------
class SourceType(str, Enum):
    question = "question"
    email = "email"
    manuscript = "manuscript"
    slides = "slides"
    other = "other"


class ResearchQuestion(VersionedArtifact):
    """The mother question. Stored verbatim and hashed; never edited."""

    kind: Literal["research_question"] = "research_question"
    run_id: Identifier
    source_type: SourceType
    verbatim_source: str = Field(min_length=1)
    source_sha256: Sha256
    mother_question: str = Field(min_length=1, description="The question as interpreted; recorded separately from hypotheses.")
    mother_question_sha256: Sha256
    received_at: datetime
    untrusted_input: bool = True

    @model_validator(mode="after")
    def _hashes_match(self) -> "ResearchQuestion":
        if hashlib.sha256(self.verbatim_source.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("source_sha256 does not match verbatim_source")
        if hashlib.sha256(self.mother_question.encode("utf-8")).hexdigest() != self.mother_question_sha256:
            raise ValueError("mother_question_sha256 does not match mother_question")
        return self


class Route(str, Enum):
    direct = "direct"
    hybrid = "hybrid"
    hypothesis = "hypothesis"


class RouteDecision(VersionedArtifact):
    kind: Literal["route_decision"] = "route_decision"
    run_id: Identifier
    route: Route
    rationale: str = Field(min_length=10)
    decided_at: datetime
    decided_by: str


class ClaimType(str, Enum):
    published_finding = "published_finding"
    proposed_idea = "proposed_idea"
    local_measurement = "local_measurement"


class LiteratureEvidence(ArtosModel):
    """One source. ``verified`` means the primary page was fetched and matched."""

    evidence_id: Identifier
    citation: str = Field(min_length=3, description="arXiv ID, DOI, or repository path")
    title: str
    first_author: str
    year: int = Field(ge=1900, le=2100)
    venue: str | None = None
    url: str | None = None
    verified: bool
    verification_method: str | None = None
    claim: str = Field(min_length=5)
    claim_type: ClaimType
    relation: Literal["supports", "conflicts", "context"]

    @model_validator(mode="after")
    def _verified_has_method(self) -> "LiteratureEvidence":
        if self.verified and not self.verification_method:
            raise ValueError("verified evidence must state its verification_method")
        return self


class HypothesisStatus(str, Enum):
    proposed = "proposed"
    accepted = "accepted"
    deferred = "deferred"
    rejected = "rejected"
    supported = "supported"
    unsupported = "unsupported"
    inconclusive = "inconclusive"


class Hypothesis(ArtosModel):
    hypothesis_id: Identifier
    statement: str = Field(min_length=10)
    mechanism: str = Field(min_length=5)
    expected_benefit: str
    implementation_cost: str
    falsification: str = Field(min_length=10, description="The result that would count against it.")
    supporting_evidence: list[str] = Field(default_factory=list, description="evidence_id references")
    conflicting_evidence: list[str] = Field(default_factory=list)
    role: Literal["candidate", "null", "control", "alternative"] = "candidate"
    status: HypothesisStatus = HypothesisStatus.proposed
    # Proposer-private fields: stripped from blind ranking packets.
    proposer_rank: int | None = None
    proposer_notes: str | None = None


class HypothesisSet(VersionedArtifact):
    kind: Literal["hypothesis_set"] = "hypothesis_set"
    run_id: Identifier
    mother_question_sha256: Sha256
    proposer: str
    hypotheses: list[Hypothesis] = Field(min_length=2, description="Competing hypotheses; at least two.")
    evidence: list[LiteratureEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "HypothesisSet":
        ids = [h.hypothesis_id for h in self.hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate hypothesis_id")
        known = {e.evidence_id for e in self.evidence}
        for h in self.hypotheses:
            missing = (set(h.supporting_evidence) | set(h.conflicting_evidence)) - known
            if missing:
                raise ValueError(f"{h.hypothesis_id} cites unknown evidence {sorted(missing)}")
        return self


PROPOSER_PRIVATE_FIELDS = frozenset({"proposer_rank", "proposer_notes", "status"})


class BlindCard(ArtosModel):
    card_id: Identifier
    statement: str
    mechanism: str
    expected_benefit: str
    implementation_cost: str
    falsification: str
    evidence: list[LiteratureEvidence]


class RankingPacket(VersionedArtifact):
    """What a ranking agent sees: shuffled cards, no proposer ordering or scores."""

    kind: Literal["ranking_packet"] = "ranking_packet"
    run_id: Identifier
    mother_question: str
    criteria: list[str]
    cards: list[BlindCard]
    seed: int
    hypothesis_set_sha256: Sha256


class CardScore(ArtosModel):
    card_id: Identifier
    scores: dict[str, float]
    verdict: Literal["accept", "defer", "reject"]
    rationale: str = Field(min_length=10)

    @field_validator("scores")
    @classmethod
    def _bounded(cls, value: dict[str, float]) -> dict[str, float]:
        for key, score in value.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"score {key}={score} outside [0, 1]")
        return value


class JudgeScores(VersionedArtifact):
    kind: Literal["judge_scores"] = "judge_scores"
    run_id: Identifier
    judge_id: str
    judge_context: str = Field(description="How this judge was isolated from the proposer")
    saw_proposer_ranking: bool = False
    packet_sha256: Sha256
    cards: list[CardScore]

    @model_validator(mode="after")
    def _blind(self) -> "JudgeScores":
        if self.saw_proposer_ranking:
            raise ValueError("a judge that saw the proposer's ranking cannot score this packet")
        return self


class RankedHypothesis(ArtosModel):
    hypothesis_id: Identifier
    card_id: Identifier
    mean_score: float
    criterion_means: dict[str, float]
    verdict: Literal["accept", "defer", "reject"]
    judges: int


class RankingResult(VersionedArtifact):
    kind: Literal["ranking_result"] = "ranking_result"
    run_id: Identifier
    packet_sha256: Sha256
    judge_ids: list[str]
    ranked: list[RankedHypothesis]
    method: str = "mean of criterion scores across blind judges; ties broken by card_id"


# ---------------------------------------------------------------------------
# Analysis contract
# ---------------------------------------------------------------------------
class DatasetSpec(ArtosModel):
    model_config = ConfigDict(extra="allow")

    name: str
    role: str
    version: str | None = None
    path: str | None = None
    sha256: str | None = None
    fingerprint_method: str | None = None
    variables: list[str] = Field(default_factory=list)
    region: str | None = None
    period: str | None = None


class SplitSpec(ArtosModel):
    name: Literal["train", "validation", "test"]
    start: str
    end: str
    source: str
    purpose: str


class MetricSpec(ArtosModel):
    model_config = ConfigDict(extra="allow")

    name: str
    definition: str
    direction: Literal["lower", "higher", "abs_lower", "toward_one", "report"] = "report"
    group: Literal["primary", "guardrail", "secondary", "cost", "other"] = "other"


class ControlSpec(ArtosModel):
    model_config = ConfigDict(extra="allow")

    name: str
    definition: str
    isolates: str | None = None


class VariantSpec(ArtosModel):
    variant_id: Identifier
    description: str
    role: Literal["baseline", "control", "candidate", "ablation"]
    overrides: dict[str, Any] = Field(default_factory=dict)


class AcceptanceRule(ArtosModel):
    """One pre-registered numeric comparison, evaluated per seed then aggregated.

    ``rel_improve``: (|ref| - |cand|) / |ref| >= threshold  (lower-is-better, signed errors by magnitude)
    ``rel_degrade``: (|cand| - |ref|) / |ref| <= threshold  (guardrail on a lower-is-better metric)
    ``abs_degrade``: ref - cand <= threshold                 (guardrail on a higher-is-better metric)
    ``toward_one_improve``: |ref - 1| - |cand - 1| >= threshold (std ratios)
    """

    rule_id: Identifier
    hypothesis_id: Identifier
    group: Literal["primary", "guardrail"]
    candidate: Identifier
    reference: Identifier
    variable: str
    metric: str
    mode: Literal["rel_improve", "rel_degrade", "abs_degrade", "toward_one_improve"]
    threshold: float


class DecisionPolicy(ArtosModel):
    """How per-seed rule outcomes become a hypothesis verdict."""

    min_seed_agreement: float = Field(default=1.0, gt=0, le=1.0,
                                      description="Fraction of seeds whose paired outcome must pass")
    supported: str = "every primary and guardrail rule passes on the seed mean and on >= min_seed_agreement of seeds"
    unsupported: str = "a primary rule's seed-mean effect is in the wrong direction (no improvement) on most seeds"
    inconclusive: str = "anything else, including missing metrics or seed disagreement"


class TestAccess(ArtosModel):
    actor: str
    accessed: str
    scope: str


class ComputeBudget(ArtosModel):
    model_config = ConfigDict(extra="allow")

    cpu_hours: float = Field(default=0.0, ge=0)
    gpu_hours: float = Field(default=0.0, ge=0)
    max_jobs: int | None = Field(default=None, ge=1)
    max_attempts_per_job: int = Field(default=2, ge=1, le=10)


class AnalysisContract(VersionedArtifact):
    """Pre-registered plan. Frozen and hashed before any test data is read."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["analysis_contract"] = "analysis_contract"
    contract_id: Identifier
    mother_question_sha256: Sha256
    selected_hypothesis_ids: list[Identifier] = Field(min_length=1)
    datasets: list[DatasetSpec] = Field(min_length=1)
    metrics: list[MetricSpec] = Field(min_length=1)
    null_controls: list[ControlSpec] = Field(min_length=1)
    stopping_rule: str
    test_access_history: list[TestAccess] = Field(default_factory=list)
    compute_budget: ComputeBudget = Field(default_factory=ComputeBudget)
    # v2 additions (optional so v1 contracts still load)
    splits: list[SplitSpec] = Field(default_factory=list)
    variants: list[VariantSpec] = Field(default_factory=list)
    seeds: list[int] = Field(default_factory=list)
    leakage_controls: list[str] = Field(default_factory=list)
    decision_rules: dict[str, str] = Field(default_factory=dict)
    acceptance_rules: list[AcceptanceRule] = Field(default_factory=list)
    decision_policy: DecisionPolicy = Field(default_factory=DecisionPolicy)
    code_refs: dict[str, str] = Field(default_factory=dict, description="repo -> commit")
    interpretation_limits: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _variants_unique(self) -> "AnalysisContract":
        ids = [v.variant_id for v in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate variant_id in contract")
        known = set(ids)
        for rule in self.acceptance_rules:
            if known and not {rule.candidate, rule.reference} <= known:
                raise ValueError(f"rule {rule.rule_id} references undeclared variants")
            if rule.hypothesis_id not in self.selected_hypothesis_ids:
                raise ValueError(f"rule {rule.rule_id} tests unselected hypothesis {rule.hypothesis_id}")
        return self

    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class FrozenContract(VersionedArtifact):
    kind: Literal["frozen_contract"] = "frozen_contract"
    run_id: Identifier
    contract_sha256: Sha256
    frozen_at: datetime
    frozen_path: str
    contract: AnalysisContract

    @model_validator(mode="after")
    def _hash_matches(self) -> "FrozenContract":
        if self.contract.content_sha256() != self.contract_sha256:
            raise ValueError("frozen contract hash does not match its content")
        return self


# ---------------------------------------------------------------------------
# Experiments and execution
# ---------------------------------------------------------------------------
class ExperimentSpec(VersionedArtifact):
    """One job ARTOS may submit. Its idempotency key is derived, not chosen."""

    kind: Literal["experiment_spec"] = "experiment_spec"
    run_id: Identifier
    contract_sha256: Sha256
    variant_id: Identifier
    seed: int
    executor: Literal["local", "mcp", "mock"]
    command: list[str] = Field(default_factory=list)
    tool: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    working_dir: str | None = None
    output_dir: str
    code_refs: dict[str, str] = Field(default_factory=dict)
    requires_gpu: bool = False
    estimated_cpu_hours: float = Field(default=0.0, ge=0)
    estimated_gpu_hours: float = Field(default=0.0, ge=0)

    def idempotency_key(self) -> str:
        payload = self.model_dump(mode="json", exclude={"schema_version"})
        return "exp-" + canonical_sha256(payload)[:24]


class JobState(str, Enum):
    planned = "planned"
    submitting = "submitting"
    submitted = "submitted"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    unknown_after_crash = "unknown_after_crash"


TERMINAL_JOB_STATES = {JobState.succeeded, JobState.failed, JobState.cancelled}


class ExecutionRecord(VersionedArtifact):
    kind: Literal["execution_record"] = "execution_record"
    run_id: Identifier
    idempotency_key: str
    variant_id: Identifier
    seed: int
    executor: str
    external_job_id: str | None = None
    state: JobState
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    submitted_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    code_refs: dict[str, str] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    data_versions: dict[str, Any] = Field(default_factory=dict)
    output_dir: str
    outputs: dict[str, str] = Field(default_factory=dict, description="name -> sha256")
    runtime_seconds: float | None = None
    peak_memory_bytes: int | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Evaluation and audit
# ---------------------------------------------------------------------------
class MetricValue(ArtosModel):
    variant_id: Identifier
    seed: int | None
    variable: str
    metric: str
    value: float | None
    split: Literal["validation", "test"] = "test"


class EvaluationResult(VersionedArtifact):
    kind: Literal["evaluation_result"] = "evaluation_result"
    run_id: Identifier
    contract_sha256: Sha256
    computed_by: str = Field(description="Deterministic code path that produced these numbers")
    computed_at: datetime
    values: list[MetricValue]
    decisions: dict[str, Any] = Field(default_factory=dict)
    verdicts: dict[str, Literal["supported", "unsupported", "inconclusive"]] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)


class Severity(str, Enum):
    critical = "critical"
    major = "major"
    minor = "minor"
    informational = "informational"


class FindingStatus(str, Enum):
    open = "open"
    repaired = "repaired"
    accepted = "accepted"
    not_reproducible = "not_reproducible"


BLOCKING_SEVERITIES = {Severity.critical, Severity.major}


class AuditFinding(ArtosModel):
    finding_id: Identifier
    severity: Severity
    description: str
    evidence: list[str] = Field(default_factory=list)
    status: FindingStatus
    resolution: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES and self.status in {
            FindingStatus.open,
            FindingStatus.not_reproducible,
        }


class Independence(ArtosModel):
    model_config = ConfigDict(extra="allow")

    separate_agent_session: bool = False
    separate_work_dir: bool = False
    primary_interpretation_withheld_on_first_pass: bool = False
    headline_results_independently_recomputed: list[str] = Field(default_factory=list)
    recomputation_method: str | None = None


class AuditGate(str, Enum):
    not_ready = "not_ready"
    repair_required = "repair_required"
    pass_with_disclosures = "pass_with_disclosures"
    passed = "pass"


class AuditFindings(VersionedArtifact):
    model_config = ConfigDict(extra="allow")

    kind: Literal["audit_findings"] = "audit_findings"
    run_id: Identifier
    auditor_session: str | None = None
    independence: Independence = Field(default_factory=Independence)
    checks: dict[str, Any] = Field(default_factory=dict)
    findings: list[AuditFinding] = Field(default_factory=list)
    gate: AuditGate = AuditGate.not_ready

    @property
    def blocking_findings(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def independently_reproduced(self) -> bool:
        """True only if the audit actually recomputed results by another path."""
        ind = self.independence
        return bool(ind.headline_results_independently_recomputed and ind.recomputation_method)

    @model_validator(mode="after")
    def _gate_consistent(self) -> "AuditFindings":
        if self.gate in {AuditGate.passed, AuditGate.pass_with_disclosures} and self.blocking_findings:
            ids = [f.finding_id for f in self.blocking_findings]
            raise ValueError(f"audit gate {self.gate.value!r} is inconsistent with unresolved blocking findings {ids}")
        return self


class DecisionRecord(VersionedArtifact):
    """Why an agent or the controller chose something, and on what evidence."""

    kind: Literal["decision_record"] = "decision_record"
    run_id: Identifier
    decision_id: Identifier
    actor: str
    decision: str
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    decided_at: datetime


# ---------------------------------------------------------------------------
# Loading with legacy upgrade
# ---------------------------------------------------------------------------
ARTIFACT_MODELS: dict[str, type[VersionedArtifact]] = {
    "research_question": ResearchQuestion,
    "route_decision": RouteDecision,
    "hypothesis_set": HypothesisSet,
    "ranking_packet": RankingPacket,
    "judge_scores": JudgeScores,
    "ranking_result": RankingResult,
    "analysis_contract": AnalysisContract,
    "frozen_contract": FrozenContract,
    "experiment_spec": ExperimentSpec,
    "execution_record": ExecutionRecord,
    "evaluation_result": EvaluationResult,
    "audit_findings": AuditFindings,
    "decision_record": DecisionRecord,
}


class ArtifactError(ValueError):
    """Raised when an artifact fails validation; message names the file."""


def upgrade_legacy(kind: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Upgrade v0.1/v1 JSON to the current schema. Returns (payload, notes)."""
    data = dict(payload)
    notes: list[str] = []
    version = int(data.get("schema_version", 1))
    if version >= SCHEMA_VERSION:
        return data, notes
    data["schema_version"] = SCHEMA_VERSION
    data.setdefault("kind", kind)
    notes.append(f"upgraded {kind} from schema_version {version} to {SCHEMA_VERSION}")
    if kind == "audit_findings":
        ind = dict(data.get("independence") or {})
        # v0.1 example used free-form booleans; keep them as extras and map the one
        # ARTOS gates on. A legacy boolean claim of recomputation is NOT trusted as
        # independent reproduction without a named result list and method.
        if "recomputed_via_independent_path" in ind and not ind.get("headline_results_independently_recomputed"):
            notes.append("legacy recomputed_via_independent_path flag retained but not treated as reproduction evidence")
        data["independence"] = ind
        data.pop("created_at", None)
    if kind == "analysis_contract":
        budget = data.get("compute_budget")
        if isinstance(budget, dict):
            data["compute_budget"] = budget
    return data, notes


def parse_artifact(kind: str, payload: dict[str, Any]) -> tuple[VersionedArtifact, list[str]]:
    model = ARTIFACT_MODELS[kind]
    data, notes = upgrade_legacy(kind, payload)
    try:
        return model.model_validate(data), notes
    except ValidationError as exc:
        raise ArtifactError(f"invalid {kind}: {exc}") from exc


def load_artifact(path: Path, kind: str) -> tuple[VersionedArtifact, list[str]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path}: not valid JSON ({exc})") from exc
    try:
        return parse_artifact(kind, payload)
    except ArtifactError as exc:
        raise ArtifactError(f"{path}: {exc}") from exc


def write_artifact(path: Path, artifact: BaseModel) -> str:
    """Write an artifact atomically; return the sha256 of the bytes written."""
    from .utils import atomic_write_text

    text = artifact.model_dump_json(indent=2) + "\n"
    atomic_write_text(Path(path), text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def export_json_schemas(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for kind, model in ARTIFACT_MODELS.items():
        schema = model.model_json_schema()
        schema["$id"] = f"artos://schemas/{kind.replace('_', '-')}.v{SCHEMA_VERSION}.schema.json"
        path = directory / f"{kind.replace('_', '-')}.v{SCHEMA_VERSION}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
