import os
import io
import re
import hmac
import json
import wave
import shutil
import secrets
import subprocess
import tempfile
import threading
import time
import base64
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, Response, jsonify, request, session, send_from_directory, stream_with_context
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_socketio import SocketIO
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from db import init_db, list_reminders, create_reminder, update_reminder, delete_reminder, list_alerts, update_alert, create_alert, list_activity, log_activity, create_user, get_user_by_email, get_user_by_id, create_motion_event, list_motion_events
from hardware import HardwareService
from rooms import list_environment, record_reading, list_devices, get_robot_location, set_robot_location, navigation_zones, DRIVE_ID
from cameras import CameraService, CAMERA_IDS
import assistant
import motion
import voice

load_dotenv()
app = Flask(__name__, static_folder=None)
# Behind a reverse proxy (Render, Cloudflare Tunnel, nginx) the browser is on
# HTTPS while Flask sees plain HTTP on the internal hop. Trust one proxy hop so
# url_for(), request.scheme and secure cookies match what the visitor sees.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
# Override SESSION_SECRET in backend/.env for deployments; a generated key is suitable only for local development.
app.config.update(SECRET_KEY=os.getenv('SESSION_SECRET') or secrets.token_hex(32), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE','false').lower() == 'true', PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
origins = [x.strip() for x in os.getenv('CORS_ORIGINS','http://localhost:5173').split(',') if x.strip()]
CORS(app, resources={r"/api/*": {"origins": origins}}, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins=origins or None, async_mode='threading')
init_db()
motion.ensure_state_loaded()
voice.restore()
hardware = HardwareService(socketio)
cameras = CameraService()

# --- device ingress ----------------------------------------------------------
# The ESP32 nodes are not browsers, so they cannot hold a caregiver session.
# They present a shared device key instead; without a key configured the ingest
# endpoints stay session-only. The drive controller and both ESP32-CAM boards
# use the same key for their telemetry and snapshots.
DEVICE_API_KEY = os.getenv('DEVICE_API_KEY', '').strip()
DEVICE_INGEST_PATHS = {'/api/readings', '/api/motion', '/api/voice', '/api/voice/listen',
                       '/api/voice/audio', '/api/drive/telemetry'}

CAMERA_NAMES = {'cam-robot-front': 'Front body camera', 'cam-robot-rear': 'Rear body camera'}


def _device_authorised():
    if not DEVICE_API_KEY or request.path not in DEVICE_INGEST_PATHS:
        return False
    supplied = request.headers.get('X-Device-Key', '')
    return bool(supplied) and hmac.compare_digest(supplied, DEVICE_API_KEY)

def now_iso(): return datetime.now(timezone.utc).isoformat()
def error(message, code=400): return jsonify({'error':message}), code
def emit_status():
    socketio.emit('status_update', hardware.status())
    socketio.emit('activity_update', {'at':now_iso()})

@app.before_request
def require_session_for_api():
    # Keep health and authentication endpoints public; all robot and caregiver data endpoints require a session.
    if request.path.startswith('/api/') and request.method != 'OPTIONS':
        if request.path in {'/api/health', '/api/auth/signup', '/api/auth/signin', '/api/auth/guest', '/api/auth/me', '/api/auth/logout'}:
            return None
        if _device_authorised():
            return None
        if not (session.get('user_id') or session.get('guest')):
            return jsonify({'error':'Please sign in to continue.'}), 401
    return None

# Local access without an account. The dashboard opens straight into this
# session so the caregiver never has to sign in to watch the home; the sign-in
# page stays available for anyone who wants a named account.
GUEST_USER = {'id': 0, 'full_name': 'Guest caregiver', 'email': 'Local guest session', 'created_at': None}


@app.post('/api/auth/guest')
def guest_session():
    session.clear()
    session['guest'] = True
    session.permanent = True
    return jsonify({'message': 'Guest session started.', 'user': GUEST_USER, 'guest': True})

@app.post('/api/auth/signup')
def signup():
    data = request.get_json(silent=True) or {}
    full_name = data.get('full_name')
    email = data.get('email')
    password = data.get('password')
    if not isinstance(full_name, str) or not 2 <= len(full_name.strip()) <= 80:
        return error('Full name must be between 2 and 80 characters.')
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email.strip()):
        return error('Enter a valid email address.')
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        return error('Password must be between 10 and 128 characters.')
    user = create_user(full_name.strip(), email.strip(), generate_password_hash(password, method='scrypt'))
    if user is None:
        return error('An account with this email already exists.', 409)
    log_activity('auth', f'Caregiver account created: {user["email"]}')
    return jsonify({'message':'Account created. Please sign in.', 'user':user}), 201

