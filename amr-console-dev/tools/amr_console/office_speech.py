"""Office tasks, speech input: check a short recording, have it written down, return the words.

The page records at most a few seconds of 16 kHz mono WAV and posts it to this computer only.
The speech worker runs in its own Python environment (config speech.path, default
../amr-speech) with pinned local models and no network. The words go back into the page's text
box; a person reads them and presses Send. Nothing spoken is acted on directly.
"""
import array
import hashlib
import io
import json
import math
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import sys
import time
import wave

LANGUAGES = ('en', 'ta', 'ml', 'te')
LANGUAGE_NAMES = {'en': 'English', 'ta': 'Tamil', 'ml': 'Malayalam', 'te': 'Telugu'}
MAX_AUDIO_BYTES = 1024 * 1024
SAMPLE_RATE = 16000
MINIMUM_SECONDS = .3
# A recording whose loudest sound is below this is silence: a muted or disconnected microphone.
SILENT_PEAK_DB = -50.0
# More clipped samples than this, or a centre offset beyond this, means an overloaded or faulty input.
DISTORTED_CLIPPED = 0.01
DISTORTED_OFFSET = 0.05
TRANSCRIBE_TIMEOUT_S = 90
ENGINES = ('faster_whisper', 'indic_conformer')
ITEM_WORDS = ('file', 'folder', 'document', 'charger', 'encoder', 'laptop', 'battery pack',
              'cable', 'board', 'parcel', 'keys', 'tray')


class SpeechError(Exception):
    def __init__(self, status, code, reason):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


def wav_level(raw):
    """Loudest sample and average level of a checked WAV recording, in dB below full scale."""
    with wave.open(io.BytesIO(bytes(raw)), 'rb') as stream:
        data = stream.readframes(stream.getnframes())
    samples = array.array('h')
    samples.frombytes(data)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples:
        return -120.0, -120.0
    peak = max(max(samples), -min(samples)) / 32768
    rms = math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768

    def decibels(value):
        return round(max(-120.0, 20 * math.log10(value)), 1) if value > 0 else -120.0
    return decibels(peak), decibels(rms)


def wav_distortion(raw):
    """Share of clipped samples and the signal's centre offset (both 0 for a clean recording)."""
    with wave.open(io.BytesIO(bytes(raw)), 'rb') as stream:
        data = stream.readframes(stream.getnframes())
    samples = array.array('h')
    samples.frombytes(data)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples:
        return 0.0, 0.0
    clipped = sum(1 for v in samples if v >= 32440 or v <= -32440) / len(samples)
    return round(clipped, 3), round(sum(samples) / len(samples) / 32768, 3)


