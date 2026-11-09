#!/usr/bin/python3
"""Install or remove the current user's source installation; no sudo or pip."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

APP_ID = 'com.elgemmy.DisplayDesk'
PAYLOAD = ('app.py', 'backend.py', 'README.md', 'VERSION', 'LICENSE')
MARKER = '.display-desk-user-install'


def xdg_directory(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else fallback


def desktop_exec(executable):
    """Quote a literal path for both Desktop Entry escaping and Exec parsing."""
    value = str(executable).replace('\\', '\\\\\\\\').replace('%', '%%')
    for character in ('"', '$', '`'):
        value = value.replace(character, '\\\\' + character)
    return '"' + value + '"'


def desktop_directory(home):
    if shutil.which('xdg-user-dir'):
        result = subprocess.run(['xdg-user-dir', 'DESKTOP'], text=True, capture_output=True)
        value = result.stdout.strip()
        if result.returncode == 0 and value and Path(value).is_absolute():
            return Path(value)
    return home / 'Desktop'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true', help='Remove only the user-installed application; retain profiles and Niri settings')
    parser.add_argument('--no-desktop-shortcut', action='store_true', help='Do not place a launcher on the desktop')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run this user installer without sudo. Use the .deb package for a system installation.')
    source = Path(__file__).resolve().parent
    home = Path.home()
    data = xdg_directory('XDG_DATA_HOME', home / '.local/share')
    config = xdg_directory('XDG_CONFIG_HOME', home / '.config')
    app = data / 'display-desk'
    bindir = home / '.local/bin'
    launcher = bindir / 'display-desk'
    entry = data / f'applications/{APP_ID}.desktop'
    autostart = config / f'autostart/{APP_ID}.Restore.desktop'
    icon = data / f'icons/hicolor/scalable/apps/{APP_ID}.svg'
    desktop_dir = desktop_directory(home)
    shortcut = desktop_dir / 'Display Desk.desktop'
    if args.uninstall:
        if not (app / MARKER).exists():
            parser.error('No marked source installation found. Remove a system package with apt instead.')
        if source == app:
            parser.error('Run uninstall from the source checkout, not the installed directory.')
        if shutil.which('systemctl') and os.environ.get('XDG_RUNTIME_DIR'):
            subprocess.run(['systemctl', '--user', 'stop', f'app-{APP_ID}.Restore@autostart.service'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for path in (entry, autostart, shortcut):
            if path.is_file() and desktop_exec(launcher) in path.read_text():
                path.unlink()
        if launcher.is_file() and str(app) in launcher.read_text():
            launcher.unlink()
        icon.unlink(missing_ok=True)
        for name in (*PAYLOAD, MARKER):
            (app / name).unlink(missing_ok=True)
        shutil.rmtree(app / '__pycache__', ignore_errors=True)
        try:
            app.rmdir()
        except OSError:
            pass
        print('Removed the user installation. Profiles, backups, and Niri display settings were retained.')
        return
    launcher_source = (source / 'packaging/display-desk').read_text()
    placeholder = 'Path("/usr/share/display-desk")'
    if launcher_source.count(placeholder) != 1:
        parser.error('The packaged launcher has an unexpected application directory.')
    for directory in (app, bindir, entry.parent, autostart.parent, icon.parent):
        directory.mkdir(parents=True, exist_ok=True)
    for filename in PAYLOAD:
        if (source / filename).resolve() != (app / filename).resolve():
            shutil.copy2(source / filename, app / filename)
    launcher.write_text(launcher_source.replace(placeholder, f'Path({str(app)!r})'))
    launcher.chmod(0o755)
    shutil.copy2(source / 'icon.svg', icon)
    for template, destination in [('packaging/com.elgemmy.DisplayDesk.desktop', entry),
                                  ('packaging/com.elgemmy.DisplayDesk.Restore.desktop', autostart)]:
        content = (source / template).read_text().replace('Exec=display-desk', 'Exec=/usr/bin/python3 ' + desktop_exec(launcher))
        destination.write_text(content)
    (app / MARKER).write_text('Display Desk source installation\n')
    if not args.no_desktop_shortcut and desktop_dir != home and desktop_dir.is_dir():
        shortcut.write_text(entry.read_text())
        shortcut.chmod(0o755)
    for command in (['update-desktop-database', str(entry.parent)],
                    ['gtk-update-icon-cache', '-f', '-t', str(data / 'icons/hicolor')]):
        if shutil.which(command[0]):
            subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('Installed:', launcher)
    print('Application menu:', entry)
    print('Automatic restore starts at the next Niri desktop login and is controlled by the app toggle.')


if __name__ == '__main__':
    main()
