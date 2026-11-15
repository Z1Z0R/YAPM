import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cache_backend", ROOT / "backend.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
APPROVED = "https://github.com/example/approved"
SUBSTITUTED = "https://github.com/example/substituted"


class CacheSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / b.PLUGIN_ID
        self.cache_patch = patch.object(b, "CACHE", self.cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def catalog(self, repository=APPROVED):
        return {"rows": [{"id": "test.plugin", "name": "Test", "repo": repository,
                          "installable": True}], "at": 1}

    def test_catalog_swap_after_confirmation_cannot_change_install_repository(self):
        b.save("catalog", self.catalog())
        confirmed = b.load("catalog", {})["rows"][0]["repo"]
        b.save("catalog", self.catalog(SUBSTITUTED))
        for action in ("install", "install-enable"):
            with self.subTest(action=action), patch.object(b, "installed", return_value=[]), patch.object(b, "run") as run:
                with self.assertRaisesRegex(ValueError, "repository changed"):
                    b.perform(action, "test.plugin", confirmed)
                run.assert_not_called()
        b.save("catalog", self.catalog())
        with patch.object(b, "installed", return_value=[]), patch.object(b, "run") as run:
            b.perform("install-enable", "test.plugin", confirmed)
            self.assertEqual(run.call_args.args[0], ["omarchy", "plugin", "add", APPROVED, "--yes", "--enable"])

    def test_install_without_confirmed_repository_fails_closed(self):
        b.save("catalog", self.catalog())
        with patch.object(b, "installed", return_value=[]), patch.object(b, "run") as run:
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                b.perform("install", "test.plugin")
            run.assert_not_called()

    def test_lock_symlink_cannot_truncate_target_in_start_or_view(self):
        b.save("job", {"busy": True})
        victim = self.root / "victim"
        victim.write_bytes(b"preserve owner file")
        (self.cache / "operation.lock").symlink_to(victim)
        with patch.object(b.subprocess, "Popen") as spawn:
            with self.assertRaises(b.CacheSecurityError):
                b.start("enable", "test.plugin")
            spawn.assert_not_called()
        with patch.object(b, "installed", return_value=[]):
            with self.assertRaises(b.CacheSecurityError):
                b.view()
        self.assertEqual(victim.read_bytes(), b"preserve owner file")

    def test_hardlinked_lock_cannot_modify_target(self):
        b.save("job", {"busy": False})
        victim = self.root / "victim"
        victim.write_bytes(b"preserve hardlink target")
        os.link(victim, self.cache / "operation.lock")
        with self.assertRaises(b.CacheSecurityError):
            b.start("enable", "test.plugin")
        self.assertEqual(victim.read_bytes(), b"preserve hardlink target")

    def test_both_catalog_formats_reject_symlinks_and_writable_files(self):
        for name in ("catalog.json.gz", "catalog.json"):
            self.cache.mkdir(exist_ok=True)
            victim = self.root / "victim"
            victim.write_bytes(b"private owner data")
            entry = self.cache / name
            entry.symlink_to(victim)
            with self.subTest(name=name, variant="symlink"), self.assertRaises(b.CacheSecurityError):
                b.load("catalog", {})
            self.assertEqual(victim.read_bytes(), b"private owner data")
            entry.unlink()
            entry.write_text(json.dumps(self.catalog()))
            entry.chmod(0o666)
            with self.subTest(name=name, variant="writable"), self.assertRaises(b.CacheSecurityError):
                b.load("catalog", {})
            entry.unlink()

    def test_fifo_cache_objects_are_rejected_without_blocking(self):
        self.cache.mkdir()
        for name in ("catalog.json.gz", "operation.lock"):
            entry = self.cache / name
            os.mkfifo(entry)
            with self.subTest(name=name), self.assertRaises(b.CacheSecurityError):
                if name == "operation.lock":
                    b.start("enable", "test.plugin")
                else:
                    b.load("catalog", {})
            entry.unlink()

    def test_writable_cache_or_ancestor_is_rejected(self):
        self.cache.mkdir()
        self.cache.chmod(0o777)
        with patch.object(b.subprocess, "Popen") as spawn:
            with self.assertRaises(b.CacheSecurityError):
                b.start("enable", "test.plugin")
            spawn.assert_not_called()
        self.cache.chmod(0o700)
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        with patch.object(b, "CACHE", shared / b.PLUGIN_ID):
            with self.assertRaises(b.CacheSecurityError):
                b.save("catalog", self.catalog())
        self.assertFalse((shared / b.PLUGIN_ID).exists())
        shared.chmod(0o1777)
        with patch.object(b, "CACHE", shared / b.PLUGIN_ID):
            b.save("catalog", self.catalog())
            self.assertEqual(b.load("catalog", {}), self.catalog())

    def test_foreign_cache_directory_owner_is_rejected(self):
        self.cache.mkdir()
        inode = self.cache.stat().st_ino
        original_fstat = os.fstat
        def foreign_owner(fd):
            info = original_fstat(fd)
            if info.st_ino == inode:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
            return info
        with patch.object(b.os, "fstat", side_effect=foreign_owner):
            with self.assertRaises(b.CacheSecurityError):
                b.load("catalog", {})

    def test_trusted_cache_symlink_works_but_unsafe_target_is_rejected(self):
        actual = self.root / "actual"
        actual.mkdir()
        self.cache.symlink_to(actual, target_is_directory=True)
        b.save("catalog", self.catalog())
        self.assertEqual(b.load("catalog", {}), self.catalog())
        actual.chmod(0o777)
        with self.assertRaises(b.CacheSecurityError):
            b.save("catalog", self.catalog(SUBSTITUTED))
        actual.chmod(0o700)
        self.assertEqual(b.load("catalog", {}), self.catalog())

    def test_new_cache_and_lock_are_private_and_existing_cache_is_compatible(self):
        b.save("catalog", self.catalog())
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)
        self.cache.chmod(0o755)
        self.assertEqual(b.load("catalog", {}), self.catalog())
        with b.cache_directory() as directory_fd:
            with b.open_cache_lock(directory_fd):
                self.assertEqual(stat.S_IMODE((self.cache / "operation.lock").stat().st_mode), 0o600)

    def test_worker_requires_matching_inherited_cache_and_lock(self):
        with patch.object(b, "perform") as perform:
            with self.assertRaises(b.CacheSecurityError):
                b.worker("install-enable", "test.plugin", "3", APPROVED)
            perform.assert_not_called()
        b.save("catalog", self.catalog())
        (self.cache / "operation.lock").write_bytes(b"")
        other = self.root / "other-lock"
        other.write_bytes(b"do not truncate")
        lock_fd = os.open(other, os.O_RDWR)
        directory_fd = os.open(self.cache, os.O_RDONLY | os.O_DIRECTORY)
        with patch.object(b, "perform") as perform:
            with self.assertRaisesRegex(b.CacheSecurityError, "does not belong"):
                b.worker("install-enable", "test.plugin", lock_fd, APPROVED, directory_fd)
            perform.assert_not_called()
        self.assertEqual(other.read_bytes(), b"do not truncate")

    def test_detached_worker_carries_confirmed_repository_for_both_install_modes(self):
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        cli = binary_dir / "omarchy"
        cli.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                       'if "--json" in sys.argv: print("[]")\n'
                       'else:\n'
                       ' with open(os.environ["YAPM_TEST_LOG"],"a") as stream: stream.write(json.dumps(sys.argv[1:])+"\\n")\n')
        cli.chmod(0o755)
        log = self.root / "commands"
        env = dict(b.ENV, XDG_CACHE_HOME=str(self.root),
                   PATH=str(binary_dir) + ":" + os.environ["PATH"], YAPM_TEST_LOG=str(log))
        with patch.object(b, "ENV", env):
            for action in ("install", "install-enable"):
                b.save("catalog", self.catalog())
                launch = subprocess.run([sys.executable, str(ROOT / "backend.py"), "start", action,
                                         "test.plugin", APPROVED], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(launch.returncode, 0, launch.stdout + launch.stderr)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    job = b.load("job", {})
                    if not job.get("busy"):
                        break
                    time.sleep(0.05)
                self.assertTrue(job.get("ok"), job)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(commands, [["plugin", "add", APPROVED, "--yes"],
                                        ["plugin", "add", APPROVED, "--yes", "--enable"]])
            b.save("catalog", self.catalog(SUBSTITUTED))
            launch = subprocess.run([sys.executable, str(ROOT / "backend.py"), "start", "install-enable",
                                     "test.plugin", APPROVED], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(launch.returncode, 0, launch.stdout + launch.stderr)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = b.load("job", {})
                if not job.get("busy"):
                    break
                time.sleep(0.05)
            self.assertFalse(job.get("ok"), job)
            self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_cache_operations_remain_on_the_validated_directory_descriptor(self):
        b.save("catalog", self.catalog())
        original = self.root / "pinned-cache"
        with b.cache_directory():
            self.cache.rename(original)
            self.cache.mkdir()
            self.cache.chmod(0o777)
            b.save("job", {"busy": False})
            self.assertEqual(b.load("catalog", {}), self.catalog())
        self.assertTrue((original / "job.json").exists())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_foreign_cache_file_owner_is_rejected_before_reading(self):
        b.save("catalog", self.catalog())
        inode = (self.cache / "catalog.json.gz").stat().st_ino
        original_fstat = os.fstat
        def foreign_owner(fd):
            info = original_fstat(fd)
            if info.st_ino == inode:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1,
                                       st_nlink=info.st_nlink)
            return info
        with patch.object(b.os, "fstat", side_effect=foreign_owner):
            with self.assertRaises(b.CacheSecurityError):
                b.load("catalog", {})

    def test_confirmation_passes_snapshot_repository_to_backend(self):
        panel = (ROOT / "Panel.qml").read_text()
        self.assertIn("row: Object.assign({}, row)", panel)
        self.assertIn("root.execute(root.pending.verb, root.pending.row.id, root.pending.row.repo)", panel)
        self.assertIn('repository || ""', panel)


if __name__ == "__main__":
    unittest.main()
