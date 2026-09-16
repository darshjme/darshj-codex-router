"""Darshj's Codex Router installer.

Owns exactly three things and nothing else: the per-user launchd service
`ai.darshj.codex-router`, and the two keys `openai_base_url` and
`model_catalog_json` in ~/.codex/config.toml. The original config is backed up
once, every write is atomic, and configuration this installer did not write is
never overwritten. Subcommands: install, uninstall, status, token, migrate.
"""
import argparse
import json
import os
import plistlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
import tomlkit

PRODUCT = "Darshj's Codex Router"
ROOT = Path(__file__).resolve().parent
HOME = Path.home()
CONFIG = HOME / '.codex/config.toml'
STATE = ROOT / 'state'
MANIFEST = STATE / 'installation.json'
TOKEN = STATE / 'dashboard-token'
CATALOG = STATE / 'models.json'            # generated at router start: models.base.json + Ollama entries
BASE_CATALOG = ROOT / 'models.base.json'   # tracked: native Codex snapshot + Claude/Grok entries
PORT = 18740
LABEL = 'ai.darshj.codex-router'
OLD_LABELS = ['ai.darsh.codex-max-router']  # Codex Max Router; retired by `install`
AGENTS = HOME / 'Library/LaunchAgents'
PLIST = AGENTS / (LABEL + '.plist')
BACKUPS = HOME / '.codex/backups/darshj-codex-router'
BASE_URL = 'http://127.0.0.1:%d' % PORT
VALUES = {'openai_base_url': BASE_URL, 'model_catalog_json': str(CATALOG)}
HEALTH_PATHS = ('/api/v1/health', '/health')
# State worth carrying over from another checkout. Existing files are never overwritten.
# installation.json comes too: it proves the old config values are router-owned, so the
# following `install` upgrades them instead of refusing, and it keeps the original backup path.
MIGRATE_FILES = ('reserve.json', 'settings.json', 'stats.sqlite', 'stats.sqlite-wal',
                 'stats.sqlite-shm', 'dashboard-token', 'installation.json')
PRIVATE_FILES = ('dashboard-token', 'installation.json')


def version():
    try:
        return (ROOT / 'VERSION').read_text().strip() or 'unknown'
    except OSError:
        return 'unknown'


def atomic(path, data, mode=None):
    """Write bytes through a same-directory temp file and rename into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def present(path):
    return 'present' if path.exists() else 'missing'


# ---- launchd -----------------------------------------------------------------

def domain():
    return 'gui/' + str(os.getuid())


def loaded(label):
    """(is_loaded, pid_or_None) for a label in the user's launchd domain."""
    proc = subprocess.run(['launchctl', 'print', domain() + '/' + label], capture_output=True, text=True)
    if proc.returncode != 0:
        return False, None
    match = re.search(r'^\s*pid = (\d+)', proc.stdout, re.M)
    return True, (int(match.group(1)) if match else None)


