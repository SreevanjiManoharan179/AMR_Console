#!/usr/bin/env python3
"""AMR Speech Lab: a small test console for speech and language, apart from the robot console.

It starts the same language service and speech models as the console's Office tasks tab (without the
robot) and serves one page on 127.0.0.1 where you speak or type a request and see:

  * the microphone level while you speak (loudness, clipping, noise floor);
  * the words as the speech model writes them down, updated about once a second;
  * the final words against a reference sentence (word and character errors, key words found);
  * what the language model made of it (English rewrite, decision, steps, read-back, time taken).

Every take is logged in data/evaluation/speech_lab/<time>/ (takes.jsonl, understand.jsonl and, if
"keep audio" is ticked, the recordings), so models and settings can be compared over time.

Close the AMR Console first: both start the same language model.

    python3 tools/speech_lab/server.py            # prints the page's address and opens it
"""
import argparse
import array
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import secrets
import statistics
import sys
import threading
import time
from types import SimpleNamespace
import unicodedata
from urllib.parse import parse_qs, urlparse
import wave
import webbrowser

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / 'tools/amr_console'))

PAGE = Path(__file__).resolve().parent / 'web/index.html'
SENTENCES = ROOT / 'config/speech_test_sentences.tsv'
ACTIVE = ('submitted', 'queued', 'understanding', 'awaiting_confirmation', 'running')
MAX_BODY = 1_100_000
GROUPS = {'en': 'en', 'ta': 'ta', 'tanglish': 'ta', 'ml': 'ml', 'te': 'te'}   # group -> speech model language


class Backend:
    """What OfficeTasks needs from the console when no robot session runs."""
    def __init__(self, root):
        self.root = Path(root)
        self.state, self.session, self.stopping = 'idle', None, False
        self.events = SimpleNamespace(publish=lambda event: None)
        self.office_cancel_generation = 0

    def office_cancel_goal(self):
        self.office_cancel_generation += 1
        return True

    def office_navigate(self, pose, **kw):
        raise RuntimeError('The Speech Lab runs without the robot.')


# -- measuring a recording ------------------------------------------------------------------

def decibels(value):
    return round(20 * math.log10(value), 1) if value > 1e-6 else -120.0


