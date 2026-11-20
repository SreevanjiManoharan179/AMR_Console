"""Office tasks: approved context, one drive per stop, the job runner, speech input. Offline.

The language and speech workers are replaced by small fakes, except in the integration tests at
the end, which start the real worker scripts with stand-in models (no model, no network, no ROS).
"""
import copy
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import unittest.mock
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / 'src/amr_physical_ai') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src/amr_physical_ai'))

from errors import ConsoleError
import office_context
from office_context import OfficeContext
import office_navigation
import office_speech
from office_tasks import OfficeTasks
import speech_worker
from test_support import assert_guard_active
import worker


def setUpModule():
    assert_guard_active()


def wav_bytes(seconds, rate=16000, channels=1, width=2):
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        frames = int(seconds * rate)
        if width == 2:
            stream.writeframes(b''.join(struct.pack('<h', int(6000 * math.sin(i / 7))) * channels for i in range(frames)))
        else:
            stream.writeframes(b'\x80' * frames * channels * width)
    return out.getvalue()


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.02)
    return predicate()


# -- approved context ---------------------------------------------------------------------
class OfficeContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = OfficeContext(ROOT, 'proposed_system')

    def test_every_desk_and_room_is_reachable_and_people_have_their_desk(self):
        names = self.context.names(requester_place='home')
        self.assertEqual('home', names['requester_place'])
        self.assertTrue(names['rooms'] and names['seats'] and names['people'])
        self.assertTrue(all(row['reachable'] and row['on_map'] for row in names['rooms'] + names['seats']))
        for person in names['people']:
            self.assertEqual(person['seat'], person['usual_place'])
            self.assertIn(person.get('language', 'en'), office_context.LANGUAGES)

    def test_stop_poses_come_from_the_approved_files_only(self):
        desk = self.context.place('manager_cabin_r1c1')
        self.assertEqual("Divya's desk", desk['label'])
        self.assertTrue(all(math.isfinite(desk[k]) for k in ('x', 'y', 'yaw')))
        with self.assertRaises(office_context.OfficeConfigError):
            self.context.place('the roof')
        with self.assertRaises(office_context.OfficeConfigError):
            self.context.place({'x': 1, 'y': 2})

    def test_robot_motion_waits_for_every_owner_approval(self):
        reasons = ' '.join(self.context.motion_blocks())
        if self.context.capability.get('approval_status') != 'confirmed':
            self.assertIn('capability', reasons)

    def test_a_rehearsal_may_use_proposed_limits_but_marks_them(self):
        value = self.context.worker_capability(rehearsal=True)
        self.assertEqual('confirmed', value['approval_status'])
        if self.context.capability.get('approval_status') != 'confirmed':
            self.assertTrue(value['rehearsal_only_unconfirmed'])
            self.assertNotEqual('confirmed', self.context.worker_capability(rehearsal=False)['approval_status'])

    def test_the_roster_for_the_learning_review_is_written_once_never_over_a_file(self):
        import yaml
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'names.yaml'
            with unittest.mock.patch('sys.stdout', io.StringIO()):
                self.assertEqual(0, office_context.main(['--write-names', str(target)]))
            names = yaml.safe_load(target.read_text(encoding='utf-8'))
            self.assertEqual(self.context.names()['people'], names['people'])
            with unittest.mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                office_context.main(['--write-names', str(target)])

    def test_requesters_are_the_operator_then_the_people(self):
        people = self.context.requesters()
        self.assertEqual('operator', people[0]['kind'])
        self.assertEqual('home', people[0]['place'])
        self.assertEqual(people[1], self.context.requester(people[1]['id']))
        with self.assertRaises(office_context.OfficeConfigError):
            self.context.requester('Nobody At All')


# -- one drive per stop -------------------------------------------------------------------
class FakeSession:
    def __init__(self, grid, outcome='SUCCEEDED', raise_in_navigate=None):
        self.stop_requested = threading.Event()
        self.active_goal = None
        self.grid, self.outcome, self.raise_in_navigate = grid, outcome, raise_in_navigate
        self.sent = []
        self.position = {'x': 0.0, 'y': 0.0}

    def command_grid(self):
        return self.grid

    def truth_pose(self, after=None):
        return dict(self.position)

    def navigate(self, goal, before_send=None, cancel=None, timeout=90):
        before_send()
        self.sent.append(goal)
        if self.raise_in_navigate:
            raise self.raise_in_navigate
        self.position = {'x': goal['x'], 'y': goal['y']}
        return {'navigation_outcome': self.outcome, 'status': 4}


class OfficeNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from amr_physical_ai.command_pipeline import Grid
        context = OfficeContext(ROOT, 'proposed_system')
        cls.map_yaml = str((context.tags_path.parent / context.tags.data['map_yaml']).resolve().relative_to(ROOT))
        cls.grid = Grid.from_map(ROOT / cls.map_yaml)
        stop = context.place('reception')
        cls.params = {'x': stop['x'], 'y': stop['y'], 'yaw': stop['yaw'], 'place': 'reception', 'label': 'reception',
                      'job_id': 'job_' + 'a' * 12, 'nav_id': 'b' * 32, 'purpose': 'step', 'map_yaml': cls.map_yaml,
                      'minimum_clearance_m': float(context.tags.data['minimum_clearance_m'])}

    def test_a_valid_stop_is_checked_on_the_live_map_and_recorded(self):
        session = FakeSession(self.grid)
        record = office_navigation.navigate_leg(ROOT, session, dict(self.params), threading.Event())
        self.assertEqual('SUCCEEDED', record['nav2']['status'])
        self.assertTrue(record['nav2']['goal_sent'] and record['validation']['valid'])
        self.assertAlmostEqual(0.0, record['true_final_error_m'], places=6)
        self.assertEqual(1, len(session.sent))

    def test_stop_or_cancel_before_sending_sends_nothing(self):
        for flag in ('stop', 'cancel'):
            with self.subTest(flag=flag):
                session, cancel = FakeSession(self.grid), threading.Event()
                (session.stop_requested if flag == 'stop' else cancel).set()
                record = office_navigation.navigate_leg(ROOT, session, dict(self.params), cancel)
                self.assertEqual('NOT_SENT', record['nav2']['status'])
                self.assertEqual([], session.sent)

    def test_cancel_during_the_drive_is_reported_as_cancelled(self):
        session = FakeSession(self.grid, raise_in_navigate=InterruptedError('Office task cancel interrupted the drive.'))
        record = office_navigation.navigate_leg(ROOT, session, dict(self.params), threading.Event())
        self.assertEqual('CANCELED', record['nav2']['status'])

    def test_another_map_or_a_bad_pose_is_refused_before_any_goal(self):
        import numpy as np
        from amr_physical_ai.command_pipeline import Grid
        other = Grid(np.zeros((10, 10), dtype=int), .05, (0, 0, 0))
        for session, params in ((FakeSession(other), dict(self.params)),
                                (FakeSession(self.grid), dict(self.params, x=float('nan'))),
                                (FakeSession(self.grid), dict(self.params, minimum_clearance_m=5.0))):
            record = office_navigation.navigate_leg(ROOT, session, params, threading.Event())
            self.assertEqual('CLARIFICATION', record['nav2']['status'])
            self.assertEqual([], session.sent)

    def test_a_goal_already_running_blocks_a_new_one(self):
        session = FakeSession(self.grid)
        session.active_goal = object()
        record = office_navigation.navigate_leg(ROOT, session, dict(self.params), threading.Event())
        self.assertEqual('ERROR', record['nav2']['status'])
        self.assertEqual([], session.sent)
        self.assertFalse(session.stop_requested.is_set())


