#!/usr/bin/env python3
"""Build a small architecture-independent Debian binary package, without root."""
import argparse
from email.utils import formatdate
import gzip
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# A fixed fallback keeps source archives reproducible without Git metadata.
DEFAULT_EPOCH = 1794182400


def build(output_dir):
    version = (ROOT / "VERSION").read_text().strip()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+~]*", version):
        raise ValueError("VERSION must be a plain upstream Debian version")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH))
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be nonnegative")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"display-desk_{version}_all.deb"
    with tempfile.TemporaryDirectory(prefix="display-desk-deb-") as temporary:
        package = Path(temporary) / "display-desk"
        # Deliberate allowlist: never package review captures or local state.
        files = {
            "app.py": "usr/share/display-desk/app.py",
            "backend.py": "usr/share/display-desk/backend.py",
            "VERSION": "usr/share/display-desk/VERSION",
            "README.md": "usr/share/doc/display-desk/README.md",
            "packaging/display-desk": "usr/bin/display-desk",
            "icon.svg": "usr/share/icons/hicolor/scalable/apps/com.elgemmy.DisplayDesk.svg",
            "packaging/com.elgemmy.DisplayDesk.desktop":
                "usr/share/applications/com.elgemmy.DisplayDesk.desktop",
            "packaging/com.elgemmy.DisplayDesk.Restore.desktop":
                "etc/xdg/autostart/com.elgemmy.DisplayDesk.Restore.desktop",
        }
        for source, destination in files.items():
            target = package / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, target)
        (package / "usr/bin/display-desk").chmod(0o755)
        metadata = package / "DEBIAN"
        metadata.mkdir()
        (metadata / "control").write_text(f"""Package: display-desk
Version: {version}
Section: x11
Priority: optional
Architecture: all
Maintainer: elgemmy <94416115+elgemmy@users.noreply.github.com>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.5)
Recommends: niri (>= 25.11)
Homepage: https://github.com/elgemmy/display-desk
Description: Native display profiles for the Niri compositor
 Arrange displays, save desk profiles, and restore them when monitors reconnect.
 Requires a running Niri 25.11 or later session. The restore watcher exits
 quietly outside a Niri session.
""", encoding="utf-8")
        documentation = package / "usr/share/doc/display-desk"
        documentation.mkdir(parents=True, exist_ok=True)
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        (documentation / "copyright").write_text(
            "Display Desk\nSource: https://github.com/elgemmy/display-desk\n\n" + license_text,
            encoding="utf-8",
        )
        changelog_date = formatdate(epoch, localtime=False, usegmt=False).replace("-0000", "+0000")
        changelog = f"""display-desk ({version}) unstable; urgency=medium

  * Display Desk release {version}.

 -- elgemmy <94416115+elgemmy@users.noreply.github.com>  {changelog_date}
"""
        with (documentation / "changelog.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                compressed.write(changelog.encode("utf-8"))
        for item in sorted(package.rglob("*")):
            if item.is_dir():
                item.chmod(0o755)
            elif item != package / "usr/bin/display-desk":
                item.chmod(0o644)
            os.utime(item, (epoch, epoch))
        package.chmod(0o755)
        os.utime(package, (epoch, epoch))
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
        subprocess.run([
            "dpkg-deb", "--build", "--root-owner-group", "--uniform-compression",
            "-Zgzip", "-z9", str(package), str(output),
        ], check=True, env=env)
    print(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.output_dir.resolve())
