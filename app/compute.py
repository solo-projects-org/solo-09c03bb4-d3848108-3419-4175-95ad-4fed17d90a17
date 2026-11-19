"""Pure validation and q-value computation for the spectrum release table.

The module has no database or web dependencies so the semantics can be tested
in isolation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Iterable

# Keep more precision than any realistic FDR table needs.  The +1 formula is
# evaluated exactly for the values involved (division is the only inexact
# operation).
getcontext().prec = 50

TARGET = "target"
DECOY = "decoy"

MAX_ISSUE_DETAILS = 100


@dataclass(frozen=True)
class SpectrumKey:
    sample: str
    spectrum: str

    def display(self) -> str:
        return f"样本 {self.sample} / 谱图 {self.spectrum}"


@dataclass(frozen=True)
class Candidate:
    key: SpectrumKey
    peptide: str
    label: str
    score: Decimal
    line_no: int = 0

    @property
    def is_target(self) -> bool:
        return self.label == TARGET


@dataclass(frozen=True)
class ResultRow:
    key: SpectrumKey
    peptide: str
    label: str
    score: Decimal
    q_value: Decimal
    accepted: bool
    rank: int
    score_group: int

    @property
    def is_target(self) -> bool:
        return self.label == TARGET


@dataclass
class ReleaseComputation:
    rows: list[ResultRow]
    summary: dict[str, object] = field(default_factory=dict)


def decimal_text(value: Decimal) -> str:
    """Canonical database/CSV representation without exponent notation."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def canonical_threshold(value: Decimal) -> str:
    return decimal_text(value)


def display_q(value: Decimal | str) -> str:
    q = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(q.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def _issue(
    issue_type: str,
    title: str,
    details: list[str],
    count: int | None = None,
) -> dict[str, object]:
    truncated = len(details) > MAX_ISSUE_DETAILS
    return {
        "type": issue_type,
        "title": title,
        "count": len(details) if count is None else count,
        "details": details[:MAX_ISSUE_DETAILS],
        "truncated": truncated,
    }


def validate(
    spectra: Iterable[SpectrumKey],
    candidates: Iterable[Candidate],
) -> list[dict[str, object]]:
    """Return release-blocking issues in a deterministic order.

    Checks:
      * duplicate (sample, spectrum) rows in the deliverable list
      * candidate spectra that are absent from the list
      * listed spectra with no candidate at all
      * a listed spectrum with more than one candidate at its maximum score
    """
    spectra = list(spectra)
    candidates = list(candidates)

    ordered_keys: list[SpectrumKey] = []
    seen: set[SpectrumKey] = set()
    for key in spectra:
        if key not in seen:
            seen.add(key)
            ordered_keys.append(key)

    counts = Counter(spectra)
    duplicate_details = [
        f"{key.display()} 出现 {counts[key]} 次"
        for key in ordered_keys
        if counts[key] > 1
    ]

    list_set = set(ordered_keys)
    by_key: dict[SpectrumKey, list[Candidate]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate.key, []).append(candidate)

    extra_keys = sorted(
        (key for key in by_key if key not in list_set),
        key=lambda k: (k.sample, k.spectrum),
    )
    extra_details = [
        f"{key.display()}（{len(by_key[key])} 条候选）" for key in extra_keys
    ]

    missing_keys = [
        key for key in ordered_keys if key not in by_key
    ]
    missing_details = [
        f"{key.display()} 没有任何候选" for key in missing_keys
    ]

    tie_details: list[str] = []
    for key in ordered_keys:
        group = by_key.get(key)
        if not group:
            continue
        top_score = max(candidate.score for candidate in group)
        winners = [
            candidate for candidate in group if candidate.score == top_score
        ]
        if len(winners) > 1:
            peptides = ", ".join(
                sorted({candidate.peptide for candidate in winners})
            )
            tie_details.append(
                f"{key.display()}：最高分 {decimal_text(top_score)} 并列 "
                f"{len(winners)} 个候选（{peptides}）"
            )

    issues: list[dict[str, object]] = []
    if duplicate_details:
        issues.append(
            _issue(
                "duplicate_spectrum",
                "清单重复：同一（样本号，谱图号）出现多次",
                duplicate_details,
            )
        )
    if extra_details:
        issues.append(
            _issue(
                "candidate_not_in_list",
                "候选不在清单：候选谱图未出现在待交付清单中",
                extra_details,
            )
        )
    if missing_details:
        issues.append(
            _issue(
                "missing_candidate",
                "清单谱图缺候选：清单中的谱图没有任何候选",
                missing_details,
            )
        )
    if tie_details:
        issues.append(
            _issue(
                "top_score_tie",
                "单谱最高分并列：无法唯一确定该谱图的最高分候选",
                tie_details,
            )
        )
    return issues


def compute_fdr(
    candidates: Iterable[Candidate],
    threshold: Decimal,
) -> ReleaseComputation:
    """Compute top-hit q-values over the whole experiment.

    All top hits are sorted by score descending.  Equal scores form one group;
    cumulative target/decoy counts are updated only after the whole group.
    ``FDR = (cumulative decoys + 1) / max(cumulative targets, 1)`` and a
    group's q-value is the minimum FDR over that group and all lower-scoring
    groups.
    """
    top: dict[SpectrumKey, Candidate] = {}
    for candidate in candidates:
        current = top.get(candidate.key)
        if current is None or candidate.score > current.score:
            top[candidate.key] = candidate

    by_score: dict[Decimal, list[Candidate]] = {}
    for candidate in top.values():
        by_score.setdefault(candidate.score, []).append(candidate)

    scores = sorted(by_score, reverse=True)
    cumulative_targets = 0
    cumulative_decoys = 0
    fdrs: list[Decimal] = []
    group_targets: list[int] = []
    group_decoys: list[int] = []

    for score in scores:
        group = by_score[score]
        targets = sum(1 for candidate in group if candidate.is_target)
        decoys = len(group) - targets
        cumulative_targets += targets
        cumulative_decoys += decoys
        fdr = (Decimal(cumulative_decoys + 1)) / Decimal(
            max(cumulative_targets, 1)
        )
        fdrs.append(fdr)
        group_targets.append(targets)
        group_decoys.append(decoys)

    q_values: list[Decimal] = [Decimal(0)] * len(scores)
    running_min: Decimal | None = None
    for index in range(len(scores) - 1, -1, -1):
        if running_min is None or fdrs[index] < running_min:
            running_min = fdrs[index]
        q_values[index] = running_min

    rows: list[ResultRow] = []
    rank = 0
    for group_index, score in enumerate(scores):
        group = sorted(
            by_score[score],
            key=lambda c: (c.key.sample, c.key.spectrum, c.peptide),
        )
        for candidate in group:
            rank += 1
            rows.append(
                ResultRow(
                    key=candidate.key,
                    peptide=candidate.peptide,
                    label=candidate.label,
                    score=candidate.score,
                    q_value=q_values[group_index],
                    accepted=(
                        candidate.is_target
                        and q_values[group_index] <= threshold
                    ),
                    rank=rank,
                    score_group=group_index + 1,
                )
            )

    target_count = sum(1 for row in rows if row.is_target)
    decoy_count = len(rows) - target_count
    accepted_count = sum(1 for row in rows if row.accepted)
    summary: dict[str, object] = {
        "threshold": canonical_threshold(threshold),
        "top_hit_count": len(rows),
        "target_top_count": target_count,
        "decoy_top_count": decoy_count,
        "accepted_target_count": accepted_count,
        "score_group_count": len(scores),
    }
    return ReleaseComputation(rows=rows, summary=summary)
