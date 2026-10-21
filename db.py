import os, sqlite3, json
from datetime import datetime, timezone, timedelta

DB_PATH = os.getenv('DATABASE_PATH', 'eldris.sqlite3')


def connect():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


def init_db():
    """Create the schema. Nothing is seeded: the dashboard starts empty and
    every value comes from a real device."""
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,full_name TEXT NOT NULL,email TEXT NOT NULL UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,category TEXT NOT NULL,scheduled_at TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'scheduled',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT NOT NULL,severity TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',created_at TEXT NOT NULL,simulated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,category TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS motion_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,level TEXT NOT NULL,device TEXT NOT NULL,device_name TEXT NOT NULL,detail TEXT NOT NULL,value_json TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'live',created_at TEXT NOT NULL);
        -- Persistent per-device state (vitals, drive telemetry, climate, robot
        -- room). One row per device id, so a restart does not lose the last
        -- reading a node sent.
        CREATE TABLE IF NOT EXISTS device_state(device TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
        -- Small key/value store for dashboard state that is not a sensor.
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_activity_created ON activity(id DESC);
        CREATE INDEX IF NOT EXISTS idx_motion_created ON motion_events(id DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
        ''')


def rowdict(r):
    d = dict(r)
    if 'enabled' in d:
        d['enabled'] = bool(d['enabled'])
    if 'simulated' in d:
        d['simulated'] = bool(d['simulated'])
    return d


# --- key/value settings ------------------------------------------------------
def get_setting(key, default=None):
    with connect() as c:
        row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row['value'])
    except (TypeError, ValueError):
        return default


def set_setting(key, value):
    with connect() as c:
        c.execute('INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) '
                  'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                  (key, json.dumps(value), iso()))


# --- per-device persisted state ---------------------------------------------
def load_device_state(device):
    """Last stored payload for one device, or None. Never raises on bad JSON."""
    with connect() as c:
        row = c.execute('SELECT payload,updated_at FROM device_state WHERE device=?', (str(device),)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row['payload'])
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        payload.setdefault('updated_at', row['updated_at'])
        return payload
    return None


def save_device_state(device, payload, updated_at=None):
    with connect() as c:
        c.execute('INSERT INTO device_state(device,payload,updated_at) VALUES(?,?,?) '
                  'ON CONFLICT(device) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at',
                  (str(device), json.dumps(payload, default=str), updated_at or iso()))


def load_all_device_state():
    with connect() as c:
        rows = c.execute('SELECT device,payload,updated_at FROM device_state').fetchall()
    out = {}
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            payload.setdefault('updated_at', row['updated_at'])
            out[row['device']] = payload
    return out


# --- reminders ---------------------------------------------------------------
def list_reminders():
    with connect() as c:
        rows = c.execute('SELECT * FROM reminders ORDER BY scheduled_at ASC').fetchall()
    return [rowdict(r) for r in rows]


def create_reminder(title, category, scheduled, enabled):
    with connect() as c:
        cur = c.execute('INSERT INTO reminders(title,category,scheduled_at,enabled,status,created_at) VALUES(?,?,?,?,?,?)',
                        (title, category, scheduled, int(enabled), 'scheduled', iso()))
        r = c.execute('SELECT * FROM reminders WHERE id=?', (cur.lastrowid,)).fetchone()
    return rowdict(r)


def update_reminder(item_id, data):
    allowed = {'title', 'category', 'scheduled_at', 'enabled', 'status'}
    with connect() as c:
        old = c.execute('SELECT * FROM reminders WHERE id=?', (item_id,)).fetchone()
        if not old:
            return None
        values = dict(old)
        for k, v in data.items():
            if k not in allowed:
                continue
            if k == 'title' and (not isinstance(v, str) or not v.strip() or len(v) > 100):
                raise ValueError('Invalid title')
            if k == 'enabled':
                v = int(bool(v))
            values[k] = v
        c.execute('UPDATE reminders SET title=?,category=?,scheduled_at=?,enabled=?,status=? WHERE id=?',
                  (values['title'], values['category'], values['scheduled_at'], int(values['enabled']), values['status'], item_id))
        return rowdict(c.execute('SELECT * FROM reminders WHERE id=?', (item_id,)).fetchone())


def delete_reminder(item_id):
    with connect() as c:
        return c.execute('DELETE FROM reminders WHERE id=?', (item_id,)).rowcount > 0


# --- alerts ------------------------------------------------------------------
def list_alerts():
    with connect() as c:
        rows = c.execute('SELECT * FROM alerts ORDER BY id DESC').fetchall()
    return [rowdict(r) for r in rows]


def create_alert(kind, description, severity, simulated=False):
    with connect() as c:
        cur = c.execute('INSERT INTO alerts(type,severity,description,status,created_at,simulated) VALUES(?,?,?,?,?,?)',
                        (kind, severity, description, 'open', iso(), int(simulated)))
        return rowdict(c.execute('SELECT * FROM alerts WHERE id=?', (cur.lastrowid,)).fetchone())


def update_alert(item_id, state):
    with connect() as c:
        cur = c.execute('UPDATE alerts SET status=? WHERE id=?', (state, item_id))
        if not cur.rowcount:
            return None
        return rowdict(c.execute('SELECT * FROM alerts WHERE id=?', (item_id,)).fetchone())


# --- activity ----------------------------------------------------------------
def log_activity(category, message):
    with connect() as c:
        c.execute('INSERT INTO activity(category,message,created_at) VALUES(?,?,?)', (category, message, iso()))


def list_activity(search='', category='all'):
    sql = 'SELECT * FROM activity WHERE message LIKE ?'
    params = ['%' + search + '%']
    if category != 'all':
        sql += ' AND category=?'
        params.append(category)
    sql += ' ORDER BY id DESC LIMIT 250'
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [rowdict(r) for r in rows]


# --- motion events -----------------------------------------------------------
def create_motion_event(kind, level, device, device_name, detail, value, source='live'):
    """Persist one tremor, fall or impact event so the timeline survives a restart."""
    with connect() as c:
        cur = c.execute('INSERT INTO motion_events(kind,level,device,device_name,detail,value_json,source,created_at) VALUES(?,?,?,?,?,?,?,?)',
                        (kind, level, device, device_name, detail, json.dumps(value or {}), source, iso()))
        row = c.execute('SELECT * FROM motion_events WHERE id=?', (cur.lastrowid,)).fetchone()
    return motion_rowdict(row)


def motion_rowdict(row):
    d = dict(row)
    try:
        d['value'] = json.loads(d.pop('value_json', '{}') or '{}')
    except ValueError:
        d['value'] = {}
        d.pop('value_json', None)
    d['simulated'] = d.get('source') == 'simulated'
    return d


def list_motion_events(limit=50, kind='all'):
    limit = max(1, min(200, int(limit)))
    sql = 'SELECT * FROM motion_events'
    params = []
    if kind and kind != 'all':
        sql += ' WHERE kind=?'
        params.append(kind)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [motion_rowdict(r) for r in rows]


# --- users -------------------------------------------------------------------
def create_user(full_name, email, password_hash):
    with connect() as c:
        try:
            cur = c.execute('INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)',
                            (full_name, email.lower(), password_hash, iso()))
        except sqlite3.IntegrityError:
            return None
        row = c.execute('SELECT id,full_name,email,created_at FROM users WHERE id=?', (cur.lastrowid,)).fetchone()
    return dict(row)


def get_user_by_email(email):
    with connect() as c:
        row = c.execute('SELECT * FROM users WHERE email=? COLLATE NOCASE', (email.lower(),)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id):
    with connect() as c:
        row = c.execute('SELECT id,full_name,email,created_at FROM users WHERE id=?', (user_id,)).fetchone()
    return dict(row) if row else None


# Create the schema as soon as this module loads. Modules that read a stored
# setting while they are being imported (the robot room, the last help phrase)
# would otherwise hit a missing table on a fresh database. init_db() is
# idempotent, so the call in app.py stays harmless.
init_db()