@app.post('/api/auth/signin')
def signin():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')
    if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or len(password) > 128:
        return error('Enter your email and password.', 400)
    user = get_user_by_email(email.strip())
    if not user or not check_password_hash(user['password_hash'], password):
        return error('Email or password is incorrect.', 401)
    session.clear()
    session['user_id'] = user['id']
    session.permanent = True
    safe_user = {'id':user['id'], 'full_name':user['full_name'], 'email':user['email'], 'created_at':user['created_at']}
    log_activity('auth', f'Caregiver signed in: {user["email"]}')
    return jsonify({'message':'Signed in successfully.', 'user':safe_user})

@app.get('/api/auth/me')
def current_user():
    if session.get('guest') and not session.get('user_id'):
        return jsonify({'authenticated': True, 'user': GUEST_USER, 'guest': True})
    user_id = session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    if not user:
        session.clear()
        return jsonify({'authenticated':False, 'user':None, 'guest':False})
    return jsonify({'authenticated':True, 'user':user, 'guest':False})

@app.post('/api/auth/logout')
def logout():
    session.clear()
    return jsonify({'message':'Signed out successfully.'})

@app.get('/api/health')
def health(): return jsonify({'ok':True,'service':'ELDRIS 3.0 API','time':now_iso()})

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def frontend(path):
    if path.startswith(('api/', 'socket.io/')):
        return error('Not found.', 404)
    dist = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
    requested = dist / path
    if path and requested.is_file():
        return send_from_directory(dist, path)
    if (dist / 'index.html').is_file():
        return send_from_directory(dist, 'index.html')
    return error('Frontend build is missing.', 404)

@app.get('/api/status')
def status(): return jsonify(hardware.status())

@app.post('/api/movement')
def movement():
    data=request.get_json(silent=True) or {}
    command=data.get('command')
    if not isinstance(command,str) or command not in {'forward','reverse','left','right','stop'}: return error('Invalid command. Use forward, reverse, left, right, or stop.')
    result=hardware.command(command)
    if not result['ok']: return jsonify({'error':result['error']}), 409
    log_activity('movement',f'Movement command issued: {command}')
    emit_status()
    return jsonify({'message':result['message'],'status':hardware.status()})

# --- drive ESP32 -------------------------------------------------------------
# The board can also push its own telemetry, which is what makes the dashboard
# work when the server is not on the same LAN as the robot (a public host, for
# example). app.py keeps it in the same cache the poller uses.
@app.post('/api/drive/telemetry')
def drive_telemetry():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return error('Send a JSON object with the drive telemetry.')
    state = hardware.ingest_telemetry(data)
    socketio.emit('status_update', state)
    socketio.emit('activity_update', {'at': now_iso()})
    return jsonify({'status': state}), 201

@app.get('/api/drive')
def drive():
    """The drive ESP32 link, as the settings page shows it."""
    state = hardware.status()
    return jsonify({'connected': state['drive_connected'], 'url': state['drive_url'],
                    'error': state['drive_error'], 'hint': state['drive_hint'],
                    'movement': state['movement'],
                    'speed_pct': state['speed_pct'], 'rssi': state['rssi'],
                    'last_command': state['last_command'], 'updated_at': state['updated_at']})

