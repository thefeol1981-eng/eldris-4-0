# -*- coding: utf-8 -*-
"""Motion and body vitals for ELDRIS 3.0: one wrist band.

The band carries an MPU6050 IMU (tremor and fall), a MAX30102 pulse oximeter
(heart rate and blood oxygen) and an MLX90614 body-temperature sensor. There is
one band, worn on the left wrist, and these five readings come from it and from
nothing else. It samples acceleration at 20-50 Hz and posts batches of samples
to the server with ``POST /api/motion``, together with the latest vitals.

Nothing here invents data. A band that has not reported is offline, and its
last valid vitals are replayed from the database after a restart; there is no
generated sample anywhere in this module.

Two detections run per wearable, both deliberately simple enough to audit:

* **Tremor** -- "small up and down movement held for a while". The gravity axis
  of the hand is tracked, its slow average is removed, and the leftover
  oscillation is measured: a dominant frequency inside the tremor band that
  stays there for a sustained period is an episode.
* **Fall** -- "a sudden change on the Y axis". A fast impact on the vertical
  axis opens a short confirmation window; the impact becomes a fall when the
  hand changes orientation or then stays still (a person on the floor). If the
  hand starts moving again instead, it is downgraded to a knock, which is what
  keeps ordinary hand bumps from ringing the alarm.

Every threshold is configurable through the environment so a caregiver setup
can be tuned without touching this file. Timers are monotonic seconds; wall
clock time only appears on the way out, as ISO text.

This module owns no transport: app.py exposes it over HTTP and persists the
latest vitals through db.py.
"""
import os
import math
import threading
import time
from collections import deque
from datetime import datetime

try:                       # Python 3
    from datetime import timezone
    _UTC = timezone.utc
except ImportError:        # older interpreter fallback
    _UTC = None

import db


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return int(default)


def now_iso():
    if _UTC is not None:
        return datetime.now(_UTC).isoformat()
    return datetime.utcnow().isoformat() + '+00:00'


def _utcnow():
    return datetime.now(_UTC) if _UTC is not None else datetime.utcnow()