def signal(raw):
    """Level figures of a 16 kHz mono 16-bit WAV: peak, loudness, clipping, noise floor and SNR."""
    with wave.open(io.BytesIO(raw), 'rb') as stream:
        samples = array.array('h')
        samples.frombytes(stream.readframes(stream.getnframes()))
    if sys.byteorder != 'little':
        samples.byteswap()
    count = len(samples) or 1
    mean = sum(samples) / count
    peak = max((abs(v) for v in samples), default=0) / 32768
    clipped = sum(1 for v in samples if v >= 32440 or v <= -32440) / count
    frame, levels = 800, []                                   # 50 ms frames, offset removed
    for start in range(0, len(samples) - frame + 1, frame):
        part = samples[start:start + frame]
        levels.append(math.sqrt(sum((v - mean) ** 2 for v in part) / frame) / 32768)
    levels.sort()
    noise = levels[len(levels) // 10] if levels else 0.0
    speech = levels[(len(levels) * 9) // 10] if levels else 0.0
    rms = math.sqrt(sum((v - mean) ** 2 for v in samples) / count) / 32768
    return {'peak_db': decibels(peak), 'rms_db': decibels(rms), 'clipped_pct': round(clipped * 100, 2),
            'offset': round(mean / 32768, 4), 'noise_db': decibels(noise), 'speech_db': decibels(speech),
            'snr_db': round(decibels(speech) - decibels(noise), 1) if noise > 1e-6 and speech > 1e-6 else None}


# -- scoring against a reference ---------------------------------------------------------------

def normal(text):
    text = unicodedata.normalize('NFC', str(text)).casefold()
    return ' '.join(''.join(' ' if unicodedata.category(c)[0] in 'PS' else c for c in text).split())


def align(reference, heard):
    """Word alignment: list of (kind, reference word, heard word), kind = ok, sub, del or ins."""
    r, h = reference, heard
    cost = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        cost[i][0] = i
    for j in range(len(h) + 1):
        cost[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost[i][j] = min(cost[i - 1][j] + 1, cost[i][j - 1] + 1, cost[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    out, i, j = [], len(r), len(h)
    while i or j:
        if i and j and cost[i][j] == cost[i - 1][j - 1] + (r[i - 1] != h[j - 1]):
            out.append(('ok' if r[i - 1] == h[j - 1] else 'sub', r[i - 1], h[j - 1]))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            out.append(('del', r[i - 1], ''))
            i -= 1
        else:
            out.append(('ins', '', h[j - 1]))
            j -= 1
    return out[::-1]


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        last, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            last, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, last + (x != y))
    return row[-1]


def keyword_found(keyword, heard):
    """Whether a key word starts a word of the heard text (both already normalised).

    The word may go on, so suffixes count (திவ்யா in திவ்யாவிடம், file in files), but the key word
    must start a word ("ready" is not found in "already") and a number must not go on with
    another digit ("3" is found in "3 pm" and "3pm", not in "13" or "30")."""
    text = ' ' + heard
    start = text.find(' ' + keyword)
    while start >= 0:
        end = start + 1 + len(keyword)
        if not (keyword[-1].isdigit() and end < len(text) and text[end].isdigit()):
            return True
        start = text.find(' ' + keyword, start + 1)
    return False


def score(reference, heard, keywords):
    """Word errors (substitutions, deletions and insertions over the reference words), character
    errors (edit distance over the characters without spaces, as code points) and key words found.

    keywords: groups separated by ';', spellings of one key word separated by '|'; a group is
    found when any of its spellings is (keyword_found)."""
    ref, hyp = normal(reference), normal(heard)
    words = align(ref.split(), hyp.split())
    wrong = sum(1 for kind, _, _ in words if kind != 'ok')
    total = len(ref.split()) or 1
    groups = [[normal(a) for a in group.split('|') if normal(a)] for group in keywords.split(';')]
    groups = [group for group in groups if group]
    found = [any(keyword_found(a, hyp) for a in group) for group in groups]
    return {'words': total, 'word_errors': wrong, 'wer': round(wrong / total, 3),
            'cer': round(distance(ref.replace(' ', ''), hyp.replace(' ', '')) / (len(ref.replace(' ', '')) or 1), 3),
            'diff': words, 'keywords': [{'word': g[0], 'found': f} for g, f in zip(groups, found)],
            'keywords_found': sum(found), 'keywords_total': len(groups)}


def load_sentences():
    rows = []
    if SENTENCES.exists():
        for line in SENTENCES.read_text(encoding='utf-8').splitlines():
            parts = line.split('\t')
            if len(parts) >= 3 and not line.startswith('#'):
                key, language, text = parts[:3]
                rows.append({'key': key, 'group': key.rsplit('_', 1)[0], 'language': language, 'text': text,
                             'say': parts[3] if len(parts) > 3 else text, 'keywords': parts[4] if len(parts) > 4 else ''})
    return rows


# -- the lab ------------------------------------------------------------------------------------

class Lab:
    def __init__(self, test=False):
        from office_tasks import OfficeTasks
        if test:      # stand-in model and speech engine: for checking the page itself
            os.environ['AMR_CONSOLE_TEST_LANGUAGE'] = os.environ['AMR_CONSOLE_TEST_SPEECH'] = '1'
            self.office = OfficeTasks(Backend(ROOT), test_backend='echo', test_speech=sys.executable)
        else:
            self.office = OfficeTasks(Backend(ROOT))
        # The lab keeps all four languages and the plain question-and-answer flow, whatever the
        # console's own switches say (config/office_tasks.yaml: assistance, continued_operation).
        self.office.config_overrides = {
            'assistance': {'english_only': False, 'speak_to_everyone_in': None, 'conversational': False,
                           'search_for_person': False, 'model_reads_replies': False},
            'continued_operation': {'closing_question': False}}
        self.sentences = load_sentences()
        self.session = ROOT / 'data/evaluation/speech_lab' / time.strftime('%Y%m%d_%H%M%S')
        self.takes, self.lock, self.understanding = [], threading.Lock(), threading.Lock()
        self.start_error = None
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        try:
            self.office.start('rehearsal')
        except Exception as exc:      # shown on the page
            self.start_error = str(exc)

    def state(self):
        snap = self.office.snapshot()
        service = dict(snap['service'])
        if self.start_error:
            service = {'state': 'error', 'detail': self.start_error}
        return {'service': service, 'speech': snap['speech'], 'sentences': self.sentences,
                'session': str(self.session.relative_to(ROOT)), 'takes': self.takes[-60:], 'summary': self.summary(),
                'context': {key: (snap.get('context') or {}).get(key) for key in ('model', 'runner', 'language_module')}}

    def summary(self):
        out = {}
        for take in self.takes:
            out.setdefault(take['group'], []).append(take)
        rows = []
        for group, takes in out.items():
            scored = [t for t in takes if t.get('wer') is not None]
            keys = [t for t in scored if t.get('keywords_total')]
            rows.append({'group': group, 'takes': len(takes),
                         'wer': round(sum(t['word_errors'] for t in scored) / max(1, sum(t['words'] for t in scored)), 3) if scored else None,
                         'cer': round(statistics.mean(t['cer'] for t in scored), 3) if scored else None,
                         'keywords': round(sum(t['keywords_found'] for t in keys) / sum(t['keywords_total'] for t in keys), 3) if keys else None,
                         'latency_ms': round(statistics.median(t['latency_ms'] for t in takes if t.get('latency_ms') is not None))
                         if any(t.get('latency_ms') is not None for t in takes) else None,
                         'snr_db': round(statistics.median(t['snr_db'] for t in takes if t.get('snr_db') is not None), 1)
                         if any(t.get('snr_db') is not None for t in takes) else None})
        return rows

    def partial(self, raw, group):
        return self.office.transcribe(raw, GROUPS[group], GROUPS[group])

    def final(self, raw, group, ref_key, keep, client):
        started = time.monotonic()
        result = self.office.transcribe(raw, GROUPS[group], GROUPS[group])
        roundtrip = (time.monotonic() - started) * 1000
        sentence = next((s for s in self.sentences if s['key'] == ref_key), None)
        with self.lock:
            number = len(self.takes) + 1
            take = {'take': number, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'group': group, 'language': GROUPS[group],
                    'reference_key': ref_key or None, 'reference': sentence['text'] if sentence else None,
                    'heard': result.get('text', ''), 'model': result.get('model'), 'seconds': result.get('seconds'),
                    'latency_ms': result.get('latency_ms'), 'roundtrip_ms': round(roundtrip),
                    'realtime_factor': round((result.get('latency_ms') or 0) / 1000 / result['seconds'], 3) if result.get('seconds') else None,
                    'silent': result.get('silent'), 'distorted': result.get('distorted'), **signal(raw),
                    'first_words_ms': client.get('first_words_ms'), 'partials': client.get('partials'),
                    'browser_filters': client.get('filters'), 'audio': None}
            if sentence:
                take.update(score(sentence['text'], take['heard'], sentence['keywords']))
            self.session.mkdir(parents=True, exist_ok=True)
            if keep:
                (self.session / 'audio').mkdir(exist_ok=True)
                take['audio'] = f"audio/take_{number:03d}_{group}.wav"
                (self.session / take['audio']).write_bytes(raw)
            self.takes.append(take)
            with (self.session / 'takes.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(take, ensure_ascii=False) + '\n')
        return take

    def understand(self, text, language, take_number=None):
        """Send the words to the language model, report what it decided, and never carry the job out."""
        if not self.understanding.acquire(timeout=1):
            raise ValueError('Another request is being understood. Try again in a moment.')
        try:
            started = time.monotonic()
            job_id = self.office.submit(text, 'Bharathwaj', language or 'auto', None)['job_id']
            job, decided_at = {}, None
            while time.monotonic() - started < 120:
                snap = self.office.snapshot()
                job = next((j for j in snap['jobs'] if j['job_id'] == job_id), {})
                if job.get('decision') and decided_at is None:
                    decided_at = time.monotonic()
                waiting = snap['awaiting']
                if waiting and waiting.get('job_id') == job_id:
                    # The lab only looks at the understanding: the read-back is answered "no", a question "stop".
                    self.office.reply(waiting['input_id'], 'no' if 'no' in (waiting.get('expect') or []) else 'stop')
                elif job.get('state') not in ACTIVE and not snap['busy']:
                    break
                time.sleep(.08)
            else:
                self.office.cancel()
            lines = [l for l in self.office.snapshot()['timeline'] if l.get('job_id') == job_id and l.get('kind') == 'robot']
            result = {'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'take': take_number, 'text': text, 'language_sent': language or 'auto',
                      'english': job.get('english'), 'decision': job.get('decision'), 'planner': job.get('planner'),
                      'steps': [{'action': s.get('action'), 'to': s.get('to'), 'from': s.get('from'), 'at': s.get('at'),
                                 'item': s.get('item'), 'question': s.get('question'), 'message': s.get('message'),
                                 'if_not_there': s.get('if_not_there')} for s in job.get('steps') or []],
                      'ask': job.get('ask'), 'refuse': job.get('refuse'), 'language_detected': job.get('language_detected'),
                      'readback': job.get('readback') or next((l.get('text') for l in lines), None),
                      'model_ms': round(job['latency_ms']) if job.get('latency_ms') is not None else None,
                      'decided_ms': round((decided_at - started) * 1000) if decided_at else None}
            self.session.mkdir(parents=True, exist_ok=True)
            with (self.session / 'understand.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
            return result
        finally:
            self.understanding.release()

    def export(self):
        fields = ['take', 'at', 'group', 'language', 'reference_key', 'reference', 'heard', 'words', 'word_errors', 'wer', 'cer',
                  'keywords_found', 'keywords_total', 'seconds', 'latency_ms', 'roundtrip_ms', 'realtime_factor', 'first_words_ms',
                  'partials', 'peak_db', 'rms_db', 'noise_db', 'speech_db', 'snr_db', 'clipped_pct', 'offset', 'silent',
                  'distorted', 'model', 'audio']
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(self.takes)
        return out.getvalue()


def handler(lab, token, port):
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def send(self, status, body, kind='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def allowed(self):
            if self.headers.get('Host') not in hosts:
                self.send(403, {'error': 'Open the Speech Lab on this computer.'})
                return False
            if self.path.startswith('/api/') and not secrets.compare_digest(self.headers.get('X-Lab-Token', ''), token):
                self.send(401, {'error': 'Open the Speech Lab with the address printed in its terminal.'})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlparse(self.path).path
            if path == '/':
                self.send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
            elif path == '/api/state':
                self.send(200, lab.state())
            elif path == '/api/export.csv':
                self.send(200, lab.export(), 'text/csv; charset=utf-8')
            else:
                self.send(404, {'error': 'Not found.'})

        def do_POST(self):
            if not self.allowed():
                return
            url = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.send(413, {'error': 'The request is too large.'})
                return
            body = self.rfile.read(length)
            try:
                if url.path in ('/api/partial', '/api/final'):
                    group = query.get('group', 'en')
                    if group not in GROUPS:
                        raise ValueError('Choose English, Tamil or Tanglish.')
                    if url.path == '/api/partial':
                        self.send(200, lab.partial(body, group))
                    else:
                        client = {'first_words_ms': int(query['first']) if query.get('first', '').isdigit() else None,
                                  'partials': int(query['partials']) if query.get('partials', '').isdigit() else None,
                                  'filters': query.get('filters') == '1'}
                        self.send(200, lab.final(body, group, query.get('ref', ''), query.get('keep') == '1', client))
                elif url.path == '/api/understand':
                    data = json.loads(body.decode('utf-8'))
                    self.send(200, lab.understand(str(data.get('text', '')), data.get('language'), data.get('take')))
                else:
                    self.send(404, {'error': 'Not found.'})
            except Exception as exc:      # ConsoleError carries a reason for the person
                self.send(getattr(exc, 'status', 400) if isinstance(getattr(exc, 'status', None), int) else 400,
                          {'error': getattr(exc, 'reason', None) or str(exc), 'code': getattr(exc, 'code', None)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stand-in', action='store_true', help='stand-in model and speech engine (checks the page only)')
    args = parser.parse_args()
    token = secrets.token_urlsafe(18)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), None)
    except OSError as exc:
        sys.exit(f'Port {args.port} is in use ({exc}). Is the Speech Lab already open?')
    lab = Lab(test=args.stand_in)
    server.RequestHandlerClass = handler(lab, token, args.port)
    address = f'http://127.0.0.1:{args.port}/#{token}'
    print('AMR Speech Lab:', address, flush=True)
    print('Starting the language service and the speech models (about 20 s). Ctrl+C stops the lab.', flush=True)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [address]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        lab.office.shutdown()
        server.server_close()
        print('Speech Lab stopped. Takes are in', lab.session if lab.takes else '(none recorded)')


if __name__ == '__main__':
    main()