@app.get('/api/reminders')
def reminders(): return jsonify({'items':list_reminders()})

@app.post('/api/reminders')
def add_reminder():
    data=request.get_json(silent=True) or {}
    title=data.get('title'); category=data.get('category','Other'); scheduled=data.get('scheduled_at')
    if not isinstance(title,str) or not title.strip() or len(title)>100: return error('Title is required and must be 100 characters or fewer.')
    if not isinstance(scheduled,str) or len(scheduled)>50: return error('A valid scheduled_at ISO timestamp is required.')
    allowed={'Medication','Hydration','Daily activity','Check-in','Appointment','Other'}
    if category not in allowed: return error('Invalid reminder category.')
    item=create_reminder(title.strip(),category,scheduled,bool(data.get('enabled',True)))
    log_activity('reminder',f'Reminder scheduled: {title.strip()}')
    socketio.emit('activity_update',{'at':now_iso()})
    return jsonify({'item':item}),201

@app.patch('/api/reminders/<int:item_id>')
def patch_reminder(item_id):
    data=request.get_json(silent=True) or {}
    if not set(data).issubset({'title','category','scheduled_at','enabled','status'}): return error('Unsupported reminder fields.')
    item=update_reminder(item_id,data)
    if item is None: return error('Reminder not found.',404)
    return jsonify({'item':item})

@app.delete('/api/reminders/<int:item_id>')
def remove_reminder(item_id):
    if not delete_reminder(item_id): return error('Reminder not found.',404)
    log_activity('reminder',f'Reminder deleted: #{item_id}')
    return jsonify({'ok':True})

@app.get('/api/alerts')
def alerts(): return jsonify({'items':list_alerts()})

@app.post('/api/alerts')
def add_alert():
    data=request.get_json(silent=True) or {}
    kind=data.get('type'); description=data.get('description')
    if not isinstance(kind,str) or not isinstance(description,str) or len(kind)>60 or len(description)>500: return error('type and description are required (maximum 60 and 500 characters).')
    severity=data.get('severity','medium')
    if severity not in {'low','medium','high','critical'}: return error('Invalid severity.')
    item=create_alert(kind,description,severity,bool(data.get('simulated',False)))
    log_activity('alert',f'Alert received: {kind}')
    socketio.emit('activity_update',{'at':now_iso()})
    return jsonify({'item':item}),201

@app.patch('/api/alerts/<int:item_id>')
def patch_alert(item_id):
    data=request.get_json(silent=True) or {}; state=data.get('status')
    if state not in {'acknowledged','resolved'}: return error('status must be acknowledged or resolved.')
    item=update_alert(item_id,state)
    if item is None: return error('Alert not found.',404)
    log_activity('alert',f'Alert #{item_id} {state}')
    return jsonify({'item':item})

@app.get('/api/activity')
def activity():
    search=request.args.get('search','')[:100]; category=request.args.get('category','all')[:50]
    return jsonify({'items':list_activity(search,category)})

def camera_overview():
    """Camera availability, addresses and privacy posture, shared by the API and the assistant."""
    overview = cameras.overview(CAMERA_NAMES)
    robot = get_robot_location()
    for source in overview['sources']:
        source['room_name'] = robot['room_name']
        source['follows_robot'] = True
    overview['face_present'] = None
    overview['last_detection'] = None
    return overview

@app.get('/api/camera')
def camera():
    return jsonify(camera_overview())

def _camera_names():
    return {s['id']: s['name'] for s in camera_overview()['sources']}

@app.get('/api/camera/<device_id>/snapshot')
def camera_snapshot(device_id):
    device_id = (device_id or '').strip().lower()
    if device_id not in CAMERA_IDS:
        return error('Unknown camera.', 404)
    try:
        data, content_type = cameras.read_snapshot(device_id)
    except LookupError:
        return error('No address is configured for this camera. Set CAMERA_ESP_FRONT_IP or CAMERA_ESP_REAR_IP.', 404)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log_activity('camera', 'Camera snapshot failed for {}: {}'.format(device_id, exc))
        return error('The ESP32-CAM did not return a frame. Check its power and Wi-Fi.', 503)
    return Response(data, mimetype=content_type, headers={'Cache-Control': 'no-store'})

