#!/usr/bin/env python3
"""Publish ELDRIS 3.0 from this computer to a free public HTTPS URL.

What it does, in order:

1. creates (once) a virtual environment in ``backend/.venv`` and installs
   ``backend/requirements.txt``;
2. makes sure ``backend/.env`` exists and has a ``SESSION_SECRET`` and a
   ``DEVICE_API_KEY`` -- generating them if they are empty;
3. starts the Flask app on a local port and waits for ``/api/health``;
4. downloads the free ``cloudflared`` binary into ``tools/`` (once) and opens a
   quick tunnel, which prints a public ``https://*.trycloudflare.com`` address;
5. prints the exact lines to paste into the wrist band sketch.

No account, card, or domain is needed. The catch is that the public address is
new on every run and this computer must stay awake while it is published. For a
stable address, see the named-tunnel section of docs/DEPLOY-FREE.md.

Usage:
    python tools/publish_free.py              # tunnel + server
    python tools/publish_free.py --no-tunnel  # local server only
    python tools/publish_free.py --port 5055
"""
import argparse
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'backend'
TOOLS = ROOT / 'tools'
ENV_FILE = BACKEND / '.env'
EXAMPLE = BACKEND / '.env.example'

CLOUDFLARED = {
    'Windows': 'cloudflared-windows-amd64.exe',
    'Linux': 'cloudflared-linux-amd64',
    'Darwin': 'cloudflared-darwin-amd64.tgz',
}


def say(message=''):
    print(message, flush=True)


def step(number, text):
    say()
    say('[{}/5] {}'.format(number, text))


# --- environment -------------------------------------------------------------
def venv_python():
    if platform.system() == 'Windows':
        return BACKEND / '.venv' / 'Scripts' / 'python.exe'
    return BACKEND / '.venv' / 'bin' / 'python'


def ensure_dependencies():
    python = venv_python()
    if not python.exists():
        step(1, 'Creating the Python environment (first run only)...')
        subprocess.check_call([sys.executable, '-m', 'venv', str(BACKEND / '.venv')])
    else:
        step(1, 'Python environment already exists.')
    marker = BACKEND / '.venv' / '.eldris-deps'
    if marker.exists():
        return python
    say('    Installing backend requirements...')
    subprocess.check_call([str(python), '-m', 'pip', 'install', '--quiet',
                           '--disable-pip-version-check', '-r', str(BACKEND / 'requirements.txt')])
    marker.write_text('installed', encoding='utf-8')
    return python


def read_env():
    values = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
    return values


def write_env(values):
    """Rewrite .env from the example, then apply ``values``."""
    lines = []
    template = EXAMPLE.read_text(encoding='utf-8', errors='replace') if EXAMPLE.exists() else ''
    seen = set()
    for line in template.splitlines():
        if '=' in line and not line.strip().startswith('#'):
            key = line.split('=', 1)[0].strip()
            seen.add(key)
            if key in values:
                line = '{}={}'.format(key, values[key])
        lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append('{}={}'.format(key, value))
    ENV_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def ensure_env(port):
    step(2, 'Preparing backend/.env ...')
    values = read_env()
    changed = []
    if not values.get('SESSION_SECRET'):
        values['SESSION_SECRET'] = secrets.token_hex(32)
        changed.append('SESSION_SECRET')
    if not values.get('DEVICE_API_KEY'):
        values['DEVICE_API_KEY'] = 'eldris-' + secrets.token_hex(8)
        changed.append('DEVICE_API_KEY')
    if not values.get('HOST'):
        values['HOST'] = '0.0.0.0'
        changed.append('HOST')
    values['PORT'] = str(port)
    values['DEBUG'] = 'false'
    # Quick tunnels terminate TLS in front of the app, but a Secure cookie would
    # then also be rejected when you open http://127.0.0.1 locally. Keep it off
    # for this demo; set SESSION_COOKIE_SECURE=true on a permanent HTTPS host.
    values.setdefault('SESSION_COOKIE_SECURE', 'false')
    values['CORS_ORIGINS'] = ''
    write_env(values)
    say('    .env ready' + (' (generated: {})'.format(', '.join(changed)) if changed else '.'))
    return values


