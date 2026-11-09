"""Safety checks use fake IPC and temporary config trees; never live outputs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backend import Backend, DisplayError, config_text, identity, logical_size, match_layout, normalize_outputs, validate_layout


def raw_output(name="DP-1", serial="123", x=0, internal=False):
    return {"name": name, "make": "Laptop" if internal else "Acme", "model": "Panel", "serial": serial,
            "modes": [{"width": 1920, "height": 1080, "refresh_rate": 60000, "is_preferred": True}],
            "current_mode": 0, "logical": {"x": x, "y": 0, "width": 1920, "height": 1080, "scale": 1.0, "transform": "Normal"}}


def output(name="DP-1", serial="123", x=0):
    return normalize_outputs({name: raw_output(name, serial, x)})[0]


class FakeNiri:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []
        self.fail_validation = None

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[1:4] == ["msg", "--json", "outputs"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.outputs), "")
        if command[1] == "validate":
            candidate = Path(command[-1])
            if self.fail_validation and self.fail_validation(candidate):
                return subprocess.CompletedProcess(command, 1, "", "invalid configuration")
        return subprocess.CompletedProcess(command, 0, "", "")


class IdentityTests(unittest.TestCase):
    def test_unique_edid_follows_port_changes(self):
        saved = [output("DP-1", "one"), output("DP-2", "two", 1920)]
        current = [output("HDMI-A-4", "two"), output("DP-8", "one")]
        matched = match_layout(saved, current)
        self.assertEqual([(x["connector"], x["x"]) for x in matched], [("DP-8", 0), ("HDMI-A-4", 1920)])
        self.assertIsNone(match_layout(saved, [output("DP-1", "different"), current[0]]))

    def test_identical_monitors_require_exact_connector_group(self):
        saved = [output("DP-1", None), output("DP-2", None, 1920)]
        self.assertIsNotNone(match_layout(saved, list(reversed(saved))))
        self.assertIsNone(match_layout(saved, [output("DP-1", None), output("DP-3", None)]))
        self.assertIsNone(match_layout(saved, [output("DP-1", None)]))

    def test_unknown_identity_falls_back_to_connector(self):
        a = output()
        a.update(make="Unknown", model="Unknown", serial=None)
        b = copy.deepcopy(a)
        b["connector"] = "HDMI-A-1"
        self.assertIsNone(match_layout([a], [b]))

    def test_internal_identity_survives_panel_connector(self):
        a, b = output("eDP-1", "one"), output("eDP-2", "other")
        self.assertEqual(identity(a), ("internal",))
        self.assertEqual(match_layout([a], [b])[0]["connector"], "eDP-2")

    def test_disabled_output_keeps_preferred_mode(self):
        raw = raw_output()
        raw.update(current_mode=None, logical=None)
        row = normalize_outputs({"DP-1": raw})[0]
        self.assertFalse(row["enabled"])
        self.assertEqual(row["mode"]["width"], 1920)


class GeometryTests(unittest.TestCase):
    def test_scale_rotation_and_edge_touching(self):
        a, b = output(), output("DP-2", "two", 864)
        a.update(scale=1.25, transform="90")
        self.assertEqual(logical_size(a), (864, 1536))
        validate_layout([a, b], [a, b])
        b["x"] = 863
        with self.assertRaisesRegex(DisplayError, "overlap"):
            validate_layout([a, b], [a, b])

    def test_fractional_extent_matches_niri_ipc_truncation(self):
        row = output()
        row["scale"] = 1.375
        self.assertEqual(logical_size(row), (1396, 785))

    def test_all_off_disconnected_invalid_mode_and_nan_rejected(self):
        current = [output()]
        for key, value in (("enabled", False), ("scale", float("nan")), ("transform", "45"),
                           ("connector", "DP-9"), ("mode", {"width": 1, "height": 1, "refresh_rate": 60})):
            changed = copy.deepcopy(current)
            changed[0][key] = value
            with self.subTest(key=key), self.assertRaises(DisplayError):
                validate_layout(changed, current)

    def test_persistent_config_never_disables_internal(self):
        row = output("eDP-1")
        row["enabled"] = False
        self.assertNotIn("    off", config_text([row]))

    def test_ambiguous_external_connectors_are_runtime_only(self):
        rows = [output("DP-1", None), output("DP-2", None, 1920), output("eDP-1")]
        rows[0]["enabled"] = False
        text = config_text(rows)
        self.assertNotIn('output "DP-1"', text)
        self.assertNotIn('output "DP-2"', text)
        self.assertIn('output "eDP-1"', text)
        unknown = output("HDMI-A-1")
        unknown.update(make="Unknown", model="Unknown", serial=None)
        self.assertNotIn('output "HDMI-A-1"', config_text([unknown]))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.ipc = FakeNiri({"DP-1": raw_output()})
        self.backend = Backend(self.home / "config", self.home / "state", runner=self.ipc, watchdog=False)
        self.backend.config.parent.mkdir(parents=True)
        self.original = b'// my settings\noutput "Old Home Monitor 12" { scale 1; }\nbinds { Mod+Q { close-window; }; }\n'
        self.backend.config.write_bytes(self.original)
        self.layout = self.backend.query()

    @patch("backend.time.sleep")
    def test_include_first_and_unrelated_bytes_preserved(self, _):
        self.backend._persist(self.layout)
        include = f'include "{self.backend.managed}"\n'.encode()
        self.assertEqual(self.backend.config.read_bytes(), include + self.original)
        self.backend._persist(self.layout)
        self.assertEqual(self.backend.config.read_bytes(), include + self.original)
        backups = list((self.backend.directory / "backups").glob("*/config.kdl"))
        self.assertEqual(len(backups), 2)
        self.assertIn(self.original, [p.read_bytes() for p in backups])

    def test_validation_failure_has_zero_config_writes(self):
        self.ipc.fail_validation = lambda path: True
        with self.assertRaisesRegex(DisplayError, "invalid configuration"):
            self.backend._persist(self.layout)
        self.assertEqual(self.backend.config.read_bytes(), self.original)
        self.assertFalse(self.backend.managed.exists())
        self.assertFalse((self.backend.directory / "backups").exists())

    @patch("backend.time.sleep")
    def test_post_write_failure_restores_both_files(self, _):
        self.backend.managed.write_bytes(b"// prior managed config\n")
        self.ipc.fail_validation = lambda path: path == self.backend.config
        with self.assertRaises(DisplayError):
            self.backend._persist(self.layout)
        self.assertEqual(self.backend.config.read_bytes(), self.original)
        self.assertEqual(self.backend.managed.read_bytes(), b"// prior managed config\n")

    def test_preview_no_persistence_and_revert_idempotent(self):
        token = self.backend.begin(self.layout)
        self.assertTrue(self.backend.pending.exists())
        self.assertEqual(self.backend.config.read_bytes(), self.original)
        self.assertFalse(self.backend.database.exists())
        self.backend.revert(token)
        self.backend.revert(token)
        self.assertFalse(self.backend.pending.exists())

    @patch("backend.time.sleep")
    def test_keep_persists_and_saves_machine_lid_identity(self, _):
        with patch("backend.lid_state", return_value="closed"):
            token = self.backend.begin(self.layout)
            saved = self.backend.keep(token, "Work")
        self.assertEqual(saved["lid"], "closed")
        self.assertEqual(saved["machine_id"], self.backend.machine)
        self.assertFalse(self.backend.pending.exists())
        self.assertEqual(self.backend._db()["profiles"][0]["name"], "Work")

    def test_foreign_machine_profile_guard(self):
        self.backend.directory.mkdir()
        self.backend.database.write_text(json.dumps({"version": 1, "machine_id": "another", "profiles": []}))
        with self.assertRaisesRegex(DisplayError, "different computer"):
            self.backend.get_auto()

    def test_invalid_profile_name_has_no_side_effects(self):
        with self.assertRaises(DisplayError):
            self.backend.save_profile("   ")
        self.assertEqual(self.backend.config.read_bytes(), self.original)
        self.assertFalse(self.backend.managed.exists())

    def test_keep_failure_reverts_immediately(self):
        token = self.backend.begin(self.layout)
        self.ipc.fail_validation = lambda path: True
        with self.assertRaises(DisplayError):
            self.backend.keep(token, "Work")
        self.assertFalse(self.backend.pending.exists())
        self.assertEqual(self.backend.config.read_bytes(), self.original)

    def test_expired_keep_rolls_back(self):
        token = self.backend.begin(self.layout)
        with patch("backend.time.time", return_value=token["deadline"] + 1):
            with self.assertRaisesRegex(DisplayError, "timed out"):
                self.backend.keep(token, "Work")
        self.assertFalse(self.backend.pending.exists())
        self.assertFalse(self.backend.database.exists())

    @patch("backend.time.sleep")
    def test_watchdog_rolls_back_files_after_interrupted_keep(self, _):
        token = self.backend.begin(self.layout)
        transaction = self.backend._transaction(token)
        self.backend._persist(self.layout, transaction)
        self.assertNotEqual(self.backend.config.read_bytes(), self.original)
        with patch("backend.time.time", return_value=token["deadline"] + 1):
            self.backend.watchdog(token["id"])
        self.assertEqual(self.backend.config.read_bytes(), self.original)
        self.assertFalse(self.backend.managed.exists())
        self.assertFalse(self.backend.pending.exists())

    def test_apply_enables_before_disabling_and_negative_positions(self):
        rows = [output("DP-1"), output("eDP-1", "internal", -1920)]
        rows[0]["enabled"] = False
        self.backend._apply(rows)
        actions = [call[4:] for call in self.ipc.calls if call[1:3] == ["msg", "output"]]
        self.assertEqual(actions[0], ["on"])
        self.assertEqual(actions[-1], ["off"])
        self.assertIn(["position", "set", "--", "-1920", "0"], actions)

    @patch("backend.lid_state", return_value="open")
    def test_watcher_does_not_reapply_after_revert_or_timeout(self, _):
        self.backend._store_profile("Work", self.layout, "open")
        previous = self.backend.watch_once()
        token = self.backend.begin(self.layout)
        self.assertEqual(self.backend.watch_once(previous), previous)
        self.backend.revert(token)
        with patch.object(self.backend, "_apply") as apply:
            self.backend.watch_once(previous)
            apply.assert_not_called()
        token = self.backend.begin(self.layout)
        with patch("backend.time.time", return_value=token["deadline"] + 1):
            previous = self.backend.watch_once(previous)
        with patch.object(self.backend, "_apply") as apply:
            self.backend.watch_once(previous)
            apply.assert_not_called()

    @patch("backend.lid_state", return_value="open")
    def test_watcher_startup_during_preview_does_not_replay_after_revert(self, _):
        self.backend._store_profile("Work", self.layout, "open")
        token = self.backend.begin(self.layout)
        with patch.object(self.backend, "_apply") as apply:
            previous = self.backend.watch_once()
            apply.assert_not_called()
        self.backend.revert(token)
        with patch.object(self.backend, "_apply") as apply:
            self.backend.watch_once(previous)
            apply.assert_not_called()

    @patch("backend.lid_state", return_value="open")
    def test_profile_crud_does_not_trigger_automatic_apply(self, _):
        self.backend._store_profile("Work", self.layout, "open")
        previous = self.backend.watch_once()
        with patch.object(self.backend, "_apply") as apply:
            saved = self.backend._store_profile("Another", self.layout, "open")
            previous = self.backend.watch_once(previous)
            self.backend.delete_profile(saved["id"])
            previous = self.backend.watch_once(previous)
            apply.assert_not_called()
            self.backend.set_auto(False)
            previous = self.backend.watch_once(previous)
            self.backend.set_auto(True)
            self.backend.watch_once(previous)
            apply.assert_called_once()

    def test_detached_watchdog_survives_owner_process_exit(self):
        # A fake executable gives the independently spawned watchdog real CLI
        # boundaries without letting this test contact the compositor.
        binary_dir = self.home / "bin"
        binary_dir.mkdir()
        state_file = self.home / "fake-outputs.json"
        state_file.write_text(json.dumps({"DP-1": raw_output()}))
        executable = binary_dir / "niri"
        executable.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ["DISPLAY_DESK_TEST_OUTPUTS"])
data = json.loads(p.read_text())
args = sys.argv[1:]
if args == ["msg", "--json", "outputs"]:
    print(json.dumps(data))
elif args[:2] == ["msg", "output"]:
    row = data[args[2]]
    if args[3] == "scale":
        row["logical"]["scale"] = float(args[4])
    p.write_text(json.dumps(data))
''')
        executable.chmod(0o700)
        environment = os.environ.copy()
        environment.update(PATH=str(binary_dir) + os.pathsep + environment["PATH"],
                           DISPLAY_DESK_TEST_OUTPUTS=str(state_file))
        program = '''from backend import Backend
import os
b = Backend(os.environ["DISPLAY_DESK_TEST_CONFIG"], os.environ["DISPLAY_DESK_TEST_STATE"])
rows = b.query()
rows[0]["scale"] = 1.25
b.begin(rows, timeout=5)
os._exit(0)
'''
        environment.update(DISPLAY_DESK_TEST_CONFIG=str(self.backend.config_home),
                           DISPLAY_DESK_TEST_STATE=str(self.backend.state.parent))
        owner = subprocess.run([sys.executable, "-c", program], cwd=Path(__file__).parent,
                               env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(owner.returncode, 0, owner.stderr)
        self.assertAlmostEqual(json.loads(state_file.read_text())["DP-1"]["logical"]["scale"], 1.25)
        deadline = time.monotonic() + 10
        while self.backend.pending.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertFalse(self.backend.pending.exists(), "detached watchdog did not finish")
        self.assertAlmostEqual(json.loads(state_file.read_text())["DP-1"]["logical"]["scale"], 1.0)


if __name__ == "__main__":
    unittest.main()
