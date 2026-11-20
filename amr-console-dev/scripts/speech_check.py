#!/usr/bin/env python3
"""Measure speech input on the owner's own recordings: word and character error, and time.

Put 16 kHz mono WAV clips in one folder with a clips.csv (columns: file, language, reference),
for example the recordings kept by speech.keep_audio: true. Then, from this repository:

    ../amr-speech/venv/bin/python -I scripts/speech_check.py --clips <folder> [--model indic_conformer]

clips.csv columns: file, language, reference, and optionally group (a separate summary line).
Writes <folder>/speech_check.json (with --office-names: speech_check_office_names.json) and prints
one line per language. Uses the same models and
settings as the console (config/office_tasks.yaml); nothing is downloaded.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import statistics
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools/amr_console'))


def words(text):
    text = unicodedata.normalize('NFC', text.lower())
    kept = ''.join(ch if ch.isalnum() or unicodedata.category(ch).startswith('M') or ch.isspace() else ' ' for ch in text)
    return kept.split()


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def office_prompt():
    """The office's names as the console gives them to the English model.

    The speech environment has no ROS or imaging packages, so building the office context can fail here;
    the console then already wrote the same words into each language service's speech_spec.json.
    """
    try:
        import office_context
        import office_speech
        return office_speech.prompt_words(office_context.OfficeContext(ROOT))
    except ImportError:
        specs = sorted((ROOT / 'data/logs/office').glob('*/speech_spec.json'))
        for spec in reversed(specs):
            words = json.loads(spec.read_text(encoding='utf-8')).get('prompt_words')
            if words:
                return words
        return ''


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--clips', required=True, type=Path)
    parser.add_argument('--model', help='Use this model key for every clip instead of the per-language setting.')
    parser.add_argument('--office-names', action='store_true',
                        help="Give the English model the office's names, as the console does on the page.")
    args = parser.parse_args()
    import yaml
    config = yaml.safe_load((ROOT / 'config/office_tasks.yaml').read_text(encoding='utf-8'))
    speech = config['speech']
    base = (ROOT / speech['path']).resolve()
    models = json.loads((ROOT / speech.get('models', 'config/speech_models.json')).read_text())['models']
    os.environ.update(HF_HOME=str(base / 'hf'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    import speech_worker
    rows = list(csv.DictReader((args.clips / 'clips.csv').open(encoding='utf-8')))
    prompt = ''
    if args.office_names:
        prompt = office_prompt()
        print('names given to the English model:', prompt or '(none found)')
    if not rows:
        sys.exit('clips.csv has no rows.')
    engines, results = {}, []
    for row in rows:
        language = row['language'].strip()
        key = args.model or speech['languages'][language]
        if key not in engines:
            started = time.monotonic()
            engines[key] = speech_worker.ENGINES[models[key]['engine']](str(base), models[key],
                                                                         {'threads': speech.get('threads', 4), 'prompt_words': prompt})
            print(f'{key}: loaded in {time.monotonic() - started:.1f} s')
        # The same preparation as the console: centre the recording and raise a quiet one.
        audio = speech_worker.louder(speech_worker.read_audio(args.clips / row['file']))
        started = time.monotonic()
        heard = engines[key].transcribe(audio, language)
        seconds = time.monotonic() - started
        ref, hyp = row['reference'].strip(), heard
        rw, hw = words(ref), words(hyp)
        rc, hc = list(''.join(rw)), list(''.join(hw))
        results.append({'file': row['file'], 'language': language, 'group': (row.get('group') or language).strip(),
                        'model': key, 'reference': ref, 'heard': hyp,
                        'word_errors': distance(rw, hw), 'words': len(rw), 'char_errors': distance(rc, hc),
                        'chars': len(rc), 'audio_s': round(len(audio) / 16000, 2), 'latency_s': round(seconds, 3)})
    summary = {}
    # An optional 'group' column in clips.csv (for example Tanglish inside Tamil) is summarised on its own.
    for language in sorted({r['group'] for r in results}):
        part = [r for r in results if r['group'] == language]
        latency = sorted(r['latency_s'] for r in part)
        summary[language] = {
            'clips': len(part),
            'word_error_rate': round(sum(r['word_errors'] for r in part) / max(1, sum(r['words'] for r in part)), 3),
            'char_error_rate': round(sum(r['char_errors'] for r in part) / max(1, sum(r['chars'] for r in part)), 3),
            'latency_median_s': round(statistics.median(latency), 3),
            'latency_p95_s': round(latency[min(len(latency) - 1, int(.95 * len(latency)))], 3)}
        s = summary[language]
        print(f"{language}: {s['clips']} clips · word errors {s['word_error_rate']:.1%} · character errors "
              f"{s['char_error_rate']:.1%} · time median {s['latency_median_s']} s, p95 {s['latency_p95_s']} s")
    name = 'speech_check_office_names.json' if args.office_names else 'speech_check.json'
    (args.clips / name).write_text(json.dumps({'summary': summary, 'office_names': prompt, 'clips': results},
                                                             indent=1, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
