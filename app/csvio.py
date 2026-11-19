"""Strict CSV parsing for the two source files.

The header names are matched against a small set of English and Chinese
aliases and every row is validated before anything is written to SQLite.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .compute import Candidate, DECOY, SpectrumKey, TARGET


class ParseError(ValueError):
    """Raised when a CSV cannot be used as a source of truth."""


@dataclass(frozen=True)
class ParsedSource:
    kind: str
    content_sha256: str
    filename: str
    row_count: int
    spectra: tuple[SpectrumKey, ...]
    candidates: tuple[Candidate, ...]


MANIFEST_ALIASES = {
    "sample": {"sample", "sample_id", "样本号", "样本", "样本编号"},
    "spectrum": {"spectrum", "spectrum_id", "spectrum_no", "谱图号", "谱图", "谱图编号"},
}

CANDIDATE_ALIASES = {
    "sample": MANIFEST_ALIASES["sample"],
    "spectrum": MANIFEST_ALIASES["spectrum"],
    "peptide": {"peptide", "peptide_sequence", "sequence", "肽段", "肽段序列", "序列"},
    "label": {"label", "class", "target_decoy", "type", "靶标/诱饵", "靶标诱饵", "类型", "标记"},
    "score": {"score", "confidence", "分数", "得分"},
}

TARGET_ALIASES = {"target", "targets", "t", "靶标", "靶", "target_seq"}
DECOY_ALIASES = {"decoy", "decoys", "d", "诱饵", "诱", "decoy_seq"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError as exc:  # pragma: no cover - unusual input
            raise ParseError(f"文件编码无法识别：{exc}") from exc


def _normalize_header(name: str) -> str:
    return name.replace("﻿", "").strip().lower().replace(" ", "")


def _map_header(
    header: list[str],
    aliases: dict[str, set[str]],
    required: tuple[str, ...],
    filename: str,
) -> dict[str, int]:
    mapping: dict[str, int] = {}
    normalized = [_normalize_header(cell) for cell in header]
    for field, names in aliases.items():
        for index, cell in enumerate(normalized):
            if cell in names:
                mapping[field] = index
                break
    missing = [field for field in required if field not in mapping]
    if missing:
        raise ParseError(
            f"{filename}：缺少必需的列 {missing}（实际表头：{header}）"
        )
    return mapping


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return row[index].strip()


def parse_manifest(data: bytes, filename: str = "manifest.csv") -> ParsedSource:
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError(f"{filename}：文件为空")
    mapping = _map_header(
        header, MANIFEST_ALIASES, ("sample", "spectrum"), filename
    )

    spectra: list[SpectrumKey] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        sample = _cell(row, mapping["sample"])
        spectrum = _cell(row, mapping["spectrum"])
        if not sample or not spectrum:
            raise ParseError(
                f"{filename} 第 {line_no} 行：样本号或谱图号为空"
            )
        spectra.append(SpectrumKey(sample, spectrum))
    if not spectra:
        raise ParseError(f"{filename}：清单没有任何数据行")
    return ParsedSource(
        kind="manifest",
        content_sha256=sha256_hex(data),
        filename=filename,
        row_count=len(spectra),
        spectra=tuple(spectra),
        candidates=(),
    )


def normalise_label(value: str, filename: str, line_no: int) -> str:
    label = value.strip().lower().replace(" ", "")
    if label in TARGET_ALIASES:
        return TARGET
    if label in DECOY_ALIASES:
        return DECOY
    raise ParseError(
        f"{filename} 第 {line_no} 行：靶标/诱饵只能为 target/decoy 或 靶标/诱饵，"
        f"实际为 {value!r}"
    )


def parse_candidates(data: bytes, filename: str = "candidates.csv") -> ParsedSource:
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError(f"{filename}：文件为空")
    mapping = _map_header(
        header,
        CANDIDATE_ALIASES,
        ("sample", "spectrum", "peptide", "label", "score"),
        filename,
    )

    candidates: list[Candidate] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        sample = _cell(row, mapping["sample"])
        spectrum = _cell(row, mapping["spectrum"])
        peptide = _cell(row, mapping["peptide"])
        label_text = _cell(row, mapping["label"])
        score_text = _cell(row, mapping["score"])
        if not sample or not spectrum:
            raise ParseError(
                f"{filename} 第 {line_no} 行：样本号或谱图号为空"
            )
        if not peptide:
            raise ParseError(f"{filename} 第 {line_no} 行：肽段为空")
        label = normalise_label(label_text, filename, line_no)
        try:
            score = Decimal(score_text)
        except (InvalidOperation, ValueError):
            raise ParseError(
                f"{filename} 第 {line_no} 行：分数不是有效数字（{score_text!r}）"
            )
        if not score.is_finite():
            raise ParseError(
                f"{filename} 第 {line_no} 行：分数必须是有限数字（{score_text!r}）"
            )
        candidates.append(
            Candidate(
                key=SpectrumKey(sample, spectrum),
                peptide=peptide,
                label=label,
                score=score,
                line_no=line_no,
            )
        )
    if not candidates:
        raise ParseError(f"{filename}：候选文件没有任何数据行")
    return ParsedSource(
        kind="candidates",
        content_sha256=sha256_hex(data),
        filename=filename,
        row_count=len(candidates),
        spectra=(),
        candidates=tuple(candidates),
    )


def parse_source(kind: str, data: bytes, filename: str) -> ParsedSource:
    if kind == "manifest":
        return parse_manifest(data, filename)
    if kind == "candidates":
        return parse_candidates(data, filename)
    raise ParseError(f"未知来源类型：{kind}")
