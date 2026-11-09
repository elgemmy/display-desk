#!/usr/bin/env python3
"""Small, stdlib-only Niri display backend.

IPC edits are temporary until Keep. A detached child owns the timeout rollback;
neither closing the window nor killing the GTK process cancels that protection.
Profile matching is machine + exact connected identity multiset + lid state.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

TRANSFORMS = ("normal", "90", "180", "270", "flipped", "flipped-90", "flipped-180", "flipped-270")


def xdg_directory(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path(fallback)


class DisplayError(RuntimeError):
    pass


def identity(output):
    """Internal panels follow the laptop, external panels follow EDID identity."""
    if output.get("internal") or re.match(r"^(eDP|LVDS|DSI)-", output["connector"], re.I):
        return ("internal",)
    return tuple(str(output.get(k) or "Unknown").casefold() for k in ("make", "model", "serial"))


def mode_string(mode):
    return f'{int(mode["width"])}x{int(mode["height"])}@{int(mode["refresh_rate"])/1000:.3f}'


def logical_size(output):
    mode = output["mode"]
    w, h = mode["width"], mode["height"]
    if output.get("transform", "normal") in ("90", "270", "flipped-90", "flipped-270"):
        w, h = h, w
    # Match Niri's IPC logical_output(): width/height are f64 -> u32 casts.
    # https://github.com/niri-wm/niri/blob/main/src/utils/mod.rs
    return max(1, int(w / output["scale"])), max(1, int(h / output["scale"]))


def normalize_outputs(raw):
    result = []
    for connector, value in sorted(raw.items()):
        logical = value.get("logical") or {}
        modes = value.get("modes") or []
        current = value.get("current_mode")
        mode = modes[current] if current is not None and current < len(modes) else next(
            (m for m in modes if m.get("is_preferred")), modes[0] if modes else None)
        if mode is None:
            continue
        transform = logical.get("transform", "Normal")
        transform = {"Normal": "normal", "Flipped": "flipped", "Flipped90": "flipped-90",
                     "Flipped180": "flipped-180", "Flipped270": "flipped-270"}.get(transform, transform)
        row = dict(connector=connector, make=value.get("make", "Unknown"), model=value.get("model", "Unknown"),
                   serial=value.get("serial"), internal=bool(re.match(r"^(eDP|LVDS|DSI)-", connector, re.I)),
                   modes=copy.deepcopy(modes), mode=copy.deepcopy(mode), enabled=current is not None,
                   scale=logical.get("scale", 1.0), transform=transform,
                   x=logical.get("x", 0), y=logical.get("y", 0), width=mode["width"], height=mode["height"])
        row["id"] = hashlib.sha256(json.dumps(identity(row)).encode()).hexdigest()[:16]
        row["label"] = "Built-in display" if row["internal"] else " ".join(str(row[k]) for k in ("make", "model"))
        row["logical_width"], row["logical_height"] = logical_size(row)
        result.append(row)
    return result


def match_layout(saved, current):
    """Return saved settings on today's connectors, or None for a different desk.

    Identical EDIDs cannot be safely distinguished; only that identity group uses
    connector names. A singleton with a changed connector still matches.
    """
    if len(saved) != len(current):
        return None
    matched = []
    for old in saved:
        group_old = [x for x in saved if identity(x) == identity(old)]
        group_now = [x for x in current if identity(x) == identity(old)]
        if len(group_old) != len(group_now):
            return None
        if len(group_now) == 1 and identity(old) != ("unknown", "unknown", "unknown"):
            now = group_now[0]
        else:
            now = next((x for x in group_now if x["connector"] == old["connector"]), None)
        if now is None:
            return None
        row = copy.deepcopy(now)
        for key in ("enabled", "mode", "scale", "transform", "x", "y"):
            row[key] = copy.deepcopy(old[key])
        row["width"], row["height"] = row["mode"]["width"], row["mode"]["height"]
        row["logical_width"], row["logical_height"] = logical_size(row)
        matched.append(row)
    if len({x["connector"] for x in matched}) != len(current):
        return None
    return matched


def validate_layout(layout, current):
    if not layout or {x["connector"] for x in layout} != {x["connector"] for x in current} or len(layout) != len(current):
        raise DisplayError("The connected displays changed. Refresh before applying.")
    if not any(x.get("enabled") for x in layout):
        raise DisplayError("At least one display must remain enabled.")
    available = {x["connector"]: x for x in current}
    active = []
    result = copy.deepcopy(layout)
    for row in result:
        now = available[row["connector"]]
        if identity(row) != identity(now):
            raise DisplayError("A display changed identity. Refresh before applying.")
        try:
            row["scale"] = float(row["scale"])
            if not math.isfinite(row["scale"]) or not .5 <= row["scale"] <= 4:
                raise ValueError()
            if row["transform"] not in TRANSFORMS:
                raise ValueError()
            for axis in ("x", "y"):
                number = row[axis]
                if int(number) != number or abs(number) > 100000:
                    raise ValueError()
                row[axis] = int(number)
            if mode_string(row["mode"]) not in {mode_string(m) for m in now["modes"]}:
                raise ValueError()
        except (ValueError, KeyError, TypeError, OverflowError):
            raise DisplayError(f'Invalid mode, scale, transform or position for {row["connector"]}.') from None
        row["logical_width"], row["logical_height"] = logical_size(row)
        row["width"], row["height"] = row["mode"]["width"], row["mode"]["height"]
        if row["enabled"]:
            active.append(row)
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if (max(a["x"], b["x"]) < min(a["x"] + a["logical_width"], b["x"] + b["logical_width"])
                    and max(a["y"], b["y"]) < min(a["y"] + a["logical_height"], b["y"] + b["logical_height"])):
                raise DisplayError(f'Displays {a["connector"]} and {b["connector"]} overlap.')
    return result


def lid_state():
    for filename in Path("/proc/acpi/button/lid").glob("*/state"):
        try:
            value = filename.read_text().lower()
            if "closed" in value:
                return "closed"
            if "open" in value:
                return "open"
        except OSError:
            pass
    return "unknown"


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data if isinstance(data, bytes) else data.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config_text(layout):
    lines = ["// Managed by Display Desk. Edit display profiles in the app.",
             "// First matching Niri output block wins; the include is first in config.kdl."]
    for row in layout:
        duplicates = sum(identity(x) == identity(row) for x in layout) > 1
        if not row["internal"] and (duplicates or identity(row) == ("unknown", "unknown", "unknown")):
            # A connector could later host a different monitor. Ambiguous EDIDs
            # therefore belong only to an exact-set runtime profile.
            continue
        if row["internal"]:
            target = row["connector"]
        else:
            target = " ".join(str(row.get(k) or "Unknown") for k in ("make", "model", "serial"))
        lines.append(f"output {json.dumps(target, ensure_ascii=False)} {{")
        # Never persist a disabled internal panel: external-only/lid profiles are
        # runtime settings and the watcher restores them after login/hotplug.
        if not row["enabled"] and not row["internal"]:
            lines.append("    off")
        lines.extend((f'    mode "{mode_string(row["mode"])}"', f'    scale {row["scale"]:g}',
                      f'    transform "{row["transform"]}"', f'    position x={row["x"]} y={row["y"]}', "}"))
    return "\n".join(lines) + "\n"


class Backend:
    def __init__(self, config_home=None, state_home=None, runner=None, watchdog=True):
        self.config_home = Path(config_home) if config_home else xdg_directory("XDG_CONFIG_HOME", Path.home() / ".config")
        self.directory = self.config_home / "display-desk"
        self.config = self.config_home / "niri/config.kdl"
        self.managed = self.config_home / "niri/display-desk.kdl"
        self.database = self.directory / "profiles.json"
        self.state = (Path(state_home) if state_home else xdg_directory("XDG_STATE_HOME", Path.home() / ".local/state")) / "display-desk"
        self.pending = self.state / "pending.json"
        self.runner = runner or subprocess.run
        self.use_watchdog = watchdog
        self.machine = Path("/etc/machine-id").read_text().strip()

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _run(self, args):
        try:
            process = self.runner(["niri", *args], text=True, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DisplayError(str(exc)) from exc
        if process.returncode:
            raise DisplayError((process.stderr or process.stdout or "Niri command failed").strip())
        return process.stdout

    def query(self):
        try:
            return normalize_outputs(json.loads(self._run(["msg", "--json", "outputs"])))
        except (ValueError, TypeError, KeyError) as exc:
            raise DisplayError(f"Cannot read Niri outputs: {exc}") from exc

    def _db(self):
        if not self.database.exists():
            return {"version": 1, "machine_id": self.machine, "auto": True, "profiles": []}
        try:
            data = json.loads(self.database.read_text())
            if data.get("machine_id") != self.machine:
                raise DisplayError("Profiles belong to a different computer. Move profiles.json aside to start fresh.")
            if data.get("version") != 1 or not isinstance(data.get("profiles"), list):
                raise ValueError("unsupported profile format")
            return data
        except (OSError, ValueError) as exc:
            raise DisplayError(f"Cannot read display profiles: {exc}") from exc

    def _save_db(self, data):
        atomic_write(self.database, json.dumps(data, indent=2) + "\n")

    def profiles(self):
        rows = copy.deepcopy(self._db()["profiles"])
        current = self.query()
        lid = lid_state()
        for row in rows:
            row["matches"] = row["lid"] == lid and match_layout(row["outputs"], current) is not None
        return rows

    def get_auto(self):
        return bool(self._db().get("auto", True))

    def set_auto(self, enabled):
        with self.locked():
            data = self._db()
            data["auto"] = bool(enabled)
            self._save_db(data)

    def delete_profile(self, profile_id):
        with self.locked():
            data = self._db()
            data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
            self._save_db(data)

    def _store_profile(self, name, layout, lid=None):
        name = name.strip()
        if not name or len(name) > 100:
            raise DisplayError("Enter a profile name of 1–100 characters.")
        data = self._db()
        old = next((p for p in data["profiles"] if p["name"] == name), None)
        row = {"id": old["id"] if old else uuid.uuid4().hex, "name": name, "machine_id": self.machine,
               "lid": lid or lid_state(), "outputs": copy.deepcopy(layout), "saved_at": time.time()}
        data["profiles"] = [p for p in data["profiles"] if p["id"] != row["id"]] + [row]
        self._save_db(data)
        return row

    def save_profile(self, name, layout=None):
        with self.locked():
            if not name.strip() or len(name.strip()) > 100:
                raise DisplayError("Enter a profile name of 1–100 characters.")
            if self.pending.exists():
                raise DisplayError("Keep or revert the pending display change first.")
            current = self.query()
            layout = validate_layout(layout or current, current)
            # Saving an already-applied layout also makes it persistent.
            self._persist(layout)
            return self._store_profile(name, layout)

    def _apply(self, layout):
        # Enable before disabling, so a switch never intentionally goes all dark.
        for row in layout:
            if row["enabled"]:
                self._run(["msg", "output", row["connector"], "on"])
        for row in layout:
            if not row["enabled"]:
                continue
            for action, value in (("mode", mode_string(row["mode"])), ("scale", str(row["scale"])),
                                  ("transform", row["transform"])):
                self._run(["msg", "output", row["connector"], action, value])
        for row in layout:
            if row["enabled"]:
                self._run(["msg", "output", row["connector"], "position", "set", "--", str(row["x"]), str(row["y"])])
        for row in layout:
            if not row["enabled"]:
                self._run(["msg", "output", row["connector"], "off"])

    def begin(self, layout, timeout=20):
        with self.locked():
            if self.pending.exists():
                raise DisplayError("Another display change is awaiting confirmation.")
            before = self.query()
            layout = validate_layout(layout, before)
            token = {"id": uuid.uuid4().hex, "deadline": time.time() + min(60, max(5, timeout)),
                     "before": before, "after": layout, "lid": lid_state(), "owner": os.getpid()}
            atomic_write(self.pending, json.dumps(token))
            try:
                if self.use_watchdog:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watchdog", token["id"],
                                      "--config-home", str(self.config_home), "--state-home", str(self.state.parent)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True)
                self._apply(layout)
            except Exception:
                self._rollback(token)
                raise
            return {"id": token["id"], "deadline": token["deadline"]}

    def _transaction(self, token):
        token_id = token["id"] if isinstance(token, dict) else token
        if not self.pending.exists():
            raise DisplayError("The preview already ended; refresh to see the restored layout.")
        saved = json.loads(self.pending.read_text())
        if saved["id"] != token_id:
            raise DisplayError("This preview is no longer active.")
        return saved

    def _rollback(self, transaction):
        persistence = transaction.get("persistence")
        if persistence:
            # Only replace bytes we wrote. Concurrent edits to the main config
            # remain intact; the owned managed file can still be restored.
            main_now = self.config.read_bytes() if self.config.exists() else b""
            if hashlib.sha256(main_now).hexdigest() == persistence["main_after"]:
                atomic_write(self.config, base64.b64decode(persistence["main_before"]))
            if self.managed.exists() and hashlib.sha256(self.managed.read_bytes()).hexdigest() == persistence["managed_after"]:
                if persistence["managed_before"] is None:
                    self.managed.unlink(missing_ok=True)
                else:
                    atomic_write(self.managed, base64.b64decode(persistence["managed_before"]))
            self._run(["msg", "action", "load-config-file"])
        current = self.query()
        restored = []
        for now in current:
            old = next((x for x in transaction["before"] if x["connector"] == now["connector"] and identity(x) == identity(now)), None)
            restored.append(copy.deepcopy(old or now))
        if restored and not any(x["enabled"] for x in restored):
            fallback = next((x for x in restored if x["internal"]), restored[0])
            fallback["enabled"] = True
            fallback["x"], fallback["y"] = 0, 0
        self._apply(restored)
        self.pending.unlink(missing_ok=True)

    def revert(self, token):
        with self.locked():
            if not self.pending.exists():
                return
            self._rollback(self._transaction(token))

    def keep(self, token, profile_name=None):
        with self.locked():
            saved = self._transaction(token)
            if time.time() >= saved["deadline"]:
                self._rollback(saved)
                raise DisplayError("The confirmation timed out. The previous layout was restored.")
            if profile_name is not None and (not profile_name.strip() or len(profile_name.strip()) > 100):
                raise DisplayError("Enter a profile name of 1–100 characters.")
            current = self.query()
            layout = validate_layout(saved["after"], current)
            try:
                self._persist(layout, saved)
                row = self._store_profile(profile_name, layout, saved["lid"]) if profile_name else None
            except Exception:
                # Persistence can fail after a successful preview. Restore now;
                # the detached watchdog remains a fallback if restoration fails.
                self._rollback(self._transaction(token))
                raise
            self.pending.unlink(missing_ok=True)
            return row

    def apply_profile(self, profile_id):
        profile = next((p for p in self._db()["profiles"] if p["id"] == profile_id), None)
        if not profile:
            raise DisplayError("Profile no longer exists.")
        layout = match_layout(profile["outputs"], self.query())
        if layout is None:
            raise DisplayError("This profile needs a different set of connected displays.")
        return self.begin(layout)

    def _validate_config(self, path):
        self._run(["validate", "--config", str(path)])

    def _persist(self, layout, transaction=None):
        """Validate a staging include and preserve every original config byte.

        First-match precedence is defined in niri-config/src/output.rs::Outputs.find.
        The prepended include therefore overrides only these managed outputs.
        """
        if not self.config.exists():
            raise DisplayError(f"Niri config does not exist: {self.config}")
        original = self.config.read_bytes()
        old_managed = self.managed.read_bytes() if self.managed.exists() else None
        include = f'include {json.dumps(str(self.managed))}\n'.encode()
        # An existing managed include belongs at the front; refuse surprising
        # duplicate/commented includes rather than rewriting arbitrary KDL.
        if original.startswith(include):
            updated = original
        elif re.search(rb'(?m)^\s*include\s+"[^"\n]*display-desk\.kdl"', original):
            raise DisplayError("Move the existing Display Desk include to the first line of config.kdl.")
        else:
            updated = include + original
        generated = config_text(layout).encode()
        with tempfile.TemporaryDirectory(prefix=".display-desk-", dir=self.config.parent) as staging:
            staged_managed = Path(staging) / "outputs.kdl"
            staged_main = self.config.parent / f".display-desk-validate-{uuid.uuid4().hex}.kdl"
            atomic_write(staged_managed, generated)
            # Main candidate stays beside the actual config so its other relative
            # includes resolve exactly as before.
            candidate = updated.replace(include, f'include {json.dumps(str(staged_managed))}\n'.encode(), 1)
            try:
                atomic_write(staged_main, candidate)
                self._validate_config(staged_main)
            finally:
                staged_main.unlink(missing_ok=True)
        backup_dir = self.directory / "backups" / f'{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:8]}'
        backup_dir.mkdir(parents=True, mode=0o700)
        atomic_write(backup_dir / "config.kdl", original)
        if old_managed is not None:
            atomic_write(backup_dir / "display-desk.kdl", old_managed)
        if self.config.read_bytes() != original:
            raise DisplayError("Niri config changed while saving. Try again.")
        if transaction is not None:
            transaction["persistence"] = {
                "main_before": base64.b64encode(original).decode(),
                "managed_before": base64.b64encode(old_managed).decode() if old_managed is not None else None,
                "main_after": hashlib.sha256(updated).hexdigest(),
                "managed_after": hashlib.sha256(generated).hexdigest(),
            }
            atomic_write(self.pending, json.dumps(transaction))
        try:
            # Managed first: the old main config either ignores it or already
            # points at a validated file. The main file replacement is atomic.
            atomic_write(self.managed, generated)
            if updated != original:
                atomic_write(self.config, updated)
            self._validate_config(self.config)
            self._run(["msg", "action", "load-config-file"])
            # Niri may reset temporary output overrides on config reload.
            time.sleep(.15)
            self._apply(layout)
        except Exception:
            if old_managed is None:
                self.managed.unlink(missing_ok=True)
            else:
                atomic_write(self.managed, old_managed)
            atomic_write(self.config, original)
            try:
                self._run(["msg", "action", "load-config-file"])
            except DisplayError:
                pass
            raise

    def watchdog(self, token_id):
        while True:
            try:
                with self.locked():
                    if not self.pending.exists():
                        return
                    pending = json.loads(self.pending.read_text())
                    if pending["id"] != token_id:
                        return
                    if time.time() >= pending["deadline"]:
                        self._rollback(pending)
                        return
            except (DisplayError, OSError, ValueError) as exc:
                # A compositor restart can temporarily break IPC. Keep retrying
                # rather than silently dropping the only recovery record.
                print(f"Display Desk rollback: {exc}", file=sys.stderr, flush=True)
            time.sleep(.5)

    def watch(self, interval=3):
        """Topology/lid polling is intentionally independent of GTK and D-Bus."""
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "watcher.lock").open("a") as daemon_lock:
            try:
                fcntl.flock(daemon_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            previous = None
            while True:
                try:
                    previous = self.watch_once(previous)
                except (DisplayError, OSError, ValueError) as exc:
                    print(f"Display Desk watcher: {exc}", file=sys.stderr, flush=True)
                time.sleep(interval)

    @staticmethod
    def _watch_signature(outputs, lid, auto):
        def connector_key(row):
            ambiguous = identity(row) == ("unknown", "unknown", "unknown") or sum(identity(x) == identity(row) for x in outputs) > 1
            return row["connector"] if ambiguous else ""
        return (tuple(sorted((identity(x), connector_key(x)) for x in outputs)), lid, auto)

    def watch_once(self, previous=None):
        """One watcher iteration; return the topology/lid/auto state observed.

        Profile CRUD and preview completion are deliberately absent from that
        state: neither may unexpectedly replay a profile over a manual revert.
        """
        with self.locked():
            if self.pending.exists():
                pending = json.loads(self.pending.read_text())
                if time.time() >= pending["deadline"]:
                    self._rollback(pending)
                if previous is None:
                    return self._watch_signature(self.query(), lid_state(), self.get_auto())
                return previous
            outputs, lid = self.query(), lid_state()
            if outputs and not any(x["enabled"] for x in outputs):
                fallback = next((x for x in outputs if x["internal"]), outputs[0])
                fallback["enabled"] = True
                fallback["x"], fallback["y"] = 0, 0
                self._apply([fallback])
                outputs = self.query()
            data = self._db()
            signature = self._watch_signature(outputs, lid, data.get("auto", True))
            if signature != previous and data.get("auto", True):
                for profile in sorted(data["profiles"], key=lambda p: p["saved_at"], reverse=True):
                    if profile["lid"] != lid:
                        continue
                    layout = match_layout(profile["outputs"], outputs)
                    if layout is not None:
                        self._apply(validate_layout(layout, outputs))
                        break
            return signature


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("query", "watch", "watchdog", "profiles"))
    parser.add_argument("token", nargs="?")
    parser.add_argument("--config-home")
    parser.add_argument("--state-home")
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--watch":
        arguments[0] = "watch"
    args = parser.parse_args(arguments)
    backend = Backend(args.config_home, args.state_home)
    try:
        if args.command == "watch":
            backend.watch()
        elif args.command == "watchdog":
            backend.watchdog(args.token)
        else:
            print(json.dumps(getattr(backend, args.command)(), indent=2))
    except (DisplayError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