class SettleAfterHaltTests(unittest.TestCase):
    def test_waits_for_the_cancelled_goal_then_a_quiet_period(self):
        now = [0.0]
        adapter = SimpleNamespace(active_goal=object())
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
            if now[0] >= .3:
                adapter.active_goal = None
        record = worker.settle_after_halt(adapter, timeout=3, quiet=1.5, clock=lambda: now[0], sleep=sleep)
        self.assertTrue(record['had_goal'] and record['goal_released'])
        self.assertEqual(1.5, slept[-1])

    def test_no_goal_means_no_wait(self):
        record = worker.settle_after_halt(SimpleNamespace(active_goal=None), sleep=lambda s: self.fail('slept'))
        self.assertFalse(record['had_goal'])


# -- speech input ---------------------------------------------------------------------------
class SpeechCheckTests(unittest.TestCase):
    def test_only_short_16k_mono_16bit_wav_is_accepted(self):
        self.assertAlmostEqual(1.0, office_speech.check_wav(wav_bytes(1), 15))
        for raw, code in ((b'', 'speech_empty'), (b'RIFFxxxxWAVE', 'speech_format'),
                          (wav_bytes(1, rate=44100), 'speech_format'), (wav_bytes(1, channels=2), 'speech_format'),
                          (wav_bytes(1, width=1), 'speech_format'), (wav_bytes(.1), 'speech_short'),
                          (wav_bytes(16), 'speech_long'), (b'\0' * (office_speech.MAX_AUDIO_BYTES + 1), 'speech_size')):
            with self.subTest(code=code):
                with self.assertRaises(office_speech.SpeechError) as caught:
                    office_speech.check_wav(raw, 15)
                self.assertEqual(code, caught.exception.code)


class SpeechInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'console'
        (self.root / 'config').mkdir(parents=True)
        (self.root / 'config/speech_models.json').write_text((ROOT / 'config/speech_models.json').read_text())
        self.config = {'speech': {'enabled': True, 'path': '../speech', 'models': 'config/speech_models.json',
                                  'languages': {'en': 'whisper_base_en', 'ta': 'indic_conformer'}}}
        self.models = office_speech.load_models(self.root, self.config['speech'])

    def tearDown(self):
        self.temp.cleanup()

    def test_nothing_installed_says_so_and_typing_still_works(self):
        status = office_speech.install_status(self.root, self.config)
        self.assertEqual('not_installed', status['state'])
        self.assertIn('Typing works', status['detail'])

    def test_installed_languages_are_listed_per_model(self):
        base = Path(self.temp.name) / 'speech'
        (base / 'venv/bin').mkdir(parents=True)
        (base / 'venv/bin/python').write_text('')
        office_speech.snapshot_dir(base, self.models['whisper_base_en']).mkdir(parents=True)
        status = office_speech.install_status(self.root, self.config)
        self.assertEqual('off', status['state'])
        self.assertEqual({'en': 'installed', 'ta': 'missing'}, status['languages'])

    def test_disabled_and_broken_settings(self):
        self.assertEqual('disabled', office_speech.install_status(self.root, {'speech': {'enabled': False}})['state'])
        broken = copy.deepcopy(self.config)
        broken['speech']['languages']['fr'] = 'whisper_base_en'
        self.assertEqual('error', office_speech.install_status(self.root, broken)['state'])

    def test_every_pinned_model_names_a_revision_and_licence(self):
        for key, entry in self.models.items():
            self.assertEqual(40, len(entry['revision']), key)
            self.assertEqual('MIT', entry['licence'], key)
        # The owner's reviewed copy of IndicConformer's loader (model_onnx.py at the pinned revision).
        self.assertEqual('700f1897493f68fc522ce73dd7f91aa39b28fc20285d3d635e92de241b3910ae',
                         self.models['indic_conformer']['reviewed_code_sha256'])


