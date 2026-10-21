#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELDRIS 3.0 — Raspberry Pi voice loop (microphone -> assistant -> speaker).

Listens on the Pi's microphone, waits for the wake phrase ("ELDRIS"), records
the question, and posts it to the API. The API answers with the same live data
the dashboard shows and speaks the answer through the Pi's speaker, so this
script only has to handle listening.

Run it on the Pi (Python 3):

    pip3 install vosk
    sudo apt install alsa-utils espeak-ng
    ELDRIS_DEVICE_KEY=your-key python3 pi_voice_loop.py

Environment:
    ELDRIS_API          API base URL            (default http://127.0.0.1:5000)
    ELDRIS_DEVICE_KEY   matches DEVICE_API_KEY in backend/.env
    ELDRIS_VOSK_MODEL   path to an extracted Vosk model directory
                        (default ./vosk-model-small-en-us)
    ELDRIS_MIC_DEVICE   ALSA device for arecord  (default "default")
    ELDRIS_WAKE_WORDS   comma-separated wake words (default "eldris")
    ELDRIS_CHUNK_SECONDS  length of one listening chunk (default 4)
    ELDRIS_COMMAND_SECONDS  length of the question recording (default 5)

This is the optional voice front end. The web Assistant page already offers
browser speech input; this script exists so the room speaker and microphone
work without a browser open.
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave

API = os.getenv('ELDRIS_API', 'http://127.0.0.1:5000').rstrip('/')
DEVICE_KEY = os.getenv('ELDRIS_DEVICE_KEY', '').strip()
MODEL_DIR = os.getenv('ELDRIS_VOSK_MODEL', 'vosk-model-small-en-us')
MIC_DEVICE = os.getenv('ELDRIS_MIC_DEVICE', 'default')
WAKE_WORDS = [w.strip().lower() for w in os.getenv('ELDRIS_WAKE_WORDS', 'eldris').split(',') if w.strip()]
CHUNK_SECONDS = float(os.getenv('ELDRIS_CHUNK_SECONDS', '4'))
COMMAND_SECONDS = float(os.getenv('ELDRIS_COMMAND_SECONDS', '5'))
SAMPLE_RATE = 16000


def log(message):
    print('[voice] ' + message, flush=True)


def record(seconds, path):
    """Capture mono 16 kHz audio with ALSA's arecord."""
    argv = ['arecord', '-q', '-D', MIC_DEVICE, '-f', 'S16_LE', '-r', str(SAMPLE_RATE),
            '-c', '1', '-d', str(seconds), path]
    try:
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        log('arecord is missing. Install it with: sudo apt install alsa-utils')
        return False
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', 'ignore').strip()
        log('arecord failed: {}'.format(detail or exc))
        return False
    return True


def transcribe(recognizer, path):
    """Feed one WAV file through the recogniser and return the final text."""
    with wave.open(path, 'rb') as handle:
        if handle.getframerate() != SAMPLE_RATE:
            log('unexpected sample rate {}; expected {}'.format(handle.getframerate(), SAMPLE_RATE))
        while True:
            data = handle.readframes(4000)
            if not data:
                break
            recognizer.AcceptWaveform(data)
    return json.loads(recognizer.FinalResult()).get('text', '')


def ask(message):
    """Send recognised speech to the API, which answers and speaks it."""
    payload = json.dumps({'message': message, 'speak': True}).encode('utf-8')
    request = urllib.request.Request(API + '/api/voice', data=payload, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'X-Device-Key': DEVICE_KEY})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'ignore')
        try:
            return {'error': json.loads(body).get('error', body)}
        except ValueError:
            return {'error': body or 'HTTP {}'.format(exc.code)}
    except (urllib.error.URLError, OSError) as exc:
        return {'error': 'The ELDRIS API is not reachable at {} ({})'.format(API, exc)}


def main():
    try:
        from vosk import Model, KaldiRecognizer, SetLogLevel
    except ImportError:
        log('Vosk is not installed. Run: pip3 install vosk')
        return 1
    if not os.path.isdir(MODEL_DIR):
        log('Vosk model not found at {}. Download one, e.g. '
            'https://alphacephei.com/vosk/models (vosk-model-small-en-us-0.15) and unzip it here.'.format(MODEL_DIR))
        return 1
    if not DEVICE_KEY:
        log('ELDRIS_DEVICE_KEY is empty. Set it to the DEVICE_API_KEY from backend/.env, '
            'or run the API with a session-based setup instead.')
        return 1

    SetLogLevel(-1)
    model = Model(MODEL_DIR)
    log('listening for {} on {}'.format(', '.join(WAKE_WORDS), MIC_DEVICE))

    listener = KaldiRecognizer(model, SAMPLE_RATE)
    command = KaldiRecognizer(model, SAMPLE_RATE)
    with tempfile.TemporaryDirectory() as folder:
        chunk_path = os.path.join(folder, 'chunk.wav')
        question_path = os.path.join(folder, 'question.wav')
        while True:
            if not record(CHUNK_SECONDS, chunk_path):
                return 1
            heard = transcribe(listener, chunk_path).lower()
            if not heard:
                continue
            if not any(word in heard for word in WAKE_WORDS):
                log('heard "{}" (no wake word)'.format(heard[:80]))
                continue
            log('wake word heard, go ahead…')
            if not record(COMMAND_SECONDS, question_path):
                return 1
            question = transcribe(command, question_path).strip()
            if not question:
                log('nothing was understood, staying quiet')
                continue
            log('asked: {}'.format(question))
            result = ask(question)
            if 'error' in result:
                log('API error: {}'.format(result['error']))
                continue
            log('answered: {}'.format(result.get('reply', '')))
            log('speaker: {}'.format(result.get('spoken') or 'not spoken'))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log('stopped')
