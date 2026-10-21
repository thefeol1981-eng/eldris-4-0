# -*- coding: utf-8 -*-
"""Device inventory, home climate and robot navigation for ELDRIS 3.0.

There is exactly one home climate node (an ESP32 + DHT22) and it pushes to the
server with ``POST /api/readings``. Until it reports for the first time, the
dashboard shows the climate card as waiting -- nothing is generated.

Every board is listed by its own name and address, because the caregiver asked
for the devices list to name each ESP32:

    ESP32 #1  left wrist band    body-band          posts to /api/motion
    ESP32 #2  drive controller   drive-esp          DRIVE_ESP_IP
    ESP32 #3  home climate       esp32-home         ESP32_HOME_IP (optional)
    ESP32 #4  chatbot (mic+amp)  voice-esp          posts to /api/voice/listen
    ESP32-CAM front body camera  cam-robot-front    CAMERA_ESP_FRONT_IP
    ESP32-CAM rear body camera   cam-robot-rear     CAMERA_ESP_REAR_IP

ROOMS below is navigation only: the rooms the mobile robot can be sent to. No
per-room sensing is implied, so the dashboard never shows a room-by-room
breakdown.

Latest home reading and robot room are stored in SQLite, so a restart does not
blank the climate card or move the robot back to its default room.
"""
import os
import threading
import time
from datetime import datetime, timezone

import db
import motion   # the wrist band is part of the same inventory
import voice    # the chatbot ESP32 with the microphone and speaker

# Where the mobile robot may be sent. Names are used by the room picker and the
# assistant; they carry no sensor of their own.
ROOMS = [
    {'id': 'living', 'name': 'Living Room', 'floor': 1},
    {'id': 'bedroom', 'name': 'Bedroom', 'floor': 1},
    {'id': 'kitchen', 'name': 'Kitchen', 'floor': 2},
    {'id': 'bathroom', 'name': 'Bathroom', 'floor': 2},
    {'id': 'hallway', 'name': 'Hallway', 'floor': 1},
]

ROOM_BY_ID = {r['id']: r for r in ROOMS}
ROOM_NAME_BY_ID = {r['id']: r['name'] for r in ROOMS}

HOME_SENSOR_ID = 'esp32-home'
DRIVE_ID = 'drive-esp'

# Devices the prototype ships with. Status is resolved at request time so a
# device never has to be manually re-flagged when the hub restarts.
# Exactly two cameras exist, and both are mounted on the robot body, so they
# travel with it instead of watching fixed rooms. Every ESP32 is its own row,
# named after the job it does.
DEVICES = [
    {'id': 'pi-4', 'kind': 'hub', 'name': 'Raspberry Pi 4', 'serial': 'server',
     'room': None, 'note': 'Runs the ELDRIS API, the database and the relay for every ESP32.', 'battery': None},
    {'id': 'body-band', 'kind': 'wearable', 'name': 'ESP32 #1 · Left wrist band', 'serial': 'body-band',
     'room': None, 'note': 'The only band. Worn on the left wrist: MPU6050 tremor and fall, MAX30102 heart rate and SpO2, MLX90614 body temperature.', 'battery': None},
    {'id': DRIVE_ID, 'kind': 'controller', 'name': 'ESP32 #2 · Drive controller', 'serial': 'drive-esp',
     'room': None, 'note': 'Motors, motor driver and the physical stop on the robot chassis.', 'battery': None},
    {'id': HOME_SENSOR_ID, 'kind': 'sensor', 'name': 'ESP32 #3 · Home climate', 'serial': 'esp32-home',
     'room': None, 'note': 'The one home temperature and humidity node (DHT22).', 'battery': None},
    {'id': 'voice-esp', 'kind': 'audio', 'name': 'ESP32 #4 · Chatbot', 'serial': 'voice-esp',
     'room': 'hallway', 'note': 'INMP441 microphone, MAX98357A amplifier and speaker. Talks to the Pi and listens for the five help phrases.', 'battery': None},
    {'id': 'cam-robot-front', 'kind': 'camera', 'name': 'ESP32-CAM · Front camera', 'serial': 'cam-robot-front',
     'room': None, 'note': 'Forward-facing ESP32-CAM mounted on the robot body; travels with it.', 'battery': None},
    {'id': 'cam-robot-rear', 'kind': 'camera', 'name': 'ESP32-CAM · Rear camera', 'serial': 'cam-robot-rear',
     'room': None, 'note': 'Rear-facing ESP32-CAM on the robot body; travels with it. This is the rear, not a back room.', 'battery': None},
    {'id': 'robot-eldris', 'kind': 'robot', 'name': 'ELDRIS mobile robot', 'serial': 'chassis',
     'room': None, 'note': 'Wheeled body carrying the drive ESP32 and both ESP32-CAM boards.', 'battery': 78},
]

