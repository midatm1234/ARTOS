"""Competing hypotheses, blind ranking packets, and deterministic ranking.

The proposer writes ``questions/hypotheses.json`` (a :class:`HypothesisSet`).
:func:`create_blind_ranking_packet` strips proposer-private fields, relabels
hypotheses with opaque card IDs, and shuffles them with a recorded seed. The
card -> hypothesis key is stored separately (``ranking_key.json``) and is never
given to a judge. Judges return :class:`JudgeScores`; ranking is arithmetic.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from statistics import mean
from typing import Any

from .models import (
    BlindCard,
    Hypothesis,
    HypothesisSet,
    JudgeScores,
    LiteratureEvidence,
    RankedHypothesis,
    RankingPacket,
    RankingResult,
    canonical_sha256,
    load_artifact,
    write_artifact,
)
from .utils import atomic_write_json, read_json

DEFAULT_CRITERIA = ["scientific_value", "discriminating_power", "feasibility", "evidence_support"]


def create_hypothesis_template(run_dir: Path, count: int = 10) -> Path:
    path = Path(run_dir) / "questions" / "hypotheses.template.json"
    question = read_json(Path(run_dir) / "questions" / "research_question.json", {})
    atomic_write_json(path, {
        "schema_version": 2,
        "kind": "hypothesis_set",
        "run_id": Path(run_dir).name,
        "mother_question_sha256": question.get("mother_question_sha256", "0" * 64),
        "proposer": "primary-proposer",
        "evidence": [],
        "hypotheses": [
            {
                "hypothesis_id": f"H{i + 1}",
                "statement": "",
                "mechanism": "",
                "expected_benefit": "",
                "implementation_cost": "",
                "falsification": "",
                "supporting_evidence": [],
                "conflicting_evidence": [],
                "role": "candidate",
            }
            for i in range(max(2, count))
        ],
    })
    return path


def _card_id(seed: int, hypothesis_id: str) -> str:
    return "C" + hashlib.sha256(f"{seed}:{hypothesis_id}".encode()).hexdigest()[:8]


def create_blind_ranking_packet(hypotheses_path: Path, seed: int = 20260816, criteria: list[str] | None = None) -> Path:
    hset, _ = load_artifact(Path(hypotheses_path), "hypothesis_set")
    assert isinstance(hset, HypothesisSet)
    question = read_json(Path(hypotheses_path).parent / "research_question.json", {})
    evidence = {e.evidence_id: e for e in hset.evidence}
    order = list(hset.hypotheses)
    rng = random.Random(seed)
    for _ in range(16):
        rng.shuffle(order)
        if len(order) <= 2 or [h.hypothesis_id for h in order] != [h.hypothesis_id for h in hset.hypotheses]:
            break
    cards = []
    key = {}
    for hyp in order:
        cid = _card_id(seed, hyp.hypothesis_id)
        key[cid] = hyp.hypothesis_id
        cited = [evidence[e] for e in (*hyp.supporting_evidence, *hyp.conflicting_evidence)]
        cards.append(BlindCard(
            card_id=cid,
            statement=hyp.statement,
            mechanism=hyp.mechanism,
            expected_benefit=hyp.expected_benefit,
            implementation_cost=hyp.implementation_cost,
            falsification=hyp.falsification,
            evidence=[LiteratureEvidence.model_validate(e.model_dump()) for e in cited],
        ))
    packet = RankingPacket(
        run_id=hset.run_id,
        mother_question=question.get("mother_question", ""),
        criteria=criteria or DEFAULT_CRITERIA,
        cards=cards,
        seed=seed,
        hypothesis_set_sha256=canonical_sha256(hset.model_dump(mode="json")),
    )
    out = Path(hypotheses_path).parent / "ranking_packet.json"
    write_artifact(out, packet)
    # The key lives outside the packet and must not be shown to judges.
    atomic_write_json(Path(hypotheses_path).parent / "ranking_key.json", {"seed": seed, "card_to_hypothesis": key})
    return out


def aggregate_judge_panel(
    packet_path: Path,
    judge_paths: list[Path],
    output_path: Path,
    minimum_judges: int = 4,
    minimum_codex_judges: int = 0,
) -> Path:
    """Validate isolated judge files against the packet and combine them.

    ``minimum_codex_judges`` is retained for interface compatibility; judges
    declare their provider in ``judge_context`` and the count is checked when
    non-zero.
    """
    packet, _ = load_artifact(Path(packet_path), "ranking_packet")
    assert isinstance(packet, RankingPacket)
    packet_hash = canonical_sha256(packet.model_dump(mode="json"))
    judges: list[JudgeScores] = []
    for path in judge_paths:
        js, _ = load_artifact(Path(path), "judge_scores")
        assert isinstance(js, JudgeScores)
        if js.packet_sha256 != packet_hash:
            raise ValueError(f"{path}: judge scored a different packet")
        card_ids = {c.card_id for c in packet.cards}
        scored = {c.card_id for c in js.cards}
        if scored != card_ids:
            raise ValueError(f"{path}: judge must score every card exactly once (missing {sorted(card_ids - scored)})")
        for card in js.cards:
            missing = set(packet.criteria) - set(card.scores)
            if missing:
                raise ValueError(f"{path}: card {card.card_id} missing criteria {sorted(missing)}")
        judges.append(js)
    ids = [j.judge_id for j in judges]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate judge_id in panel")
    if len(judges) < minimum_judges:
        raise ValueError(f"{len(judges)} judges supplied; at least {minimum_judges} required")
    if minimum_codex_judges:
        codex = sum(1 for j in judges if "codex" in j.judge_context.lower())
        if codex < minimum_codex_judges:
            raise ValueError(f"{codex} codex judges supplied; at least {minimum_codex_judges} required")
    atomic_write_json(Path(output_path), {
        "packet_sha256": packet_hash,
        "judges": [j.model_dump(mode="json") for j in judges],
    })
    return Path(output_path)


def rank_scored_hypotheses(scored_path: Path, output_dir: Path) -> tuple[Path, Path]:
    panel = read_json(Path(scored_path))
    if not panel:
        raise FileNotFoundError(scored_path)
    output_dir = Path(output_dir)
    key = read_json(output_dir / "ranking_key.json")
    if not key:
        raise FileNotFoundError(output_dir / "ranking_key.json")
    judges = [JudgeScores.model_validate(j) for j in panel["judges"]]
    per_card: dict[str, list[Any]] = {}
    for judge in judges:
        for card in judge.cards:
            per_card.setdefault(card.card_id, []).append(card)
    ranked = []
    for cid, cards in per_card.items():
        criteria = sorted({k for c in cards for k in c.scores})
        crit_means = {k: round(mean(c.scores[k] for c in cards), 6) for k in criteria}
        votes = [c.verdict for c in cards]
        # Majority verdict; ties resolve to the more conservative option.
        verdict = max(("reject", "defer", "accept"), key=lambda v: (votes.count(v), {"reject": 2, "defer": 1, "accept": 0}[v]))
        ranked.append(RankedHypothesis(
            hypothesis_id=key["card_to_hypothesis"][cid],
            card_id=cid,
            mean_score=round(mean(crit_means.values()), 6),
            criterion_means=crit_means,
            verdict=verdict,
            judges=len(cards),
        ))
    ranked.sort(key=lambda r: (-r.mean_score, r.card_id))
    result = RankingResult(
        run_id=judges[0].run_id,
        packet_sha256=panel["packet_sha256"],
        judge_ids=[j.judge_id for j in judges],
        ranked=ranked,
    )
    json_path = output_dir / "ranking.json"
    write_artifact(json_path, result)
    lines = ["# Blind hypothesis ranking", "", f"Judges: {', '.join(result.judge_ids)}", "",
             "| rank | hypothesis | card | mean | verdict |", "|---|---|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        lines.append(f"| {i} | {r.hypothesis_id} | {r.card_id} | {r.mean_score:.3f} | {r.verdict} |")
    md = output_dir.parent / "05_hypothesis_tournament.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md


def strip_private(h: Hypothesis) -> dict[str, Any]:
    return h.model_dump(exclude={"proposer_rank", "proposer_notes", "status"})
