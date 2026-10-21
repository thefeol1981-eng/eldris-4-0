# -*- coding: utf-8 -*-
"""Robot driving through the drive ESP32.

The wheeled body has its own ESP32 (the "drive ESP32"). It joins Wi-Fi, runs a
tiny HTTP server and owns the motors and the local safety interlocks. This
module is the server-side half: it sends one command at a time, polls the
telemetry the board publishes, and stops the motors when the link goes quiet or
a command is not confirmed.

There is no simulation here. If no address is configured, or the board cannot
be reached, the drive reports offline and every movement command is refused
with the reason -- the dashboard never pretends the robot moved.

Protocol (served by firmware/esp32_drive/esp32_drive.ino):

    GET  {base}/api/status -> {"movement":"idle","speed_pct":0,"rssi":-58}
    POST {base}/api/command  {"command":"forward"} -> {"ok":true,"movement":"forward"}
    POST {base}/api/stop     -> {"ok":true}
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _base_url():
    """Normalise the configured address into a scheme + host, no trailing slash."""
    raw = (os.getenv('DRIVE_ESP_URL') or '').strip()
    if not raw:
        ip = (os.getenv('DRIVE_ESP_IP') or '').strip()
        if not ip:
            return ''
        raw = ip if ip.startswith(('http://', 'https://')) else 'http://' + ip
    return raw.rstrip('/')


# Camera streams are proxied from one URL per body camera, exactly as before;
# only the drive path changed from USB serial to HTTP.
CAMERA_IDS = ('cam-robot-front', 'cam-robot-rear')


class HardwareService:
    """Drive ESP32 link: command dispatch, telemetry cache and safety stop."""

    VALID = {'forward', 'reverse', 'left', 'right', 'stop'}

    def __init__(self, socketio=None):
        self.socketio = socketio
        self.base = _base_url()
        self.timeout = _env_float('DRIVE_TIMEOUT_SECONDS', 2.5)
        self.poll_seconds = _env_float('DRIVE_POLL_SECONDS', 2.0)
        self.command_timeout = _env_float('COMMAND_TIMEOUT_SECONDS', 2.0)
        self.lock = threading.RLock()
        self.last_command = None
        self.command_at = 0.0
        self.last_ok_at = None
        self.last_error = None
        # Telemetry the board last reported. None means "never received", which
        # is deliberately different from "stopped".
        self.data = {
            'movement': 'unknown',
            'mode': 'Stationary',
            'speed_pct': None,
            'rssi': None,
            'reported_at': None,
        }
        self._thread = None
        if self.base:
            self.start_polling()

    # -- transport -----------------------------------------------------------
    def _request(self, path, payload=None, timeout=None):
        """One HTTP call to the board. Returns (dict, error)."""
        url = self.base + path
        body = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'ELDRIS/3.0'},
            method='POST' if body is not None else 'GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                raw = response.read(8192)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, '{} unreachable: {}'.format(url, getattr(exc, 'reason', exc))
        if not raw:
            return {}, None
        try:
            parsed = json.loads(raw.decode('utf-8', 'ignore'))
        except ValueError:
            return None, '{} did not return JSON'.format(url)
        return (parsed if isinstance(parsed, dict) else {}), None

    def start_polling(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name='eldris-drive-poll')
        self._thread.start()

    def _poll_loop(self):
        while True:
            self.poll()
            time.sleep(max(0.5, self.poll_seconds))

    def poll(self):
        """Read telemetry once and cache it. Never raises."""
        if not self.base:
            return
        payload, err = self._request('/api/status', timeout=min(self.timeout, 2.0))
        now = time.time()
        with self.lock:
            if err or payload is None:
                self.last_error = err
                return
            self.last_ok_at = now
            self.last_error = None
            self.data['reported_at'] = now
            movement = payload.get('movement')
            if isinstance(movement, str) and movement:
                self.data['movement'] = movement
            if isinstance(payload.get('speed_pct'), (int, float)) and not isinstance(payload.get('speed_pct'), bool):
                self.data['speed_pct'] = int(payload['speed_pct'])
            if isinstance(payload.get('rssi'), (int, float)) and not isinstance(payload.get('rssi'), bool):
                self.data['rssi'] = int(payload['rssi'])
            self.data['mode'] = 'Driving' if str(self.data['movement']).lower() in ('forward', 'reverse', 'left', 'right') else 'Stationary'

    def ingest_telemetry(self, payload):
        """Accept telemetry the drive ESP32 posted itself.

        Used when the server cannot poll the board (a public host, or a board
        behind NAT). The payload is the same shape the poller reads.
        """
        with self.lock:
            now = time.time()
            self.last_ok_at = now
            self.last_error = None
            self.data['reported_at'] = now
            movement = payload.get('movement')
            if isinstance(movement, str) and movement:
                self.data['movement'] = movement
            if isinstance(payload.get('speed_pct'), (int, float)) and not isinstance(payload.get('speed_pct'), bool):
                self.data['speed_pct'] = int(payload['speed_pct'])
            if isinstance(payload.get('rssi'), (int, float)) and not isinstance(payload.get('rssi'), bool):
                self.data['rssi'] = int(payload['rssi'])
            self.data['mode'] = 'Driving' if str(self.data['movement']).lower() in ('forward', 'reverse', 'left', 'right') else 'Stationary'
        return self.status()

    # -- state ---------------------------------------------------------------
    def connected(self):
        with self.lock:
            return bool(self.last_ok_at) and (time.time() - self.last_ok_at) < max(6.0, self.poll_seconds * 3)

    def status(self):
        with self.lock:
            connected = self.connected()
            if connected:
                # Fresh telemetry wins even when the board pushes outbound and
                # no DRIVE_ESP_IP is configured (a public host cannot poll it).
                movement = self.data['movement']
                mode = self.data['mode']
            elif not self.base:
                mode, movement = 'Drive ESP32 not configured', 'unknown'
            else:
                mode, movement = 'Drive ESP32 offline', 'unknown'
            return {
                'robot_connected': connected,
                'drive_connected': connected,
                'drive_url': self.base or None,
                'drive_hint': None if self.base else 'Set DRIVE_ESP_IP (or DRIVE_ESP_URL) in backend/.env to the drive ESP32 address.',
                'drive_error': self.last_error,
                'camera_available': True,
                'mode': mode,
                'movement': movement,
                'speed_pct': self.data['speed_pct'],
                'rssi': self.data['rssi'],
                'face_present': None,
                'last_detection': None,
                'last_command': self.last_command,
                'updated_at': _now_iso(),
            }

    # -- commands ------------------------------------------------------------
    def command(self, command, source='dashboard'):
        if command not in self.VALID:
            return {'ok': False, 'error': 'Invalid movement command.'}
        if not self.base:
            return {'ok': False, 'error': 'Movement disabled: no drive ESP32 address is configured (DRIVE_ESP_IP).'}
        state = self.status()
        if not state['drive_connected']:
            return {'ok': False, 'error': 'Movement disabled: the drive ESP32 is not answering at {}'.format(self.base)}
        payload, err = self._request('/api/command', {'command': command})
        if err or payload is None:
            self._safe_stop()
            return {'ok': False, 'error': 'Command not confirmed, so the motors were stopped. {}'.format(err or 'no reply')}
        if payload.get('ok') is False:
            self._safe_stop()
            return {'ok': False, 'error': str(payload.get('error') or 'The drive ESP32 rejected the command.')}
        with self.lock:
            self.last_command = command
            self.command_at = time.time()
            if isinstance(payload.get('movement'), str):
                self.data['movement'] = payload['movement']
            self.data['mode'] = 'Driving' if command != 'stop' else 'Stationary'
        if self.socketio is not None:
            try:
                self.socketio.emit('status_update', self.status())
            except Exception:
                pass
        return {'ok': True, 'message': '{} command confirmed by the drive ESP32.'.format(command.title())}

    def _safe_stop(self):
        try:
            self._request('/api/stop', {}, timeout=1.5)
        except Exception:
            pass
        with self.lock:
            self.data['movement'] = 'stopped'
            self.data['mode'] = 'Stationary'

    def check_timeout(self):
        """Stop the motors if a command has not been followed by any movement."""
        with self.lock:
            stale = self.last_command and (time.time() - self.command_at) > self.command_timeout
        if not stale:
            return
        with self.lock:
            self.last_command = None
        self._safe_stop()

    def disconnect(self):
        """Called on shutdown so the robot does not keep driving."""
        self._safe_stop()
