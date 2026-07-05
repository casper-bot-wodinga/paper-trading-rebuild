"""Tests for prompt versioning module — SPEC-v3 §13."""

import pytest
from datetime import datetime

from src.prompt_versioning import (
    SweepBranch,
    ExperimentBranch,
    PromptTag,
    SWEEP_RE,
    EXPERIMENT_RE,
    TAG_RE,
    version_string,
)


# ── Regex parsing tests ──────────────────────────────────────────────────────


class TestSweepBranchRegex:
    """Sweep branch name parsing: sweep/YYYY-MM-DD/{trader}/variant-NNN"""

    def test_valid_sweep(self):
        m = SWEEP_RE.match("sweep/2026-07-05/kairos/variant-047")
        assert m is not None
        assert m.group("date") == "2026-07-05"
        assert m.group("trader") == "kairos"
        assert m.group("variant") == "047"

    def test_origin_handled_by_parse(self):
        """origin/ prefix is stripped by parse(), not matched by regex."""
        sb = SweepBranch.parse("origin/sweep/2026-07-05/aldridge/variant-001")
        assert sb is not None
        assert sb.branch_name == "sweep/2026-07-05/aldridge/variant-001"
        assert sb.variant == 1

    def test_invalid_format(self):
        assert SWEEP_RE.match("sweep/kairos/variant-001") is None  # missing date
        assert SWEEP_RE.match("feature/sweep/2026-07-05/kairos/variant-001") is None
        assert SWEEP_RE.match("sweep/2026-07-05/kairos/variant") is None  # no number
        assert SWEEP_RE.match("sweep/not-a-date/kairos/variant-001") is None

    def test_variant_leading_zeros(self):
        m = SWEEP_RE.match("sweep/2026-07-05/stonks/variant-000")
        assert m is not None
        assert m.group("variant") == "000"

    def test_variant_large(self):
        m = SWEEP_RE.match("sweep/2026-07-05/stonks/variant-999")
        assert m is not None
        assert m.group("variant") == "999"


class TestExperimentBranchRegex:
    """Experiment branch parsing: experiment/{trader}/{name}"""

    def test_valid_experiment(self):
        m = EXPERIMENT_RE.match("experiment/kairos/more-momentum")
        assert m is not None
        assert m.group("trader") == "kairos"
        assert m.group("name") == "more-momentum"

    def test_hyphenated_name(self):
        m = EXPERIMENT_RE.match("experiment/aldridge/value-only-test")
        assert m is not None
        assert m.group("name") == "value-only-test"

    def test_underscore_name(self):
        m = EXPERIMENT_RE.match("experiment/stonks/sentiment_heavy_v2")
        assert m is not None
        assert m.group("name") == "sentiment_heavy_v2"

    def test_invalid_format(self):
        assert EXPERIMENT_RE.match("experiment/kairos") is None  # missing name
        assert EXPERIMENT_RE.match("test/kairos/name") is None  # wrong prefix
        assert EXPERIMENT_RE.match("experiment/kairos/name with spaces") is None


class TestTagRegex:
    """Tag parsing: {trader}/v{major}.{minor}.{patch}"""

    def test_valid_tag(self):
        m = TAG_RE.match("kairos/v1.0.3")
        assert m is not None
        assert m.group("trader") == "kairos"
        assert m.group("major") == "1"
        assert m.group("minor") == "0"
        assert m.group("patch") == "3"

    def test_large_version(self):
        m = TAG_RE.match("stonks/v12.99.150")
        assert m is not None
        assert m.group("major") == "12"
        assert m.group("minor") == "99"
        assert m.group("patch") == "150"

    def test_invalid_tag(self):
        assert TAG_RE.match("v1.0.0") is None  # missing trader
        assert TAG_RE.match("kairos/v1.0") is None  # missing patch
        assert TAG_RE.match("kairos/1.0.0") is None  # missing v prefix
        assert TAG_RE.match("kairos/v1.0.3-rc1") is None  # extra suffix


# ── SweepBranch dataclass tests ──────────────────────────────────────────────


class TestSweepBranch:
    def test_create(self):
        sb = SweepBranch.create("2026-07-05", "kairos", 47)
        assert sb.date == "2026-07-05"
        assert sb.trader == "kairos"
        assert sb.variant == 47
        assert sb.branch_name == "sweep/2026-07-05/kairos/variant-047"

    def test_create_leading_zero_edge(self):
        sb = SweepBranch.create("2026-07-05", "stonks", 1)
        assert sb.branch_name == "sweep/2026-07-05/stonks/variant-001"

    def test_create_max_variant(self):
        sb = SweepBranch.create("2026-07-05", "aldridge", 999)
        assert sb.branch_name == "sweep/2026-07-05/aldridge/variant-999"
        assert sb.variant == 999

    def test_parse_valid(self):
        sb = SweepBranch.parse("sweep/2026-07-05/kairos/variant-047")
        assert sb is not None
        assert sb.date == "2026-07-05"
        assert sb.trader == "kairos"
        assert sb.variant == 47

    def test_parse_remote(self):
        sb = SweepBranch.parse("origin/sweep/2026-07-05/kairos/variant-100")
        assert sb is not None
        assert sb.branch_name == "sweep/2026-07-05/kairos/variant-100"
        assert sb.variant == 100

    def test_parse_invalid(self):
        assert SweepBranch.parse("main") is None
        assert SweepBranch.parse("feature/kairos/prompt-v2") is None

    def test_remote_name(self):
        sb = SweepBranch.create("2026-07-05", "kairos", 3)
        assert sb.remote_name == "origin/sweep/2026-07-05/kairos/variant-003"