DEVICE_BY_ID = {d['id']: d for d in DEVICES}

# The two cameras that are part of the robot body.
ROBOT_CAMERA_IDS = ('cam-robot-front', 'cam-robot-rear')

_lock = threading.RLock()
_home = {}                  # latest home climate reading
_robot_room = db.get_setting('robot_room') or 'hallway'   # where the mobile robot currently is
_robot_at = db.get_setting('robot_at') or datetime.now(timezone.utc).isoformat()
_home_reported_at = None    # epoch seconds of the last real ESP32 push
_restored = [False]
ROOM_ONLINE_TIMEOUT_SECONDS = int(os.getenv('ROOM_ONLINE_TIMEOUT_SECONDS', '120'))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _round(value, digits=1):
    return round(float(value), digits)


def _restore_home():
    """Reload the last stored home climate reading once, after the DB exists."""
    global _home, _home_reported_at
    if _restored[0]:
        return
    with _lock:
        if _restored[0]:
            return
        stored = db.load_device_state(HOME_SENSOR_ID)
        if isinstance(stored, dict):
            for key in ('temperature_c', 'humidity_pct'):
                value = stored.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    _home[key] = _round(value)
            if _home:
                _home['updated_at'] = stored.get('updated_at') or _now_iso()
                _home_reported_at = 0.0   # "reported before", but not recently
        _restored[0] = True


def comfort_for(temp_c, humidity_pct=None):
    """Caregiver-friendly label for the home, safe for missing values."""
    if temp_c is None:
        return 'unknown'
    if temp_c < 16:
        return 'too cold'
    if temp_c < 19:
        return 'cool'
    if temp_c <= 24.5:
        return 'comfortable'
    if temp_c <= 28:
        return 'warm'
    return 'too hot'


def humidity_for(value):
    """Short descriptive banding for indoor humidity."""
    if value is None:
        return 'unknown'
    if value < 30:
        return 'dry'
    if value <= 60:
        return 'comfortable'
    if value <= 70:
        return 'humid'
    return 'very humid'


def _home_online():
    """The home node is online when it reported within the offline window."""
    if _home_reported_at is None:
        return False
    if _home_reported_at == 0.0:
        return False   # restored from the database, no packet since boot
    return (time.time() - _home_reported_at) < ROOM_ONLINE_TIMEOUT_SECONDS


def _build_home():
    temp = _home.get('temperature_c')
    humidity = _home.get('humidity_pct')
    return {
        'temperature_c': temp,
        'humidity_pct': humidity,
        'comfort': comfort_for(temp, humidity),
        'humidity_state': humidity_for(humidity),
        'online': _home_online(),
        'source': 'live' if _home else None,
        'reported': bool(_home),
        'battery_pct': DEVICE_BY_ID[HOME_SENSOR_ID].get('battery'),
        'updated_at': _home.get('updated_at'),
    }


def list_environment():
    """The one home climate reading plus a summary for the dashboard."""
    _restore_home()
    with _lock:
        home = _build_home()
        summary = {
            'temperature_c': home['temperature_c'],
            'humidity_pct': home['humidity_pct'],
            'comfort': home['comfort'],
            'humidity_state': home['humidity_state'],
        }
        return {'home': home, 'summary': summary, 'updated_at': _now_iso()}


def record_reading(payload):
    """Store one home climate report.

    Partial packets are fine: a missing field keeps its previous value. An
    older node that still sends ``room`` is accepted and its climate values
    land in the single home reading.
    """
    global _home, _home_reported_at
    _restore_home()
    current = dict(_home)

    def _num(key, low, high):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if low <= value <= high else None

    temp = _num('temperature_c', -40, 80)
    humidity = _num('humidity_pct', 0, 100)
    if temp is not None:
        current['temperature_c'] = _round(temp)
    if humidity is not None:
        current['humidity_pct'] = _round(humidity)
    battery = payload.get('battery_pct')
    if isinstance(battery, (int, float)) and not isinstance(battery, bool):
        DEVICE_BY_ID[HOME_SENSOR_ID]['battery'] = max(0, min(100, int(battery)))
        current['battery_pct'] = DEVICE_BY_ID[HOME_SENSOR_ID]['battery']
    if temp is None and humidity is None:
        return None, 'Send temperature_c, humidity_pct, or both.'
    current['updated_at'] = _now_iso()
    _home = current
    _home_reported_at = time.time()
    db.save_device_state(HOME_SENSOR_ID, {
        'temperature_c': current.get('temperature_c'),
        'humidity_pct': current.get('humidity_pct'),
        'battery_pct': DEVICE_BY_ID[HOME_SENSOR_ID].get('battery'),
    }, current['updated_at'])
    return _build_home(), None


