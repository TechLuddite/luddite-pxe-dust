"""Interrupted installation blocks runtime use while keeping cleanup available."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("installation_gate_dust", str(ROOT / "bin/pxe-dust"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
dust = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(dust)


class InstallationGateTests(unittest.TestCase):
    def test_only_absent_marker_allows_runtime(self):
        def missing(path, default=None):
            self.assertEqual(path, dust.DATA / "installation-pending.json")
            return default
        with patch.object(dust, "read_json", side_effect=missing):
            dust.installation_ready()
        for value in (None, False, {}, {"transaction": "interrupted"}):
            with self.subTest(value=value), patch.object(dust, "read_json", return_value=value):
                with self.assertRaisesRegex(dust.Error, "Interrupted system installation"):
                    dust.installation_ready()

    def test_pending_marker_blocks_public_and_service_actions(self):
        actions = ((["prepare", "/image.iso", "0" * 64], "prepare"),
                   (["start", "enp1s0", "--dedicated"], "start"),
                   (["uninstall"], "uninstall"),
                   (["serve"], "serve"), (["http"], "http_serve"), (["probe"], "probe"))
        for argv, function in actions:
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(dust, "elevate"), patch.object(dust.os, "umask"), \
                 patch.object(dust, "mutation_lock", side_effect=contextlib.nullcontext), \
                 patch.object(dust, "read_json", return_value={"transaction": "interrupted"}), \
                 patch.object(dust, function) as operation:
                self.assertEqual(dust.main(argv), 1)
                operation.assert_not_called()

    def test_stop_recover_and_cleanup_remain_available(self):
        for action in ("stop", "recover", "cleanup"):
            with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(dust, "elevate"), patch.object(dust.os, "umask"), \
                 patch.object(dust, "mutation_lock", side_effect=contextlib.nullcontext), \
                 patch.object(dust, "installation_ready") as gate, \
                 patch.object(dust, "service_state", return_value="inactive"), \
                 patch.object(dust, "bounded") as command, patch.object(dust, "cleanup") as cleanup:
                self.assertEqual(dust.main([action]), 0)
                gate.assert_not_called()
                if action == "stop":
                    command.assert_called_once_with(["/usr/bin/systemctl", "stop", dust.UNIT], timeout=35)
                else:
                    cleanup.assert_called_once()