class FakeSpeechProcess:
    instances = []

    def __init__(self, root, spec, on_status):
        self.spec, self.on_status = spec, on_status
        self.requests, self.closed, self.running = [], False, False
        FakeSpeechProcess.instances.append(self)

    def start(self):
        self.running = True

    def alive(self):
        return self.running

    def transcribe(self, path, language, timeout=90):
        self.requests.append((Path(path), language, Path(path).exists()))
        return {'type': 'transcript', 'text': f'words in {language}', 'model': self.spec['languages'][language],
                'latency_ms': 5}

    def close(self):
        self.closed, self.running = True, False


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {'operator': {'name': 'Bharathwaj'},
                       'speech': {'enabled': True, 'path': '../speech', 'models': 'config/speech_models.json',
                                  'languages': {'en': 'whisper_base_en', 'ta': 'indic_conformer'}, 'keep_audio': False}}
        self.changes = []
        self.speech = office_speech.Speech(ROOT, self.config, process_factory=FakeSpeechProcess,
                                           on_change=lambda: self.changes.append(1))
        context = SimpleNamespace(requesters=lambda: [{'name': 'Divya'}], places=lambda: [{'label': 'reception', 'kind': 'room'}])
        self.assertTrue(self.speech.start(self.temp.name, context, test_python=sys.executable))
        self.process = FakeSpeechProcess.instances[-1]

    def tearDown(self):
        self.speech.stop()
        self.temp.cleanup()

    def test_loading_then_ready_per_language_and_the_page_hears_of_it(self):
        self.assertEqual('starting', self.speech.status()['state'])
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        status = self.speech.status()
        self.assertEqual('ready', status['state'])
        self.assertIn('still loading', status['detail'])
        self.process.on_status({'type': 'status', 'language': 'ta', 'state': 'error', 'detail': 'loader not reviewed'})
        self.assertIn('Tamil: loader not reviewed', self.speech.status()['detail'])
        self.assertEqual(2, len(self.changes))
        self.assertIn('Divya', self.process.spec['prompt_words'])

    def test_language_choice_hint_and_fallback(self):
        for language in ('en', 'ta'):
            self.process.on_status({'type': 'status', 'language': language, 'state': 'ready'})
        self.assertEqual('ta', self.speech.transcribe(wav_bytes(1), 'ta', None, 'en')['language'])
        self.assertEqual('ta', self.speech.transcribe(wav_bytes(1), 'auto', 'ta', 'en')['language'])
        self.assertEqual('en', self.speech.transcribe(wav_bytes(1), 'auto', None, 'en')['language'])
        with self.assertRaises(office_speech.SpeechError) as caught:
            self.speech.transcribe(wav_bytes(1), 'ml', None, 'en')
        self.assertEqual('speech_language', caught.exception.code)

    def test_a_language_still_loading_or_failed_is_refused_clearly(self):
        with self.assertRaises(office_speech.SpeechError) as caught:
            self.speech.transcribe(wav_bytes(1), 'en', None, 'en')
        self.assertEqual('speech_loading', caught.exception.code)
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'error'})
        with self.assertRaises(office_speech.SpeechError) as caught:
            self.speech.transcribe(wav_bytes(1), 'en', None, 'en')
        self.assertEqual('speech_language', caught.exception.code)

    def test_recordings_are_deleted_unless_the_owner_keeps_them(self):
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        self.speech.transcribe(wav_bytes(1), 'en', None, 'en')
        path, _, existed = self.process.requests[-1]
        self.assertTrue(existed)
        self.assertFalse(path.exists())
        self.speech.settings['keep_audio'] = True
        self.speech.transcribe(wav_bytes(1), 'en', None, 'en')
        self.assertTrue(self.process.requests[-1][0].exists())
        lines = (Path(self.temp.name) / 'speech.jsonl').read_text().splitlines()
        self.assertEqual([None, self.process.requests[-1][0].name], [json.loads(l)['audio'] for l in lines])

    def test_a_muted_microphone_is_reported_as_silence_without_asking_the_model(self):
        # Live check of 6 Nov: the laptop microphone was muted; four recordings came back as empty text.
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        asked = len(self.process.requests)
        silence = io.BytesIO()
        with wave.open(silence, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\x00\x00' * 16000)
        result = self.speech.transcribe(silence.getvalue(), 'en', None, 'en')
        self.assertEqual((True, '', -120.0), (result['silent'], result['text'], result['peak_db']))
        self.assertEqual(asked, len(self.process.requests))
        line = json.loads((Path(self.temp.name) / 'speech.jsonl').read_text().splitlines()[-1])
        self.assertTrue(line['silent'])

    def test_a_spoken_recording_reports_its_level_and_goes_to_the_model(self):
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        result = self.speech.transcribe(wav_bytes(1), 'en', None, 'en')
        self.assertFalse(result['silent'])
        self.assertAlmostEqual(-14.7, result['peak_db'], delta=0.3)   # 6000 / 32768 full scale
        self.assertLess(result['rms_db'], result['peak_db'])

    def test_an_overloaded_microphone_is_flagged_but_its_words_are_still_shown(self):
        # Live check of 6 Nov: the empty headset jack at full boost (24 % clipped) gave made-up words.
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        loud = io.BytesIO()
        with wave.open(loud, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b''.join(struct.pack('<h', 32767 if i % 4 else 9000) for i in range(16000)))
        result = self.speech.transcribe(loud.getvalue(), 'en', None, 'en')
        self.assertTrue(result['distorted'])
        self.assertGreater(result['clipped'], 0.5)
        self.assertTrue(result['text'])                     # the person still sees and corrects the words
        clean = self.speech.transcribe(wav_bytes(1), 'en', None, 'en')
        self.assertEqual((False, 0.0), (clean['distorted'], clean['clipped']))

    def test_a_worker_that_exits_marks_speech_unavailable_and_stop_ignores_late_lines(self):
        self.process.on_status({'type': 'exit', 'code': 1})
        self.assertEqual('error', self.speech.status()['state'])
        self.speech.stop()
        self.assertTrue(self.process.closed)
        self.process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        self.assertNotEqual('ready', self.speech.status()['state'])


class SpeechWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / 'run'
        self.log.mkdir()
        os.environ['AMR_CONSOLE_TEST_SPEECH'] = '1'
        self.spec = {'base': str(Path(self.temp.name) / 'speech'), 'log_dir': str(self.log), 'test_engine': True,
                     'languages': {'en': 'whisper_base_en', 'ta': 'indic_conformer'},
                     'models': json.loads((ROOT / 'config/speech_models.json').read_text())['models']}
        self.events = []
        self._emit = speech_worker.emit
        speech_worker.emit = self.events.append

    def tearDown(self):
        speech_worker.emit = self._emit
        os.environ.pop('AMR_CONSOLE_TEST_SPEECH', None)
        self.temp.cleanup()

    def test_stand_in_writes_down_each_language_and_reports_ready(self):
        work = speech_worker.Worker(self.spec)
        work.load_all()
        self.assertEqual({('en', 'ready'), ('ta', 'ready')},
                         {(e['language'], e['state']) for e in self.events if e['state'] != 'loading'})
        (self.log / 'a.wav').write_bytes(wav_bytes(1))
        reply = work.transcribe({'request_id': 'r1', 'path': str(self.log / 'a.wav'), 'language': 'ta'})
        self.assertEqual('transcript', reply['type'])
        self.assertEqual(speech_worker.StandIn.WORDS['ta'], reply['text'])

    def test_only_wav_files_in_the_run_folder_are_read(self):
        work = speech_worker.Worker(self.spec)
        work.load_all()
        outside = Path(self.temp.name) / 'outside.wav'
        outside.write_bytes(wav_bytes(1))
        (self.log / 'notes.txt').write_text('x')
        for path in (outside, self.log / 'notes.txt', self.log / '../outside.wav', self.log / 'missing.wav'):
            reply = work.transcribe({'request_id': 'r', 'path': str(path), 'language': 'en'})
            self.assertEqual('speech_path', reply['code'], path)
        self.assertEqual('speech_language', work.transcribe({'request_id': 'r', 'path': '', 'language': 'fr'})['code'])

    def test_the_stand_in_needs_both_the_spec_flag_and_the_test_environment(self):
        os.environ.pop('AMR_CONSOLE_TEST_SPEECH')
        work = speech_worker.Worker(self.spec)
        quiet = io.StringIO()
        with unittest.mock.patch('sys.stderr', quiet):
            work.load_all()  # real engines: models are not installed here, so both report an error
        self.assertIn('Traceback', quiet.getvalue())
        self.assertEqual({'error'}, {e['state'] for e in self.events if e['state'] != 'loading'})
        (self.log / 'a.wav').write_bytes(wav_bytes(1))
        self.assertEqual('speech_language', work.transcribe({'request_id': 'r', 'path': str(self.log / 'a.wav'),
                                                             'language': 'en'})['code'])

    def test_the_model_loader_code_runs_only_after_its_review(self):
        entry = self.spec['models']['indic_conformer']
        folder = Path(self.spec['base']) / 'hf/hub/models--ai4bharat--indic-conformer-600m-multilingual/snapshots' / entry['revision']
        folder.mkdir(parents=True)
        (folder / 'model_onnx.py').write_text('print("loader")\n')
        with self.assertRaisesRegex(PermissionError, 'not been reviewed'):
            speech_worker.IndicConformer(self.spec['base'], dict(entry, reviewed_code_sha256=None), {})
        with self.assertRaisesRegex(PermissionError, 'changed'):
            speech_worker.IndicConformer(self.spec['base'], dict(entry, reviewed_code_sha256='0' * 64), {})


    def test_a_quiet_microphone_is_made_louder_but_silence_and_loud_speech_are_left_alone(self):
        try:
            import numpy
        except ImportError:
            self.skipTest('numpy is part of the speech environment')
        quiet = numpy.array([0.02, -0.02], dtype='float32')
        self.assertAlmostEqual(0.5, float(numpy.max(numpy.abs(speech_worker.louder(quiet)))), places=5)
        faint = numpy.array([0.004, -0.004], dtype='float32')     # gain is capped at 30x
        self.assertAlmostEqual(0.12, float(numpy.max(speech_worker.louder(faint))), places=5)
        for unchanged in ([0.001, -0.001], [0.6, -0.6]):
            audio = numpy.array(unchanged, dtype='float32')
            self.assertTrue(numpy.array_equal(audio, speech_worker.louder(audio)))
        offset = numpy.array([0.5, 0.7, 0.5, 0.7], dtype='float32')   # a centred copy, not a shifted one
        self.assertAlmostEqual(0.0, float(numpy.mean(speech_worker.louder(offset))), places=5)

    def test_the_reviewed_loader_builds_from_the_local_folder_on_the_processor(self):
        entry = self.spec['models']['indic_conformer']
        folder = Path(self.spec['base']) / 'hf/hub/models--ai4bharat--indic-conformer-600m-multilingual/snapshots' / entry['revision']
        folder.mkdir(parents=True)
        (folder / 'config.json').write_text('{"BLANK_ID": 256, "SOS": 256}')
        loader = (b'print("a note from the loader")\n'
                  b'class IndicASRConfig:\n'
                  b'    @classmethod\n'
                  b'    def from_dict(cls, value):\n'
                  b'        c = cls(); c.value = value; return c\n'
                  b'class IndicASRModel:\n'
                  b'    def __init__(self, config): self.config = config\n'
                  b'    @classmethod\n'
                  b'    def from_pretrained(cls, *a, **k): raise AssertionError("would download")\n')
        (folder / 'model_onnx.py').write_bytes(loader)
        reviewed = hashlib.sha256(loader).hexdigest()
        fake_torch = SimpleNamespace(set_num_threads=lambda n: None)
        out = io.StringIO()
        with unittest.mock.patch.dict(sys.modules, {'torch': fake_torch}), \
                unittest.mock.patch.dict(os.environ, {}), unittest.mock.patch('sys.stdout', out), \
                unittest.mock.patch('sys.stderr', io.StringIO()):
            engine = speech_worker.IndicConformer(self.spec['base'], dict(entry, reviewed_code_sha256=reviewed), {})
            self.assertEqual('', os.environ['CUDA_VISIBLE_DEVICES'])
        self.assertEqual(str(folder), engine.model.config.value['ts_folder'])
        self.assertEqual(256, engine.model.config.value['SOS'])
        self.assertEqual('', out.getvalue())   # nothing but events may reach the console


# -- the language worker's job queue (no language module needed) ------------------------------
class Recorder:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, type_, **fields):
        with self.lock:
            self.events.append({'type': type_, **fields})

    def of(self, kind):
        with self.lock:
            return [e for e in self.events if e['type'] == kind]


