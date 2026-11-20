#!/usr/bin/env python3
"""Fetch a few native-speaker test sentences from Google FLEURS for the speech check.

FLEURS (CC BY 4.0) has read speech with transcripts in 102 languages, including Tamil, Malayalam and
Telugu. This takes the first --count test sentences per language (one speaker each, at most 20 s),
so the speech models can be checked on native speakers where the owner cannot read the language.
Only the start of each archive is downloaded; the download stops once enough clips are saved.

    ../amr-speech/venv/bin/python -I scripts/speech_fleurs.py --count 15
    ../amr-speech/venv/bin/python -I scripts/speech_check.py --clips data/evaluation/speech_fleurs

Writes data/evaluation/speech_fleurs/: <language>_NN.wav (16 kHz mono), clips.csv and manifest.json
(dataset revision, source file of every clip, licence). The sentences are general reading, not
office requests: they measure the language model of speech, not the robot's vocabulary.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import struct
import tarfile
import time
import urllib.request
import wave

ROOT = Path(__file__).resolve().parent.parent
DATASET = 'google/fleurs'
LANGUAGES = {'en': 'en_us', 'ta': 'ta_in', 'ml': 'ml_in', 'te': 'te_in'}
MAXIMUM_SECONDS = 20.0


def fetch(url, timeout=60):
    request = urllib.request.Request(url, headers={'User-Agent': 'amr-console-speech-check'})
    return urllib.request.urlopen(request, timeout=timeout)


def as_16k_mono(raw):
    """WAV bytes as 16 kHz mono 16-bit frames.

    FLEURS stores 32-bit float WAV (format 3), which Python's wave module does not read, so the RIFF
    chunks are read here: PCM (1), float (3) and their "extensible" form, any rate and channel count.
    """
    import numpy
    if raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        raise ValueError('not a WAV file')
    at, kind, channels, rate, bits, data = 12, None, 1, 16000, 16, b''
    while at + 8 <= len(raw):
        name, size = raw[at:at + 4], struct.unpack('<I', raw[at + 4:at + 8])[0]
        body = raw[at + 8:at + 8 + size]
        if name == b'fmt ':
            kind, channels, rate, _, _, bits = struct.unpack('<HHIIHH', body[:16])
            if kind == 0xFFFE and len(body) >= 26:
                kind = struct.unpack('<H', body[24:26])[0]
        elif name == b'data':
            data = body
        at += 8 + size + (size & 1)
    if kind == 3:
        audio = numpy.frombuffer(data, dtype='<f4' if bits == 32 else '<f8').astype(numpy.float64)
    elif kind == 1 and bits in (8, 16, 32):
        audio = numpy.frombuffer(data, dtype={8: numpy.uint8, 16: '<i2', 32: '<i4'}[bits]).astype(numpy.float64)
        audio = (audio - 128) / 128 if bits == 8 else audio / float(2 ** (bits - 1))
    else:
        raise ValueError(f'unsupported WAV format {kind}, {bits} bits')
    audio = audio[:len(audio) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        times = numpy.arange(0, len(audio) / rate, 1 / 16000)
        audio = numpy.interp(times, numpy.arange(len(audio)) / rate, audio)
    return (numpy.clip(audio, -1, 1) * 32767).astype('<i2').tobytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--count', type=int, default=15, help='sentences per language (default 15)')
    parser.add_argument('--languages', default='en,ta,ml,te')
    parser.add_argument('--out', type=Path, default=ROOT / 'data/evaluation/speech_fleurs')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    revision = json.load(fetch(f'https://huggingface.co/api/datasets/{DATASET}'))['sha']
    base = f'https://huggingface.co/datasets/{DATASET}/resolve/{revision}/data'
    print(f'{DATASET} at revision {revision[:12]}')
    rows, manifest = [], {'dataset': DATASET, 'revision': revision, 'licence': 'CC BY 4.0', 'split': 'test',
                          'fetched': time.strftime('%Y-%m-%dT%H:%M:%S'), 'clips': []}
    for language in [code.strip() for code in args.languages.split(',') if code.strip()]:
        name = LANGUAGES[language]
        transcripts = {}
        for line in fetch(f'{base}/{name}/test.tsv').read().decode('utf-8').splitlines():
            parts = line.split('\t')
            if len(parts) >= 7:   # id, file, raw text, normalised text, characters, samples, gender
                transcripts[parts[1]] = (parts[0], parts[3], parts[6])
        saved, sentences = 0, set()
        response = fetch(f'{base}/{name}/audio/test.tar.gz', timeout=120)
        with tarfile.open(fileobj=response, mode='r|gz') as archive:
            for member in archive:
                if saved >= args.count:
                    break
                source = Path(member.name).name
                if not member.isfile() or source not in transcripts:
                    continue
                sentence, text, gender = transcripts[source]
                if sentence in sentences:
                    continue          # one speaker per sentence, so each clip is a different sentence
                frames = as_16k_mono(archive.extractfile(member).read())
                if len(frames) / 32000 > MAXIMUM_SECONDS:
                    continue
                sentences.add(sentence)
                saved += 1
                target = f'{language}_{saved:02d}.wav'
                with wave.open(str(args.out / target), 'wb') as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(16000)
                    stream.writeframes(frames)
                rows.append({'file': target, 'language': language, 'reference': text})
                manifest['clips'].append({'file': target, 'language': language, 'source': f'{name}/test/{source}',
                                          'sentence_id': sentence, 'gender': gender,
                                          'seconds': round(len(frames) / 32000, 2)})
        response.close()
        print(f'{language}: {saved} clips')
    with (args.out / 'clips.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['file', 'language', 'reference'])
        writer.writeheader()
        writer.writerows(rows)
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Saved {len(rows)} clips in {args.out}. Next:')
    print('  ../amr-speech/venv/bin/python -I scripts/speech_check.py --clips data/evaluation/speech_fleurs')


if __name__ == '__main__':
    main()
