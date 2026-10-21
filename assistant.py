# -*- coding: utf-8 -*-
"""Caregiver assistant for ELDRIS 3.0.

Two layers, in this order:

1. An offline intent engine that answers from LIVE dashboard data (rooms,
   readings, robot location, reminders, alerts, devices, movement). It needs no
   network, no API key and no extra dependency, so it always works on the
   Raspberry Pi.
2. An optional cloud LLM used only when a provider key is configured. If the
   call fails for any reason the assistant silently falls back to layer 1, so
   the dashboard never shows a broken chat.

app.py supplies a ``context`` mapping of callables returning live data and
``actions`` mapping of safe commands the assistant is allowed to trigger.
Nothing here touches the database directly.
"""
import os
import re
import json
import datetime
import urllib.request
import urllib.error

PROVIDERS = {
    'groq': {'key': 'GROQ_API_KEY', 'url': 'https://api.groq.com/openai/v1/chat/completions', 'model': 'llama-3.3-70b-versatile'},
    'openai': {'key': 'OPENAI_API_KEY', 'url': 'https://api.openai.com/v1/chat/completions', 'model': 'gpt-4o-mini'},
    'gemini': {'key': 'GOOGLE_API_KEY', 'url': 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', 'model': 'gemini-2.0-flash'},
}

SYSTEM_PROMPT = (
    'You are ELDRIS, the onboard assistant of an elderly-care companion robot. '
    'You help a caregiver watch over their home and the robot. '
    'Be warm, brief and concrete; quote real numbers from the context you are given. '
    'Never invent sensor readings, reminders or device states. '
    'If a device is offline or has never reported, say so plainly instead of guessing. '
    'Never claim to be an emergency service, a medical device, or a guaranteed alarm.'
)


def _normalise(text):
    text = (text or '').lower()
    text = re.sub(r'[^\w\s\'-]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _configured_provider():
    """Return the provider dict to use, or None when the assistant stays offline."""
    choice = os.getenv('CHAT_PROVIDER', 'auto').strip().lower()
    if choice in ('off', 'none', 'offline'):
        return None
    wanted = [choice] if choice in PROVIDERS else list(PROVIDERS.keys())
    for name in wanted:
        cfg = PROVIDERS[name]
        if os.getenv(cfg['key']):
            merged = dict(cfg)
            merged['name'] = name
            merged['model'] = os.getenv('CHAT_MODEL') or cfg['model']
            return merged
    return None


def _fmt(value, digits=1, suffix=''):
    if value is None:
        return 'unavailable'
    try:
        return ('{:.' + str(digits) + 'f}').format(float(value)) + suffix
    except (TypeError, ValueError):
        return 'unavailable'


def _next_reminders(reminders, limit=3):
    upcoming = []
    for reminder in reminders or []:
        if not reminder.get('enabled'):
            continue
        when = str(reminder.get('scheduled_at') or '')
        upcoming.append((when, reminder))
    upcoming.sort(key=lambda pair: pair[0])
    return [item for _, item in upcoming[:limit]]


def _summarise_readings(context):
    payload = context['readings']()
    home = payload.get('home') or {}
    if not home or not home.get('reported', True):
        return ('The home climate ESP32 has not reported yet, so I have no temperature or humidity to read. '
                'Power it and check ESP32_HOME_IP once it joins the network.')
    text = 'The home is {} °C with {}% humidity'.format(
        _fmt(home.get('temperature_c'), 1), _fmt(home.get('humidity_pct'), 0))
    if home.get('comfort') and home['comfort'] != 'unknown':
        text += ', which reads {}'.format(home['comfort'])
    if not home.get('online', True):
        text += '. That is the last reading it sent, and the node has not reported recently'
    return text + '.'


def _summarise_devices(context):
    payload = context['devices']()
    summary = payload.get('summary', {})
    by_kind = summary.get('by_kind', {})
    wearables = by_kind.get('wearable', 0)
    text = ('I am tracking {} devices: {} left-wrist band, {} drive ESP32, {} home climate ESP32, '
            '{} ESP32-CAM boards on the robot body, {} chatbot ESP32 with the microphone and speaker, '
            'and the Raspberry Pi hub. '
            '{} are online and {} are offline right now.').format(
        summary.get('total', 0),
        wearables or by_kind.get('wearable', 0), by_kind.get('controller', 0),
        by_kind.get('sensor', 0), by_kind.get('camera', 0), by_kind.get('audio', 0),
        summary.get('online', 0), summary.get('offline', 0))
    if wearables:
        text += (' ESP32 #1 carries the MPU6050, the MAX30102 and the MLX90614 body-temperature sensor; '
                 'there is one band and it is worn on the left wrist. ')
    text += ' ESP32 #4 listens for the five help phrases and asks me the questions it hears.'
    return text


def _motion(context):
    """Hand-motion snapshot, or an empty one when the service is unavailable."""
    try:
        return context['motion']()
    except (KeyError, TypeError):
        return {}


def _summarise_falls(context):
    payload = _motion(context)
    emergency = payload.get('emergency') or {}
    summary = payload.get('summary') or {}
    wearables = payload.get('wearables') or []
    wearables = [w for w in wearables if isinstance(w, dict)]
    if not wearables:
        return 'The wrist band has not reported yet, so I have no motion data to read.'
    if emergency.get('active'):
        parts = ['This is an active emergency: {}.'.format(emergency.get('headline', 'caregiver attention needed')),
                 str(emergency.get('detail') or '')]
        if emergency.get('since'):
            parts.append('Raised at {}.'.format(str(emergency['since'])[:19].replace('T', ' ')))
        parts.append('Please check on them now. Acknowledge it on the Motion & Falls page when it is handled.')
        return ' '.join(p for p in parts if p).strip()
    last = summary.get('last_fall_at')
    text = 'No fall is being reported right now.'
    if last:
        text += ' The most recent fall was recorded at {}.'.format(str(last)[:19].replace('T', ' '))
    else:
        text += ' No fall has been recorded since the band started reporting.'
    text += ' ESP32 #1 is worn on the wrist; a fall is flagged when there is a sudden change on the Y axis, '
    text += 'followed by the hand staying still or changing orientation.'
    return text


def _summarise_tremor(context):
    payload = _motion(context)
    wearables = [w for w in (payload.get('wearables') or []) if isinstance(w, dict)]
    summary = payload.get('summary') or {}
    if not wearables:
        return 'The wrist band has not reported yet, so I have no tremor data to read.'
    band = summary.get('tremor_band_hz') or [4.0, 12.0]
    lines = []
    for wearable in wearables:
        tremor = wearable.get('tremor') or {}
        if tremor.get('active'):
            lines.append('{}: active tremor at {} Hz, about {} g, held long enough to count.'.format(
                wearable.get('name'), _fmt(tremor.get('frequency_hz'), 1), _fmt(tremor.get('amplitude_g'), 3)))
        else:
            measured = tremor.get('frequency_hz') or 0
            suffix = ', last measured at {} Hz'.format(_fmt(measured, 1)) if measured > 0 else ''
            lines.append('{}: no tremor, hand movement is quiet{}.'.format(wearable.get('name'), suffix))
    head = 'I measure small up-and-down hand oscillation and call it a tremor when it sits in the {}-{} Hz band '
    head += 'for more than a few seconds.\n'
    return head.format(_fmt(band[0] if len(band) > 0 else 4.0, 0), _fmt(band[1] if len(band) > 1 else 12.0, 0)) + '\n'.join(lines)


def _summarise_vitals(context):
    """Heart rate, blood oxygen and body temperature from the single wrist band."""
    payload = _motion(context)
    wearables = [w for w in (payload.get('wearables') or []) if isinstance(w, dict)]
    if not wearables:
        return 'The wrist band has not reported yet, so I have no vitals to read.'
    band = wearables[0]
    vitals = band.get('vitals') or {}
    name = band.get('name') or 'The wrist band'
    if vitals.get('heart_rate_bpm') is None and vitals.get('spo2_pct') is None and vitals.get('body_temperature_c') is None:
        return '{} is online but has not sent a heart rate, oxygen or temperature reading yet.'.format(name)
    parts = []
    if vitals.get('heart_rate_bpm') is not None:
        parts.append('heart rate {} bpm'.format(_fmt(vitals.get('heart_rate_bpm'), 0)))
    if vitals.get('spo2_pct') is not None:
        parts.append('blood oxygen {}%'.format(_fmt(vitals.get('spo2_pct'), 1)))
    if vitals.get('body_temperature_c') is not None:
        parts.append('body temperature {} °C'.format(_fmt(vitals.get('body_temperature_c'), 1)))
    text = 'From the left wrist band: {}.'.format(', '.join(parts))
    if vitals.get('stale'):
        text += ' The last vitals packet is a little old, so treat it as the latest known reading.'
    text += ' I am not a medical device and these are not a diagnosis.'
    return text


def _help_words():
    """The five phrases the chatbot board listens for."""
    try:
        import voice
        return ', '.join('"{}"'.format(word) for word in voice.HELP_KEYWORDS)
    except Exception:
        return '"help", "help me", "please help", "i need help", "i have fallen"'


def _summarise_cameras(context):
    try:
        overview = context['camera']()
    except (KeyError, TypeError):
        overview = {}
    sources = [s for s in (overview.get('sources') or []) if isinstance(s, dict)]
    live = [s for s in sources if s.get('stream_configured')]
    if not sources:
        return 'No cameras are registered yet.'
    online = [s for s in sources if s.get('online')]
    if not live:
        return ('There are {} ESP32-CAM boards on the robot, but neither has an address configured yet. '.format(len(sources))
                + 'Set CAMERA_ESP_FRONT_IP and CAMERA_ESP_REAR_IP in backend/.env to see live video on the Camera page.')
    if not online:
        return ('Both ESP32-CAM boards have an address, but neither is answering right now. '
                'Check their power and that they are on the 2.4 GHz Wi-Fi.')
    text = '{} of {} ESP32-CAM boards are online on the {} robot. '.format(
        len(online), len(sources), (sources[0].get('room_name') or 'the'))
    text += 'Streaming now: {}. '.format(', '.join(s.get('name') for s in online))
    text += 'Frames are relayed and never stored.'
    return text


def offline_reply(message, context, actions):
    """Intent layer. Always available; never touches the network."""
    text = _normalise(message)
    status = context['status']()
    zones = context['rooms']() if 'rooms' in context else []
    robot = context['robot_location']()
    reminders = context['reminders']()
    alerts = context['alerts']()
    open_alerts = [a for a in alerts if a.get('status') != 'resolved']

    if not text:
        return 'Tell me what you would like to check and I will look it up for you.', 'intent'

    # --- the five help phrases, answered before anything else ---------------
    if re.search(r'\b(help (words?|phrases?)|keywords?|safe words?|trigger words?|code words?)\b', text):
        return ('The chatbot ESP32 listens for five phrases: {}. Hearing any one of them raises a critical '
                'alert, opens a recording window and sends the robot to the patient. The list is set by '
                'HELP_KEYWORDS in backend/.env.').format(_help_words()), 'intent'

    # --- falls, emergencies and hand tremor, checked first ------------------
    safety_question = re.search(r'\bis (she|he|they|the resident|the patient|mum|mom|dad|grandma|grandpa) (ok|okay|safe|alright)\b|\bare they (ok|okay|safe|alright)\b', text)
    if safety_question or re.search(r'\b(fall|falls|fallen|fell|collapse|collapsed|emergency|sos|panic|urgent|help now|alarm)\b', text):
        reply = _summarise_falls(context)
        payload = _motion(context)
        if safety_question and not (payload.get('emergency') or {}).get('active'):
            reply = ('I can only see motion from the wrist band, not how someone feels or what '
                     'they are doing. ' + reply)
        return reply, 'intent'
    if re.search(r'\b(tremor|tremors|shaking|shaky|shake|trembling|tremble|hands?|hand band|wrist)\b', text):
        return _summarise_tremor(context), 'intent'

    # --- speaker, checked before the device inventory -----------------------
    if re.search(r'\b(say|speak|announce|out loud|aloud|voice message)\b', text):
        speaker = actions.get('speak') if isinstance(actions, dict) else None
        if speaker is None:
            return 'The speaker is not available on this installation yet.', 'intent'
        said = re.search(r'\b(?:say|speak|announce|tell them|read out)\b[:,]?\s+(.+)$', message.strip(), re.IGNORECASE)
        phrase = said.group(1).strip(' .!?') if said else ''
        if not phrase:
            return 'Tell me what to say, for example: say it is time for your medication.', 'intent'
        result = speaker(phrase)
        return result.get('message', 'Done.'), 'intent'

    # --- movement, checked before generic room talk -------------------------
    if text in ('stop', 'stop moving', 'halt', 'emergency stop', 'stop the robot'):
        result = actions['move']('stop')
        return result['message'], 'intent'
    for direction, word in (('forward', 'forward'), ('reverse', 'back'), ('reverse', 'backward'),
                            ('left', 'left'), ('right', 'right')):
        if word == text or text in ('move ' + word, word + ' please', 'go ' + word):
            result = actions['move'](direction)
            return result['message'], 'intent'

    # --- robot location -----------------------------------------------------
    move_match = re.search(r'\b(?:move|send|drive|take|place)\b.*\b(?:robot|eldris|bot|camera)\b.*\b(?:to|into)\b\s+([a-z ]+?)$', text)
    if move_match:
        target = move_match.group(1).strip()
        for room in zones:
            name = str(room.get('room_name', '')).lower()
            if target in name or name in target:
                result = actions['move_robot_to'](room.get('room'))
                if 'error' in result:
                    return result['error'], 'intent'
                return ('Moving to the {}. Both body cameras now report from the {}.').format(
                    room.get('room_name'), room.get('room_name')), 'intent'
    if re.search(r'\b(where|which room)\b.*\b(robot|eldris|bot|camera|you)\b|\brobot (location|position)\b|\bwhere are you\b', text):
        location = 'The robot is currently in the {}.'.format(robot.get('room_name'))
        if status.get('drive_connected'):
            location += ' Its drive board is answering there.'
        else:
            location += ' That is the last place it was sent; the drive board is not answering right now.'
        return location, 'intent'

    # --- body vitals (the single wrist band) --------------------------------
    if re.search(r'\b(heart rate|heartbeat|pulse|bpm|oxygen|spo2|o2|saturation|body temp|skin temp|temperature of the body)\b', text):
        return _summarise_vitals(context), 'intent'

    # --- home climate -------------------------------------------------------
    want_humidity = re.search(r'\b(humidity|humid|damp|moisture)\b', text)
    want_temp = re.search(r'\b(temp|temperature|hot|cold|warm|chill|thermostat|degree)\b', text)
    if want_humidity or want_temp:
        return _summarise_readings(context), 'intent'

    # --- status -------------------------------------------------------------
    if re.search(r'\b(status|online|offline|connected|how are you|robot ok|health)\b', text):
        if status.get('drive_connected'):
            return ('The drive ESP32 is connected. Mode {}, movement {}.').format(
                status.get('mode'), status.get('movement')), 'intent'
        if not status.get('drive_url'):
            return ('The drive ESP32 has no address configured yet. Set DRIVE_ESP_IP in backend/.env to the board address, '
                    'then it can report and drive.'), 'intent'
        return 'The drive ESP32 is not answering at {}. Check its power, Wi-Fi and that address.'.format(
            status.get('drive_url')), 'intent'

    # --- reminders ----------------------------------------------------------
    if re.search(r'\b(remind|reminder|upcoming|next|schedule|medication|tablet|appointment)\b', text):
        upcoming = _next_reminders(reminders)
        if not upcoming:
            return 'There are no upcoming reminders scheduled right now.', 'intent'
        lines = ['{} at {} ({})'.format(r.get('title'), r.get('scheduled_at', '')[:16].replace('T', ' '), r.get('category')) for r in upcoming]
        return 'You have {} upcoming reminder{}:\n{}'.format(len(upcoming), 's' if len(upcoming) > 1 else '',
                                                             '\n'.join('- ' + line for line in lines)), 'intent'

    # --- alerts -------------------------------------------------------------
    if re.search(r'\b(alert|alerts|warning|warn|alarm|attention)\b', text):
        if not open_alerts:
            return 'There are no open alerts. Everything on the dashboard is clear.', 'intent'
        lines = ['{} ({}) — {}'.format(a.get('type', '').replace('_', ' '), a.get('severity'), a.get('description')) for a in open_alerts[:4]]
        verb = 'need' if len(open_alerts) > 1 else 'needs'
        return '{} alert{} {} your attention:\n{}'.format(len(open_alerts), 's' if len(open_alerts) > 1 else '', verb,
                                                          '\n'.join('- ' + line for line in lines)), 'intent'

    # --- cameras ------------------------------------------------------------
    if re.search(r'\b(camera|cameras|cctv|video|stream|live view|picture)\b', text):
        return _summarise_cameras(context), 'intent'

    # --- devices ------------------------------------------------------------
    if re.search(r'\b(device|devices|sensor|sensors|camera|cameras|esp32|speaker|microphone|mic|hardware|mpu\s?6050|imu)\b', text):
        return _summarise_devices(context), 'intent'

    # --- time ---------------------------------------------------------------
    if re.search(r'\b(time|date|day|today|clock)\b', text):
        now = datetime.datetime.now()
        return 'It is {} on {}.'.format(now.strftime('%H:%M'), now.strftime('%A %d %B %Y')), 'intent'

    # --- courtesy -----------------------------------------------------------
    if re.search(r'^(hi|hello|hey|good morning|good afternoon|good evening)\b', text):
        return 'Hello. I can read the home temperature and humidity, report the wrist band vitals, tell you where the robot is, list reminders and alerts, and move the robot between rooms. What would you like to know?', 'intent'
    if re.search(r'\b(thanks|thank you|cheers|great)\b', text):
        return 'You are welcome. I am here if you need anything else.', 'intent'
    if re.search(r'\b(help|what can you do|commands)\b', text):
        return ('I can answer questions about home temperature and humidity, and about heart rate, '
                'blood oxygen and body temperature from the left wrist band; tell you '
                'whether there has been a fall or a hand tremor; tell you where the robot is; read '
                'your reminders and alerts; summarise the devices and cameras; speak a phrase out '
                'loud with "say it is time to take your medication"; and move the robot with '
                '"move robot to the bedroom". The chatbot ESP32 also listens for five help phrases '
                '({}) and escalates them to you. Ask me "any falls today" to try it.').format(_help_words()), 'intent'

    return ("I did not catch that one. Try asking about the home climate, the wrist band vitals, "
            "reminders, alerts, devices, the time, or say \"move robot to the kitchen\"."), 'intent'


def llm_reply(message, history, context):
    """Optional cloud layer. Returns None on any failure so the caller falls back."""
    provider = _configured_provider()
    if provider is None:
        return None
    payload = {
        'status': context['status'](),
        'home_climate': context['readings']().get('summary', {}),
        'navigation_rooms': context['rooms']() if 'rooms' in context else [],
        'robot_location': context['robot_location'](),
        'devices': context['devices']().get('summary', {}),
        'reminders': _next_reminders(context['reminders'](), 5),
        'open_alerts': [a for a in context['alerts']() if a.get('status') != 'resolved'][:5],
        'hand_motion': (_motion(context).get('summary') or {}),
        'recent_motion_events': (_motion(context).get('events') or [])[:5],
        'emergency': (_motion(context).get('emergency') or {}),
        'help_phrases': (_help_words()),
        'voice_node': (context['voice']() if 'voice' in context else {}),
    }
    system = SYSTEM_PROMPT + '\n\nLive dashboard context:\n' + json.dumps(payload, default=str)
    messages = [{'role': 'system', 'content': system}]
    for entry in (history or [])[-6:]:
        role = 'assistant' if entry.get('role') == 'assistant' else 'user'
        if entry.get('content'):
            messages.append({'role': role, 'content': str(entry['content'])[:600]})
    messages.append({'role': 'user', 'content': message})

    url = provider['url']
    headers = {'Content-Type': 'application/json', 'User-Agent': 'ELDRIS/3.0'}
    if provider['name'] == 'gemini':
        url = url.format(model=provider['model'])
        url += '?key=' + os.getenv(provider['key'])
        body = {'contents': [{'parts': [{'text': m['content']} for m in messages]}]}
        field = 'candidates'
    else:
        headers['Authorization'] = 'Bearer ' + os.getenv(provider['key'])
        body = {'model': provider['model'], 'messages': messages, 'temperature': 0.3, 'max_tokens': 400}
        field = 'choices'
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=float(os.getenv('CHAT_TIMEOUT_SECONDS', '12'))) as response:
            data = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, ValueError, OSError, KeyError):
        return None
    try:
        if provider['name'] == 'gemini':
            return data[field][0]['content']['parts'][0]['text']
        return data[field][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        return None


def respond(message, history, context, actions):
    """Answer using the LLM when configured, otherwise the offline engine."""
    provider = _configured_provider()
    if provider is not None:
        reply = llm_reply(message, history, context)
        if reply:
            return reply.strip(), 'llm:' + provider['name']
    return offline_reply(message, context, actions)