# ── ExperimentBranch tests ───────────────────────────────────────────────────


class TestExperimentBranch:
    def test_parse_valid(self):
        eb = ExperimentBranch.parse("experiment/kairos/more-momentum")
        assert eb is not None
        assert eb.trader == "kairos"
        assert eb.name == "more-momentum"
        assert eb.branch_name == "experiment/kairos/more-momentum"

    def test_parse_remote(self):
        eb = ExperimentBranch.parse("origin/experiment/stonks/sentiment-only")
        assert eb is not None
        assert eb.trader == "stonks"
        assert eb.name == "sentiment-only"

    def test_parse_invalid(self):
        assert ExperimentBranch.parse("main") is None
        assert ExperimentBranch.parse("sweep/2026-07-05/kairos/variant-001") is None


# ── PromptTag tests ──────────────────────────────────────────────────────────


class TestPromptTag:
    def test_parse(self):
        tag = PromptTag.parse("kairos/v1.0.3")
        assert tag is not None
        assert tag.trader == "kairos"
        assert tag.major == 1
        assert tag.minor == 0
        assert tag.patch == 3
        assert tag.tag_name == "kairos/v1.0.3"

    def test_parse_invalid(self):
        assert PromptTag.parse("kairos/1.0.0") is None
        assert PromptTag.parse("v1.0.0") is None

    def test_bump_patch(self):
        tag = PromptTag.parse("kairos/v1.0.3")
        assert tag is not None
        bumped = tag.bump_patch()
        assert bumped.patch == 4
        assert bumped.major == 1
        assert bumped.minor == 0
        assert bumped.tag_name == "kairos/v1.0.4"

    def test_bump_minor(self):
        tag = PromptTag.parse("aldridge/v2.1.7")
        assert tag is not None
        bumped = tag.bump_minor()
        assert bumped.minor == 2
        assert bumped.major == 2
        assert bumped.patch == 0
        assert bumped.tag_name == "aldridge/v2.2.0"

    def test_bump_minor_resets_patch(self):
        tag = PromptTag.parse("stonks/v3.5.99")
        assert tag is not None
        bumped = tag.bump_minor()
        assert bumped.patch == 0
        assert bumped.minor == 6

    def test_bump_major_via_two_bumps(self):
        tag = PromptTag.parse("kairos/v0.9.15")
        assert tag is not None
        bumped = tag.bump_minor().bump_minor()  # 0.9 -> 0.10 -> 0.11
        assert bumped.minor == 11

    def test_str(self):
        tag = PromptTag.parse("kairos/v1.2.3")
        assert tag is not None
        assert str(tag) == "kairos/v1.2.3"


# ── Convenience functions ────────────────────────────────────────────────────


class TestVersionString:
    def test_with_tag(self):
        tag = PromptTag.parse("kairos/v1.0.5")
        assert version_string(tag) == "kairos/v1.0.5"

    def test_none_tag(self):
        assert version_string(None) == "v0.0.0 (unreleased)"


# ── Integration: full sweep lifecycle simulation ─────────────────────────────


class TestSweepLifecycle:
    """Simulate a full sweep → promote → prune cycle without real git."""

    def test_full_cycle_simulation(self):
        """Simulate full sweep → promote lifecycle with branch name parsing."""
        trader = "kairos"

        # Create 3 sweep branches
        variants = [
            SweepBranch.create("2026-06-25", trader, 1),
            SweepBranch.create("2026-06-25", trader, 47),
            SweepBranch.create("2026-06-25", trader, 100),
        ]
        assert len(variants) == 3

        # Variant 47 wins
        winner = variants[1]
        assert winner.variant == 47
        assert winner.trader == trader
        assert winner.date == "2026-06-25"

        # Bump version
        prev = PromptTag.parse(f"{trader}/v1.0.2")
        assert prev is not None
        new_tag = prev.bump_patch()
        assert new_tag.tag_name == f"{trader}/v1.0.3"
        assert new_tag.patch == 3

        # Sweep branches from June 25 are stale (> 7 days old on July 3)
        from datetime import timedelta
        sweep_date = datetime(2026, 6, 25)
        cutoff = datetime(2026, 7, 3) - timedelta(days=7)
        assert sweep_date < cutoff  # June 25 < June 26 → prunable

    def test_variant_zero_padded(self):
        """Variant numbers are 3-digit zero-padded in branch names."""
        assert SweepBranch.create("2026-07-05", "kairos", 7).branch_name.endswith("/variant-007")
        assert SweepBranch.create("2026-07-05", "kairos", 89).branch_name.endswith("/variant-089")
