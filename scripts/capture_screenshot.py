#!/usr/bin/python3
"""Capture only a demo window, with fictional data and no display changes."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/screenshot.png')
    args = parser.parse_args()
    if not os.environ.get('NIRI_SOCKET') or not shutil.which('niri'):
        parser.error('Capture requires a running Niri desktop session.')
    import app
    from backend import normalize_outputs
    from gi.repository import GLib

    def fake_output(name, model, serial, width, height, scale, x, y):
        return {'name': name, 'make': 'Example Displays', 'model': model, 'serial': serial,
                'modes': [{'width': width, 'height': height, 'refresh_rate': 60000, 'is_preferred': True}],
                'current_mode': 0, 'logical': {'x': x, 'y': y, 'width': int(width/scale),
                'height': int(height/scale), 'scale': scale, 'transform': 'Normal'}}

    # Fictional identities, serials, and setup names; never call Backend.query().
    displays = normalize_outputs({
        'DP-1': fake_output('DP-1', 'Studio Display', 'DEMO-EXTERNAL', 2560, 1440, 1.25, 0, 0),
        'eDP-1': fake_output('eDP-1', 'Notebook Display', None, 1920, 1080, 1.25, 256, 1152),
    })

    class DemoBackend:
        def query(self):
            return copy.deepcopy(displays)

        def profiles(self):
            return [{'id': 'demo', 'name': 'Desk · Laptop below', 'lid': 'open', 'outputs': self.query(),
                     'matches': True, 'saved_at': 0}]

        def get_auto(self):
            return True

    app.Backend = DemoBackend
    app.lid_state = lambda: 'open'
    application = app.App()
    application.set_application_id('com.elgemmy.DisplayDesk.Screenshot')
    state = {'window_id': None, 'wait': 0, 'start': time.monotonic(), 'failed': False}

    def msg(*arguments):
        return subprocess.check_output(['niri', 'msg', *arguments], text=True, timeout=10)

    def capture():
        try:
            if time.monotonic() - state['start'] > 20:
                raise RuntimeError('Timed out waiting for the demo window.')
            window = application.get_active_window()
            if not window or window.busy or not window.displays:
                return True
            if state['window_id'] is None:
                windows = json.loads(msg('--json', 'windows'))
                state['window_id'] = str(next(w['id'] for w in windows
                    if w['app_id'] == 'com.elgemmy.DisplayDesk.Screenshot'))
                msg('action', 'move-window-to-floating', '--id', state['window_id'])
                msg('action', 'set-window-width', '--id', state['window_id'], '1120')
                msg('action', 'set-window-height', '--id', state['window_id'], '900')
                state['wait'] = time.monotonic() + .5
                return True
            if time.monotonic() < state['wait']:
                return True
            args.output.parent.mkdir(parents=True, exist_ok=True)
            msg('action', 'screenshot-window', '--id', state['window_id'], '--path', str(args.output.resolve()))
            print('Saved synthetic screenshot:', args.output)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            state['failed'] = True
        application.quit()
        return False

    application.connect('activate', lambda *_: GLib.timeout_add(150, capture))
    application.run([])
    return 1 if state['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