def check_wav(raw, maximum_seconds):
    """Return the duration of a valid 16 kHz mono 16-bit WAV recording, or raise SpeechError."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise SpeechError(422, 'speech_empty', 'The recording is empty.')
    if len(raw) > MAX_AUDIO_BYTES:
        raise SpeechError(413, 'speech_size', 'The recording is too large (1 MB at most).')
    try:
        with wave.open(io.BytesIO(bytes(raw)), 'rb') as stream:
            channels, width, rate = stream.getnchannels(), stream.getsampwidth(), stream.getframerate()
            frames = stream.getnframes()
            data = stream.readframes(frames)
    except (wave.Error, EOFError, ValueError) as exc:
        raise SpeechError(422, 'speech_format', 'Send 16 kHz mono 16-bit WAV audio.') from exc
    if (channels, width, rate) != (1, 2, SAMPLE_RATE) or len(data) != frames * 2:
        raise SpeechError(422, 'speech_format', 'Send 16 kHz mono 16-bit WAV audio.')
    seconds = frames / SAMPLE_RATE
    if seconds < MINIMUM_SECONDS:
        raise SpeechError(422, 'speech_short', 'The recording is too short. Speak for at least a second.')
    if seconds > maximum_seconds + .5:
        raise SpeechError(422, 'speech_long', f'Keep a recording under {maximum_seconds:g} seconds.')
    return seconds


def load_models(root, speech):
    path = (Path(root) / speech.get('models', 'config/speech_models.json')).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('speech.models must stay inside this repository.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('models'), dict):
        raise ValueError(f'{path.name} must have schema_version 1 and models.')
    for key, entry in value['models'].items():
        if not isinstance(entry, dict) or entry.get('engine') not in ENGINES \
                or not isinstance(entry.get('repo'), str) or not isinstance(entry.get('revision'), str) \
                or len(entry['revision']) != 40:
            raise ValueError(f'{path.name}: {key} needs an engine, a repo and a 40-character revision.')
    return value['models']


def snapshot_dir(base, entry):
    """Where an approved download of this model lives (Hugging Face cache layout, pinned revision)."""
    return Path(base) / 'hf' / 'hub' / ('models--' + entry['repo'].replace('/', '--')) / 'snapshots' / entry['revision']


def install_status(root, config):
    """What speech input can do on this computer now, without starting anything."""
    speech = config.get('speech') or {}
    if not isinstance(speech, dict) or not speech.get('enabled', False):
        return {'state': 'disabled', 'detail': 'turned off in config/office_tasks.yaml.', 'languages': {}}
    try:
        base = (Path(root) / str(speech.get('path', '../amr-speech'))).resolve()
        models = load_models(root, speech)
        languages = speech.get('languages') or {}
        if not isinstance(languages, dict) or not set(languages) <= set(LANGUAGES):
            raise ValueError('speech.languages may name en, ta, ml and te only.')
        if any(key not in models for key in languages.values()):
            raise ValueError('speech.languages names a model that is not in the models file.')
    except (OSError, ValueError) as exc:
        return {'state': 'error', 'detail': f'the speech settings could not be read ({exc}).', 'languages': {}}
    python = base / 'venv/bin/python'
    found = {language: snapshot_dir(base, models[key]).is_dir() for language, key in languages.items()}
    if not python.is_file() or not any(found.values()):
        return {'state': 'not_installed', 'base': str(base),
                'detail': 'not installed yet (it needs the owner-approved speech install). Typing works.',
                'languages': {language: 'missing' for language in languages}}
    ready = [LANGUAGE_NAMES[language] for language, ok in found.items() if ok]
    return {'state': 'off', 'base': str(base), 'python': str(python),
            'detail': 'installed for ' + ', '.join(ready) + '. It starts with the language service.',
            'languages': {language: ('installed' if ok else 'missing') for language, ok in found.items()}}


def prompt_words(context):
    """Names the English model is told to expect: people, rooms and common office items."""
    words = [row['name'] for row in context.requesters()] + [p['label'] for p in context.places() if p.get('kind') == 'room']
    return ', '.join(dict.fromkeys(words + list(ITEM_WORDS)))


class SpeechProcess:
    """The speech worker process: JSON lines, one transcription at a time."""

    def __init__(self, root, spec, on_status):
        self.root, self.spec, self.on_status = Path(root), spec, on_status
        self.proc = None
        self.write_lock = threading.Lock()
        self.busy = threading.Lock()
        self.pending = {}
        self.pending_lock = threading.Lock()

    def start(self):
        log_dir = Path(self.spec['log_dir'])
        log_dir.mkdir(parents=True, exist_ok=True)
        spec_path = log_dir / 'speech_spec.json'
        spec_path.write_text(json.dumps(self.spec, indent=2, ensure_ascii=False) + '\n')
        base = Path(self.spec['base'])
        env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'PATH') if key in os.environ}
        threads = str(self.spec.get('threads', 4))
        # Offline: models come only from the approved local folder; nothing is downloaded.
        env.update(HF_HOME=str(base / 'hf'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   HF_HUB_DISABLE_TELEMETRY='1', OMP_NUM_THREADS=threads, MKL_NUM_THREADS=threads)
        if os.environ.get('AMR_CONSOLE_TEST_SPEECH') == '1':
            env['AMR_CONSOLE_TEST_SPEECH'] = '1'
        self.proc = subprocess.Popen(
            [self.spec['python'], '-I', str(self.root / 'tools/amr_console/speech_worker.py'), '--spec', str(spec_path)],
            cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _log(self, line):
        try:
            with (Path(self.spec['log_dir']) / 'speech_worker.log').open('a') as stream:
                stream.write(line if line.endswith('\n') else line + '\n')
        except OSError:
            pass

    def _read(self):
        for line in self.proc.stdout:
            self._log(line)
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError
            except ValueError:
                continue
            waiter = None
            if event.get('request_id'):
                with self.pending_lock:
                    waiter = self.pending.get(event['request_id'])
            if waiter is not None:
                waiter.put(event)
            else:
                self.on_status(event)
        code = self.proc.wait()
        with self.pending_lock:
            for waiter in self.pending.values():
                waiter.put({'type': 'error', 'code': 'speech_exit', 'reason': f'The speech worker exited (code {code}).'})
        self.on_status({'type': 'exit', 'code': code})

    def _stderr(self):
        for line in self.proc.stderr:
            self._log('[stderr] ' + line)

    def send(self, message):
        with self.write_lock:
            if self.proc is None or self.proc.poll() is not None:
                raise SpeechError(409, 'speech_off', 'The speech worker is not running.')
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def transcribe(self, path, language, timeout=TRANSCRIBE_TIMEOUT_S):
        if not self.busy.acquire(timeout=timeout):
            raise SpeechError(409, 'speech_busy', 'Another recording is being written down. Try again.')
        try:
            request_id = secrets.token_hex(8)
            waiter = queue.Queue()
            with self.pending_lock:
                self.pending[request_id] = waiter
            try:
                self.send({'type': 'transcribe', 'request_id': request_id, 'path': str(path), 'language': language})
                try:
                    event = waiter.get(timeout=timeout)
                except queue.Empty:
                    raise SpeechError(504, 'speech_timeout', 'Writing down the recording took too long.')
            finally:
                with self.pending_lock:
                    self.pending.pop(request_id, None)
        finally:
            self.busy.release()
        if event.get('type') != 'transcript':
            raise SpeechError(409 if event.get('code') != 'speech_failed' else 500,
                              event.get('code', 'speech_failed'), event.get('reason', 'Speech could not be written down.'))
        return event

    def close(self, timeout=10):
        if self.proc is None:
            return
        try:
            self.send({'type': 'shutdown'})
        except SpeechError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Speech:
    """Speech input for one language-service run; owned by OfficeTasks."""

    def __init__(self, root, config, *, process_factory=SpeechProcess, clock=time.monotonic, on_change=None):
        self.root, self.config = Path(root), config
        self.settings = config.get('speech') or {}
        self.process_factory = process_factory
        self.clock = clock
        self.on_change = on_change
        self.lock = threading.Lock()
        self.process = None
        self.log_dir = None
        self.state = None          # None: not started; else starting / ready / error
        self.detail = ''
        self.languages = {}
        self.failures = {}
        self.cached = (0, None)

    def status(self):
        with self.lock:
            if self.state is None:
                stamp, value = self.cached
                if value is None or self.clock() - stamp > 5:
                    value = install_status(self.root, self.config)
                    self.cached = (self.clock(), value)
                return {key: value[key] for key in ('state', 'detail', 'languages')}
            return {'state': self.state, 'detail': self.detail, 'languages': dict(self.languages)}

    def start(self, log_dir, context, test_python=None):
        """Start the worker if speech is installed. test_python: tests only, with a stand-in engine."""
        status = install_status(self.root, self.config)
        if status['state'] != 'off' and test_python is None:
            return False
        models = load_models(self.root, self.settings)
        languages = {language: key for language, key in (self.settings.get('languages') or {}).items()
                     if test_python is not None or status['languages'].get(language) == 'installed'}
        base = status.get('base') or str((self.root / str(self.settings.get('path', '../amr-speech'))).resolve())
        spec = {'base': base, 'python': test_python or status['python'], 'log_dir': str(Path(log_dir)),
                'languages': languages, 'models': {key: models[key] for key in set(languages.values())},
                'threads': max(1, min(16, int(self.settings.get('threads', 4)))),
                'prompt_words': prompt_words(context), 'test_engine': test_python is not None}
        with self.lock:
            self.log_dir = Path(log_dir)
            self.state, self.detail = 'starting', 'loading the speech models…'
            self.languages = {language: 'loading' for language in languages}
            self.failures = {}
            self.process = self.process_factory(self.root, spec, self._on_status)
        try:
            self.process.start()
        except Exception as exc:
            with self.lock:
                self.process = None
                self.state, self.detail = 'error', f'the speech worker could not start ({exc}).'
            return False
        return True

    def _on_status(self, event):
        with self.lock:
            if self.state is None:
                return          # stopped: late lines from the old worker change nothing
            self._update(event)
        if self.on_change is not None:
            self.on_change()

    def _update(self, event):
        """Fold one worker line into the state. Called with the lock held."""
        kind = event.get('type')
        if kind == 'exit':
            self.state, self.detail = 'error', f"the speech worker exited (code {event.get('code')})."
            self.languages = {language: 'error' for language in self.languages}
            return
        if kind == 'status' and event.get('language') in self.languages:
            self.languages[event['language']] = event.get('state', 'error')
            if event.get('state') == 'error':
                self.failures[event['language']] = str(event.get('detail') or 'the model could not load')[:200]
        ready = [LANGUAGE_NAMES[l] for l, s in self.languages.items() if s == 'ready']
        loading = [l for l, s in self.languages.items() if s == 'loading']
        failed = [f'{LANGUAGE_NAMES[l]}: {self.failures.get(l, "could not load")}'
                  for l, s in self.languages.items() if s == 'error']
        if ready:
            self.state = 'ready'
            self.detail = ('ready for ' + ', '.join(ready) + ('; still loading the others' if loading else '')
                           + ('. Not available: ' + '; '.join(failed) if failed else '') + '.')
        elif loading:
            self.state, self.detail = 'starting', 'loading the speech models…'
        else:
            self.state, self.detail = 'error', 'no speech model could load. ' + '; '.join(failed)

    def stop(self):
        with self.lock:
            process, self.process = self.process, None
            self.state, self.detail, self.languages, self.failures = None, '', {}, {}
        if process is not None:
            process.close()

    def transcribe(self, raw, language, hint, fallback):
        """Write one recording down. language: auto/en/ta/ml/te; hint: the speaker's own language."""
        maximum = float(self.settings.get('maximum_seconds', 15))
        seconds = check_wav(raw, maximum)
        chosen = language if language in LANGUAGES else hint if hint in LANGUAGES else fallback
        with self.lock:
            process, languages, log_dir = self.process, dict(self.languages), self.log_dir
        if process is None or not process.alive():
            raise SpeechError(409, 'speech_off', 'Speech input is not running. Start the language service (speech must be installed).')
        if chosen not in languages:
            raise SpeechError(409, 'speech_language', f'{LANGUAGE_NAMES.get(chosen, chosen)} speech input is not installed. Type the request instead.')
        if languages[chosen] == 'loading':
            raise SpeechError(409, 'speech_loading', f'The {LANGUAGE_NAMES[chosen]} speech model is still loading. Try again in a moment.')
        if languages[chosen] != 'ready':
            raise SpeechError(409, 'speech_language', f'The {LANGUAGE_NAMES[chosen]} speech model could not load. Type the request instead.')
        folder = log_dir / 'speech'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (time.strftime('%H%M%S') + '_' + secrets.token_hex(4) + '.wav')
        path.write_bytes(bytes(raw))
        keep = bool(self.settings.get('keep_audio', False))
        peak_db, rms_db = wav_level(raw)
        # Silence (a muted microphone sends only zeros) is reported as such; the model is not asked.
        silent = peak_db < SILENT_PEAK_DB
        clipped, offset = wav_distortion(raw)
        # An empty headset jack at full boost sends a loud hum: the words that come back are guesses.
        distorted = not silent and (clipped > DISTORTED_CLIPPED or abs(offset) > DISTORTED_OFFSET)
        try:
            event = {'text': '', 'model': None, 'latency_ms': 0} if silent else process.transcribe(path, chosen)
        finally:
            if not keep:
                try:
                    path.unlink()
                except OSError:
                    pass
        text = str(event.get('text') or '').strip()[:1000]
        result = {'text': text, 'language': chosen, 'model': event.get('model'), 'seconds': round(seconds, 2),
                  'latency_ms': event.get('latency_ms'), 'peak_db': peak_db, 'rms_db': rms_db, 'silent': silent,
                  'clipped': clipped, 'offset': offset, 'distorted': distorted}
        try:
            with (log_dir / 'speech.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(result, audio=path.name if keep else None,
                                             at=time.strftime('%Y-%m-%dT%H:%M:%S')), ensure_ascii=False) + '\n')
        except OSError:
            pass
        return result