class LanguageWorkerJobsTests(unittest.TestCase):
    def setUp(self):
        import language_worker
        self.lw = language_worker
        self.out = Recorder()

    def test_each_drive_request_carries_the_cancel_number_it_was_checked_under(self):
        jobs = self.lw.Jobs(self.out, 'robot', {}, None)
        session = SimpleNamespace(current={'job_id': 'job_' + 'a' * 12}, Stopped=type('Stopped', (Exception,), {}))
        answer = {}
        thread = threading.Thread(target=lambda: answer.setdefault('ok', jobs.move(session, 'reception', 'step', 'reception')))
        thread.start()
        self.assertTrue(wait_for(lambda: self.out.of('navigate')))
        request = self.out.of('navigate')[-1]
        self.assertEqual(0, request['cancel_epoch'])
        jobs.navigation_result(request['nav_id'], {'ok': True})
        thread.join(5)
        self.assertTrue(answer['ok'])
        jobs.cancel('cancelled', epoch=1)
        with self.assertRaises(session.Stopped):
            jobs.move(session, 'reception', 'step', 'reception')
        self.assertEqual(1, len(self.out.of('navigate')))       # nothing new was asked for

    def test_a_cancel_ends_earlier_requests_and_keeps_later_ones(self):
        test = self

        class Session:
            def __init__(self, jobs, names, settings):
                self.jobs, self.queue, self.current, self.position = jobs, deque(), None, None

            def run(self):
                while self.queue:
                    self.current = self.queue.popleft()
                    self.jobs._state(self.current['job_id'], 'running')
                    if self.current['text'] == 'block':
                        while not self.jobs.cancelled.is_set():
                            time.sleep(.01)
                        test.cancel_seen.set()
                        test.release.wait(5)            # still unwinding when the next request arrives
                        return {'status': 'stopped'}
                    self.jobs._state(self.current['job_id'], 'done')
                return {'status': 'done'}

        from collections import deque
        self.cancel_seen, self.release = threading.Event(), threading.Event()
        jobs = self.lw.Jobs(self.out, 'rehearsal', {}, Session)
        job = lambda n, text: {'job_id': f'job_{n:012d}', 'text': text, 'requester': 'Bharathwaj', 'names': {}}
        jobs.submit(job(1, 'block'))
        self.assertTrue(wait_for(lambda: jobs.job_states.get('job_000000000001') == 'running'))
        jobs.submit(job(2, 'quick'))                       # asked for before the Cancel
        jobs.cancel('cancelled', epoch=1)
        self.assertTrue(self.cancel_seen.wait(5))
        jobs.submit(job(3, 'quick'))                       # asked for after the Cancel
        self.release.set()
        self.assertTrue(wait_for(lambda: jobs.job_states.get('job_000000000003') == 'done'))
        self.assertEqual('cancelled', jobs.job_states['job_000000000002'])
        self.assertEqual('stopped', jobs.job_states['job_000000000001'])

    def test_a_cancel_while_starting_is_still_counted(self):
        worker = self.lw.Worker({'mode': 'robot'}, out=self.out, stdin=io.StringIO())
        self.assertTrue(worker.handle({'type': 'cancel', 'epoch': 3}))
        self.assertEqual(3, worker.cancel_epoch)
        self.assertEqual([], self.out.of('refused'))

    def test_a_drive_stopped_on_the_way_forgets_the_last_stop(self):
        jobs = self.lw.Jobs(self.out, 'robot', {}, None)
        session = SimpleNamespace(current={'job_id': 'job_' + 'a' * 12}, position='home',
                                  Stopped=type('Stopped', (Exception,), {}))
        failure = {}

        def drive():
            try:
                jobs.move(session, 'manager_cabin_r1c1', 'step', 'Divya')
            except session.Stopped:
                failure['stopped'] = True
        thread = threading.Thread(target=drive)
        thread.start()
        self.assertTrue(wait_for(lambda: self.out.of('navigate')))
        jobs.cancel('cancelled', epoch=1)
        jobs.navigation_result(self.out.of('navigate')[-1]['nav_id'], {'ok': False, 'status': 'CANCELED'})
        thread.join(5)
        self.assertTrue(failure.get('stopped'))
        self.assertIsNone(session.position)      # not "home" any more: the next job drives again

    def test_stop_forgets_where_the_robot_was(self):
        worker = self.lw.Worker({'mode': 'robot'}, out=self.out, stdin=io.StringIO())
        worker.jobs = self.lw.Jobs(self.out, 'robot', {}, None)
        worker.jobs.position = 'manager_cabin_r1c1'
        worker.handle({'type': 'stop', 'epoch': 1})
        self.assertIsNone(worker.jobs.position)
        worker.jobs.position = 'reception'
        worker.handle({'type': 'cancel', 'epoch': 2})   # Cancel alone keeps a known position
        self.assertEqual('reception', worker.jobs.position)


