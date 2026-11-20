"""Public, side-effect-free command-understanding interface; never executes tasks."""
from collections import Counter, defaultdict
import math
import re
import threading
import time
import unicodedata

from .backends import BackendError, BackendTimeout, MockBackend, OllamaBackend
from .calibration import file_sha256, load_threshold, local_path, signature
from .conformal import prediction_set
from .grounding import Names, NONE, normalize
from .schema import SLOTS, valid_string, validate_task_form
from .templates import question, refusal

_MODEL_LOCK = threading.Lock()
DEFAULTS = {'model': 'qwen3.5:2b-q4_K_M', 'samples': 7, 'timeout_s': 2.,
            'temperature': .7, 'num_ctx': 4096, 'num_predict': 512, 'seed': 1729, 'port': 11434}


def detect_language(text):
    scripts = set()
    for char in text:
        for start, end, lang in ((0x0B80, 0x0BFF, 'ta'), (0x0D00, 0x0D7F, 'ml'), (0x0C00, 0x0C7F, 'te')):
            if start <= ord(char) <= end:
                scripts.add(lang)
    if len(scripts) > 1 or (scripts and re.search('[a-zA-Z]', text)):
        return 'mixed'
    return next(iter(scripts), 'en')


def disallowed(text):
    value = unicodedata.normalize('NFC', text).casefold().replace('’', "'")
    latin = r"\b(?:don't|dont|do\s+not|not|never|no|if|unless|without|pogathe|pogaadhe|pogadhe|vendam|vendaam|venam|irundha|iruntha|pokaruthu|enkil|vadd[uoa]|unte)\b"
    native = ('வேண்டாம்', 'வேண்டாம', 'போகாத', 'செல்லாத', 'என்றால்', 'இருந்தால்',
              'പോകരുത്', 'വേണ്ട', 'എങ്കിൽ', 'ഉണ്ടെങ്കിൽ', 'വെള്ളരുത്',
              'వెళ్లవద్దు', 'వద్దు', 'ఉంటే', 'అయితే')
    return bool(re.search(latin, value) or any(word in value for word in native))


def source_span(said, text):
    """Reject invented/translated names. Preserve Indic combining marks and word joiners."""
    query, source = normalize(said), normalize(text)
    def word_char(char):
        return char.isalnum() or char == '_' or unicodedata.category(char)[0] in ('M',) or char in ('\u200c','\u200d')
    for match in re.finditer(re.escape(query), source):
        before, after = source[:match.start()], source[match.end():]
        if ((not before or not word_char(before[-1])) and (not after or not word_char(after[0]))):
            return True
    return False


def _settings(raw):
    if raw is not None and not isinstance(raw, dict):
        raise ValueError('Settings must be a dictionary')
    settings = {**DEFAULTS, **(raw or {})}
    allowed = {*DEFAULTS, 'backend', 'calibration_file', 'model_file', 'model_manifest_file', 'runner_version'}
    if set(settings) - allowed:
        raise ValueError('Unknown setting')
    if settings.get('backend') is not None and not isinstance(settings['backend'], MockBackend):
        raise ValueError('Only an explicit mock backend may be injected')
    for key, lower, upper in (('samples', 5, 10), ('num_ctx', 1024, 4096),
                               ('num_predict', 128, 1024), ('port', 1, 65535), ('seed', 0, 2**31-11)):
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError('Invalid integer setting')
    for key, lower, upper in (('timeout_s', .01, 120.), ('temperature', .1, 1.2)):
        val = settings[key]
        if type(val) not in (int, float) or not math.isfinite(val) or not lower <= val <= upper:
            raise ValueError('Invalid numeric setting')
    if (not isinstance(settings['model'], str)
            or not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,150}', settings['model'])
            or 'cloud' in settings['model'].casefold()):
        raise ValueError('Need a local model ID')
    return settings