# --- server ------------------------------------------------------------------
def wait_for_health(port, seconds=45):
    deadline = time.time() + seconds
    url = 'http://127.0.0.1:{}/api/health'.format(port)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def start_server(python, port):
    step(3, 'Starting the ELDRIS server on port {} ...'.format(port))
    env = dict(os.environ)
    env.update(read_env())
    env['HOST'] = '0.0.0.0'
    env['PORT'] = str(port)
    env['DEBUG'] = 'false'
    executable = str(python)
    process = subprocess.Popen([executable, 'app.py'], cwd=str(BACKEND), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not wait_for_health(port):
        process.terminate()
        output = ''
        try:
            output = process.communicate(timeout=5)[0] or ''
        except Exception:
            pass
        say('    The server did not answer /api/health.')
        if output:
            say('    --- server output ---')
            say(output[-2000:])
        raise SystemExit(1)
    say('    Server is up: http://127.0.0.1:{}/'.format(port))
    return process


# --- tunnel ------------------------------------------------------------------
def ensure_cloudflared():
    step(4, 'Locating the free tunnel client (cloudflared) ...')
    found = shutil.which('cloudflared')
    if found:
        return found
    if platform.system() not in CLOUDFLARED:
        raise SystemExit('    Unsupported platform. Install cloudflared manually.')
    asset = CLOUDFLARED[platform.system()]
    target = TOOLS / ('cloudflared.exe' if platform.system() == 'Windows' else 'cloudflared')
    if target.exists():
        say('    Using {}'.format(target))
        return str(target)
    TOOLS.mkdir(exist_ok=True)
    url = 'https://github.com/cloudflare/cloudflared/releases/latest/download/' + asset
    say('    Downloading {} ...'.format(asset))
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(target, 'wb') as handle:
            shutil.copyfileobj(response, handle)
    except Exception as exc:
        raise SystemExit('    Download failed ({}). Install cloudflared and retry.'.format(exc))
    if asset.endswith('.tgz'):
        subprocess.check_call(['tar', '-xzf', str(target), '-C', str(TOOLS)])
        target = TOOLS / 'cloudflared'
        Path(str(target) + '.tgz').unlink(missing_ok=True)
    else:
        target.chmod(0o755)
    say('    Saved to {}'.format(target))
    return str(target)


def start_tunnel(binary, port):
    step(5, 'Opening the public tunnel ...')
    process = subprocess.Popen([binary, 'tunnel', '--url', 'http://127.0.0.1:{}'.format(port),
                                '--no-autoupdate'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    pattern = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
    public_url = None
    deadline = time.time() + 60
    while time.time() < deadline and public_url is None:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                break
            continue
        match = pattern.search(line)
        if match:
            public_url = match.group(0)
    if not public_url:
        process.terminate()
        raise SystemExit('    The tunnel did not return an address. Check the network and retry.')
    return process, public_url


# --- output ------------------------------------------------------------------
def print_handoff(values, public_url, port):
    say()
    say('=' * 70)
    say(' ELDRIS 3.0 is live')
    say('=' * 70)
    if public_url:
        say(' Dashboard   {}'.format(public_url))
        say(' Band URL    {}/api/motion'.format(public_url))
    else:
        say(' Dashboard   http://127.0.0.1:{}/ (this computer only)'.format(port))
    say()
    say(' Paste these into hardware/esp32_hand_band/esp32_hand_band.ino,')
    say(' then flash the board:')
    say()
    say('   static const char *WIFI_SSID     = "<your wifi name>";')
    say('   static const char *WIFI_PASSWORD = "<your wifi password>";')
    say('   static const char *DEVICE_API_KEY = "{}";'.format(values.get('DEVICE_API_KEY', '')))
    if public_url:
        say('   static const char *API_URL = "{}/api/motion";'.format(public_url))
    else:
        say('   static const char *API_URL = "http://<this-computer-lan-ip>:{}/api/motion";'.format(port))
    say()
    say(' The dashboard shows heart rate, SpO2, body temperature, tremor and fall')
    say(' state as soon as the band reports. Keep this window open; press Ctrl+C')
    say(' to stop publishing.')
    if public_url:
        say()
        say(' The trycloudflare address changes every run. For a permanent one,')
        say(' see docs/DEPLOY-FREE.md (named tunnel, free Cloudflare account).')
    say('=' * 70)


def main():
    parser = argparse.ArgumentParser(description='Publish ELDRIS 3.0 for free.')
    parser.add_argument('--port', type=int, default=5000, help='local port (default 5000)')
    parser.add_argument('--no-tunnel', action='store_true', help='run only the local server')
    args = parser.parse_args()

    python = ensure_dependencies()
    values = ensure_env(args.port)
    server = start_server(python, args.port)
    tunnel = None
    public_url = None
    try:
        if not args.no_tunnel:
            binary = ensure_cloudflared()
            tunnel, public_url = start_tunnel(binary, args.port)
        print_handoff(values, public_url, args.port)
        while True:
            time.sleep(1)
            if server.poll() is not None:
                say('The server stopped. Re-run this script to publish again.')
                break
            if tunnel is not None and tunnel.poll() is not None:
                say('The tunnel stopped. Re-run this script to publish again.')
                break
    except KeyboardInterrupt:
        say()
        say('Stopping...')
    finally:
        for process in (tunnel, server):
            if process is not None and process.poll() is None:
                process.terminate()
        time.sleep(0.5)
        for process in (tunnel, server):
            if process is not None and process.poll() is None:
                process.kill()


if __name__ == '__main__':
    main()
