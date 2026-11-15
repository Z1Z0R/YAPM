import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_backend", ROOT / "backend.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
VERIFIED = "a" * 40
OTHER = "b" * 40
REPO = "https://github.com/example/plugin"


class VerifiedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        cache = patch.object(b, "CACHE", Path(self.temporary.name))
        cache.start()
        self.addCleanup(cache.stop)
        self.listing = dict(id="test.plugin", name="Test", description="Plugin", repo=REPO,
                            version="1.0.0", verifiedSnapshot=True, verifiedCommit=VERIFIED,
                            verification="unverified", observed=OTHER)
        self.local = dict(id="test.plugin", name="Test", description="Plugin", repo=REPO,
                          head=OTHER, git=True, firstParty=False, kinds=["bar-widget"], enabled=True)
        b.save("catalog", {"rows": [self.listing], "at": 1, "format": b.CATALOG_FORMAT})

    def fake_release(self, commit=VERIFIED, annotated=False, tag="v1.0.0"):
        original_run = b.run
        def run(args, timeout=20):
            if args[0] == "curl":
                self.assertEqual(args[-1], "https://api.github.com/repos/example/plugin/releases/latest")
                self.assertIn("--max-filesize", args)
                Path(args[args.index("--output") + 1]).write_text(json.dumps(
                    {"tag_name": tag, "draft": False, "prerelease": False}))
                return ""
            if args[:2] == ["git", "check-ref-format"]:
                return original_run(args, timeout)
            reference = "refs/tags/" + tag
            self.assertEqual(args, ["git", "ls-remote", "--exit-code", "--", REPO,
                                    reference, reference + "^{}"])
            if annotated:
                return OTHER + "\t" + reference + "\n" + commit + "\t" + reference + "^{}\n"
            return commit + "\t" + reference + "\n"
        return run

    def visible_plugin(self, tab="installed"):
        with patch.object(b, "installed", return_value=[dict(self.local)]):
            return next(row for row in b.view(tab, "", "0", "third-party")["rows"]
                        if row["id"] == self.local["id"])

    def test_verified_metadata_is_retained_and_commit_ids_are_validated(self):
        catalog = b.normalize_catalog({"plugins": [{"id": "test.plugin", "repo": REPO,
            "verificationStatus": "unverified", "verificationSnapshotStatus": "verified",
            "verificationCommit": VERIFIED.upper(), "upstreamObservedCommit": OTHER}]})
        self.assertTrue(catalog[0]["verifiedSnapshot"])
        self.assertEqual(catalog[0]["verifiedCommit"], VERIFIED)
        for invalid in ("a" * 39, "a" * 41, "a" * 65, "prefix" + VERIFIED, None):
            self.assertEqual(b.commit_id(invalid), "")

    def test_lightweight_and_annotated_release_tags_match_verified_commit(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated), patch.object(b, "run", side_effect=self.fake_release(annotated=annotated)):
                plugin_id, result = b.check_release(self.listing)
            self.assertEqual(plugin_id, "test.plugin")
            self.assertEqual(result["commit"], VERIFIED)
            self.assertEqual(result["tag"], "v1.0.0")
            self.assertTrue(b.verified_match(self.listing, result["commit"], result["repo"]))

    def test_same_version_text_different_commit_has_no_indicator(self):
        with patch.object(b, "run", side_effect=self.fake_release(commit=OTHER)):
            b.check_release_page("test.plugin")
        self.assertFalse(self.visible_plugin()["verifiedMatch"])
        self.assertFalse(self.visible_plugin("marketplace")["verifiedMatch"])

    def test_tag_match_has_indicator_even_when_default_branch_is_newer(self):
        with patch.object(b, "run", side_effect=self.fake_release()):
            b.check_release_page("test.plugin")
        for tab in ("installed", "marketplace"):
            row = self.visible_plugin(tab)
            self.assertTrue(row["verifiedMatch"])
            self.assertEqual(row["verifiedReleaseTag"], "v1.0.0")
        self.assertNotEqual(self.listing["observed"], VERIFIED)

    def test_unknown_release_or_failed_request_clears_previously_matching_badge(self):
        with patch.object(b, "run", side_effect=self.fake_release()):
            b.check_release_page("test.plugin")
        self.assertTrue(self.visible_plugin()["verifiedMatch"])
        with patch.object(b, "run", side_effect=RuntimeError("HTTP 404 or rate limited")):
            b.check_release_page("test.plugin")
        self.assertFalse(self.visible_plugin()["verifiedMatch"])
        self.assertEqual(b.load("releases", {})["rows"]["test.plugin"]["commit"], "")

    def test_bad_tag_or_oversized_api_data_cannot_create_indicator(self):
        with patch.object(b, "run", side_effect=self.fake_release(tag="bad..tag")):
            self.assertEqual(b.check_release(self.listing)[1]["commit"], "")
        def oversized(args, timeout):
            Path(args[args.index("--output") + 1]).write_bytes(b"x" * (1048576 + 1))
            return ""
        with patch.object(b, "run", side_effect=oversized):
            self.assertEqual(b.check_release(self.listing)[1]["commit"], "")

    def test_missing_verified_metadata_wrong_repo_or_no_check_has_no_indicator(self):
        self.assertFalse(self.visible_plugin()["verifiedMatch"])
        for listing in (dict(self.listing, verifiedSnapshot=False),
                        dict(self.listing, verifiedCommit=""), dict(self.listing, repo=REPO + "-fork")):
            b.save("catalog", {"rows": [listing]})
            b.save("releases", {"rows": {"test.plugin": {"repo": REPO, "tag": "v1.0.0", "commit": VERIFIED}}})
            self.assertFalse(self.visible_plugin()["verifiedMatch"])

    def test_missing_release_tag_has_no_indicator(self):
        b.save("releases", {"rows": {"test.plugin": {"repo": REPO, "commit": VERIFIED}}})
        self.assertFalse(self.visible_plugin()["verifiedMatch"])
        self.assertFalse(self.visible_plugin("marketplace")["verifiedMatch"])

    def test_draft_prerelease_and_malformed_api_data_cannot_create_indicator(self):
        for release in ({"tag_name": "v1.0", "draft": True, "prerelease": False},
                        {"tag_name": "v1.0", "draft": False, "prerelease": True},
                        {"tag_name": "v1.0"}, []):
            def api(args, timeout):
                Path(args[args.index("--output") + 1]).write_text(json.dumps(release))
                return ""
            with self.subTest(release=release), patch.object(b, "run", side_effect=api):
                self.assertEqual(b.check_release(self.listing)[1]["commit"], "")

    def test_update_checks_include_release_verification_without_downloading_catalog(self):
        with patch.object(b, "installed", return_value=[self.local]), patch.object(b, "fetch_catalog") as fetch:
            with patch.object(b, "check_one", return_value=("test.plugin", {"head": OTHER, "remote": OTHER, "state": "current"})):
                with patch.object(b, "run", side_effect=self.fake_release()):
                    b.check_updates()
            fetch.assert_not_called()
        self.assertTrue(self.visible_plugin()["verifiedMatch"])

    def test_release_check_requests_are_bounded_and_use_fixed_github_api(self):
        for repository in ("https://github.com.evil.invalid/example/plugin", "https://localhost/a/b",
                           "https://github.com/a/b/c", "ext::bad", "https://user@github.com/a/b"):
            self.assertEqual(b.github_release_api(repository), "")
        for ids in ("", "../invalid", ",".join("test." + str(i) for i in range(21))):
            with self.assertRaises(ValueError):
                b.check_release_page(ids)

    def test_old_catalog_format_is_refetched_for_verification_metadata(self):
        b.save("catalog", {"rows": [self.listing], "at": 1, "etag": '"old"'})
        def refresh(args, timeout):
            self.assertNotIn('If-None-Match: "old"', args)
            Path(args[args.index("--output") + 1]).write_text('{"plugins": []}')
            Path(args[args.index("--dump-header") + 1]).write_text('HTTP/2 200\r\nETag: "new"\r\n')
            return "200"
        with patch.object(b, "run", side_effect=refresh):
            self.assertEqual(b.fetch_catalog()["format"], b.CATALOG_FORMAT)


if __name__ == "__main__":
    unittest.main()
