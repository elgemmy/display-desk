"""Inspect an extracted package without installing it or changing user files."""
import hashlib
import gzip
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for executable in ("dpkg-deb", "desktop-file-validate"):
            if not shutil.which(executable):
                raise RuntimeError(f"Required packaging check is missing: {executable}")
        cls.temporary = tempfile.TemporaryDirectory(prefix="display-desk-package-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.work = Path(cls.temporary.name)
        cls.env = dict(os.environ, SOURCE_DATE_EPOCH="1794182400")
        subprocess.run([
            sys.executable, str(ROOT / "scripts/build_deb.py"),
            "--output-dir", str(cls.work / "first"),
        ], check=True, env=cls.env, stdout=subprocess.DEVNULL)
        cls.deb = next((cls.work / "first").glob("*.deb"))
        cls.extracted = cls.work / "extracted"
        cls.control = cls.work / "control"
        subprocess.run(["dpkg-deb", "--extract", str(cls.deb), str(cls.extracted)], check=True)
        subprocess.run(["dpkg-deb", "--control", str(cls.deb), str(cls.control)], check=True)

    def test_payload_allowlist_and_modes(self):
        expected = {
            "usr/bin/display-desk",
            "usr/share/display-desk/app.py",
            "usr/share/display-desk/backend.py",
            "usr/share/display-desk/VERSION",
            "usr/share/icons/hicolor/scalable/apps/com.elgemmy.DisplayDesk.svg",
            "usr/share/applications/com.elgemmy.DisplayDesk.desktop",
            "etc/xdg/autostart/com.elgemmy.DisplayDesk.Restore.desktop",
            "usr/share/doc/display-desk/copyright",
            "usr/share/doc/display-desk/README.md",
            "usr/share/doc/display-desk/changelog.gz",
        }
        actual = {str(p.relative_to(self.extracted))
                  for p in self.extracted.rglob("*") if p.is_file()}
        self.assertEqual(actual, expected)
        for name in expected:
            mode = (self.extracted / name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o755 if name == "usr/bin/display-desk" else 0o644)
        self.assertEqual({p.name for p in self.control.iterdir()}, {"control"})
        for filename in ("app.py", "backend.py"):
            self.assertEqual((ROOT / filename).read_bytes(),
                             (self.extracted / "usr/share/display-desk" / filename).read_bytes())

    def test_metadata_and_desktop_entries(self):
        metadata = subprocess.check_output([
            "dpkg-deb", "--field", str(self.deb)
        ], text=True)
        self.assertIn("Architecture: all\n", metadata)
        self.assertIn("gir1.2-gtk-4.0 (>= 4.12)", metadata)
        self.assertIn("gir1.2-adw-1 (>= 1.5)", metadata)
        self.assertIn("Recommends: niri (>= 25.11)\n", metadata)
        for filename in (
            "usr/share/applications/com.elgemmy.DisplayDesk.desktop",
            "etc/xdg/autostart/com.elgemmy.DisplayDesk.Restore.desktop",
        ):
            subprocess.run(["desktop-file-validate", str(self.extracted / filename)], check=True)
        listing = subprocess.check_output(["dpkg-deb", "--contents", str(self.deb)], text=True)
        self.assertTrue(all(" root/root " in line for line in listing.splitlines()))
        with gzip.open(self.extracted / "usr/share/doc/display-desk/changelog.gz", "rt") as changelog:
            self.assertIn("Mon, 09 Nov 2026 00:00:00 +0000", changelog.read())

    def test_watch_exits_outside_niri_without_writing_user_state(self):
        home = self.work / "empty-home"
        home.mkdir(exist_ok=True)
        environment = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
                           XDG_DATA_HOME=str(home / "data"), XDG_STATE_HOME=str(home / "state"))
        environment.pop("NIRI_SOCKET", None)
        result = subprocess.run([
            sys.executable, str(self.extracted / "usr/bin/display-desk"), "--watch"
        ], env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(list(home.iterdir()), [])

    def test_launcher_dispatches_without_importing_gtk(self):
        launcher = self.extracted / "usr/bin/display-desk"
        for arguments, script in (([], "app.py"), (["--watch"], "backend.py")):
            with self.subTest(arguments=arguments), \
                    patch.dict(os.environ, {"NIRI_SOCKET": "/test/niri.sock"}), \
                    patch.object(sys, "argv", [str(launcher), *arguments]), \
                    patch("os.execv") as execute:
                runpy.run_path(str(launcher), run_name="__main__")
                execute.assert_called_once_with("/usr/bin/python3", [
                    "/usr/bin/python3", "/usr/share/display-desk/" + script, *arguments
                ])

    def test_version_from_extracted_installation(self):
        # Relocate just the fixed system payload path for an unprivileged test.
        launcher = self.work / "relocated-launcher"
        payload = self.extracted / "usr/share/display-desk"
        source = (self.extracted / "usr/bin/display-desk").read_text()
        launcher.write_text(source.replace('Path("/usr/share/display-desk")', f"Path({str(payload)!r})"))
        environment = dict(os.environ)
        environment.pop("NIRI_SOCKET", None)
        result = subprocess.run([sys.executable, str(launcher), "--version"],
                                env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Display Desk " + (ROOT / "VERSION").read_text().strip() + "\n")
        self.assertEqual(result.stderr, "")

    def test_reproducible_archive(self):
        subprocess.run([
            sys.executable, str(ROOT / "scripts/build_deb.py"),
            "--output-dir", str(self.work / "second"),
        ], check=True, env=self.env, stdout=subprocess.DEVNULL)
        second = next((self.work / "second").glob("*.deb"))
        self.assertEqual(hashlib.sha256(self.deb.read_bytes()).digest(),
                         hashlib.sha256(second.read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
