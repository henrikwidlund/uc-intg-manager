"""Tests for version comparison helpers."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from github_api import GitHubClient, normalize_version  # noqa: E402


# ---------------------------------------------------------------------------
# normalize_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.2.3", "1.2.3"),
        ("V1.2.3", "1.2.3"),
    ],
)
def test_strips_v_prefix(tag, expected):
    assert normalize_version(tag) == expected


def test_plain_release_unchanged():
    assert normalize_version("1.2.3") == "1.2.3"


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v0.0.1-pre01", "0.0.1rc1"),
        ("0.0.1-pre", "0.0.1rc0"),
    ],
)
def test_pre_suffix_becomes_rc(tag, expected):
    assert normalize_version(tag) == expected


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("1.2.3-alpha.1", "1.2.3a1"),
        ("1.2.3-beta2", "1.2.3b2"),
        ("1.2.3-rc4", "1.2.3rc4"),
        ("1.2.3-dev3", "1.2.3.dev3"),
        ("1.2.3-preview1", "1.2.3rc1"),
        ("1.2.3-preview.1", "1.2.3rc1"),
    ],
)
def test_alpha_beta_rc_dev(tag, expected):
    assert normalize_version(tag) == expected


def test_empty_input():
    assert normalize_version("") == ""


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.2.3+build.5", "1.2.3"),
        ("v1.2.3-pre01+sha.abc", "1.2.3rc1"),
    ],
)
def test_strips_build_metadata(tag, expected):
    assert normalize_version(tag) == expected


@pytest.mark.parametrize(
    "tag, expected",
    [
        # Tags like ``1.2.3rc1`` (no separator) must still normalize.
        ("1.2.3rc1", "1.2.3rc1"),
        ("1.2.3a1", "1.2.3a1"),
    ],
)
def test_dashless_suffix(tag, expected):
    assert normalize_version(tag) == expected


@pytest.mark.parametrize(
    "tag, expected",
    [
        # The bare ``a|b`` alternation used to silently turn ``-build`` into
        # ``b0``. The tightened regex leaves unknown suffixes alone.
        ("1.0.0-build", "1.0.0-build"),
        ("banana", "banana"),
    ],
)
def test_does_not_rewrite_non_prerelease_tokens(tag, expected):
    assert normalize_version(tag) == expected


# ---------------------------------------------------------------------------
# GitHubClient.is_newer_version
# ---------------------------------------------------------------------------


def test_release_outranks_prerelease():
    # Reported bug: installing v0.0.1 over v0.0.1-pre01 must be an upgrade.
    assert GitHubClient.is_newer_version("v0.0.1-pre01", "v0.0.1") is True
    assert GitHubClient.is_newer_version("v0.0.1", "v0.0.1-pre01") is False


@pytest.mark.parametrize(
    "current, candidate, expected",
    [
        ("v0.0.1", "v0.0.2", True),
        ("v0.0.2", "v0.0.1", False),
    ],
)
def test_basic_release_progression(current, candidate, expected):
    assert GitHubClient.is_newer_version(current, candidate) is expected


def test_equal_versions():
    assert GitHubClient.is_newer_version("v1.0.0", "v1.0.0") is False


@pytest.mark.parametrize(
    "current, candidate, expected",
    [
        ("v0.0.1-pre01", "v0.0.1-pre02", True),
        ("v0.0.1-pre02", "v0.0.1-pre01", False),
    ],
)
def test_prerelease_progression(current, candidate, expected):
    assert GitHubClient.is_newer_version(current, candidate) is expected


def test_numeric_segment_compare_not_lex():
    # Lexicographic compare would say "0.10.0" < "0.9.0".
    assert GitHubClient.is_newer_version("v0.9.0", "v0.10.0") is True
    assert GitHubClient.is_newer_version("v0.10.0", "v0.9.0") is False


def test_beta_below_release():
    assert GitHubClient.is_newer_version("v1.2.3-beta.1", "v1.2.3") is True
    assert GitHubClient.is_newer_version("v1.2.3", "v1.2.3-rc1") is False


def test_invalid_version_returns_false():
    assert GitHubClient.is_newer_version("garbage", "more-garbage") is False


@pytest.mark.parametrize(
    "current, candidate, expected",
    [
        # Tags like "1.0.0-build" are not PEP 440 — fall back to numeric core.
        ("1.0.0-build", "1.0.1-build", True),
        ("1.0.1-build", "1.0.0-build", False),
        ("1.0.0-build", "1.0.0-build", False),
    ],
)
def test_non_pep440_tags_use_numeric_core_fallback(current, candidate, expected):
    assert GitHubClient.is_newer_version(current, candidate) is expected


@pytest.mark.parametrize(
    "current, candidate",
    [
        # SemVer: build metadata after "+" must not change ordering.
        ("v1.0.0", "v1.0.0+build.1"),
        ("v1.0.0+build.1", "v1.0.0"),
    ],
)
def test_build_metadata_does_not_affect_precedence(current, candidate):
    assert GitHubClient.is_newer_version(current, candidate) is False
