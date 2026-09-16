"""Install/roll back only this router's two Codex config keys and user service."""
import argparse
import json
import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path
import tomlkit

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
CONFIG = HOME / '.codex/config.toml'
STATE = ROOT / 'state'
MANIFEST = STATE / 'installation.json'
LABEL = 'ai.darsh.codex-max-router'
PLIST = HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
VALUES = {'openai_base_url': 'http://127.0.0.1:18740', 'model_catalog_json': str(ROOT / 'models.json')}

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def install():
    STATE.mkdir(exist_ok=True, mode=0o700)
    text = CONFIG.read_text()
    doc = tomlkit.parse(text)
    if doc.get('model_provider', 'openai') != 'openai':
        raise SystemExit('Existing custom provider requires manual integration; no changes made.')
    for key, value in VALUES.items():
        if doc.get(key) not in (None, value):
            raise SystemExit('Existing ' + key + ' requires merging; no changes made.')
    if not MANIFEST.exists():
        backup_dir = HOME / '.codex/backups/codex-max-router'
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / ('config-' + str(int(time.time())) + '.toml')
        atomic(backup, text.encode())
        atomic(MANIFEST, json.dumps({'original': {k: doc.get(k) for k in VALUES},
                                     'installed': VALUES, 'backup': str(backup)}).encode())
    plist = {'Label': LABEL, 'ProgramArguments': [str(ROOT / '.venv/bin/python'),
        str(ROOT / 'router.py'), '--catalog', str(ROOT / 'models.json')],
        'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 10, 'StandardOutPath': str(STATE / 'service.log'),
        'StandardErrorPath': str(STATE / 'service-error.log'),
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:' + str(HOME / '.local/bin')}}
    atomic(PLIST, plistlib.dumps(plist))
    target = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl', 'bootout', target + '/' + LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['launchctl', 'bootstrap', target, str(PLIST)], check=True)
    import urllib.request
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(VALUES['openai_base_url'] + '/health', timeout=1) as r:
                ready = json.load(r).get('status') == 'ok'
            if ready:
                break
        except OSError:
            time.sleep(0.2)
    if not ready:
        raise SystemExit('Router did not become healthy; Codex configuration was not changed.')
    # Preserve concurrent edits instead of restoring a stale snapshot.
    current = CONFIG.read_text()
    if current != text:
        raise SystemExit('Codex config changed concurrently; service ready but configuration not changed.')
    for key, value in VALUES.items():
        doc[key] = value
    atomic(CONFIG, tomlkit.dumps(doc).encode())
    print('Installed healthy user service and two Codex configuration keys.')
    print('Native GPT model/default and ChatGPT authentication preserved.')

def uninstall():
    saved = json.loads(MANIFEST.read_text())
    doc = tomlkit.parse(CONFIG.read_text())
    for key, value in saved['installed'].items():
        if doc.get(key) != value:
            raise SystemExit('Router-owned config changed since install: ' + key + '. No changes made.')
    for key, value in saved['original'].items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    atomic(CONFIG, tomlkit.dumps(doc).encode())
    subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LABEL], check=False)
    if PLIST.exists():
        PLIST.rename(STATE / ('disabled-service-' + str(int(time.time())) + '.plist'))
    print('Router config keys removed and user service stopped. Source, backups, and state retained.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'uninstall'])
    args = parser.parse_args()
    install() if args.action == 'install' else uninstall()
