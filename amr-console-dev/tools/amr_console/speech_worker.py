#!/usr/bin/env python3
"""Speech worker for office tasks: writes down short recordings, offline, on this computer.

Runs in the speech environment (../amr-speech/venv) started by the console with -I. Models come
only from the approved local Hugging Face folder at their pinned revisions (HF_HUB_OFFLINE=1).
Protocol, one JSON object per line:
  in : {"type": "transcribe", "request_id": "...", "path": "<wav in the run folder>", "language": "ta"}
       {"type": "shutdown"}
  out: {"type": "status", "language": "ta", "state": "loading|ready|error", "detail": "..."}
       {"type": "transcript", "request_id": "...", "text": "...", "language": "ta", "model": "...", "latency_ms": 812}
       {"type": "error", "request_id": "...", "code": "...", "reason": "..."}
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from types import ModuleType
import wave

OUT = threading.Lock()


def emit(event):
    with OUT:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def read_audio(path):
    """16 kHz mono 16-bit WAV to float32 samples in [-1, 1]."""
    with wave.open(str(path), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16000):
            raise ValueError('expected 16 kHz mono 16-bit WAV')
        data = stream.readframes(stream.getnframes())
    try:
        import numpy
    except ImportError:  # only the test stand-in runs without the speech environment
        import array
        samples = array.array('h')
        samples.frombytes(data)
        if sys.byteorder != 'little':
            samples.byteswap()
        return [value / 32768.0 for value in samples]
    return numpy.frombuffer(data, dtype='<i2').astype('float32') / 32768.0


def louder(audio):
    """Centre the recording, then raise it if quiet but real (peak -50 to -12 dB) so speech detection hears it.

    Laptop microphones often record far below full scale; the gain is at most 30x, to a peak of -6 dB.
    """
    try:
        import numpy
    except ImportError:  # only the test stand-in runs without the speech environment
        return audio
    if not len(audio):
        return audio
    audio = (audio - float(numpy.mean(audio))).astype('float32')   # remove the input's centre offset
    peak = float(numpy.max(numpy.abs(audio)))
    if 0.003 < peak < 0.25:
        return (audio * min(30.0, 0.5 / peak)).astype('float32')
    return audio


def snapshot(base, entry):
    path = Path(base) / 'hf' / 'hub' / ('models--' + entry['repo'].replace('/', '--')) / 'snapshots' / entry['revision']
    if not path.is_dir():
        raise FileNotFoundError(f"{entry['repo']} at revision {entry['revision'][:12]} is not installed")
    return path


class FasterWhisper:
    """English: OpenAI Whisper weights converted for CTranslate2 (faster-whisper)."""

    def __init__(self, base, entry, spec):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(str(snapshot(base, entry)), device=entry.get('device', 'cpu'),
                                  compute_type=entry.get('compute_type', 'int8'),
                                  cpu_threads=int(spec.get('threads', 4)), local_files_only=True)
        words = str(spec.get('prompt_words') or '')[:600]
        # A short prompt with the office's names helps spelling; it is not an instruction.
        self.prompt = ('Office robot request. Names: ' + words + '.') if words else None

    def transcribe(self, audio, language):
        segments, _info = self.model.transcribe(audio, language=language, beam_size=5, vad_filter=True,
                                                condition_on_previous_text=False, initial_prompt=self.prompt,
                                                without_timestamps=True)
        return ' '.join(segment.text.strip() for segment in segments).strip()


class IndicConformer:
    """Tamil, Malayalam, Telugu: AI4Bharat IndicConformer 600M multilingual (ONNX, processor)."""

    def __init__(self, base, entry, spec):
        folder = snapshot(base, entry)
        code = folder / entry.get('remote_code', 'model_onnx.py')
        reviewed = entry.get('reviewed_code_sha256')
        # The model ships its own Python loader. It runs only after a person has read that exact
        # file and recorded its SHA-256 in config/speech_models.json.
        if not reviewed:
            raise PermissionError("the model's own loader code has not been reviewed yet "
                                  '(reviewed_code_sha256 is empty in config/speech_models.json)')
        loader_source = code.read_bytes()
        actual = hashlib.sha256(loader_source).hexdigest()
        if actual != reviewed:
            raise PermissionError(f'the model loader code changed (SHA-256 {actual[:12]}…); review it again')
        # The upstream entry point calls snapshot_download and can select CUDA.
        # This worker uses the reviewed bytes, local assets and the CPU.
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
        import torch
        torch.set_num_threads(int(spec.get('threads', 4)))
        # The loader prints notes while it loads; stdout carries this worker's events, so they go to
        # the log instead. Its from_pretrained() would download again: it is never called.
        with contextlib.redirect_stdout(sys.stderr):
            loader = ModuleType('indic_conformer_loader')
            exec(compile(loader_source, str(code), 'exec'), loader.__dict__)
            model_config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
            model_config['ts_folder'] = str(folder)
            self.model = loader.IndicASRModel(loader.IndicASRConfig.from_dict(model_config))
        self.torch = torch
        self.decoding = entry.get('decoding', 'rnnt')

    def transcribe(self, audio, language):
        wav = self.torch.from_numpy(audio.copy()).unsqueeze(0)
        with self.torch.inference_mode():
            text = self.model(wav, language, self.decoding)
        if isinstance(text, (list, tuple)):
            text = text[0] if text else ''
        return str(text).strip()


class StandIn:
    """Tests only (AMR_CONSOLE_TEST_SPEECH=1 and test_engine in the spec): no model, fixed words."""

    WORDS = {'en': 'give the file to divya', 'ta': 'ஃபைலை திவ்யாவிடம் கொடுங்கள்',
             'ml': 'ഫയൽ ദിവ്യയ്ക്ക് കൊടുക്കൂ', 'te': 'ఫైల్ దివ్యకు ఇవ్వండి'}

    def __init__(self, base, entry, spec):
        time.sleep(float(spec.get('test_load_s', 0)))

    def transcribe(self, audio, language):
        if not len(audio):
            return ''
        return self.WORDS.get(language, '')


ENGINES = {'faster_whisper': FasterWhisper, 'indic_conformer': IndicConformer}


class Worker:
    def __init__(self, spec):
        self.spec = spec
        self.base = spec['base']
        self.log_dir = Path(spec['log_dir']).resolve()
        self.test = bool(spec.get('test_engine')) and os.environ.get('AMR_CONSOLE_TEST_SPEECH') == '1'
        self.engines = {}         # model key -> engine
        self.errors = {}          # model key -> reason
        self.loaded = {}          # model key -> threading.Event
        self.run_lock = threading.Lock()

    def load_all(self):
        by_model = {}
        for language, key in self.spec['languages'].items():
            by_model.setdefault(key, []).append(language)
            self.loaded.setdefault(key, threading.Event())
        for language in self.spec['languages']:
            emit({'type': 'status', 'language': language, 'state': 'loading'})
        for key, languages in by_model.items():
            entry = self.spec['models'][key]
            started = time.monotonic()
            try:
                engine = StandIn if self.test else ENGINES[entry['engine']]
                self.engines[key] = engine(self.base, entry, self.spec)
                for language in languages:
                    emit({'type': 'status', 'language': language, 'state': 'ready', 'model': key,
                          'load_s': round(time.monotonic() - started, 2)})
            except Exception as exc:  # report and keep the other languages working
                self.errors[key] = f'{type(exc).__name__}: {exc}'
                sys.stderr.write(traceback.format_exc())
                for language in languages:
                    emit({'type': 'status', 'language': language, 'state': 'error', 'model': key,
                          'detail': str(exc)[:300]})
            finally:
                self.loaded[key].set()

    def transcribe(self, request):
        request_id = request.get('request_id')
        language = request.get('language')
        key = self.spec['languages'].get(language)
        if key is None:
            return {'type': 'error', 'request_id': request_id, 'code': 'speech_language',
                    'reason': f'No speech model is set up for {language}.'}
        path = Path(str(request.get('path', ''))).resolve()
        if not path.is_relative_to(self.log_dir) or path.suffix != '.wav' or not path.is_file():
            return {'type': 'error', 'request_id': request_id, 'code': 'speech_path',
                    'reason': 'The recording must be a WAV file in this run folder.'}
        if not self.loaded[key].wait(120):
            return {'type': 'error', 'request_id': request_id, 'code': 'speech_loading',
                    'reason': 'The speech model is still loading.'}
        if key in self.errors:
            return {'type': 'error', 'request_id': request_id, 'code': 'speech_language',
                    'reason': f'The speech model could not load: {self.errors[key][:200]}'}
        started = time.monotonic()
        try:
            audio = louder(read_audio(path))
            with self.run_lock:
                text = self.engines[key].transcribe(audio, language)
        except Exception as exc:
            sys.stderr.write(traceback.format_exc())
            return {'type': 'error', 'request_id': request_id, 'code': 'speech_failed',
                    'reason': f'Speech could not be written down ({type(exc).__name__}).'}
        return {'type': 'transcript', 'request_id': request_id, 'text': text[:1000], 'language': language,
                'model': key, 'latency_ms': round((time.monotonic() - started) * 1000)}

    def run(self):
        threading.Thread(target=self.load_all, daemon=True).start()
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if not isinstance(request, dict):
                continue
            if request.get('type') == 'shutdown':
                break
            if request.get('type') == 'transcribe':
                # Requests are answered one at a time; the console sends one at a time as well.
                emit(self.transcribe(request))
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding='utf-8'))
    return Worker(spec).run()


if __name__ == '__main__':
    sys.exit(main())
