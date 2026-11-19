from decimal import Decimal

from app.compute import (
    Candidate,
    SpectrumKey,
    compute_fdr,
    validate,
)


def cand(sample: str, spectrum: str, score: str, label: str, peptide: str = "P"):
    return Candidate(
        key=SpectrumKey(sample, spectrum),
        peptide=peptide,
        label=label,
        score=Decimal(score),
    )


def test_duplicate_manifest_detected():
    spectra = [
        SpectrumKey("S1", "1"),
        SpectrumKey("S1", "2"),
        SpectrumKey("S1", "1"),
    ]
    issues = validate(spectra, [])
    types = [issue["type"] for issue in issues]
    assert "duplicate_spectrum" in types
    duplicate = next(i for i in issues if i["type"] == "duplicate_spectrum")
    assert duplicate["count"] == 1
    assert "S1" in duplicate["details"][0]


def test_candidate_not_in_manifest_detected():
    spectra = [SpectrumKey("S1", "1")]
    candidates = [
        cand("S1", "1", "10", "target"),
        cand("S2", "9", "9", "decoy"),
    ]
    issues = validate(spectra, candidates)
    extra = next(i for i in issues if i["type"] == "candidate_not_in_list")
    assert extra["count"] == 1
    assert "S2" in extra["details"][0]


def test_missing_candidates_detected():
    spectra = [SpectrumKey("S1", "1"), SpectrumKey("S1", "2")]
    candidates = [cand("S1", "1", "10", "target")]
    issues = validate(spectra, candidates)
    missing = next(i for i in issues if i["type"] == "missing_candidate")
    assert missing["count"] == 1
    assert "S1" in missing["details"][0] and "2" in missing["details"][0]


def test_top_score_tie_detected_even_with_other_candidates():
    spectra = [SpectrumKey("S1", "1")]
    candidates = [
        cand("S1", "1", "10", "target", peptide="A"),
        cand("S1", "1", "10", "decoy", peptide="B"),
        cand("S1", "1", "9", "target", peptide="C"),
    ]
    issues = validate(spectra, candidates)
    tie = next(i for i in issues if i["type"] == "top_score_tie")
    assert tie["count"] == 1
    assert "10" in tie["details"][0]


def test_all_issue_kinds_are_reported_together():
    spectra = [SpectrumKey("S1", "1"), SpectrumKey("S1", "1"), SpectrumKey("S1", "2")]
    candidates = [
        cand("S1", "1", "10", "target"),
        cand("S1", "1", "10", "decoy"),
        cand("S9", "9", "3", "target"),
    ]
    issues = validate(spectra, candidates)
    assert [i["type"] for i in issues] == [
        "duplicate_spectrum",
        "candidate_not_in_list",
        "missing_candidate",
        "top_score_tie",
    ]


def test_q_values_group_ties_and_plus_one():
    # score 100: one target and one decoy tied -> cumulative 1T/1D
    # score 90: one target -> 2T/1D
    # score 80: one target -> 3T/1D
    # score 70: one target -> 4T/1D
    candidates = [
        cand("S1", "1", "100", "target"),
        cand("S1", "2", "100", "decoy"),
        cand("S1", "3", "90", "target"),
        cand("S1", "4", "80", "target"),
        cand("S1", "5", "70", "target"),
    ]
    result = compute_fdr(candidates, Decimal("0.01"))
    # FDRs per group: 2/1, 2/2, 2/3, 2/4; q is the running minimum, 2/4.
    qs = [row.q_value for row in result.rows]
    assert qs == [Decimal(1) / 2] * 5
    assert all(not row.accepted for row in result.rows)
    assert result.rows[1].score_group == 1
    assert result.rows[2].score_group == 2


def test_tied_group_counts_all_labels_before_fdr():
    # Two decoys and one target tie at the top.  Only a group-level count can
    # give FDR = (2 + 1) / 1 = 3; a row-by-row ordering would produce
    # different values for the tied rows.
    candidates = [
        cand("S1", "1", "100", "decoy", peptide="A"),
        cand("S1", "2", "100", "decoy", peptide="B"),
        cand("S1", "3", "100", "target", peptide="C"),
        cand("S1", "4", "90", "target", peptide="D"),
    ]
    result = compute_fdr(candidates, Decimal("0.99"))
    tied = [row for row in result.rows if row.score == Decimal("100")]
    assert {row.score_group for row in tied} == {1}
    # group 1 FDR = 3/1 = 3, group 2 FDR = 3/2 = 1.5 -> q = 1.5
    assert {row.q_value for row in result.rows} == {Decimal(3) / 2}
    assert not any(row.accepted for row in result.rows)


def test_q_values_accept_targets_below_threshold():
    candidates = [cand("S1", "0", "100", "decoy")]
    candidates += [
        cand("S1", str(i), str(90 - i), "target") for i in range(1, 11)
    ]
    result = compute_fdr(candidates, Decimal("0.2"))
    # FDRs 2/1 ... 2/10; q = 2/10 = 0.2 for every row.
    assert all(row.q_value == Decimal("0.2") for row in result.rows)
    accepted = [row.key.spectrum for row in result.rows if row.accepted]
    assert "0" not in accepted
    assert len(accepted) == 10
    tighter = compute_fdr(candidates, Decimal("0.19"))
    assert not any(row.accepted for row in tighter.rows)


def test_q_value_uses_minimum_of_lower_groups():
    # A decoy at the very top is rescued by lower-scoring target-only groups.
    candidates = [
        cand("S", "1", "100", "decoy"),
        cand("S", "2", "99", "target"),
        cand("S", "3", "98", "target"),
        cand("S", "4", "97", "target"),
    ]
    result = compute_fdr(candidates, Decimal("0.01"))
    expected = Decimal(2) / Decimal(3)
    assert all(row.q_value == expected for row in result.rows)
    # q must never decrease as score decreases.
    assert result.rows[0].q_value >= result.rows[-1].q_value


def test_only_top_hit_per_spectrum_is_used():
    candidates = [
        cand("S", "1", "100", "target", peptide="A"),
        cand("S", "1", "50", "decoy", peptide="B"),
    ]
    result = compute_fdr(candidates, Decimal("0.01"))
    assert len(result.rows) == 1
    assert result.rows[0].peptide == "A"
    assert result.summary["decoy_top_count"] == 0


def test_threshold_accepts_target_only():
    candidates = [cand("S", "1", "100", "decoy")]
    candidates += [
        cand("S", str(i), str(100 - i), "target") for i in range(2, 103)
    ]
    result = compute_fdr(candidates, Decimal("0.02"))
    decoy_row = next(row for row in result.rows if row.label == "decoy")
    assert not decoy_row.accepted
    assert decoy_row.q_value == Decimal(2) / Decimal(101)
    assert decoy_row.q_value > Decimal("0.0198")
    assert sum(1 for row in result.rows if row.accepted) == 101