# -- the job runner ----------------------------------------------------------------------
class FakeLanguageProcess:
    instances = []

    def __init__(self, root, spec, on_event, on_exit):
        self.spec, self.on_event, self.on_exit = spec, on_event, on_exit
        self.sent, self.running = [], False
        FakeLanguageProcess.instances.append(self)

    def start(self):
        self.running = True

    def send(self, message):
        if not self.running:
            raise RuntimeError('The language worker is not running.')
        self.sent.append(message)

    def alive(self):
        return self.running

    def close(self, timeout=20):
        self.running = False


class FakeBackend:
    def __init__(self, root):
        self.root = Path(root)
        self.state, self.session, self.stopping = 'idle', None, False
        self.events = SimpleNamespace(publish=lambda event: None)
        self.navigations, self.cancels = [], 0
        self.office_cancel_generation = 0
        self.outcome = {'nav2': {'status': 'SUCCEEDED', 'navigation_wall_s': 3.0}, 'true_final_error_m': .05}

    def office_navigate(self, pose, **kw):
        # Same rule as the engine: a Cancel since the request refuses the drive.
        if kw.get('expected_generation') is not None and kw['expected_generation'] != self.office_cancel_generation:
            raise ConsoleError(409, 'office_cancelled', 'The office task was cancelled before this drive started.')
        self.navigations.append((pose, kw))
        return self.outcome

    def office_cancel_goal(self):
        self.cancels += 1
        self.office_cancel_generation += 1
        return True


class OfficeTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # A private copy of the product so approvals and logs never touch the real files.
        self.root = Path(self.temp.name) / 'console'
        for relative in ('config', 'tools/amr_task', 'tools/amr_console/approvals.yaml', 'src/amr_physical_ai/config',
                         'src/amr_physical_ai/maps', 'src/amr_physical_ai/worlds'):
            source = ROOT / relative
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                import shutil
                shutil.copytree(source, target, ignore=lambda d, names: [n for n in names if n == '__pycache__'])
            elif source.exists():
                target.write_bytes(source.read_bytes())
        (self.root / 'tools/amr_console').mkdir(parents=True, exist_ok=True)
        (self.root / 'tools/amr_console/approvals.yaml').write_text('proposed_system_places: false\n')
        # The fake worker never imports it; the console only checks that the folder is there.
        stub = Path(self.temp.name) / 'amr-language/amr_language/__init__.py'
        stub.parent.mkdir(parents=True)
        stub.write_text("__version__ = '0.5.0'\n")
        self.backend = FakeBackend(self.root)
        self.office = OfficeTasks(self.backend, process_factory=FakeLanguageProcess, speech_factory=FakeSpeechProcess)

    def tearDown(self):
        self.office.shutdown()
        self.temp.cleanup()

    def start(self, mode='rehearsal'):
        self.office.start(mode)
        process = FakeLanguageProcess.instances[-1]
        process.on_event({'type': 'service', 'state': 'ready', 'model': 'qwen', 'fully_on_gpu': True})
        return process

    def approve_everything(self):
        (self.root / 'tools/amr_console/approvals.yaml').write_text('proposed_system_places: true\n')
        for relative in ('config/robot_capability_simulation.yaml',
                         office_context.registry.COMMAND_MAPS['proposed_system']['tags']):
            path = self.root / relative
            path.write_text('\n'.join('approval_status: confirmed' if line.startswith('approval_status:') else line
                                      for line in path.read_text().splitlines()) + '\n')

    def test_rehearsal_needs_only_the_service_robot_needs_session_and_approvals(self):
        self.assertIn('Start the language service first.', self.office.blocks('rehearsal'))
        robot = ' '.join(self.office.blocks('robot'))
        self.assertIn('Start navigation', robot)
        self.assertIn('approvals.yaml', robot)
        with self.assertRaises(ConsoleError) as caught:
            self.office.start('robot')
        self.assertEqual('office_approval', caught.exception.code)
        self.start('rehearsal')
        self.assertEqual([], self.office.blocks('rehearsal'))
        self.assertEqual('rehearsal_only_unconfirmed' in FakeLanguageProcess.instances[-1].spec['capability'],
                         self.office.context.capability.get('approval_status') != 'confirmed')

    def test_requests_are_checked_and_sent_with_the_approved_roster(self):
        process = self.start()
        for text, language, requester in (('', 'en', 'Bharathwaj'), ('x' * 1001, 'en', 'Bharathwaj'),
                                          ('give\x07it', 'en', 'Bharathwaj'), ('give the file', 'fr', 'Bharathwaj'),
                                          ('give the file', 'en', 'Somebody')):
            with self.assertRaises(ConsoleError):
                self.office.submit(text, requester, language, None)
        job = self.office.submit('Give the file to Divya.', 'Priya', 'auto', None)['job_id']
        message = process.sent[-1]
        self.assertEqual(('submit', job, None), (message['type'], message['job_id'], message['language']))
        self.assertEqual('open_office_r1c1', message['names']['requester_place'])
        # Priya's own language is Tamil; assistance.speak_to_everyone_in in the config overrides it.
        self.assertEqual(self.office.context.everyone_language or 'ta', message['requester']['language'])

    def test_each_published_snapshot_is_newer_than_the_last(self):
        published = []
        self.backend.events = SimpleNamespace(publish=published.append)
        self.start()
        self.office.submit('Give the file to Divya.', 'Bharathwaj', 'en', 'home')
        versions = [event['office']['version'] for event in published]
        self.assertEqual(sorted(set(versions)), versions)
        self.assertEqual(versions[-1], self.office.snapshot()['version'])
        self.assertEqual({self.office.epoch}, {event['office']['epoch'] for event in published})

    def test_replies_go_only_to_the_question_that_is_open(self):
        process = self.start()
        job = self.office.submit('Give the file to Divya.', 'Bharathwaj', 'en', 'home')['job_id']
        process.on_event({'type': 'awaiting_input', 'input_id': 'i1', 'job_id': job, 'prompt': 'reply> ',
                          'expect': ['yes', 'no'], 'audience': 'requester', 'place': 'home', 'wait_s': 120})
        self.assertEqual('home', self.office.snapshot()['awaiting']['place_label'])
        with self.assertRaises(ConsoleError):
            self.office.reply('old', 'yes')
        self.office.reply('i1', 'yes')
        self.assertEqual({'type': 'input', 'input_id': 'i1', 'text': 'yes'}, process.sent[-1])
        self.assertIsNone(self.office.snapshot()['awaiting'])

    def test_robot_moves_use_the_approved_pose_and_report_back(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        process = self.start('robot')
        self.assertEqual([], self.office.blocks('robot'))
        process.on_event({'type': 'navigate', 'nav_id': 'n' * 32, 'job_id': 'job_' + 'c' * 12,
                          'place': 'manager_cabin_r1c1', 'purpose': 'step', 'cancel_epoch': 0})
        self.assertTrue(wait_for(lambda: any(m.get('type') == 'navigate_result' for m in process.sent)))
        pose, kw = self.backend.navigations[-1]
        self.assertEqual(self.office.context.place('manager_cabin_r1c1'), pose)
        self.assertEqual('proposed_system', kw['map_id'])
        result = [m for m in process.sent if m.get('type') == 'navigate_result'][-1]
        self.assertTrue(result['ok'])
        snap = self.office.snapshot()
        self.assertEqual(("Divya's desk", None), (snap['position_label'], snap['moving']))

    def test_a_refused_move_is_reported_to_the_conversation_not_driven(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        process = self.start('robot')
        self.backend.session = {'kind': 'navigation', 'map': 'tb3_sandbox', 'record_truth': True}
        process.on_event({'type': 'navigate', 'nav_id': 'n' * 32, 'job_id': 'job_' + 'c' * 12,
                          'place': 'reception', 'purpose': 'step', 'cancel_epoch': 0})
        self.assertTrue(wait_for(lambda: any(m.get('type') == 'navigate_result' for m in process.sent)))
        result = [m for m in process.sent if m.get('type') == 'navigate_result'][-1]
        self.assertEqual((False, 'REFUSED'), (result['ok'], result['status']))
        self.assertEqual([], self.backend.navigations)

    def test_a_drive_asked_for_before_cancel_never_starts(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        process = self.start('robot')
        self.office.cancel()                       # Cancel number 1 reaches the worker
        self.assertEqual({'type': 'cancel', 'epoch': 1}, process.sent[-1])
        # The worker had checked before it saw the Cancel: its request carries number 0.
        for epoch in (0, None):
            process.on_event({'type': 'navigate', 'nav_id': 'n' * 32, 'job_id': 'job_' + 'c' * 12,
                              'place': 'reception', 'purpose': 'step', 'cancel_epoch': epoch})
            result = [m for m in process.sent if m.get('type') == 'navigate_result'][-1]
            self.assertEqual((False, 'CANCELED'), (result['ok'], result['status']))
        self.assertEqual([], self.backend.navigations)
        self.assertIsNone(self.office.snapshot()['moving'])

    def test_a_cancel_between_request_and_drive_refuses_the_drive(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        process = self.start('robot')
        gate = threading.Event()
        original = self.office._navigate

        def slow(event):                           # the drive thread is slow to start
            gate.wait(5)
            original(event)
        self.office._navigate = slow
        process.on_event({'type': 'navigate', 'nav_id': 'n' * 32, 'job_id': 'job_' + 'c' * 12,
                          'place': 'reception', 'purpose': 'step', 'cancel_epoch': 0})
        self.office.cancel()
        gate.set()
        self.assertTrue(wait_for(lambda: any(m.get('type') == 'navigate_result' for m in process.sent)))
        result = [m for m in process.sent if m.get('type') == 'navigate_result'][-1]
        self.assertEqual((False, 'CANCELED'), (result['ok'], result['status']))
        self.assertEqual([], self.backend.navigations)

    def test_cancel_reaches_the_robot_and_the_conversation(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        process = self.start('robot')
        self.office.cancel()
        self.assertEqual({'type': 'cancel', 'epoch': 1}, process.sent[-1])
        self.assertEqual(1, self.backend.cancels)
        self.office.on_session_stop()
        self.assertEqual({'type': 'stop', 'epoch': 2}, process.sent[-1])

    def test_start_after_a_failed_runner_retries_with_a_fresh_worker(self):
        self.office.start('rehearsal')
        first = FakeLanguageProcess.instances[-1]
        first.on_event({'type': 'service', 'state': 'error', 'code': 'runner_isolation_blocked', 'detail': 'blocked'})
        self.assertTrue(first.alive())             # the worker itself is still running
        self.office.start('rehearsal')             # the owner fixed the cause and presses Start again
        second = FakeLanguageProcess.instances[-1]
        self.assertIsNot(first, second)
        self.assertFalse(first.alive())
        self.assertEqual('starting', self.office.snapshot()['service']['state'])

    def test_rehearsal_cancel_never_touches_the_robot(self):
        self.start('rehearsal')
        self.office.cancel()
        self.assertEqual(0, self.backend.cancels)

    def test_a_drive_left_by_a_dead_worker_is_cancelled_and_cancel_still_works(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        release = threading.Event()
        outcome = self.backend.outcome

        def long_drive(pose, **kw):
            self.backend.navigations.append((pose, kw))
            release.wait(5)
            return outcome
        self.backend.office_navigate = long_drive
        process = self.start('robot')
        process.on_event({'type': 'navigate', 'nav_id': 'n' * 32, 'job_id': 'job_' + 'c' * 12,
                          'place': 'reception', 'purpose': 'step', 'cancel_epoch': 0})
        self.assertTrue(wait_for(lambda: self.backend.navigations))
        process.running = False
        process.on_exit(-9)                       # the worker dies while the robot drives
        self.assertEqual(1, self.backend.cancels)
        self.assertIsNotNone(self.office.snapshot()['moving'])
        self.office.cancel()                      # still allowed: a drive is under way
        self.assertEqual(2, self.backend.cancels)
        release.set()
        self.assertTrue(wait_for(lambda: self.office.snapshot()['moving'] is None))
        # Nothing new may start without a running worker.
        self.office._navigate({'nav_id': 'm' * 32, 'job_id': 'job_' + 'c' * 12, 'place': 'reception',
                               'purpose': 'step', 'expected_generation': self.backend.office_cancel_generation})
        self.assertEqual(1, len(self.backend.navigations))
        self.office.stop_service()                # with no worker it still cancels any drive
        self.assertEqual(3, self.backend.cancels)

    def test_with_the_robot_the_position_comes_from_drive_results_only(self):
        self.approve_everything()
        self.backend.state = 'running'
        self.backend.session = {'kind': 'navigation', 'map': 'proposed_system', 'record_truth': True}
        process = self.start('robot')
        process.on_event({'type': 'session', 'status': 'stopped', 'position': 'home'})
        self.assertIsNone(self.office.snapshot()['position'])

    def test_a_worker_that_exits_fails_open_jobs_and_ends_speech(self):
        process = self.start()
        self.office.submit('Give the file to Divya.', 'Bharathwaj', 'en', 'home')
        speech = self.office.speech
        process.running = False
        process.on_exit(1)
        snap = self.office.snapshot()
        self.assertEqual('error', snap['service']['state'])
        self.assertEqual('failed', snap['jobs'][-1]['state'])
        self.assertIsNone(self.office.speech)
        if speech is not None:
            self.assertTrue(wait_for(lambda: speech.process is None))

    def test_speech_needs_the_service_and_an_installed_model(self):
        with self.assertRaises(ConsoleError) as caught:
            self.office.transcribe(wav_bytes(1), 'en')
        self.assertEqual('office_off', caught.exception.code)
        self.start()
        with self.assertRaises(ConsoleError) as caught:
            self.office.transcribe(wav_bytes(1), 'xx')
        self.assertEqual(422, caught.exception.status)
        with self.assertRaises(ConsoleError) as caught:
            self.office.transcribe(wav_bytes(1), 'en')
        self.assertEqual('speech_off', caught.exception.code)     # not installed in this copy
        self.assertEqual('not_installed', self.office.snapshot()['speech']['state'])

    def test_speech_runs_beside_the_service_and_stops_with_it(self):
        self.office.test_speech = sys.executable
        self.start()
        speech_process = FakeSpeechProcess.instances[-1]
        speech_process.on_status({'type': 'status', 'language': 'en', 'state': 'ready'})
        self.assertEqual('ready', self.office.snapshot()['speech']['state'])
        self.assertEqual('words in en', self.office.transcribe(wav_bytes(1), 'auto', 'en')['text'])
        self.office.stop_service()
        self.assertTrue(speech_process.closed)
        self.assertEqual('not_installed', self.office.snapshot()['speech']['state'])


class EngineStopTests(unittest.TestCase):
    def test_stop_never_waits_for_the_office(self):
        from engine import ConsoleBackend
        from fixtures import ENVIRONMENT, FakeClock, FakeSupervisors, FixtureRepo
        repo = FixtureRepo()
        self.addCleanup(repo.close)
        heard, release = threading.Event(), threading.Event()

        class SlowOffice:
            def __init__(self, backend):
                pass

            def on_session_stop(self):           # e.g. its lock is busy for a while
                heard.set()
                release.wait(5)

            def brief(self):
                return {}

            def robot_busy(self):
                return False
        clock = FakeClock()
        backend = ConsoleBackend(root=repo.root, inventory=lambda: [], lock_probe=lambda: False, clock=clock,
                                 wall_clock=clock.wall, supervisor_factory=FakeSupervisors(),
                                 transport_selector=lambda kind: {'ROS_DOMAIN_ID': '80', 'GZ_PARTITION': 'x'},
                                 environment=dict(ENVIRONMENT), office_factory=SlowOffice)
        started = time.monotonic()
        backend.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(heard.wait(2))           # and the office still hears it
        release.set()


class OfficeServerSpeechTests(unittest.TestCase):
    token = 'b' * 32

    def setUp(self):
        from server import make_server
        self.calls = []
        office = SimpleNamespace(transcribe=lambda raw, language, hint: self.calls.append((len(raw), language, hint))
                                 or {'text': 'ok', 'language': language},
                                 snapshot=lambda: {}, shutdown=lambda: None)
        backend = SimpleNamespace(office=office, root=ROOT, status=dict, actions=dict, results=dict)
        self.server = make_server(backend, self.token, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, content_type='audio/wav', token=None, path='/api/office/speech?language=ta&hint=en', length=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {'X-Console-Token': token or self.token, 'Content-Type': content_type}
        if length is not None:
            headers['Content-Length'] = str(length)
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        value = (response.status, json.loads(response.read()))
        connection.close()
        return value

    def test_the_recording_reaches_the_office_with_its_language_and_hint(self):
        status, value = self.post(wav_bytes(1))
        self.assertEqual((200, 'ok'), (status, value['text']))
        self.assertEqual(('ta', 'en'), self.calls[-1][1:])

    def test_a_refused_upload_never_spoils_the_next_request_on_the_connection(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request('POST', '/api/office/speech?language=en', body=wav_bytes(1),
                           headers={'X-Console-Token': 'c' * 32, 'Content-Type': 'audio/wav'})
        response = connection.getresponse()
        response.read()
        self.assertEqual((403, 'close'), (response.status, response.getheader('Connection')))
        connection.request('GET', '/api/office', headers={'X-Console-Token': self.token})
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.assertEqual({}, json.loads(response.read()))
        connection.close()

    def test_wrong_type_size_or_token_is_refused_before_the_office_sees_it(self):
        self.assertEqual(415, self.post(wav_bytes(1), content_type='application/json')[0])
        self.assertEqual(403, self.post(wav_bytes(1), token='c' * 32)[0])
        self.assertEqual(413, self.post(b'', length=2 * 1024 * 1024)[0])
        self.assertEqual([], self.calls)


# -- the real worker scripts with stand-in models -------------------------------------------
def language_module_version():
    try:
        config = office_context.load_config(ROOT)
        init = office_context.language_root(ROOT, config) / 'amr_language/__init__.py'
        text = init.read_text()
        return tuple(int(x) for x in text.split('__version__', 1)[1].split("'")[1].split('.')[:3])
    except Exception:
        return None


class OfficeWorkerIntegrationTests(unittest.TestCase):
    """Start the real language and speech workers (stand-ins: no model, no network)."""

    @unittest.skipUnless((language_module_version() or (0,)) >= (0, 5, 0), 'needs ../amr-language 0.5.0 or newer')
    def test_a_delivery_rehearsal_runs_end_to_end(self):
        os.environ['AMR_CONSOLE_TEST_LANGUAGE'] = '1'
        os.environ['AMR_CONSOLE_TEST_SPEECH'] = '1'
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(os.environ.pop, 'AMR_CONSOLE_TEST_LANGUAGE', None)
        self.addCleanup(os.environ.pop, 'AMR_CONSOLE_TEST_SPEECH', None)
        backend = FakeBackend(ROOT)
        office = OfficeTasks(backend, test_backend='echo', test_speech=sys.executable)
        self.addCleanup(office.shutdown)
        office.start('rehearsal')
        # This run's log folder is removed at the end of the test.
        self.addCleanup(__import__('shutil').rmtree, office.log_dir, True)
        self.assertTrue(wait_for(lambda: office.snapshot()['service']['state'] == 'ready', 60), office.snapshot()['service'])
        self.assertTrue(wait_for(lambda: office.snapshot()['speech']['state'] == 'ready', 30), office.snapshot()['speech'])
        heard = office.transcribe(wav_bytes(1.2), 'auto', 'en')
        self.assertEqual('give the file to divya', heard['text'])
        office.submit('Give the file to Divya.', 'Bharathwaj', 'en', 'home')
        answers = ['yes', 'taken']
        for _ in range(600):
            snap = office.snapshot()
            if snap['awaiting'] and answers:
                office.reply(snap['awaiting']['input_id'], answers.pop(0))
            job = snap['jobs'][-1] if snap['jobs'] else {}
            if job.get('state') in ('done', 'failed', 'cancelled', 'stopped') and not snap['moving'] and snap['position'] == 'home':
                break
            time.sleep(.05)
        self.assertEqual('done', snap['jobs'][-1]['state'], [e.get('text') for e in snap['timeline']])
        moves = [e['text'] for e in snap['timeline'] if e['kind'] == 'move']
        self.assertTrue(any("Divya's desk" in m for m in moves), moves)
        log_dir = office.log_dir
        office.stop_service()
        self.assertEqual([], list((log_dir / 'speech').glob('*.wav')) if (log_dir / 'speech').exists() else [])
        if (language_module_version() or (0,)) >= (0, 8, 1):
            # The finished job's learning suggestion stays in this run's folder (removed below),
            # not in the owner's review queue in ../amr-language/records.
            saved = [json.loads(line) for path in (log_dir / 'language_records/learning_suggestions').glob('*.jsonl')
                     for line in path.read_text().splitlines()]
            self.assertEqual(['owner_row'], [row['kind'] for row in saved])


if __name__ == '__main__':
    unittest.main()
