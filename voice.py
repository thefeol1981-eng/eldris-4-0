# -*- coding: utf-8 -*-
"""The chatbot ESP32: five help phrases, a recording window and the escalation.

ESP32 #4 carries the INMP441 microphone, the MAX98357A amplifier and the
speaker. It is the room's voice front end: the Raspberry Pi transcribes and
answers, and this module is the server-side half of the promise that a person
who calls for help is heard even when nobody is looking at the dashboard.

Five phrases are watched for, exactly as the caregiver asked:

    help · help me · please help · i need help · i have fallen

Any of them, heard by the chatbot board, does three things:

1. raises a `help_request` alert at critical severity, which paints the
   dashboard red and puts the headline "Help requested" in the banner;
2. opens a recording window, so the board keeps the audio of what happens next
   locally for the caregiver (the server stores the transcript, never audio);
3. asks app.py to send the robot to the patient's room and creep forward,
   bounded by a timer and stopped by the drive board's own command timeout.

Matching is deliberately simple and offline: the transcript is lowercased,
punctuation is dropped, and a phrase matches when its words appear
consecutively. Every rule is visible below, and the list is configurable with
HELP_KEYWORDS, so a false positive can be removed without a redeploy.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import db


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _iso(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _phrases():
    raw = (os.getenv('HELP_KEYWORDS') or '').strip()
    if not raw:
        return ['help', 'help me', 'please help', 'i need help', 'i have fallen']
    parts = [p.strip().lower() for p in re.split(r'[,;]', raw) if p.strip()]
    # The five spoken phrases, nothing else. A longer list makes the phrase
    # "help" unreachable because it matches first, so keep the order stable.
    return parts[:5] or ['help']


HELP_KEYWORDS = _phrases()
HELP_RECORD_SECONDS = _env_int('HELP_RECORD_SECONDS', 30)
HELP_COOLDOWN_SECONDS = _env_int('HELP_COOLDOWN_SECONDS', 45)
HELP_APPROACH_SECONDS = _env_int('HELP_APPROACH_SECONDS', 4)
VOICE_OFFLINE_SECONDS = _env_int('VOICE_OFFLINE_SECONDS', 120)
PATIENT_ROOM = (os.getenv('VOICE_PATIENT_ROOM') or 'bedroom').strip().lower()

VOICE_DEVICE_ID = 'voice-esp'
VOICE_DEVICE_NAME = 'ESP32 #4 · Chatbot'

_lock = threading.RLock()
_last_seen = [None]          # epoch seconds of the last packet from the board
_last_trigger = [None]       # the last help request, as a dict
_recording_until = [0.0]     # epoch seconds; > now means the window is open
_last_trigger_at = [0.0]     # cooldown clock


def _normalise(text):
    """Lowercase and drop punctuation, keeping words apart."""
    text = (text or '').lower()
    text = re.sub(r"[^a-z0-9\s']", ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def detect(text):
    """Return the help phrase heard in ``text``, or None.

    A phrase matches when its words appear consecutively: "please, I need
    help!" matches both "please help" and "i need help", and the longest
    phrase wins so the caregiver sees the most specific one.
    """
    spoken = _normalise(text)
    if not spoken:
        return None
    words = spoken.split()
    best = None
    for phrase in HELP_KEYWORDS:
        target = phrase.split()
        if not target:
            continue
        for index in range(0, len(words) - len(target) + 1):
            if words[index:index + len(target)] == target:
                if best is None or len(target) > len(best.split()):
                    best = phrase
                break
    return best


def note_seen():
    with _lock:
        _last_seen[0] = time.time()


def recording_state():
    with _lock:
        remaining = max(0.0, _recording_until[0] - time.time())
        return {
            'active': remaining > 0,
            'seconds_left': int(round(remaining)),
            'window_seconds': HELP_RECORD_SECONDS,
            'until': _iso(_recording_until[0]) if remaining > 0 else None,
            'stores_audio_on_server': False,
            'note': ('The chatbot ESP32 keeps the audio locally for the caregiver; '
                     'the server stores the transcript and the time only.'),
        }


def start_recording(seconds=None):
    with _lock:
        _recording_until[0] = time.time() + float(seconds or HELP_RECORD_SECONDS)
        return recording_state()


def stop_recording():
    with _lock:
        _recording_until[0] = 0.0
        return recording_state()


def cooldown_left():
    with _lock:
        return max(0.0, HELP_COOLDOWN_SECONDS - (time.time() - _last_trigger_at[0]))


def ingest(payload):
    """Read one transcript from the chatbot board.

    Returns ``(result, error)``. ``result['keyword']`` is set when one of the
    five phrases was heard; app.py then raises the alert and moves the robot.
    The cooldown keeps one shouted sentence from opening five alerts: a repeat
    inside HELP_COOLDOWN_SECONDS is reported as heard but not escalated.
    """
    if not isinstance(payload, dict):
        return None, 'Send a JSON object with the recognised speech as text.'
    text = payload.get('text') or payload.get('message') or payload.get('transcript')
    if not isinstance(text, str) or not text.strip():
        return None, 'Send the recognised speech as text.'
    text = text.strip()
    if len(text) > 500:
        return None, 'Transcripts are limited to 500 characters.'
    note_seen()
    keyword = detect(text)
    escalated = bool(keyword) and cooldown_left() <= 0
    trigger = None
    if escalated:
        with _lock:
            _last_trigger_at[0] = time.time()
            _last_trigger[0] = {
                'keyword': keyword,
                'heard': text[:200],
                'at': _now_iso(),
                'device': payload.get('device') or VOICE_DEVICE_ID,
            }
            trigger = dict(_last_trigger[0])
        start_recording(payload.get('record_seconds'))
    return {
        'heard': text,
        'keyword': keyword,
        'escalated': escalated,
        'cooldown_seconds_left': int(round(cooldown_left())),
        'trigger': trigger,
        'recording': recording_state(),
    }, None


def _approach_plan():
    """What the robot is asked to do when a help phrase is heard."""
    return {
        'room': PATIENT_ROOM,
        'approach_seconds': HELP_APPROACH_SECONDS,
        'enabled': HELP_APPROACH_SECONDS > 0,
        'note': ('The robot is sent to the patient room and creeps forward for at most '
                 '{} s, then stops. Set HELP_APPROACH_SECONDS=0 to move the label only.').format(
                     HELP_APPROACH_SECONDS),
    }


def snapshot():
    """Everything the dashboard shows about the voice node."""
    with _lock:
        last = _last_trigger[0]
        online = _last_seen[0] is not None and (time.time() - _last_seen[0]) <= VOICE_OFFLINE_SECONDS
        return {
            'device': {
                'id': VOICE_DEVICE_ID,
                'name': VOICE_DEVICE_NAME,
                'online': bool(online),
                'last_seen_at': _iso(_last_seen[0]),
                'note': 'INMP441 microphone, MAX98357A amplifier and speaker. Runs the ChatGPT conversation.',
            },
            'keywords': list(HELP_KEYWORDS),
            'last_trigger': last,
            'cooldown_seconds_left': int(round(cooldown_left())),
            'recording': recording_state(),
            'approach': _approach_plan(),
            'patient_room': PATIENT_ROOM,
            'updated_at': _now_iso(),
        }


def device_status():
    """Online state for the device inventory. Reads real packets only."""
    with _lock:
        last = _last_seen[0]
        return {
            'online': last is not None and (time.time() - last) <= VOICE_OFFLINE_SECONDS,
            'last_seen_at': _iso(last),
        }


def restore():
    """Reload the last help phrase after a restart.

    The alert row is already in SQLite, so this only restores the panel's "last
    heard" line. A corrupt or missing row leaves the panel empty instead of
    raising at import time.
    """
    try:
        stored = db.get_setting('voice_last_trigger')
    except Exception:
        return
    if not stored:
        return
    try:
        item = json.loads(stored)
    except (TypeError, ValueError):
        return
    if not isinstance(item, dict):
        return
    with _lock:
        if _last_trigger[0] is None:
            _last_trigger[0] = {
                'keyword': item.get('keyword'),
                'heard': str(item.get('heard') or '')[:200],
                'at': item.get('at'),
                'device': item.get('device') or VOICE_DEVICE_ID,
            }


def remember(trigger):
    """Store the last trigger so the dashboard can show it after a restart."""
    if not isinstance(trigger, dict):
        return
    try:
        db.set_setting('voice_last_trigger', json.dumps({
            'keyword': trigger.get('keyword'),
            'heard': trigger.get('heard'),
            'at': trigger.get('at'),
            'device': trigger.get('device'),
        }))
    except Exception:
        pass