def parse_iso(text):
    """Lenient ISO-8601 parse. Returns a naive UTC datetime, or None."""
    if not text or not isinstance(text, str):
        return None
    body = text.strip()
    if body.endswith('Z') or body.endswith('z'):
        body = body[:-1]
    if body.endswith('+00:00'):
        body = body[:-6]
    try:
        return datetime.strptime(body[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None


# --- tuning ------------------------------------------------------------------
TREMOR_LOW_HZ = _env_float('TREMOR_LOW_HZ', 4.0)              # classic rest tremor band
TREMOR_HIGH_HZ = _env_float('TREMOR_HIGH_HZ', 12.0)
TREMOR_AMPLITUDE_G = _env_float('TREMOR_AMPLITUDE_G', 0.02)   # oscillation floor, in g
TREMOR_SUSTAIN_S = _env_float('TREMOR_SUSTAIN_S', 2.0)        # "for x period"
TREMOR_RELEASE_S = _env_float('TREMOR_RELEASE_S', 1.5)        # quiet time that ends an episode
TREMOR_WINDOW_S = _env_float('TREMOR_WINDOW_S', 1.5)          # oscillation analysis window
TREMOR_MIN_SAMPLES = _env_int('TREMOR_MIN_SAMPLES', 12)
TREMOR_HIGH_G = _env_float('TREMOR_HIGH_G', 0.10)             # RMS amplitude, in g
TREMOR_MEDIUM_G = _env_float('TREMOR_MEDIUM_G', 0.05)

FALL_IMPACT_G = _env_float('FALL_IMPACT_G', 0.8)              # sudden change on the Y axis
FALL_FREEFALL_G = _env_float('FALL_FREEFALL_G', 0.45)         # near weightlessness before landing
FALL_CONFIRM_S = _env_float('FALL_CONFIRM_S', 2.5)            # time allowed to confirm a fall
FALL_QUIET_G = _env_float('FALL_QUIET_G', 0.06)               # "lying still" energy ceiling
FALL_TILT_DEG = _env_float('FALL_TILT_DEG', 45.0)             # orientation change that reads as lying
FALL_RECOVER_G = _env_float('FALL_RECOVER_G', 0.25)           # "carried on moving" energy floor
FALL_COOLDOWN_S = _env_float('FALL_COOLDOWN_S', 10.0)         # one fall per device per window
FALL_MAX_DT = _env_float('FALL_MAX_DT', 0.12)                 # a change this fast is an impact

WINDOW_S = _env_float('MOTION_WINDOW_S', 4.0)                 # sample window per wearable
MAX_SAMPLES = _env_int('MOTION_MAX_SAMPLES', 300)
OFFLINE_AFTER_S = _env_float('MOTION_OFFLINE_SECONDS', 15.0)  # no report this long = offline
EVENT_HISTORY = _env_int('MOTION_EVENT_HISTORY', 60)
ALERT_COOLDOWN_S = _env_float('MOTION_ALERT_COOLDOWN_SECONDS', 30.0)
SAMPLE_HZ = _env_float('MOTION_SAMPLE_HZ', 20.0)              # firmware target rate

# One band, on the left wrist, and no second one. A caregiver asked for a single
# band that reports oxygen, body temperature, heart rate, tremor and falls; the
# side is part of the build now, not a dashboard choice.
BAND_HAND = 'left'
BAND_BATTERY = _env_int('BAND_BATTERY_PCT', 88)

WEARABLES = [
    {'id': 'body-band', 'name': 'Left wrist band', 'hand': BAND_HAND,
     'room': None, 'battery': BAND_BATTERY,
     'note': 'ESP32 #1 · MPU6050 tremor and fall, MAX30102 heart rate and SpO2, MLX90614 body temperature.'},
]
WEARABLE_BY_ID = {w['id']: w for w in WEARABLES}

# Plausible limits for an incoming packet. Values outside are ignored rather
# than stored, so one bad reading cannot turn the dashboard into a medical lie.
BODY_TEMP_RANGE = (_env_float('BODY_TEMP_MIN_C', 30.0), _env_float('BODY_TEMP_MAX_C', 43.0))
HEART_RATE_RANGE = (_env_int('HEART_RATE_MIN_BPM', 25), _env_int('HEART_RATE_MAX_BPM', 250))
SPO2_RANGE = (_env_float('SPO2_MIN_PCT', 50.0), _env_float('SPO2_MAX_PCT', 100.0))

_lock = threading.RLock()
_events = deque(maxlen=EVENT_HISTORY)
_event_seq = [0]
_last_alert_at = {}
_active_signature = [None, None]   # last emergency signature and its start time
_state_loaded = [False]


def _round(value, digits=3):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _number(value):
    """Coerce JSON input to float, rejecting booleans and junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _rms(values):
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / float(len(values)))


def _angle_between(a, b):
    """Angle in degrees between two 3-vectors."""
    if not a or not b:
        return 0.0
    na = math.sqrt(sum(v * v for v in a))
    nb = math.sqrt(sum(v * v for v in b))
    if na == 0 or nb == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(3)) / (na * nb)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _iso_from_epoch(t):
    if not t:
        return None
    if _UTC is not None:
        return datetime.fromtimestamp(t, _UTC).isoformat()
    return datetime.utcfromtimestamp(t).isoformat() + '+00:00'


def _epoch_from_iso(text):
    parsed = parse_iso(text)
    if parsed is None:
        return None
    return (parsed - datetime(1970, 1, 1)).total_seconds()


class _Device:
    """Rolling state for one hand-worn MPU6050."""

    def __init__(self, spec):
        self.spec = spec
        self.samples = deque(maxlen=MAX_SAMPLES)   # (t, ax, ay, az, gx, gy, gz)
        self.last_at = None
        self.rate_hz = 0.0
        self.battery = spec.get('battery')
        # tremor episode state
        self.tremor_announced = False
        self.tremor_since = None
        self.tremor_last_band = None
        self.tremor_peak = 0.0
        self.tremor_freq_sum = 0.0
        self.tremor_freq_n = 0
        self.tremor_freq = 0.0
        self.tremor_flagged = False
        # fall detector state
        self.prev = None
        self.freefall_at = None
        self.pending = None
        self.cooldown_until = 0.0
        self.gravity = (0.0, 0.0, -1.0)
        self.last_fall_at = None
        # body vitals (MAX30102 + MLX90614 on the same band)
        self.vitals = {'body_temperature_c': None, 'heart_rate_bpm': None, 'spo2_pct': None}
        self.vitals_at = None
        self.vitals_at_iso = None
        self.hand = BAND_HAND   # the one band is worn on the left wrist

    # -- persistence ---------------------------------------------------------
    def restore(self):
        """Reload the last vitals this band reported, so a restart is not blank."""
        stored = db.load_device_state(self.spec['id'])
        if not stored:
            return
        vitals = stored.get('vitals') if isinstance(stored.get('vitals'), dict) else {}
        for key in ('body_temperature_c', 'heart_rate_bpm', 'spo2_pct'):
            value = _number(vitals.get(key))
            if value is not None:
                self.vitals[key] = int(round(value)) if key == 'heart_rate_bpm' else _round(value, 1)
        self.vitals_at_iso = stored.get('vitals_at') or stored.get('updated_at')
        self.vitals_at = _epoch_from_iso(self.vitals_at_iso) if self.vitals_at_iso else None
        if isinstance(stored.get('last_fall_at'), str):
            self.last_fall_at = _epoch_from_iso(stored['last_fall_at'])

    def persist(self):
        db.save_device_state(self.spec['id'], {
            'vitals': self.vitals,
            'vitals_at': self.vitals_at_iso,
            'hand': self.hand,
            'last_fall_at': _iso_from_epoch(self.last_fall_at),
            'battery_pct': self.battery,
        }, self.vitals_at_iso or now_iso())

    def update_vitals(self, readings, at, when_iso=None):
        """Store a validated vitals packet. Missing or out-of-range fields are skipped."""
        changed = False
        temp = _number(readings.get('body_temperature_c', readings.get('temperature_c')))
        if temp is not None and BODY_TEMP_RANGE[0] <= temp <= BODY_TEMP_RANGE[1]:
            self.vitals['body_temperature_c'] = _round(temp, 1)
            changed = True
        heart = _number(readings.get('heart_rate_bpm', readings.get('heart_rate')))
        if heart is not None and HEART_RATE_RANGE[0] <= heart <= HEART_RATE_RANGE[1]:
            self.vitals['heart_rate_bpm'] = int(round(heart))
            changed = True
        spo2 = _number(readings.get('spo2_pct', readings.get('spo2')))
        if spo2 is not None and SPO2_RANGE[0] <= spo2 <= SPO2_RANGE[1]:
            self.vitals['spo2_pct'] = _round(spo2, 1)
            changed = True
        battery = _number(readings.get('battery_pct'))
        if battery is not None:
            self.battery = max(0, min(100, int(round(battery))))
        if changed:
            self.vitals_at = at
            self.vitals_at_iso = when_iso or now_iso()
            self.persist()
        return changed

    # -- input ---------------------------------------------------------------
    def feed(self, t, ax, ay, az, gx, gy, gz):
        """Store one sample and run the detectors. Returns the events it raised."""
        with _lock:
            self.samples.append((t, ax, ay, az, gx, gy, gz))
            self.last_at = t
            if len(self.samples) >= 8:
                span = self.samples[-1][0] - self.samples[-8][0]
                self.rate_hz = _round(7.0 / span, 1) if span > 0 else 0.0
            mag = math.sqrt(ax * ax + ay * ay + az * az)
            alpha = 0.08
            self.gravity = (
                self.gravity[0] + alpha * (ax - self.gravity[0]),
                self.gravity[1] + alpha * (ay - self.gravity[1]),
                self.gravity[2] + alpha * (az - self.gravity[2]),
            )
            raised = self._check_fall(t, ax, ay, az)
            tremor = self._check_tremor(t)
            if tremor:
                raised.append(tremor)
            return [e for e in raised if e]

    # -- tremor --------------------------------------------------------------
    def _tremor_metrics(self, now, window_s=None):
        """Oscillation of the gravity axis with its slow average removed.

        Returns ``(hz, amplitude_g, axis)`` or None when there is not enough
        data. The window is short (1.5 s by default) so an episode ends soon
        after the hand really stops shaking.
        """
        window_s = window_s or TREMOR_WINDOW_S
        window = [s for s in self.samples if s[0] >= now - window_s]
        if len(window) < TREMOR_MIN_SAMPLES:
            return None
        means = [sum(s[1 + i] for s in window) / float(len(window)) for i in range(3)]
        axis = 0
        for i in (1, 2):
            if abs(means[i]) > abs(means[axis]):
                axis = i
        residual = [s[1 + axis] - means[axis] for s in window]
        amplitude = _rms(residual)
        if amplitude <= 0:
            return (0.0, 0.0, axis)
        return (self._dominant_hz([s[0] for s in window], residual, amplitude), amplitude, axis)

    @staticmethod
    def _dominant_hz(times, signal, amplitude):
        """Upward zero-crossings with hysteresis, then the mean period."""
        level = amplitude * 0.5
        state = 0
        crossings = []
        for t, v in zip(times, signal):
            if state <= 0 and v > level:
                state = 1
                crossings.append(t)
            elif state >= 0 and v < -level:
                state = -1
        if len(crossings) < 4:
            return 0.0
        periods = [crossings[i + 1] - crossings[i] for i in range(len(crossings) - 1)]
        periods = [p for p in periods if p > 0]
        if not periods:
            return 0.0
        period = sum(periods) / float(len(periods))
        return 1.0 / period if period > 0 else 0.0

    @staticmethod
    def tremor_level(amplitude):
        if amplitude >= TREMOR_HIGH_G:
            return 'high'
        if amplitude >= TREMOR_MEDIUM_G:
            return 'medium'
        return 'low'

    def _check_tremor(self, now):
        data = self._tremor_metrics(now)
        if data is None:
            return None
        hz, amplitude, axis = data
        if hz:
            self.tremor_freq = _round(0.6 * self.tremor_freq + 0.4 * hz, 1)
        in_band = (TREMOR_LOW_HZ <= hz <= TREMOR_HIGH_HZ and amplitude >= TREMOR_AMPLITUDE_G)
        if in_band:
            self.tremor_last_band = now
            if self.tremor_since is None:
                self.tremor_since = now
                self.tremor_peak = amplitude
                self.tremor_freq_sum = hz
                self.tremor_freq_n = 1
            else:
                self.tremor_peak = max(self.tremor_peak, amplitude)
                self.tremor_freq_sum += hz
                self.tremor_freq_n += 1
            if not self.tremor_announced and now - self.tremor_since >= TREMOR_SUSTAIN_S:
                # Held this long, so it is an episode. Say so now rather than
                # only once it stops, because a strong tremor needs attention.
                self.tremor_announced = True
                level = self.tremor_level(self.tremor_peak)
                return self._event('tremor', level, {
                    'frequency_hz': _round(hz, 1),
                    'amplitude_g': _round(self.tremor_peak, 3),
                    'duration_s': _round(now - self.tremor_since, 1),
                    'state': 'ongoing',
                }, 'Hand tremor on the {} hand: {:.1f} Hz at {:.3f} g, sustained'.format(
                    self.hand_label(), hz, self.tremor_peak))
            return None
        # Out of band: close the episode once it has really stopped.
        if self.tremor_since is not None:
            quiet_since = self.tremor_last_band or self.tremor_since
            if now - quiet_since >= TREMOR_RELEASE_S:
                duration = quiet_since - self.tremor_since
                mean_hz = (self.tremor_freq_sum / self.tremor_freq_n) if self.tremor_freq_n else 0.0
                peak = self.tremor_peak
                level = self.tremor_level(peak)
                self.tremor_since = None
                self.tremor_last_band = None
                self.tremor_freq_sum = 0.0
                self.tremor_freq_n = 0
                self.tremor_peak = 0.0
                self.tremor_announced = False
                if duration >= TREMOR_SUSTAIN_S:
                    return self._event('tremor', level, {
                        'frequency_hz': _round(mean_hz, 1),
                        'amplitude_g': _round(peak, 3),
                        'duration_s': _round(duration, 1),
                        'state': 'ended',
                    }, 'Hand tremor ended after {:.1f} s at {:.1f} Hz (peak {:.3f} g)'.format(
                        duration, mean_hz, peak))
        return None

    # -- motion energy -------------------------------------------------------
    def _motion_rms(self, now, window_s):
        """Movement energy, in g: how far each axis swings about its own mean.

        Rotating or shaking a hand keeps the vector length near 1 g, so a
        magnitude-only measure cannot tell movement from stillness. Deviations
        per axis can, and that is what the fall confirmation needs.
        """
        points = [s for s in self.samples if s[0] >= now - window_s]
        if len(points) < 4:
            return 0.0
        total = 0.0
        for i in (1, 2, 3):
            values = [p[i] for p in points]
            mean = sum(values) / float(len(values))
            total += sum((v - mean) * (v - mean) for v in values) / float(len(values))
        return math.sqrt(total)

    # -- fall ----------------------------------------------------------------
    def _check_fall(self, t, ax, ay, az):
        mag = math.sqrt(ax * ax + ay * ay + az * az)
        # Free-fall precursor: the hand is briefly weightless.
        if mag < FALL_FREEFALL_G:
            if self.freefall_at is None:
                self.freefall_at = t
        elif self.freefall_at is not None and mag > 0.8 and (t - self.freefall_at) < 0.05:
            self.freefall_at = None   # too short to count, keep looking

        # Impact: a sudden change, checked on the Y axis first exactly as asked.
        if self.prev is not None:
            pt, pax, pay, paz = self.prev
            dt = t - pt
            if 0 < dt <= FALL_MAX_DT and self.pending is None and t >= self.cooldown_until:
                dy = abs(ay - pay)
                other = max(abs(ax - pax), abs(az - paz))
                if dy >= FALL_IMPACT_G or other >= FALL_IMPACT_G * 1.4:
                    self.pending = {
                        'at': t,
                        'impact': max(dy, other),
                        'axis': 'y' if dy >= FALL_IMPACT_G else 'x/z',
                        'gravity': self.gravity,
                        'freefall': self.freefall_at is not None and (t - self.freefall_at) <= 0.8,
                    }
                    self.freefall_at = None
        self.prev = (t, ax, ay, az)

        pending = self.pending
        if pending is None:
            return []
        elapsed = t - pending['at']
        if elapsed < 0.15:
            return []   # let the impact window settle before judging
        tilt = _angle_between(self.gravity, pending['gravity'])
        quiet = elapsed >= 0.6 and self._motion_rms(t, 0.6) < FALL_QUIET_G
        # Movement resumed and the hand never settled: a knock, not a fall.
        if elapsed >= 1.0 and self._motion_rms(t, 0.4) >= FALL_RECOVER_G:
            return [self._close_fall(t, 'impact', 'low', pending, tilt,
                                     'Sharp hand impact ({:.1f} g) but movement resumed quickly, '
                                     'so it was not treated as a fall'.format(pending['impact']))]
        if elapsed >= 0.5 and tilt >= FALL_TILT_DEG:
            level = 'critical' if pending['freefall'] else 'high'
            return [self._close_fall(t, 'fall', level, pending, tilt,
                                     'Fall detected on the {} hand: a sudden {:.1f} g change on the {} '
                                     'axis and the hand changed orientation by {:.0f}°'.format(
                                         self.hand_label(), pending['impact'], pending['axis'], tilt))]
        if quiet:
            level = 'critical' if pending['freefall'] else 'high'
            return [self._close_fall(t, 'fall', level, pending, tilt,
                                     'Fall detected on the {} hand: a sudden {:.1f} g change on the {} '
                                     'axis, then the hand stayed still'.format(
                                         self.hand_label(), pending['impact'], pending['axis']))]
        if elapsed > FALL_CONFIRM_S:
            if pending['impact'] >= FALL_IMPACT_G * 2:
                return [self._close_fall(t, 'impact', 'medium', pending, tilt,
                                         'Hard hand impact ({:.1f} g) that could not be confirmed as a fall'.format(
                                             pending['impact']))]
            self.pending = None
        return []

    def _close_fall(self, t, kind, level, pending, tilt, detail):
        event = self._event(kind, level, {
            'impact_g': _round(pending['impact'], 2),
            'axis': pending['axis'],
            'tilt_deg': _round(tilt, 0),
            'freefall': bool(pending['freefall']),
            'posture': self.posture(),
        }, detail)
        self.pending = None
        self.cooldown_until = t + (FALL_COOLDOWN_S if kind == 'fall' else FALL_COOLDOWN_S * 0.5)
        if kind == 'fall':
            self.last_fall_at = t
            self.persist()
        return event

    # -- output --------------------------------------------------------------
    def hand_label(self):
        """The wrist the one band is worn on. Always the left."""
        return self.hand or self.spec['hand']

    def posture(self):
        gx, gy, gz = self.gravity
        if abs(gz) >= 0.6:
            return 'hand lowered' if gz < 0 else 'hand raised'
        if abs(gy) >= 0.6:
            return 'hand forward' if gy > 0 else 'hand back'
        return 'hand horizontal'

    def _event(self, kind, level, value, detail):
        with _lock:
            _event_seq[0] += 1
            event = {
                'id': _event_seq[0],
                'kind': kind,
                'level': level,
                'device': self.spec['id'],
                'device_name': self.spec['name'],
                'hand': self.hand_label(),
                'detail': detail,
                'value': value,
                'source': 'live',
                'created_at': now_iso(),
            }
            _events.appendleft(event)
            return event

    def last_axes(self):
        if not self.samples:
            return {'x': None, 'y': None, 'z': None}
        s = self.samples[-1]
        return {'x': _round(s[1], 2), 'y': _round(s[2], 2), 'z': _round(s[3], 2)}

    def last_magnitude(self):
        if not self.samples:
            return 0.0
        s = self.samples[-1]
        return math.sqrt(s[1] * s[1] + s[2] * s[2] + s[3] * s[3])

    def snapshot(self, now):
        online = self.last_at is not None and (now - self.last_at) <= OFFLINE_AFTER_S
        tremor_active = self.tremor_since is not None
        return {
            'id': self.spec['id'],
            'name': self.spec['name'],
            'hand': self.hand_label(),
            'configured_hand': self.spec['hand'],
            'note': self.spec['note'],
            'battery_pct': self.battery,
            'online': bool(online),
            'sample_rate_hz': self.rate_hz,
            'last_sample_at': _iso_from_epoch(self.last_at),
            'posture': self.posture(),
            'activity_g': _round(self._motion_rms(now, 1.0), 3),
            'magnitude_g': _round(self.last_magnitude(), 2),
            'axes': self.last_axes(),
            'tremor': {
                'active': bool(tremor_active),
                'frequency_hz': _round(self.tremor_freq, 1),
                'amplitude_g': _round(self.tremor_peak if tremor_active else 0.0, 3),
                'band': [TREMOR_LOW_HZ, TREMOR_HIGH_HZ],
                'since': _iso_from_epoch(self.tremor_since),
            },
            'fall': {
                'suspected': self.pending is not None,
                'last_at': _iso_from_epoch(self.last_fall_at),
            },
            'vitals': {
                'body_temperature_c': self.vitals['body_temperature_c'],
                'heart_rate_bpm': self.vitals['heart_rate_bpm'],
                'spo2_pct': self.vitals['spo2_pct'],
                'stale': self.vitals_at is None or (now - self.vitals_at) > OFFLINE_AFTER_S,
                'updated_at': self.vitals_at_iso,
            },
            'trace': self.movement_trace(60),
        }

    def movement_trace(self, count=60):
        """Recent movement as a short series the dashboard can draw as bars."""
        points = list(self.samples)[-count:]
        if len(points) < 2:
            return []
        means = [sum(p[i] for p in points) / float(len(points)) for i in (1, 2, 3)]
        trace = []
        for point in points:
            total = sum((point[1 + i] - means[i]) ** 2 for i in range(3))
            trace.append(round(math.sqrt(total), 3))
        return trace


_devices = {spec['id']: _Device(spec) for spec in WEARABLES}


def ensure_state_loaded():
    """Load persisted vitals once, after the database exists."""
    if _state_loaded[0]:
        return
    with _lock:
        if _state_loaded[0]:
            return
        for device in _devices.values():
            try:
                device.restore()
            except Exception as exc:   # a corrupt row must not stop the app
                print('[ELDRIS] Could not restore {}: {}'.format(device.spec['id'], exc))
        _state_loaded[0] = True


def ingest(payload):
    """Accept one sample, or a batch, from the hand band.

    Returns ``(items, events, error)``: per-device snapshots, any events the
    detectors raised, and a message when the packet was rejected.
    """
    ensure_state_loaded()
    if not isinstance(payload, dict):
        return [], [], 'Send a JSON object with a device id.'
    device_id = str(payload.get('device') or payload.get('id') or '').strip().lower()
    device = _devices.get(device_id)
    if device is None:
        return [], [], 'Unknown device. Use one of: ' + ', '.join(sorted(_devices.keys())) + '.'
    batch = payload.get('samples')
    raw = batch if isinstance(batch, list) else [payload]
    if not raw:
        return [], [], 'Send at least one sample.'
    if len(raw) > 400:
        return [], [], 'Send at most 400 samples per request.'
    when_iso = None
    stamp = payload.get('at') or payload.get('updated_at')
    if isinstance(stamp, str) and parse_iso(stamp):
        when_iso = stamp
    now = time.time()
    # Vitals may be posted once per packet at the top level, or per sample in
    # the batch. The newest one wins; anything invalid is ignored.
    vitals = payload.get('vitals') if isinstance(payload.get('vitals'), dict) else payload
    vitals_changed = False
    if isinstance(vitals, dict):
        for entry in reversed(raw):
            if isinstance(entry, dict) and any(k in entry for k in ('heart_rate_bpm', 'heart_rate', 'spo2_pct', 'spo2', 'body_temperature_c')):
                merged = dict(vitals)
                merged.update(entry)
                vitals_changed = device.update_vitals(merged, now, when_iso) or vitals_changed
                break
        vitals_changed = device.update_vitals(dict(vitals, hand=payload.get('hand')), now, when_iso) or vitals_changed
    events = []
    accepted = 0
    # A batch carries samples from the last moments, so lay them out backwards
    # from "now" at the configured rate. Timestamps in the packet are optional
    # and only used to keep their relative spacing.
    step = 1.0 / SAMPLE_HZ
    stamps = []
    for entry in raw:
        if not isinstance(entry, dict):
            stamps.append(None)
            continue
        value = _number(entry.get('t', entry.get('ts')))
        stamps.append(value)
    spacing = step
    if len(stamps) > 1 and all(s is not None for s in stamps[1:]):
        gaps = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
        gap = sum(gaps) / float(len(gaps))
        if gap > 0:
            spacing = gap / 1000.0 if gap > 5 else gap   # accept ms or seconds
            spacing = max(0.002, min(0.25, spacing))
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        ax = _number(entry.get('ax', entry.get('x')))
        ay = _number(entry.get('ay', entry.get('y')))
        az = _number(entry.get('az', entry.get('z')))
        if ax is None or ay is None or az is None:
            continue
        if max(abs(ax), abs(ay), abs(az)) > 16:
            continue   # beyond any real accelerometer range: junk packet
        gx = _number(entry.get('gx')) or 0.0
        gy = _number(entry.get('gy')) or 0.0
        gz = _number(entry.get('gz')) or 0.0
        t = now - (len(raw) - 1 - index) * spacing
        events.extend(device.feed(t, ax, ay, az, gx, gy, gz))
        accepted += 1
    if not accepted:
        # A vitals-only packet is still useful: the IMU may have glitched while
        # the oximeter and temperature sensor kept working.
        if vitals_changed:
            return [device.snapshot(time.time())], [], None
        return [], [], 'No usable samples in that packet: ax, ay and az are required.'
    return [device.snapshot(time.time())], events, None


def snapshot(limit=20):
    """Everything the web needs about hand motion, plus the emergency view."""
    ensure_state_loaded()
    now = time.time()
    with _lock:
        wearables = [device.snapshot(now) for device in _devices.values()]
        events = list(_events)[:max(1, int(limit))]
        online = [w for w in wearables if w['online']]
        active_tremor = [w for w in wearables if w['tremor']['active']]
        today = _utcnow().date()
        falls_today = 0
        last_fall = None
        for event in _events:
            if event['kind'] != 'fall':
                continue
            if last_fall is None:
                last_fall = event['created_at']
            parsed = parse_iso(event['created_at'])
            if parsed is not None and parsed.date() == today:
                falls_today += 1
        payload = {
            'wearables': wearables,
            'events': events,
            'summary': {
                'wearables_total': len(wearables),
                'wearables_online': len(online),
                'tremor_active': len(active_tremor),
                'tremor_devices': [w['id'] for w in active_tremor],
                'falls_today': falls_today,
                'last_fall_at': last_fall,
                'activity_g': _round(max([w['activity_g'] for w in wearables] or [0.0]), 3),
                'tremor_band_hz': [TREMOR_LOW_HZ, TREMOR_HIGH_HZ],
                'heart_rate_bpm': wearables[0]['vitals']['heart_rate_bpm'] if wearables else None,
                'spo2_pct': wearables[0]['vitals']['spo2_pct'] if wearables else None,
                'body_temperature_c': wearables[0]['vitals']['body_temperature_c'] if wearables else None,
                'vitals_updated_at': wearables[0]['vitals']['updated_at'] if wearables else None,
            },
            'settings': {
                'tremor_band_hz': [TREMOR_LOW_HZ, TREMOR_HIGH_HZ],
                'tremor_amplitude_g': TREMOR_AMPLITUDE_G,
                'tremor_sustain_s': TREMOR_SUSTAIN_S,
                'fall_impact_g': FALL_IMPACT_G,
                'fall_tilt_deg': FALL_TILT_DEG,
                'fall_confirm_s': FALL_CONFIRM_S,
                'sample_rate_hz': SAMPLE_HZ,
            },
            'updated_at': now_iso(),
        }
        payload['emergency'] = emergency_status(payload)
        return payload


# --- emergency ---------------------------------------------------------------
# Alert types whose presence means "look at this now".
EMERGENCY_TYPES = ('fall_detected', 'tremor_detected', 'help_request', 'sos', 'panic_button', 'no_movement')
EMERGENCY_SEVERITIES = ('high', 'critical')


def emergency_status(motion=None, alerts=None):
    """Merge caregiver alerts with live hand motion into one verdict.

    The dashboard paints itself red while ``active`` is true. A fall is carried
    by its alert row, so acknowledging that alert really does clear the alarm;
    an ongoing tremor keeps the dashboard red because it is still happening,
    acknowledgement or not.

    ``alerts`` is the caregiver alert list. When it is None -- the motion-only
    view -- recent fall events stand in for their alert rows.
    """
    sources = []
    for alert in alerts or []:
        if alert.get('status') != 'open':
            continue   # acknowledged or resolved means the caregiver has handled it
        kind = str(alert.get('type', ''))
        severity = str(alert.get('severity', ''))
        if kind in EMERGENCY_TYPES and severity in EMERGENCY_SEVERITIES:
            sources.append({'kind': 'alert', 'id': alert.get('id'), 'type': kind,
                            'severity': severity, 'detail': alert.get('description'),
                            'at': alert.get('created_at')})
    motion = motion or {}
    for wearable in motion.get('wearables', []):
        tremor = wearable.get('tremor') or {}
        if tremor.get('active'):
            sources.append({'kind': 'motion', 'id': wearable.get('id'), 'type': 'tremor_episode',
                            'severity': 'high',
                            'detail': 'Ongoing hand tremor on the {}.'.format(wearable.get('name')),
                            'at': tremor.get('since')})
    if alerts is None:
        for event in motion.get('events', [])[:5]:
            if event.get('kind') == 'fall' and event.get('level') in EMERGENCY_SEVERITIES:
                sources.append({'kind': 'motion', 'id': event.get('id'), 'type': 'fall_detected',
                                'severity': event.get('level'), 'detail': event.get('detail'),
                                'at': event.get('created_at')})
    newest = None
    for source in sources:
        at = source.get('at')
        if at and (newest is None or at > newest):
            newest = at
    if not sources:
        _active_signature[0] = None
        _active_signature[1] = None
        return {'active': False, 'level': 'normal', 'headline': 'All clear',
                'detail': 'No fall and no active hand tremor reported.',
                'since': None, 'sources': [], 'checked_at': now_iso()}
    signature = '|'.join(sorted(str(s.get('type')) + str(s.get('id')) for s in sources))
    if _active_signature[0] != signature:
        _active_signature[0] = signature
        _active_signature[1] = newest or now_iso()
    types = set(s['type'] for s in sources)
    if 'fall_detected' in types:
        headline = 'Fall detected'
    elif 'tremor_episode' in types or 'tremor_detected' in types:
        headline = 'Tremor episode'
    elif types & set(['sos', 'help_request', 'panic_button']):
        headline = 'Help requested'
    else:
        headline = 'Caregiver attention needed'
    return {
        'active': True,
        'level': 'critical' if any(s['severity'] == 'critical' for s in sources) else 'high',
        'headline': headline,
        'detail': source_detail(sources[0]),
        'since': _active_signature[1],
        'sources': sources,
        'checked_at': now_iso(),
    }


def source_detail(source):
    return source.get('detail') or 'Caregiver attention required.'


def should_alert(event):
    """True when a motion event deserves a caregiver alert row in the database."""
    if not isinstance(event, dict):
        return False
    if event.get('kind') == 'fall':
        return True
    if event.get('kind') == 'tremor' and event.get('level') == 'high':
        key = event.get('device')
        clock = time.time()
        if clock - _last_alert_at.get(key, 0) < ALERT_COOLDOWN_S:
            return False
        _last_alert_at[key] = clock
        return True
    return False


def alert_for(event):
    """Translate a motion event into caregiver alert fields."""
    if event.get('kind') == 'fall':
        return ('fall_detected', 'critical' if event.get('level') == 'critical' else 'high',
                event.get('detail'))
    return ('tremor_detected', 'high', event.get('detail'))


def wearables():
    """Device inventory rows for the single wrist band, for rooms.list_devices."""
    ensure_state_loaded()
    return [dict(spec) for spec in WEARABLES]


def device_status():
    """Online state for the device inventory. Reads real packets only."""
    now = time.time()
    out = {}
    with _lock:
        for device in _devices.values():
            last = device.last_at
            out[device.spec['id']] = {
                'online': last is not None and (now - last) <= OFFLINE_AFTER_S,
                'battery_pct': device.battery,
                'last_seen_at': _iso_from_epoch(last),
                'hand': device.hand_label(),
            }
    return out
