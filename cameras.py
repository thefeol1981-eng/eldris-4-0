# -*- coding: utf-8 -*-
"""The two ESP32-CAM body cameras.

Each body camera is an ESP32-CAM board with its own Wi-Fi connection. The board
runs its own web server: a single JPEG on one URL, an MJPEG stream on another,
and a small JSON status endpoint. This module is the bridge -- the browser
never learns the camera address, it asks this API, which relays the bytes.

Configuration is deliberately simple, and any IP can be given:

    CAMERA_ESP_FRONT_IP=192.168.1.51
    CAMERA_ESP_REAR_IP=192.168.1.52

Default paths match the firmware in hardware/esp32_cam_front/ and
hardware/esp32_cam_rear/: ``/stream`` for MJPEG and ``/snapshot`` for a single
JPEG. Both can be overridden per camera, and the older
``CAMERA_STREAMS=cam-robot-front=http://host:81/?action=stream,...`` form is
still accepted for mjpg-streamer-style sources.

Nothing is written to disk. Frames are read from the board and passed straight
to the browser, so there is no image store to secure.
"""
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CAMERA_IDS = ('cam-robot-front', 'cam-robot-rear')
DEFAULT_PORT = os.getenv('CAMERA_ESP_PORT', '81')
DEFAULT_STREAM_PATH = os.getenv('CAMERA_ESP_STREAM_PATH', '/stream')
DEFAULT_SNAPSHOT_PATH = os.getenv('CAMERA_ESP_SNAPSHOT_PATH', '/snapshot')
ONLINE_TIMEOUT_S = float(os.getenv('CAMERA_ONLINE_SECONDS', '30'))
TIMEOUT_S = float(os.getenv('CAMERA_TIMEOUT_SECONDS', '5'))
MAX_SNAPSHOT_BYTES = int(os.getenv('CAMERA_SNAPSHOT_MAX_BYTES', '4000000'))


def _url_from_ip(ip, path):
    ip = (ip or '').strip()
    if not ip:
        return None
    if ip.startswith(('http://', 'https://')):
        return ip.rstrip('/') + path if not ip.endswith(path) else ip
    return 'http://{}:{}{}'.format(ip.rstrip('/'), DEFAULT_PORT, path)


def _parse_stream_map():
    """'cam-robot-front=http://host/?action=stream, cam-robot-rear=...' -> dict."""
    mapping = {}
    for entry in os.getenv('CAMERA_STREAMS', '').split(','):
        entry = entry.strip()
        if not entry or '=' not in entry:
            continue
        device_id, url = entry.split('=', 1)
        device_id, url = device_id.strip().lower(), url.strip()
        if device_id and url.startswith(('http://', 'https://')):
            mapping[device_id] = url
    return mapping


def snapshot_map():
    mapping = {}
    for entry in os.getenv('CAMERA_SNAPSHOTS', '').split(','):
        entry = entry.strip()
        if not entry or '=' not in entry:
            continue
        device_id, url = entry.split('=', 1)
        device_id, url = device_id.strip().lower(), url.strip()
        if device_id and url.startswith(('http://', 'https://')):
            mapping[device_id] = url
    return mapping


class CameraService:
    """Known ESP32-CAM boards and their reachability."""

    def __init__(self):
        self.lock = threading.RLock()
        self._reach = {}   # device id -> (ok, checked_at, detail)

    def endpoints(self):
        """Resolve every configured camera into (stream_url, snapshot_url)."""
        stream_map = _parse_stream_map()
        snap_map = snapshot_map()
        configured = {
            'cam-robot-front': os.getenv('CAMERA_ESP_FRONT_IP') or os.getenv('CAMERA_FRONT_IP') or '',
            'cam-robot-rear': os.getenv('CAMERA_ESP_REAR_IP') or os.getenv('CAMERA_REAR_IP') or '',
        }
        out = {}
        for device_id in CAMERA_IDS:
            stream = stream_map.get(device_id) or _url_from_ip(configured[device_id], DEFAULT_STREAM_PATH)
            snapshot = snap_map.get(device_id)
            if snapshot is None and stream:
                snapshot = _url_from_ip(configured[device_id], DEFAULT_SNAPSHOT_PATH) if configured[device_id] else None
                if snapshot is None and 'action=stream' in stream:
                    snapshot = stream.replace('action=stream', 'action=snapshot')
            out[device_id] = {'stream': stream, 'snapshot': snapshot,
                              'ip': configured[device_id].strip() or None}
        return out

    def stream_url(self, device_id):
        return self.endpoints().get(device_id, {}).get('stream')

    def snapshot_url(self, device_id):
        return self.endpoints().get(device_id, {}).get('snapshot')

    def configured(self, device_id):
        return bool(self.stream_url(device_id))

    def probe(self, device_id, force=False):
        """Ask the board for a frame at most every few seconds, and cache it."""
        with self.lock:
            cached = self._reach.get(device_id)
            if cached and not force and (time.time() - cached[1]) < ONLINE_TIMEOUT_S / 3:
                return cached
        url = self.snapshot_url(device_id)
        if not url:
            result = (False, time.time(), 'No address configured')
        else:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'ELDRIS/3.0'})
                with urllib.request.urlopen(req, timeout=min(TIMEOUT_S, 2)) as response:
                    response.read(2048)
                result = (True, time.time(), None)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                result = (False, time.time(), str(getattr(exc, 'reason', exc)))
        with self.lock:
            self._reach[device_id] = result
        return result

    def overview(self, names):
        """Metadata the dashboard and the assistant quote."""
        endpoints = self.endpoints()
        sources = []
        # Both boards are probed at the same time, so one slow camera cannot
        # hold up the dashboard refresh.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(zip(CAMERA_IDS, pool.map(self.probe, CAMERA_IDS)))
        for device_id in CAMERA_IDS:
            info = endpoints.get(device_id) or {}
            ok, checked_at, detail = results[device_id]
            configured = bool(info.get('stream'))
            sources.append({
                'id': device_id,
                'name': names.get(device_id, device_id),
                'ip': info.get('ip'),
                'stream_configured': configured,
                'snapshot_configured': bool(info.get('snapshot')),
                'online': bool(ok) if configured else False,
                'checked_at': checked_at,
                'error': None if ok else (detail or 'No frame received'),
            })
        live = [s for s in sources if s['online']]
        return {
            'available': any(sources),
            'stream_configured': any(s['stream_configured'] for s in sources),
            'live': len(live),
            'sources': sources,
            'frames_stored': False,
            'note': 'Each body camera is an ESP32-CAM board on the local network. Frames are relayed, never stored.',
        }

    def read_snapshot(self, device_id):
        """Fetch one JPEG. Returns (bytes, content_type) or raises ValueError."""
        url = self.snapshot_url(device_id)
        if not url:
            raise LookupError('No address is configured for this camera.')
        req = urllib.request.Request(url, headers={'User-Agent': 'ELDRIS/3.0'})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as upstream:
            data = upstream.read(MAX_SNAPSHOT_BYTES)
            content_type = upstream.headers.get('Content-Type') or 'image/jpeg'
        if not data:
            raise ValueError('The camera returned an empty frame.')
        return data, content_type.split(';')[0]

    def open_stream(self, device_id):
        url = self.stream_url(device_id)
        if not url:
            raise LookupError('No address is configured for this camera.')
        req = urllib.request.Request(url, headers={'User-Agent': 'ELDRIS/3.0'})
        upstream = urllib.request.urlopen(req, timeout=TIMEOUT_S)
        content_type = upstream.headers.get('Content-Type') or 'multipart/x-mixed-replace; boundary=frame'
        return upstream, content_type