def navigation_zones():
    """Rooms the mobile robot can be sent to. Navigation only, no sensors."""
    return [{'room': room['id'], 'room_name': room['name'], 'floor': room['floor']} for room in ROOMS]


def camera_online(camera_id):
    """Reachability is probed by cameras.CameraService; app.py merges it in."""
    try:
        import cameras
    except ImportError:
        return False
    return bool(cameras.CameraService().probe(camera_id)[0])


def list_devices(camera_status=None, drive_status=None):
    """Hardware inventory with live status resolved for each device.

    ``camera_status`` and ``drive_status`` are supplied by app.py so this module
    never has to make network calls of its own.
    """
    _restore_home()
    camera_status = camera_status or {}
    drive_status = drive_status or {}
    with _lock:
        items = []
        motion_status = motion.device_status()
        for device in DEVICES:
            # The chassis and both of its cameras move together.
            follows_robot = device['id'] in ('robot-eldris',) + ROBOT_CAMERA_IDS
            room_id = _robot_room if follows_robot else device['room']
            entry = {
                'id': device['id'],
                'kind': device['kind'],
                'name': device['name'],
                'serial': device.get('serial'),
                'room': room_id,
                'room_name': ROOM_NAME_BY_ID.get(room_id) if room_id else None,
                'note': device['note'],
                'battery_pct': device['battery'],
                'follows_robot': bool(follows_robot and device['kind'] == 'camera'),
                'address': None,
                'online': False,
                'last_seen_at': None,
            }
            if device['id'] == 'pi-4':
                entry['online'] = True
                entry['address'] = 'this server'
            elif device['id'] == DRIVE_ID:
                entry['online'] = bool(drive_status.get('drive_connected'))
                entry['address'] = drive_status.get('drive_url')
                entry['last_seen_at'] = drive_status.get('updated_at') if entry['online'] else None
            elif device['id'] in ROBOT_CAMERA_IDS:
                info = camera_status.get(device['id']) or {}
                entry['online'] = bool(info.get('online'))
                entry['address'] = info.get('ip')
                entry['last_seen_at'] = info.get('checked_at') and datetime.fromtimestamp(
                    info['checked_at'], timezone.utc).isoformat()
                entry['error'] = info.get('error') if not entry['online'] else None
            elif device['id'] == HOME_SENSOR_ID:
                entry['online'] = _home_online()
                entry['address'] = os.getenv('ESP32_HOME_IP') or None
                entry['last_seen_at'] = _home.get('updated_at')
            elif device['id'] == 'robot-eldris':
                entry['online'] = bool(drive_status.get('drive_connected'))
                entry['last_seen_at'] = drive_status.get('updated_at') if entry['online'] else None
            elif device['id'] == 'voice-esp':
                status = voice.device_status()
                entry['online'] = bool(status.get('online'))
                entry['last_seen_at'] = status.get('last_seen_at')
                entry['address'] = os.getenv('VOICE_ESP_IP') or None
            elif device['id'] == 'body-band':
                status = motion_status.get(device['id'], {})
                entry['online'] = bool(status.get('online'))
                entry['battery_pct'] = status.get('battery_pct', device['battery'])
                entry['last_seen_at'] = status.get('last_seen_at')
                entry['hand'] = status.get('hand')
                entry['address'] = None
            if entry['battery_pct'] is not None:
                entry['low_battery'] = entry['battery_pct'] < 25
            items.append(entry)
        online = sum(1 for i in items if i['online'])
        return {
            'items': items,
            'summary': {
                'total': len(items),
                'online': online,
                'offline': len(items) - online,
                'by_kind': {kind: sum(1 for i in items if i['kind'] == kind)
                            for kind in sorted(set(i['kind'] for i in items))},
            },
            'updated_at': _now_iso(),
        }


def get_robot_location():
    """Where the mobile robot currently is; both body cameras follow it."""
    with _lock:
        return {
            'room': _robot_room,
            'room_name': ROOM_NAME_BY_ID.get(_robot_room, 'Unknown'),
            'camera': ROBOT_CAMERA_IDS[0],
            'cameras': list(ROBOT_CAMERA_IDS),
            'camera_online': True,
            'updated_at': _robot_at,
        }


def set_robot_location(room_id):
    """Move the mobile robot (and both of its cameras) to another room."""
    global _robot_room, _robot_at
    room_id = str(room_id or '').strip().lower()
    if room_id not in ROOM_BY_ID:
        return None, 'Unknown room. Use one of: ' + ', '.join(sorted(ROOM_BY_ID.keys())) + '.'
    with _lock:
        previous = _robot_room
        _robot_room = room_id
        _robot_at = _now_iso()
        db.set_setting('robot_room', _robot_room)
        db.set_setting('robot_at', _robot_at)
        return {'room': room_id, 'room_name': ROOM_NAME_BY_ID[room_id], 'previous': previous, 'updated_at': _robot_at}, None
