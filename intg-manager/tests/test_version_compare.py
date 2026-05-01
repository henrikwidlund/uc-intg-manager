"""Tests for version comparison helpers."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from github_api import GitHubClient, normalize_version


class NormalizeVersionTests(unittest.TestCase):
    def test_strips_v_prefix(self):
        self.assertEqual(normalize_version("v1.2.3"), "1.2.3")
        self.assertEqual(normalize_version("V1.2.3"), "1.2.3")

    def test_plain_release_unchanged(self):
        self.assertEqual(normalize_version("1.2.3"), "1.2.3")

    def test_pre_suffix_becomes_rc(self):
        self.assertEqual(normalize_version("v0.0.1-pre01"), "0.0.1rc1")
        self.assertEqual(normalize_version("0.0.1-pre"), "0.0.1rc0")

    def test_alpha_beta_rc_dev(self):
        self.assertEqual(normalize_version("1.2.3-alpha.1"), "1.2.3a1")
        self.assertEqual(normalize_version("1.2.3-beta2"), "1.2.3b2")
        self.assertEqual(normalize_version("1.2.3-rc4"), "1.2.3rc4")
        self.assertEqual(normalize_version("1.2.3-dev3"), "1.2.3.dev3")
        self.assertEqual(normalize_version("1.2.3-preview1"), "1.2.3rc1")
        self.assertEqual(normalize_version("1.2.3-preview.1"), "1.2.3rc1")

    def test_empty_input(self):
        self.assertEqual(normalize_version(""), "")

    def test_strips_build_metadata(self):
        self.assertEqual(normalize_version("v1.2.3+build.5"), "1.2.3")
        self.assertEqual(normalize_version("v1.2.3-pre01+sha.abc"), "1.2.3rc1")

    def test_dashless_suffix(self):
        # Tags like ``1.2.3rc1`` (no separator) must still normalize.
        self.assertEqual(normalize_version("1.2.3rc1"), "1.2.3rc1")
        self.assertEqual(normalize_version("1.2.3a1"), "1.2.3a1")

    def test_does_not_rewrite_non_prerelease_tokens(self):
        # The bare ``a|b`` alternation used to silently turn ``-build`` into
        # ``b0``. The tightened regex leaves unknown suffixes alone.
        self.assertEqual(normalize_version("1.0.0-build"), "1.0.0-build")
        self.assertEqual(normalize_version("banana"), "banana")


class IsNewerVersionTests(unittest.TestCase):
    def test_release_outranks_prerelease(self):
        # Reported bug: installing v0.0.1 over v0.0.1-pre01 must be an upgrade.
        self.assertTrue(GitHubClient.is_newer_version("v0.0.1-pre01", "v0.0.1"))
        self.assertFalse(GitHubClient.is_newer_version("v0.0.1", "v0.0.1-pre01"))

    def test_basic_release_progression(self):
        self.assertTrue(GitHubClient.is_newer_version("v0.0.1", "v0.0.2"))
        self.assertFalse(GitHubClient.is_newer_version("v0.0.2", "v0.0.1"))

    def test_equal_versions(self):
        self.assertFalse(GitHubClient.is_newer_version("v1.0.0", "v1.0.0"))

    def test_prerelease_progression(self):
        self.assertTrue(
            GitHubClient.is_newer_version("v0.0.1-pre01", "v0.0.1-pre02")
        )
        self.assertFalse(
            GitHubClient.is_newer_version("v0.0.1-pre02", "v0.0.1-pre01")
        )

    def test_numeric_segment_compare_not_lex(self):
        # Lexicographic compare would say "0.10.0" < "0.9.0".
        self.assertTrue(GitHubClient.is_newer_version("v0.9.0", "v0.10.0"))
        self.assertFalse(GitHubClient.is_newer_version("v0.10.0", "v0.9.0"))

    def test_beta_below_release(self):
        self.assertTrue(GitHubClient.is_newer_version("v1.2.3-beta.1", "v1.2.3"))
        self.assertFalse(GitHubClient.is_newer_version("v1.2.3", "v1.2.3-rc1"))

    def test_invalid_version_returns_false(self):
        self.assertFalse(GitHubClient.is_newer_version("garbage", "more-garbage"))

    def test_build_metadata_does_not_affect_precedence(self):
        # SemVer: build metadata after "+" must not change ordering.
        self.assertFalse(GitHubClient.is_newer_version("v1.0.0", "v1.0.0+build.1"))
        self.assertFalse(GitHubClient.is_newer_version("v1.0.0+build.1", "v1.0.0"))


if __name__ == "__main__":
    unittest.main()