def understand(text: str, names: dict, language: str | None = None,
               settings: dict | None = None) -> dict:
    started = time.monotonic()
    lang = language if language in ('en', 'ta', 'ml', 'te') else detect_language(text if isinstance(text, str) else '')
    result = {'text': text if isinstance(text, str) else '', 'language': lang, 'steps': [],
              'decision': 'refuse', 'question': None, 'refusal': None,
              'model': {'name': None, 'file_sha256': None}, 'samples': 0, 'latency_ms': 0.}

    def finish(reason=None):
        if reason:
            result['decision'] = 'refuse'
            result['question'] = None
            result['refusal'] = refusal(reason, result['language'])
        result['latency_ms'] = round((time.monotonic()-started)*1000, 3)
        return result

    if not valid_string(text) or language not in (None, 'en', 'ta', 'ml', 'te'):
        return finish('invalid_input')
    text_nfc = unicodedata.normalize('NFC', text)
    try:
        roster = Names(names)
    except (ValueError, TypeError, KeyError):
        return finish('invalid_names')
    try:
        config = _settings(settings)
    except (ValueError, TypeError):
        return finish('invalid_settings')
    if disallowed(text_nfc):
        return finish('out_of_scope')
    backend = config.get('backend')
    if backend is None:
        model_hash = None
        if config.get('model_file'):
            try:
                model_hash = file_sha256(local_path(config['model_file']))
            except (OSError, ValueError, TypeError):
                return finish('invalid_settings')
        backend = OllamaBackend(config['model'], model_hash)
    if not isinstance(backend, (MockBackend, OllamaBackend)):
        return finish('invalid_settings')
    if not isinstance(backend, MockBackend) and roster.raw.get('fixture_source'):
        return finish('invalid_names')
    result['model'] = {'name': backend.name, 'file_sha256': backend.file_sha256}
    q_hat = None
    if config.get('calibration_file'):
        try:
            if not isinstance(backend, MockBackend):
                if not backend.file_sha256 or not config.get('runner_version') or not config.get('model_manifest_file'):
                    raise ValueError('Missing model or runner provenance')
                backend.load_manifest(local_path(config['model_manifest_file']))
            q_hat = load_threshold(config['calibration_file'], signature(roster, config, backend, language))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return finish('uncalibrated')
    deadline = started + config['timeout_s']
    forms = []
    # Serialize the complete sampling operation, including concurrent callers. Queue time
    # belongs to the end-to-end deadline; STOP handling must run in another console worker.
    acquired = _MODEL_LOCK.acquire(timeout=max(0., deadline-time.monotonic()))
    if not acquired:
        return finish('model_timeout')
    try:
        if q_hat is not None and isinstance(backend, OllamaBackend):
            try:
                backend.verify_identity(config, max(.001, deadline-time.monotonic()))
            except BackendTimeout:
                return finish('model_timeout')
            except BackendError:
                return finish('model_unavailable')
        for index in range(config['samples']):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return finish('model_timeout')
            try:
                form = backend.sample(text_nfc, roster, language, timeout=remaining, index=index, settings=config)
                result['samples'] += 1
                validate_task_form(form)
                if time.monotonic() > deadline:
                    return finish('model_timeout')
                if form['out_of_scope']:
                    return finish('out_of_scope')
                for step in form['steps']:
                    if any(step[s] is not None and not source_span(step[s], text_nfc) for s in SLOTS):
                        return finish('model_output_invalid')
                forms.append(form)
            except BackendTimeout:
                return finish('model_timeout')
            except BackendError:
                return finish('model_unavailable')
            except (ValueError, TypeError, KeyError):
                return finish('model_output_invalid')
        if q_hat is not None and isinstance(backend, OllamaBackend):
            try:
                backend.verify_identity(config, max(.001, deadline-time.monotonic()))
            except BackendTimeout:
                return finish('model_timeout')
            except BackendError:
                return finish('model_unavailable')
            if time.monotonic() > deadline:
                return finish('model_timeout')
    finally:
        _MODEL_LOCK.release()
    # Never align different actions or lengths as if they were the same plan.
    shapes = {tuple(step['action'] for step in f['steps']) for f in forms}
    if len(shapes) != 1:
        return finish('inconsistent_plan')
    if language is None:
        result['language'] = Counter(f['language'] for f in forms).most_common(1)[0][0]
    uncertain = []
    for index, action in enumerate(next(iter(shapes))):
        slots = {}
        resolved = {}
        for slot in SLOTS:
            values = [f['steps'][index][slot] for f in forms]
            if not any(value is not None for value in values):
                continue
            said = Counter(values).most_common(1)[0][0]
            if slot == 'item':
                if len({normalize(v) if v is not None else None for v in values}) != 1:
                    return finish('inconsistent_plan')
                slots[slot] = {'said': said}
                continue
            votes = defaultdict(float)
            # Include ALL eligible IDs and <none>, even with zero votes: at q_hat=1
            # the finite-sample conformal set must contain zero-vote true labels too.
            groups = {'place': ('rooms', 'seats'), 'person': ('people',), 'object': ('objects',)}[slot]
            for group in groups:
                for key in roster.groups[group]:
                    votes[key] = 0.
            votes[NONE] = 0.
            for value in values:
                matches = roster.match(slot, value)
                for key in matches:
                    votes[key] += 1/(len(matches)*len(forms))
            candidates = dict(sorted(votes.items()))
            options = prediction_set(candidates, q_hat) if q_hat is not None else []
            slots[slot] = {'said': said, 'candidates': candidates, 'prediction_set': options}
            if all(roster.match(slot, value) == (NONE,) for value in values):
                return finish('unknown_name')
            if q_hat is not None:
                if not options or NONE in options:
                    return finish('unknown_name')
                if len(options) == 1:
                    resolved[slot] = options[0]
                else:
                    uncertain.append((max(candidates.values()), index, slot, options))
        result['steps'].append({'action': action, 'slots': slots})
        if resolved:
            rooms = {roster.room(slot, key) for slot, key in resolved.items()}
            # Multiple object/person targets are never implicitly interchangeable, even
            # in one room. An explicit room plus one entity is allowed when consistent.
            entities = {key for slot, key in resolved.items() if slot != 'place' or key in roster.groups['seats']}
            if len(rooms) > 1 or len(entities) > 1:
                return finish('conflicting_targets')
    if q_hat is None:
        return finish('uncalibrated')
    if uncertain:
        _, _, _, options = min(uncertain, key=lambda row: (row[0], row[1], row[2]))
        result['decision'] = 'ask'
        result['question'] = question(options, result['language'])
    else:
        result['decision'] = 'act'
    return finish()