def bootout(label):
    subprocess.run(['launchctl', 'bootout', domain() + '/' + label],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bootstrap(plist):
    proc = subprocess.run(['launchctl', 'bootstrap', domain(), str(plist)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit('launchctl bootstrap failed for %s: %s' % (plist, (proc.stderr or proc.stdout).strip()))


def disable_plist(label):
    """Move a label's LaunchAgent plist into state/ (kept, never deleted)."""
    src = AGENTS / (label + '.plist')
    if not src.exists():
        return None
    STATE.mkdir(exist_ok=True, mode=0o700)
    dst = STATE / ('disabled-%s-%d.plist' % (label, int(time.time())))
    src.rename(dst)
    return dst


def plist_document():
    return {'Label': LABEL,
            'ProgramArguments': [str(ROOT / '.venv/bin/python'), str(ROOT / 'router.py'),
                                 '--catalog', str(CATALOG), '--base-catalog', str(BASE_CATALOG),
                                 '--port', str(PORT)],
            'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10,
            'StandardOutPath': str(STATE / 'service.log'),
            'StandardErrorPath': str(STATE / 'service-error.log'),
            'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:' + str(HOME / '.local/bin')}}


# ---- health ------------------------------------------------------------------

def health(timeout=1):
    """The router's health JSON when it answers status ok, else None."""
    for path in HEALTH_PATHS:
        try:
            with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as r:
                data = json.load(r)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('status') == 'ok':
            return data
    return None


def wait_healthy(attempts=30):
    for _ in range(attempts):
        data = health()
        if data:
            return data
        time.sleep(0.2)
    return None


# ---- dashboard token ---------------------------------------------------------

def ensure_token():
    """Return the dashboard token, creating state/dashboard-token (0600) if missing."""
    STATE.mkdir(exist_ok=True, mode=0o700)
    if TOKEN.exists():
        token = TOKEN.read_text().strip()
        if token:
            os.chmod(TOKEN, 0o600)
            return token
    token = secrets.token_hex(32)
    atomic(TOKEN, token.encode(), mode=0o600)
    return token


# ---- commands ----------------------------------------------------------------

def install():
    for required in (ROOT / '.venv/bin/python', ROOT / 'router.py', BASE_CATALOG):
        if not required.exists():
            raise SystemExit('Missing %s. Create the venv (python3 -m venv .venv && .venv/bin/pip install -r '
                             'requirements.txt) and keep models.base.json in the checkout. No changes made.' % required)
    if not CONFIG.exists():
        raise SystemExit('%s not found; open the Codex app once so it exists. No changes made.' % CONFIG)
    STATE.mkdir(exist_ok=True, mode=0o700)
    text = CONFIG.read_text()
    doc = tomlkit.parse(text)
    if doc.get('model_provider', 'openai') != 'openai':
        raise SystemExit('Existing custom provider requires manual integration; no changes made.')
    saved = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    owned = (saved or {}).get('installed') or {}
    for key, value in VALUES.items():
        current = doc.get(key)
        # Unset, already ours, or the value this installer wrote earlier (an upgrade) are fine.
        if current not in (None, value) and current != owned.get(key):
            raise SystemExit('Existing ' + key + ' requires merging; no changes made.')
    if saved is None:
        BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = BACKUPS / ('config-' + str(int(time.time())) + '.toml')
        atomic(backup, text.encode())
        saved = {'original': {k: doc.get(k) for k in VALUES}, 'backup': str(backup)}
        atomic(MANIFEST, json.dumps(saved, indent=2).encode(), mode=0o600)
    atomic(PLIST, plistlib.dumps(plist_document()))
    # Retire the previous product's service first so the port is free.
    retired = []
    for old in OLD_LABELS:
        was_loaded, _ = loaded(old)
        if was_loaded:
            bootout(old)
        disabled = disable_plist(old)
        if was_loaded or disabled:
            retired.append((old, disabled))
            print('Retired %s%s.' % (old, ' (plist moved to %s)' % disabled if disabled else ''))
    bootout(LABEL)
    bootstrap(PLIST)
    if not wait_healthy():
        bootout(LABEL)
        parked = disable_plist(LABEL)
        for old, disabled in retired:
            if disabled:
                back = AGENTS / (old + '.plist')
                disabled.rename(back)
                try:
                    bootstrap(back)
                    print('Restored %s from %s.' % (old, back))
                except SystemExit as error:
                    print('Could not reload %s: %s' % (old, error))
        raise SystemExit('Router did not become healthy (see %s). New plist parked at %s. '
                         'Codex configuration was not changed.' % (STATE / 'service-error.log', parked))
    # Preserve concurrent edits instead of restoring a stale snapshot.
    if CONFIG.read_text() != text:
        raise SystemExit('Codex config changed concurrently; service ready but configuration not changed.')
    for key, value in VALUES.items():
        doc[key] = value
    atomic(CONFIG, tomlkit.dumps(doc).encode())
    saved['installed'] = dict(VALUES)
    atomic(MANIFEST, json.dumps(saved, indent=2).encode(), mode=0o600)
    print('Installed %s %s: healthy user service %s and two Codex configuration keys.' % (PRODUCT, version(), LABEL))
    print('Native GPT model/default and ChatGPT authentication preserved.')
    print('Fully quit and reopen Codex to load the catalog at %s.' % CATALOG)
    print('Dashboard: %s/dashboard/  (token: .venv/bin/python install.py token)' % BASE_URL)


def uninstall():
    saved = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    installed = saved.get('installed') or {}
    if installed:
        doc = tomlkit.parse(CONFIG.read_text())
        for key, value in installed.items():
            if doc.get(key) != value:
                raise SystemExit('Router-owned config changed since install: ' + key + '. No changes made.')
        for key, value in (saved.get('original') or {}).items():
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
        atomic(CONFIG, tomlkit.dumps(doc).encode())
        saved['installed'] = {}
        atomic(MANIFEST, json.dumps(saved, indent=2).encode(), mode=0o600)
        print('Restored the two Codex configuration keys (original backup kept at %s).' % saved.get('backup'))
    else:
        print('No router-owned Codex configuration recorded; leaving %s untouched.' % CONFIG)
    for label in [LABEL] + OLD_LABELS:
        was_loaded, _ = loaded(label)
        if was_loaded:
            bootout(label)
        disabled = disable_plist(label)
        if was_loaded or disabled:
            print('Stopped %s%s.' % (label, ' (plist moved to %s)' % disabled if disabled else ''))
    print('Source, backups, and state/ retained. Fully quit and reopen Codex.')


def status():
    is_loaded, pid = loaded(LABEL)
    data = health()
    print('%s %s' % (PRODUCT, version()))
    print('  service        %s: %s' % (LABEL, 'loaded (pid %s)' % pid if is_loaded else 'not loaded'))
    print('  plist          %s (%s)' % (PLIST, present(PLIST)))
    for old in OLD_LABELS:
        old_loaded, _ = loaded(old)
        if old_loaded or (AGENTS / (old + '.plist')).exists():
            print('  old service    %s: %s' % (old, 'STILL LOADED; run install to retire it' if old_loaded
                                                 else 'plist present, not loaded'))
    print('  health         %s' % (json.dumps(data, sort_keys=True) if data else 'unreachable at ' + BASE_URL))
    print('  catalog        %s (%s)' % (CATALOG, present(CATALOG)))
    print('  base catalog   %s (%s)' % (BASE_CATALOG, present(BASE_CATALOG)))
    print('  token          %s (%s)' % (TOKEN, present(TOKEN)))
    print('  dashboard      %s/dashboard/' % BASE_URL)
    try:
        doc = tomlkit.parse(CONFIG.read_text())
    except OSError:
        doc = {}
    for key, value in VALUES.items():
        current = doc.get(key)
        verdict = 'unset' if current is None else ('ok' if current == value else 'DIFFERS: %s' % current)
        print('  config         %s = %s' % (key, verdict))
    if MANIFEST.exists():
        saved = json.loads(MANIFEST.read_text())
        print('  manifest       %s (config backup: %s)' % (MANIFEST, saved.get('backup')))
    else:
        print('  manifest       %s (missing: nothing installed by this installer)' % MANIFEST)
    return 0 if is_loaded and data else 1


def migrate(source):
    src_state = Path(source).expanduser().resolve()
    if src_state.name != 'state':
        src_state = src_state / 'state'
    if not src_state.is_dir():
        raise SystemExit('No state/ directory at %s; nothing migrated.' % src_state)
    if src_state == STATE.resolve():
        raise SystemExit('--from points at this checkout; nothing to migrate.')
    for old in OLD_LABELS:
        if loaded(old)[0]:
            print('Note: %s is still running, so stats.sqlite may be mid-write. For a clean copy stop it first:\n'
                  '      launchctl bootout %s/%s' % (old, domain(), old))
    STATE.mkdir(exist_ok=True, mode=0o700)
    copied, skipped = [], []
    for name in MIGRATE_FILES:
        src, dst = src_state / name, STATE / name
        if not src.is_file():
            continue
        if dst.exists():
            skipped.append(name)
            continue
        mode = 0o600 if name in PRIVATE_FILES else (src.stat().st_mode & 0o777)
        atomic(dst, src.read_bytes(), mode=mode)
        copied.append(name)
    print('Migrated from %s: copied %s; kept existing %s.'
          % (src_state, ', '.join(copied) or 'nothing', ', '.join(skipped) or 'nothing'))
    if copied:
        print('Now run: .venv/bin/python install.py install')


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='install.py',
        description='%s installer. Manages only the per-user launchd service %s and the two Codex '
                    'configuration keys openai_base_url and model_catalog_json in %s. Backs the original '
                    'config up once, writes atomically, and refuses to overwrite settings it did not install.'
                    % (PRODUCT, LABEL, CONFIG),
        epilog='Typical flow: install.py install -> fully quit and reopen Codex -> install.py token -> '
               'open %s/dashboard/' % BASE_URL)
    parser.add_argument('--version', action='version', version='%s %s' % (PRODUCT, version()))
    sub = parser.add_subparsers(dest='action', metavar='action', required=True)
    sub.add_parser('install', help='write the plist, retire %s, load %s, wait for health, then set the two Codex keys'
                   % (', '.join(OLD_LABELS), LABEL))
    sub.add_parser('uninstall', help='restore the two Codex keys and stop the service; source, backups and state/ stay')
    sub.add_parser('status', help='show service, health JSON, catalog, token and config-key state (exit 1 if unhealthy)')
    sub.add_parser('token', help='print the dashboard token from state/dashboard-token, creating it (mode 0600) if missing')
    mig = sub.add_parser('migrate', help='copy reserve.json, settings.json, stats.sqlite (+ WAL journal), '
                                         'dashboard-token and installation.json from another checkout; '
                                         'existing files are kept')
    mig.add_argument('--from', dest='source', required=True, metavar='DIR',
                     help='the old checkout (or its state/ directory), e.g. ~/repos/codex-max-router')
    args = parser.parse_args(argv)
    if args.action == 'install':
        install()
    elif args.action == 'uninstall':
        uninstall()
    elif args.action == 'status':
        return status()
    elif args.action == 'token':
        print(ensure_token())
    elif args.action == 'migrate':
        migrate(args.source)
    return 0


if __name__ == '__main__':
    sys.exit(main())
