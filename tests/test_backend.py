import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import os
import time
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backend", ROOT / "backend.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = patch.object(b, "CACHE", Path(self.temp.name))
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_catalog_bounds_and_manual_setup(self):
        rows = b.normalize_catalog({"plugins": [
            {"id": "test.good", "name": '<img src="https://tracker.invalid/">',
             "repo": "https://github.com/test/good.git", "installAvailable": True,
             "repositoryLayout": "root-plugin", "description": "x" * 10000},
            {"id": "test.manual", "repo": "https://github.com/test/suite", "installAvailable": True,
             "repositoryLayout": "suite"},
            {"id": "../escape"}, {"id": "test.good"}]})
        self.assertEqual(len(rows), 2)
        good = next(row for row in rows if row["id"] == "test.good")
        self.assertTrue(good["installable"])
        self.assertEqual(len(good["description"]), 512)
        self.assertIn("<img", good["name"])
        self.assertFalse(next(row for row in rows if row["id"] == "test.manual")["installable"])
        with self.assertRaises(ValueError):
            b.normalize_catalog({"plugins": "wrong"})

    def test_repository_urls_refuse_commands_and_credentials(self):
        for value in ("--upload-pack=evil", "ext::sh -c whoami", "file:///tmp/repo",
                      "https://user:secret@github.com/a/b", "https://github.com/a/$(id)",
                      "https://github.com/a/b?x=1", "https://localhost/a/b"):
            self.assertEqual(b.repo_url(value), "")

    def test_install_uses_catalog_url_as_argument_and_stays_disabled(self):
        b.save("catalog", {"rows": [{"id": "test.plugin", "repo": "https://github.com/a/b", "installable": True}]})
        with patch.object(b, "installed", return_value=[]), patch.object(b, "run") as run:
            b.perform("install", "test.plugin", "https://github.com/a/b")
            self.assertEqual(run.call_args.args[0], ["omarchy", "plugin", "add", "https://github.com/a/b", "--yes"])

    def test_protected_plugins_and_dirty_updates(self):
        with self.assertRaises(ValueError):
            b.perform("remove", b.PLUGIN_ID)
        builtin = {"id": "omarchy.bar", "firstParty": True, "canDisable": False}
        with patch.object(b, "installed", return_value=[builtin]), patch.object(b, "run") as run:
            for action in ("remove", "disable"):
                with self.assertRaises(ValueError):
                    b.perform(action, builtin["id"])
            run.assert_not_called()
        row = {"id": "test.plugin", "git": True, "repo": "https://github.com/a/b"}
        b.save("catalog", {"rows": [dict(row, installable=True)]})
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "run", return_value=" M Panel.qml") as run:
            with self.assertRaisesRegex(ValueError, "Local changes"):
                b.perform("update", row["id"])
            self.assertEqual(run.call_count, 1)

    def test_install_with_auto_enable_uses_explicit_omarchy_flag(self):
        b.save("catalog", {"rows": [{"id": "test.plugin", "repo": "https://github.com/a/b", "installable": True}]})
        with patch.object(b, "installed", return_value=[]), patch.object(b, "run") as run:
            b.perform("install-enable", "test.plugin", "https://github.com/a/b")
            self.assertEqual(run.call_args.args[0], ["omarchy", "plugin", "add", "https://github.com/a/b", "--yes", "--enable"])
        with patch.object(b, "installed", return_value=[]), patch.object(b, "run") as run:
            with self.assertRaises(ValueError):
                b.perform("install-enable", "unknown.plugin", "https://github.com/a/b")
            run.assert_not_called()

    def test_update_checks_use_installed_origin_and_handle_offline(self):
        row = {"id": "test.plugin", "git": True, "repo": "https://github.com/a/b", "head": "a" * 40}
        with patch.object(b, "run", return_value="a" * 40 + "\tHEAD\n"):
            self.assertEqual(b.check_one(row)[1]["state"], "current")
        with patch.object(b, "run", side_effect=RuntimeError("offline")):
            self.assertEqual(b.check_one(row)[1]["state"], "error")
        with patch.object(b, "run") as run:
            self.assertEqual(b.check_one(dict(row, repo="ext::bad"))[1]["state"], "local")
            run.assert_not_called()

    def test_catalog_cache_is_lossless_gzip_and_migrates_legacy_file(self):
        value = {"rows": [{"id": "test.plugin", "description": "Å plugin " * 500}], "at": 42}
        legacy = b.CACHE / "catalog.json"
        legacy.write_text(json.dumps(value))
        self.assertEqual(b.load("catalog", None), value)
        self.assertTrue((b.CACHE / "catalog.json.gz").exists())
        self.assertFalse(legacy.exists())
        self.assertLess((b.CACHE / "catalog.json.gz").stat().st_size, 500)
        self.assertEqual(b.load("catalog", None), value)

    def test_catalog_compression_and_conditional_requests(self):
        body = {"plugins": [{"id": "test.plugin", "name": "Test"}]}
        def initial(args, timeout):
            self.assertIn("--compressed", args)
            self.assertEqual(args[args.index("--max-filesize") + 1], str(64 * 1024 * 1024))
            Path(args[args.index("--output") + 1]).write_text(json.dumps(body))
            Path(args[args.index("--dump-header") + 1]).write_text('HTTP/2 200\r\nETag: "revision-1"\r\n')
            return "200"
        with patch.object(b, "run", side_effect=initial):
            first = b.fetch_catalog()
        def unchanged(args, timeout):
            self.assertIn('If-None-Match: "revision-1"', args)
            return "304"
        with patch.object(b, "run", side_effect=unchanged):
            second = b.fetch_catalog()
        self.assertEqual(first["rows"], second["rows"])

    def test_oversize_catalog_preserves_existing_cache(self):
        value = {"rows": [], "at": 42}
        b.save("catalog", value)
        before = (b.CACHE / "catalog.json.gz").read_bytes()
        def oversized(args, timeout):
            Path(args[args.index("--output") + 1]).write_bytes(b"x" * 101)
            return "200"
        with patch.object(b, "MAX_BYTES", 100), patch.object(b, "run", side_effect=oversized):
            with self.assertRaisesRegex(ValueError, "size limit"):
                b.fetch_catalog()
        self.assertEqual((b.CACHE / "catalog.json.gz").read_bytes(), before)

    def test_update_check_does_not_fetch_or_require_catalog(self):
        row = {"id": "test.unlisted", "git": True, "repo": "https://github.com/a/b", "head": "a" * 40}
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "fetch_catalog") as fetch, patch.object(b, "run", return_value="a" * 40 + "\tHEAD\n"):
            b.check_updates()
            fetch.assert_not_called()
        self.assertEqual(b.load("updates", {})["rows"][row["id"]]["state"], "current")
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "run", return_value="") as run:
            b.perform("update", row["id"])
            self.assertIn(["omarchy", "plugin", "update", row["id"], "--yes"], [call.args[0] for call in run.call_args_list])

    def test_markup_sinks_are_plain_and_tooltip_is_escaped(self):
        plain = (ROOT / "PlainText.qml").read_text()
        self.assertIn("textFormat: Text.PlainText", plain)
        panel = (ROOT / "Panel.qml").read_text()
        self.assertNotIn("tooltipText:", panel)
        self.assertNotIn("notification", panel)
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn('replace(/</g, "&lt;")', widget)
        self.assertIn('replace(/>/g, "&gt;")', widget)
        self.assertNotRegex(panel, r"(?m)^\s*Text\s*\{")
        # All displayed model fields go through our PlainText component.
        self.assertIn("text: entry.modelData.name", panel)

    def test_view_paginates_and_invalidates_checks_when_head_changes(self):
        rows = [{"id": "test." + str(i), "name": "Plugin " + str(i), "head": "new", "repo": "", "firstParty": False} for i in range(35)]
        b.save("updates", {"rows": {"test.0": {"head": "old", "state": "available"}}, "at": 1})
        with patch.object(b, "installed", return_value=rows):
            result = b.view("installed", "", "0")
            self.assertEqual(len(result["rows"]), 20)
            self.assertEqual(result["total"], 35)
            self.assertEqual(result["updates"], 0)

    def test_enable_disable_remove_command_routes(self):
        row = {"id": "test.plugin", "firstParty": False, "canDisable": True}
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "run") as run:
            for action in ("enable", "disable", "remove"):
                b.perform(action, row["id"])
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["omarchy", "plugin", "enable", "test.plugin"],
                ["omarchy", "plugin", "disable", "test.plugin"],
                ["omarchy", "plugin", "remove", "test.plugin", "--yes"]])

    def test_official_and_third_party_search_are_separate(self):
        rows = [
            {"id": "omarchy.clock", "name": "Clock", "firstParty": True, "head": "", "repo": ""},
            {"id": "user.clock", "name": "Custom Clock", "firstParty": False, "head": "", "repo": ""},
            {"id": "user.weather", "name": "Weather", "firstParty": False, "head": "", "repo": ""},
        ]
        with patch.object(b, "installed", return_value=rows):
            official = b.view("installed", "CLOCK", "0", "official")
            third_party = b.view("installed", "clock", "0", "third-party")
            self.assertEqual([p["id"] for p in official["rows"] if not p.get("sectionOnly")], ["omarchy.clock"])
            self.assertEqual([p["id"] for p in third_party["rows"] if not p.get("sectionOnly")], ["user.clock"])
            self.assertEqual(official["officialCount"], 1)
            self.assertEqual(official["thirdPartyCount"], 2)
            self.assertEqual(b.view("installed", "weather", "9", "official")["total"], 0)
            self.assertEqual(b.view("installed", "weather", "9", "third-party")["page"], 0)

    def test_detached_worker_completes_and_serializes_operations(self):
        folder = Path(self.temp.name)
        cli = folder / "omarchy"
        cli.write_text('#!/usr/bin/env python3\nimport sys,json,time\n'
                       'if "--json" in sys.argv: print(json.dumps([{"id":"test.fixture","name":"Fixture","firstParty":False,"canDisable":True}]))\n'
                       'else: time.sleep(1)\n')
        cli.chmod(0o755)
        env = dict(os.environ, XDG_CACHE_HOME=str(folder / "cache"), PATH=str(folder) + ":" + os.environ["PATH"])
        command = [sys.executable, str(ROOT / "backend.py"), "start", "enable", "test.fixture"]
        first = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(first.returncode, 0, first.stdout)
        second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already running", second.stdout)
        job_path = folder / "cache" / b.PLUGIN_ID / "job.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = json.loads(job_path.read_text())
            if not job["busy"]:
                break
            time.sleep(0.05)
        self.assertFalse(job["busy"])
        self.assertTrue(job["ok"], job)

    def test_enabled_and_disabled_sections_within_each_category(self):
        rows = [dict(id=f"{origin}.{state}.{name}", name=name, firstParty=official,
                     enabled=enabled, head="", repo="")
                for origin, official in (("omarchy", True), ("user", False))
                for state, enabled in (("disabled", False), ("enabled", True))
                for name in ("Zebra", "Alpha")]
        with patch.object(b, "installed", return_value=rows):
            for origin, official in (("official", True), ("third-party", False)):
                result = b.view("installed", "", "0", origin)["rows"]
                self.assertTrue(all(row["firstParty"] == official for row in result))
                self.assertEqual([row["enabled"] for row in result], [True, True, False, False])
                self.assertEqual([row["name"] for row in result], ["Alpha", "Zebra", "Alpha", "Zebra"])
                self.assertEqual([row["sectionStart"] for row in result], [True, False, True, False])
                self.assertEqual([row["stateSection"] for row in result], ["enabled", "enabled", "disabled", "disabled"])
                self.assertEqual([row["sectionCount"] for row in result], [2, 2, 2, 2])
                filtered = b.view("installed", "alpha", "0", origin)["rows"]
                self.assertEqual([row["sectionCount"] for row in filtered], [1, 1])

    def test_section_heading_repeats_when_page_starts_inside_a_group(self):
        rows = [dict(id=f"user.{i}", name=f"Plugin {i:02}", firstParty=False,
                     enabled=i < 25, head="", repo="") for i in range(30)]
        with patch.object(b, "installed", return_value=rows):
            page = b.view("installed", "", "1", "third-party")["rows"]
            self.assertTrue(page[0]["sectionStart"])
            self.assertEqual(page[0]["stateSection"], "enabled")
            self.assertEqual(page[0]["sectionCount"], 25)
            self.assertTrue(page[5]["sectionStart"])
            self.assertEqual(page[5]["stateSection"], "disabled")
            self.assertEqual(page[5]["sectionCount"], 5)

    def test_collapsed_sections_leave_both_dropdowns_accessible(self):
        rows = [dict(id=f"user.{i}", name=f"Plugin {i:02}", firstParty=False,
                     enabled=i < 25, head="", repo="") for i in range(30)]
        with patch.object(b, "installed", return_value=rows):
            result = b.view("installed", "", "0", "third-party", "enabled")
            self.assertEqual(result["pages"], 1)
            self.assertEqual(result["total"], 30)
            self.assertEqual(len(result["rows"]), 6)
            self.assertTrue(result["rows"][0]["sectionOnly"])
            self.assertEqual(result["rows"][0]["sectionCount"], 25)
            self.assertTrue(result["rows"][1]["sectionStart"])
            self.assertFalse(result["rows"][1]["enabled"])
            both = b.view("installed", "", "0", "third-party", "enabled,disabled")
            self.assertEqual(len(both["rows"]), 2)
            self.assertTrue(all(row["sectionOnly"] and row["sectionStart"] for row in both["rows"]))

    def test_descriptions_use_local_metadata_then_marketplace_fallback(self):
        rows = [dict(id="user.local", name="Local", description="Local description", head="", repo=""),
                dict(id="user.remote", name="Remote", description="", head="", repo="")]
        b.save("catalog", {"rows": [dict(id="user.local", description="Other description"),
                                    dict(id="user.remote", description='<img src="https://tracker.invalid/">' + "x" * 400)]})
        with patch.object(b, "installed", return_value=rows):
            result = b.view("installed", "", "0", "third-party")
            plugins = {row["id"]: row for row in result["rows"] if not row.get("sectionOnly")}
            self.assertEqual(plugins["user.local"]["description"], "Local description")
            self.assertEqual(len(plugins["user.remote"]["description"]), 280)
            self.assertIn("<img", plugins["user.remote"]["description"])

    def test_official_descriptions_load_from_installed_manifests(self):
        root = Path(self.temp.name) / "omarchy"
        folder = root / "shell/plugins/clock"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"id": "omarchy.clock", "description": "Clock and calendar", "version": "1.0"}))
        b.official_metadata.cache_clear()
        self.addCleanup(b.official_metadata.cache_clear)
        with patch.dict(os.environ, OMARCHY_PATH=str(root)), patch.object(b, "run", return_value=json.dumps([
            {"id": "omarchy.clock", "name": "Clock", "firstParty": True}])):
            self.assertEqual(b.installed()[0]["description"], "Clock and calendar")

    def test_bar_placement_moves_enabled_widgets_in_each_section(self):
        row = {"id": "test.widget", "kinds": ["bar-widget"], "enabled": True}
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "run") as run:
            for action, section in b.MOVE_ACTIONS.items():
                b.perform(action, row["id"])
                self.assertEqual(run.call_args.args[0], ["omarchy", "bar", "move", row["id"], "--section", section])

    def test_bar_placement_enables_disabled_widgets_at_selected_location(self):
        row = {"id": "test.widget", "kinds": ["bar-widget"], "enabled": False}
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "run") as run:
            b.perform("move-centerBegin", row["id"])
            self.assertEqual(run.call_args.args[0], ["omarchy", "plugin", "enable", row["id"], "--section", "centerBegin"])

    def test_bar_placement_allows_manager_but_rejects_services_and_invalid_sections(self):
        row = {"id": b.PLUGIN_ID, "kinds": ["bar-widget"], "enabled": True}
        with patch.object(b, "installed", return_value=[row]), patch.object(b, "run") as run:
            b.perform("move-left", row["id"])
            self.assertEqual(run.call_args.args[0], ["omarchy", "bar", "move", b.PLUGIN_ID, "--section", "left"])
        for kinds in (["service"], ["bar"], ["bar", "bar-widget"]):
            row = {"id": "test.plugin", "kinds": kinds, "enabled": True}
            with patch.object(b, "installed", return_value=[row]), patch.object(b, "run") as run:
                with self.assertRaisesRegex(ValueError, "bar widget"):
                    b.perform("move-left", row["id"])
                run.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Unknown action"):
            b.start("move-../../invalid", "test.widget")

    def test_manifest_uses_marketplace_compatible_runtime_id(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        plugin_id = manifest["id"]
        self.assertEqual(plugin_id, plugin_id.lower())
        self.assertEqual(plugin_id, b.PLUGIN_ID)
        for name in ("BarWidget.qml", "Panel.qml"):
            self.assertIn('moduleName: "' + plugin_id + '"', (ROOT / name).read_text())


if __name__ == "__main__":
    unittest.main()