@app.get('/api/camera/<device_id>/stream')
def camera_stream(device_id):
    """Relay the ESP32-CAM's own MJPEG stream to the signed-in browser.

    The frame data is passed straight through and never stored. The browser
    only ever sees this same-origin URL, so no camera address or port has to be
    exposed to the page.
    """
    device_id = (device_id or '').strip().lower()
    if device_id not in CAMERA_IDS:
        return error('Unknown camera.', 404)
    try:
        upstream, content_type = cameras.open_stream(device_id)
    except LookupError:
        return error('No address is configured for this camera. Set CAMERA_ESP_FRONT_IP or CAMERA_ESP_REAR_IP.', 404)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log_activity('camera', 'Camera stream failed for {}: {}'.format(device_id, exc))
        return error('The ESP32-CAM stream is not reachable. Check its power and Wi-Fi.', 503)

    def relay():
        try:
            while True:
                chunk = upstream.read(16384)
                if not chunk:
                    break
                yield chunk
        except (urllib.error.URLError, OSError):
            return
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return Response(stream_with_context(relay()), mimetype=content_type,
                    headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

# --- speaker -----------------------------------------------------------------
# Speech engines, in the order we prefer them on a Raspberry Pi. The caregiver
# can override the whole thing with TTS_COMMAND, which must contain {text}.
TTS_ENGINES = [
    ('espeak-ng', ['-v', 'en-gb', '-s', '150', '-p', '45']),
    ('espeak', ['-v', 'en', '-s', '150', '-p', '45']),
    ('say', ['-r', '170']),
]


def tts_command(text):
    """Build the argv for the first available speech engine, or explain why not."""
    template = os.getenv('TTS_COMMAND', '').strip()
    if template:
        if '{text}' not in template:
            return None, 'TTS_COMMAND must contain the {text} placeholder.'
        return [part.replace('{text}', text) for part in template.split()], None
    for name, options in TTS_ENGINES:
        path = shutil.which(name)
        if path:
            return [path] + options + [text], None
    return None, ('No speech engine found on this machine. Install espeak-ng on the Raspberry Pi '
                  'or set TTS_COMMAND, for example TTS_COMMAND=espeak-ng -v en -s 150 {text}')


def speak_text(text):
    """Speak through the Pi's speaker. Returns (ok, message)."""
    text = (text or '').strip()
    if not text:
        return False, 'Send the text to speak.'
    if len(text) > int(os.getenv('TTS_MAX_CHARS', '400')):
        return False, 'Text to speak is limited to {} characters.'.format(os.getenv('TTS_MAX_CHARS', '400'))
    argv, problem = tts_command(text)
    if argv is None:
        return False, problem
    try:
        result = subprocess.run(argv, timeout=float(os.getenv('TTS_TIMEOUT_SECONDS', '15')),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, 'The speaker could not be reached: {}'.format(exc)
    if result.returncode != 0:
        detail = (result.stderr or b'').decode('utf-8', 'ignore').strip()[:200]
        return False, 'The speech engine failed ({}).'.format(detail or 'exit code {}'.format(result.returncode))
    log_activity('audio', 'Spoken on the speaker: {}'.format(text[:80]))
    return True, 'Spoken on the ELDRIS speaker.'


def speech_wav_bytes(text):
    """Render a phrase to WAV so the chatbot board can speak it itself.

    espeak-ng and espeak can write a file; the macOS ``say`` engine cannot in a
    way the ESP32 can play, so it is skipped here and the board falls back to
    the text. Returns None rather than a broken file.
    """
    text = (text or '').strip()
    if not text:
        return None
    text = text[:int(os.getenv('TTS_MAX_CHARS', '400'))]
    for name in ('espeak-ng', 'espeak'):
        path = shutil.which(name)
        if not path:
            continue
        handle = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        handle.close()
        try:
            subprocess.run([path, '-v', 'en-gb', '-s', '150', '-p', '45', '-w', handle.name, text],
                           timeout=float(os.getenv('TTS_TIMEOUT_SECONDS', '15')),
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with open(handle.name, 'rb') as audio:
                return audio.read()
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
    return None

@app.post('/api/speak')
def speak():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    ok, message = speak_text(data.get('text'))
    if not ok:
        return error(message, 503 if 'engine' in message or 'speaker' in message else 400)
    socketio.emit('activity_update', {'at': now_iso()})
    return jsonify({'message': message})

# --- Home climate, device inventory and the caregiver assistant ---

@app.get('/api/readings')
def readings():
    """The single home climate reading (temperature and humidity)."""
    return jsonify(list_environment())

@app.post('/api/readings')
def push_reading():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    item, err = record_reading(data)
    if err:
        return error(err)
    log_activity('sensor', 'Home climate reading received')
    socketio.emit('reading_update', {'sensor': 'home', 'at': now_iso()})
    return jsonify({'item': item}), 201

@app.get('/api/devices')
def devices():
    overview = camera_overview()
    camera_status = {s['id']: s for s in overview['sources']}
    return jsonify(list_devices(camera_status=camera_status, drive_status=hardware.status()))

# --- hand-band motion, falls and the emergency view -------------------------

def motion_payload():
    """Hand-motion state with the caregiver alerts merged into one verdict."""
    payload = motion.snapshot()
    payload['emergency'] = motion.emergency_status(payload, list_alerts())
    return payload


def record_motion_events(events):
    """Persist motion events and raise caregiver alerts for the serious ones."""
    recorded = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        recorded.append(create_motion_event(event.get('kind', 'motion'), event.get('level', 'low'),
                                            event.get('device', ''), event.get('device_name', ''),
                                            event.get('detail', ''), event.get('value'),
                                            event.get('source', 'live')))
        log_activity('motion', event.get('detail', 'Motion event'))
        if motion.should_alert(event):
            kind, severity, detail = motion.alert_for(event)
            create_alert(kind, detail, severity, False)
    return recorded


def broadcast_motion():
    state = motion_payload()['emergency']
    socketio.emit('motion_update', {'at': now_iso()})
    socketio.emit('emergency_update', state)
    socketio.emit('activity_update', {'at': now_iso()})
    return state

@app.get('/api/motion')
def get_motion():
    return jsonify(motion_payload())

@app.post('/api/motion')
def push_motion():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    items, events, err = motion.ingest(data)
    if err:
        return error(err)
    recorded = record_motion_events(events)
    state = broadcast_motion()
    return jsonify({'items': items, 'events': recorded, 'emergency': state}), 201

@app.get('/api/motion/events')
def motion_history():
    kind = (request.args.get('kind') or 'all')[:20]
    limit = request.args.get('limit', '50')
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({'items': list_motion_events(limit, kind)})

@app.get('/api/emergency')
def emergency():
    return jsonify(motion_payload()['emergency'])

@app.post('/api/emergency/acknowledge')
def acknowledge_emergency():
    """Acknowledge every open emergency alert, which clears the red dashboard.

    An alert that is still being raised by live motion, such as an ongoing
    tremor, reopens on the next check, so this only silences what is over.
    """
    cleared = []
    for alert in list_alerts():
        if alert.get('status') == 'resolved':
            continue
        if alert.get('type') in motion.EMERGENCY_TYPES and alert.get('severity') in motion.EMERGENCY_SEVERITIES:
            item = update_alert(alert['id'], 'acknowledged')
            if item:
                cleared.append(item['id'])
    if cleared:
        log_activity('alert', 'Emergency acknowledged by the caregiver ({} alert(s))'.format(len(cleared)))
    state = broadcast_motion()
    return jsonify({'acknowledged': cleared, 'emergency': state})

@app.get('/api/robot/location')
def robot_location():
    return jsonify(get_robot_location())

@app.get('/api/robot/rooms')
def robot_rooms():
    """Navigation zones the robot can be sent to. No room sensors are implied."""
    return jsonify({'items': navigation_zones()})

@app.post('/api/robot/location')
def relocate_robot():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    item, err = set_robot_location(data.get('room'))
    if err:
        return error(err)
    log_activity('movement', 'Robot relocated to {}'.format(item['room_name']))
    socketio.emit('activity_update', {'at': now_iso()})
    return jsonify({'item': item})

def chat_context():
    # Live data the assistant may quote. Cheap: these read in-memory state or
    # single-row selects, never an unbounded query.
    return {
        'status': hardware.status,
        'readings': list_environment,
        'rooms': navigation_zones,
        'devices': lambda: list_devices(camera_status={s['id']: s for s in camera_overview()['sources']},
                                        drive_status=hardware.status()),
        'robot_location': get_robot_location,
        'reminders': list_reminders,
        'alerts': list_alerts,
        'motion': motion_payload,
        'camera': camera_overview,
        'voice': voice.snapshot,
    }

def chat_actions():
    # Commands the assistant is allowed to trigger. Both are already guarded by
    # the same safety checks as the buttons on the Robot Control page.
    def move(command):
        result = hardware.command(command, source='assistant')
        if result['ok']:
            log_activity('movement', 'Movement command issued by assistant: {}'.format(command))
            emit_status()
            return {'ok': True, 'message': result['message']}
        return {'ok': False, 'message': result['error']}
    def move_robot_to(room):
        item, err = set_robot_location(room)
        if err:
            return {'error': err}
        log_activity('movement', 'Robot relocated to {}'.format(item['room_name']))
        socketio.emit('activity_update', {'at': now_iso()})
        return item
    def speak(text):
        ok, message = speak_text(text)
        if ok:
            socketio.emit('activity_update', {'at': now_iso()})
            return {'ok': True, 'message': message}
        return {'ok': False, 'message': message}
    return {'move': move, 'move_robot_to': move_robot_to, 'speak': speak}

@app.post('/api/chat')
def chat():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    message = data.get('message')
    if not isinstance(message, str) or not message.strip():
        return error('Send a message to talk to the ELDRIS assistant.')
    if len(message) > 500:
        return error('Messages are limited to 500 characters.')
    history = data.get('history')
    if not isinstance(history, list):
        history = []
    reply, source = assistant.respond(message, history, chat_context(), chat_actions())
    return jsonify({'reply': reply, 'source': source, 'at': now_iso()})

# --- chatbot ESP32: five help phrases, the recording window and escalation ---
# ESP32 #4 carries the microphone, the amplifier and the speaker. What it hears
# arrives here as text (the Pi does the speech-to-text) or as a WAV recording.
# A help phrase is the only input that moves the robot without someone pressing
# a button, so the motion is bounded from both ends: the server asks for at most
# HELP_APPROACH_SECONDS of creeping, and the drive board stops itself after 2 s
# without a new command.

def _approach_patient(seconds):
    """Creep towards the patient, then always stop. Never raises."""
    deadline = time.time() + max(0.0, float(seconds))
    moved = False
    failure = None
    try:
        while time.time() < deadline:
            result = hardware.command('forward', source='voice')
            if not result.get('ok'):
                failure = result.get('error')
                break
            moved = True
            time.sleep(0.8)
        hardware.command('stop', source='voice')
    except Exception as exc:   # a dead link must never leave the motors running
        failure = str(exc)
        hardware._safe_stop()
    finally:
        log_activity('movement', 'Help approach finished: {}'.format(
            'robot moved forward and stopped' if moved else
            'robot did not move ({})'.format(failure or 'drive ESP32 offline')))
        try:
            socketio.emit('status_update', hardware.status())
            socketio.emit('activity_update', {'at': now_iso()})
        except Exception:
            pass


def escalate_help(trigger, room=None):
    """A help phrase was heard: alert the caregiver, record, send the robot."""
    trigger = trigger or {}
    keyword = trigger.get('keyword') or 'help'
    heard = str(trigger.get('heard') or '')[:180]
    detail = 'Help phrase "{}" heard by the chatbot ESP32: "{}"'.format(keyword, heard)
    create_alert('help_request', detail, 'critical', False)
    log_activity('audio', detail)
    voice.remember(trigger)
    placed, _problem = set_robot_location(room or voice.PATIENT_ROOM)
    if placed is None:
        placed, _problem = set_robot_location(voice.PATIENT_ROOM)
    if placed:
        log_activity('movement', 'Robot sent to the {} after a help request'.format(placed['room_name']))
    voice.start_recording()
    if voice.HELP_APPROACH_SECONDS > 0:
        threading.Thread(target=_approach_patient, args=(voice.HELP_APPROACH_SECONDS,),
                         daemon=True, name='eldris-help-approach').start()
    state = motion_payload()['emergency']
    socketio.emit('emergency_update', state)
    socketio.emit('motion_update', {'at': now_iso()})
    socketio.emit('activity_update', {'at': now_iso()})
    socketio.emit('voice_update', voice.snapshot())
    return state


def answer_voice_text(text, speak=True):
    """Shared by the chatbot board, the Pi loop and the audio endpoint."""
    trigger = None
    if voice.detect(text) and voice.cooldown_left() <= 0:
        result, err = voice.ingest({'text': text})
        if not err and result.get('trigger'):
            trigger = result['trigger']
            escalate_help(trigger)
    if trigger:
        reply = ('Help is on the way. I heard "{}" and the robot is coming to you. '
                 'A caregiver has been alerted.').format(trigger.get('keyword'))
        return reply, 'help', trigger, speak_text(reply)[1] if speak else None
    reply, source = assistant.respond(text, [], chat_context(), chat_actions())
    return reply, source, None, speak_text(reply)[1] if speak else None


@app.get('/api/voice/help')
def voice_help():
    """The five phrases, the last one heard and the recording window."""
    return jsonify(voice.snapshot())


@app.post('/api/voice/listen')
def voice_listen():
    """One transcript from the chatbot ESP32. Escalates on a help phrase."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    result, err = voice.ingest(data)
    if err:
        return error(err)
    state = None
    if result['escalated'] and result.get('trigger'):
        state = escalate_help(result['trigger'], data.get('room'))
    else:
        socketio.emit('voice_update', voice.snapshot())
    return jsonify({'result': result, 'emergency': state or motion_payload()['emergency']}), 201


# --- speech: optional cloud model, local Vosk transcription ------------------
# The Pi does the listening for the microphone on ESP32 #4. Vosk is optional: if
# it is not installed the endpoint explains how to add it instead of pretending
# it understood something.
_vosk = [None, None]


def _load_vosk():
    if _vosk[0] is not None:
        return _vosk[0], None
    if _vosk[1]:
        return None, _vosk[1]
    try:
        from vosk import Model, KaldiRecognizer, SetLogLevel
    except ImportError:
        _vosk[1] = 'Speech-to-text is not installed on the Pi. Run: pip3 install vosk'
        return None, _vosk[1]
    folder = os.getenv('VOSK_MODEL', 'vosk-model-small-en-us')
    if not os.path.isdir(folder):
        _vosk[1] = ('The Vosk model is not in {}. Download vosk-model-small-en-us-0.15 and '
                    'unzip it there, then restart the API.').format(folder)
        return None, _vosk[1]
    SetLogLevel(-1)
    _vosk[0] = (Model(folder), KaldiRecognizer)
    return _vosk[0], None


def _as_wav(data, rate=16000):
    """Wrap raw 16-bit mono PCM in a WAV header; pass a WAV file through."""
    if data[:4] == b'RIFF':
        return data
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(max(8000, min(48000, int(rate))))
        handle.writeframes(data)
    return buffer.getvalue()


def transcribe(data, rate=16000):
    """Return (text, error). Never invents words when the engine is missing."""
    loaded, problem = _load_vosk()
    if loaded is None:
        return None, problem
    model, recogniser = loaded
    try:
        with wave.open(io.BytesIO(_as_wav(data, rate)), 'rb') as handle:
            audio = recogniser(model, handle.getframerate())
            while True:
                chunk = handle.readframes(4000)
                if not chunk:
                    break
                audio.AcceptWaveform(chunk)
        return (json.loads(audio.FinalResult()).get('text') or '').strip(), None
    except (wave.Error, ValueError, OSError) as exc:
        return None, 'That recording could not be read: {}'.format(exc)


@app.post('/api/voice/audio')
def voice_audio():
    """A recording from the chatbot ESP32, transcribed here and answered."""
    data = request.get_data(cache=False)
    if not data:
        return error('Send the recorded audio as the request body (16-bit mono PCM or WAV).')
    if len(data) > int(os.getenv('VOICE_AUDIO_MAX_BYTES', '900000')):
        return error('Recordings are limited to {} bytes.'.format(os.getenv('VOICE_AUDIO_MAX_BYTES', '900000')))
    try:
        rate = int(request.headers.get('X-Sample-Rate', '16000'))
    except (TypeError, ValueError):
        rate = 16000
    text, problem = transcribe(data, rate)
    voice.note_seen()
    if text is None:
        return error(problem or 'Speech-to-text is unavailable.', 503)
    if not text:
        return jsonify({'text': '', 'reply': 'I did not catch that. Try again a little closer to the microphone.',
                        'spoken': None, 'help': None}), 200
    reply, source, trigger, spoken = answer_voice_text(text, speak=bool(request.args.get('speak')))
    log_activity('audio', 'Voice question: {}'.format(text[:120]))
    socketio.emit('activity_update', {'at': now_iso()})
    wav = speech_wav_bytes(reply)
    return jsonify({'text': text, 'reply': reply, 'source': source,
                    'help': trigger, 'spoken': spoken,
                    'audio_wav': base64.b64encode(wav).decode('ascii') if wav else None})


@app.post('/api/voice')
def voice_chat():
    """A question from the Pi voice loop or the chatbot board, answered here."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    message = data.get('message')
    if not isinstance(message, str) or not message.strip():
        return error('Send the recognised speech as message.')
    if len(message) > 500:
        return error('Voice messages are limited to 500 characters.')
    message = message.strip()
    reply, source, trigger, spoken = answer_voice_text(message, speak=bool(data.get('speak', True)))
    log_activity('audio', 'Voice question: {}'.format(message[:120]))
    socketio.emit('activity_update', {'at': now_iso()})
    return jsonify({'reply': reply, 'source': source, 'spoken': spoken, 'help': trigger})

@socketio.on('connect')
def on_connect(auth=None):
    if not (session.get('user_id') or session.get('guest')):
        return False
    socketio.emit('status_update',hardware.status(),to=request.sid)

def background_status():
    last_emergency = None
    while True:
        socketio.sleep(2)
        hardware.check_timeout()
        emit_status()
        try:
            state = motion_payload()['emergency']
        except Exception as exc:   # a detector fault must never kill the status loop
            print('[ELDRIS] Motion snapshot failed: {}'.format(exc))
            continue
        signature = json.dumps({'active': state['active'], 'level': state['level'],
                                'since': state['since'], 'count': len(state['sources'])}, sort_keys=True)
        if signature != last_emergency:
            last_emergency = signature
            socketio.emit('emergency_update', state)
            socketio.emit('motion_update', {'at': now_iso()})

socketio.start_background_task(background_status)

if __name__=='__main__':
    host=os.getenv('HOST','127.0.0.1'); port=int(os.getenv('PORT','5000'))
    socketio.run(app,host=host,port=port,debug=os.getenv('DEBUG','true').lower()=='true',use_reloader=False,allow_unsafe_werkzeug=True)
