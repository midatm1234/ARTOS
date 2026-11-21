from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_NAME = "artos.json"


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProjectSection(_Section):
    name: str = "ARTOS Research Orchestrator"
    state_dir: str = "state"
    projects_dir: str = "projects"


class InventorySection(_Section):
    compute_ttl_hours: float = Field(default=72, gt=0)
    data_incremental_ttl_hours: float = Field(default=72, gt=0)
    data_full_ttl_hours: float = Field(default=168, gt=0)
    max_scan_files: int = Field(default=250000, ge=1)
    hash_file_limit_mb: int = Field(default=64, ge=0)
    data_roots: list[str] = Field(default_factory=list)
    artifact_roots: list[str] = Field(default_factory=list)


class AgentsSection(_Section):
    provider: str = "claude"
    ranker_model: str = "opus"
    primary_model: str = "opus"
    adversary_model: str = "opus"
    allow_model_fallback: bool = False
    permission_mode: str = "default"
    monitor_interval_seconds: int = Field(default=60, ge=1)
    max_budget_usd: float = Field(default=0.0, ge=0)
    require_budget_limit: bool = False


class ResearchSection(_Section):
    default_hypothesis_count: int = Field(default=10, ge=2)
    minimum_hypothesis_judges: int = Field(default=4, ge=1)
    minimum_codex_judges: int = Field(default=2, ge=0)
    require_local_novelty_judge: bool = True
    maximum_repair_rounds: int = Field(default=2, ge=0)
    require_independent_ranking: bool = True
    require_adversarial_audit: bool = True


class SafetySection(_Section):
    max_download_gb_without_approval: float = Field(default=1.0, ge=0)
    max_gpu_hours_without_approval: float = Field(default=2.0, ge=0)
    allow_external_uploads: bool = False
    allow_outgoing_email: bool = False
    allow_destructive_actions: bool = False


class MCPSection(_Section):
    """An MCP server ARTOS submits jobs to (``executor: mcp``)."""

    command: list[str] = Field(min_length=1, description="argv that starts the MCP server on stdio")
    tools: dict[str, str] = Field(default_factory=dict, description="override status/cancel/lookup/request_id_arg names")
    states: dict[str, str] = Field(default_factory=dict, description="map server job states to ARTOS states")
    timeout_seconds: float = Field(default=120, gt=0)


class ExecutionSection(_Section):
    python: str | None = None
    job_timeout_seconds: float | None = Field(default=None, gt=0)
    poll_seconds: float = Field(default=10, gt=0)
    min_free_gpu_mib: int = Field(default=16384, ge=0)
    max_concurrent_jobs: int | None = Field(default=None, ge=1)
    mcp: "MCPSection | None" = None


class ArtosConfig(BaseModel):
    """Validated ``artos.json``. Unknown top-level sections are rejected."""

    model_config = ConfigDict(extra="forbid")

    project: ProjectSection = Field(default_factory=ProjectSection)
    inventory: InventorySection = Field(default_factory=InventorySection)
    agents: AgentsSection = Field(default_factory=AgentsSection)
    research: ResearchSection = Field(default_factory=ResearchSection)
    safety: SafetySection = Field(default_factory=SafetySection)
    execution: ExecutionSection = Field(default_factory=ExecutionSection)
    approvals: dict[str, dict[str, Any]] = Field(default_factory=dict, description="Per-run explicit user approvals")


def find_root(start: Path | None = None) -> Path:
    configured = os.environ.get("ARTOS_PROJECT_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
        if not (root / CONFIG_NAME).is_file():
            raise FileNotFoundError(f"ARTOS_PROJECT_ROOT has no {CONFIG_NAME}: {root}")
        return root

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise FileNotFoundError(f"Could not find {CONFIG_NAME} from {current}")


def load_config(root: Path) -> dict[str, Any]:
    """Load and validate ``artos.json``; returns a plain dict (with defaults filled)."""
    with (root / CONFIG_NAME).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    try:
        return ArtosConfig.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(f"{root / CONFIG_NAME} is invalid: {exc}") from exc


def configured_path(root: Path, config: dict[str, Any], section: str, key: str) -> Path:
    raw = config[section][key]
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def state_dir(root: Path, config: dict[str, Any]) -> Path:
    return configured_path(root, config, "project", "state_dir")


def projects_dir(root: Path, config: dict[str, Any]) -> Path:
    return configured_path(root, config, "project", "projects_dir")
