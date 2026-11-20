"""Break tests: hostile or careless HTTP clients, races and STOP timing. Real loopback HTTP,
process-free backend; no ROS launches."""
import http.client
import json
import os
import socket
import threading
import time
import unittest

import registry
from engine import ConsoleBackend
from fixtures import ENVIRONMENT, FakeClock, FakeSupervisors, FixtureRepo
from server import make_server
from test_support import assert_guard_active


def setUpModule():
    assert_guard_active()


class RobustnessTests(unittest.TestCase):
    token = 'b' * 32

    def setUp(self):
        self.repo = FixtureRepo()
        self.repo.copy_from_repo(*registry.map_files('proposed_system'), *registry.EXPLORE_FILES)
        self.repo.write('tools/amr_console/approvals.yaml', 'proposed_system_places: true\n')
        self.clock = FakeClock()
        self.supervisors = FakeSupervisors()
        self.backend = ConsoleBackend(
            root=self.repo.root, inventory=lambda: [], lock_probe=lambda: False,
            clock=self.clock, wall_clock=self.clock.wall,
            supervisor_factory=self.supervisors,
            transport_selector=lambda kind: {'ROS_DOMAIN_ID': '80',
                                             'GZ_PARTITION': f'amr_console_{kind}_80'},
            environment=dict(ENVIRONMENT))
        self.server = make_server(self.backend, self.token, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        for supervisor in self.supervisors.instances:
            supervisor.respond_to_operations = True
        if self.backend.status()['state'] != 'idle':
            self.backend.stop()
        self.server.closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.repo.close()

    # --- helpers -------------------------------------------------------------------------------
    def call(self, method, path, payload=None, token=token, raw=None, headers=None, timeout=5):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=timeout)
        sent = {'X-Console-Token': token} if token is not None else {}
        sent.update(headers or {})
        body = json.dumps(payload) if payload is not None else raw
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        text = response.read().decode('utf-8', 'replace')
        connection.close()
        try:
            return response.status, json.loads(text)
        except ValueError:
            return response.status, text

    def defaults(self, action_id):
        action = next(a for a in self.backend.actions()['actions'] if a['id'] == action_id)
        return {p['name']: p['default'] for p in action['params']}

    def preview(self, action_id, **params):
        values = self.defaults(action_id)
        values.update(params)
        status, body = self.call('POST', '/api/preview', {'action_id': action_id, 'params': values})
        self.assertEqual(200, status, body)
        return values, body

    def run_action(self, action_id, values, spec_id):
        return self.call('POST', '/api/run', {'action_id': action_id, 'params': values,
                                              'spec_id': spec_id}, timeout=10)

    def start_navigation(self):
        values, preview = self.preview('nav.start', map='proposed_system')
        status, body = self.run_action('nav.start', values, preview['spec_id'])
        self.assertEqual(200, status, body)
        self.assertEqual('running', self.backend.status()['state'])
        return self.supervisors.last

    def wait_for(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.01)
        return False

    def assert_schema_error(self, status, body, expected):
        self.assertEqual(expected, status, body)
        self.assertIsInstance(body, dict, body)
        self.assertTrue(body.get('error') and body.get('reason'), body)

    # --- authentication ------------------------------------------------------------------------
    def test_every_api_route_refuses_missing_wrong_or_query_tokens(self):
        for token in (None, '', 'b' * 31, 'b' * 33, 'B' * 32, 'x' * 32, 'b' * 31 + 'é'):
            for method, path in (('GET', '/api/status'), ('GET', '/api/actions'), ('GET', '/api/results'),
                                 ('GET', '/api/file?path=data/environment.txt'),
                                 ('POST', '/api/stop'), ('POST', '/api/teleop'), ('POST', '/api/run')):
                try:
                    status, body = self.call(method, path, {} if method == 'POST' else None, token=token)
                except UnicodeEncodeError:
                    continue  # http.client refuses non-Latin-1 headers before sending
                self.assert_schema_error(status, body, 403)
        # The header token is the only one accepted outside the event stream.
        status, body = self.call('GET', '/api/status?token=' + self.token, token=None)
        self.assert_schema_error(status, body, 403)
        # A previous server's token does not open this one.
        status, body = self.call('GET', '/api/status', token='a' * 32)
        self.assert_schema_error(status, body, 403)

    def test_stop_without_token_never_reaches_a_running_session(self):
        supervisor = self.start_navigation()
        status, _ = self.call('POST', '/api/stop', {}, token='wrong')
        self.assertEqual(403, status)
        self.assertEqual('running', self.backend.status()['state'])
        self.assertTrue(supervisor.started)

    # --- malformed requests ----------------------------------------------------------------------
    def test_malformed_bodies_get_a_reason_and_never_a_backend_fault(self):
        for raw in ('{', '[]', '7', '"text"', 'null', '\xff\xfe', '{"action_id": 5}',
                    '{"action_id": "map.start", "params": []}',
                    '{"action_id": "map.start", "params": "world"}',
                    '{"action_id": ["map.start"], "params": {}}',
                    '{"action_id": null}', '{"action_id": "nope.nope", "params": {}}'):
            for path in ('/api/preview', '/api/run'):
                status, body = self.call('POST', path, raw=raw.encode('utf-8', 'surrogateescape')
                                         if isinstance(raw, str) else raw,
                                         headers={'Content-Type': 'application/json'})
                self.assertIn(status, (404, 409, 422), (path, raw, body))
                self.assert_schema_error(status, body, status)

    def test_bad_content_lengths_are_refused(self):
        for length in ('-1', 'abc', str(10 ** 9), str(64 * 1024 + 1)):
            with socket.create_connection(self.server.server_address, timeout=5) as sock:
                sock.sendall(('POST /api/preview HTTP/1.1\r\nHost: x\r\nX-Console-Token: ' + self.token +
                              '\r\nContent-Length: ' + length + '\r\n\r\n').encode())
                reply = sock.recv(4096).decode()
            self.assertIn(' 422 ', reply.splitlines()[0], (length, reply))

    def test_a_client_that_never_sends_its_body_is_dropped(self):
        # A slow or broken client must not hold a server thread forever.
        handler = self.server.RequestHandlerClass
        self.assertTrue(0 < handler.timeout <= 60, handler.timeout)
        shipped, handler.timeout = handler.timeout, 1
        try:
            started = time.monotonic()
            with socket.create_connection(self.server.server_address, timeout=6) as sock:
                sock.sendall(('POST /api/preview HTTP/1.1\r\nHost: x\r\nX-Console-Token: ' + self.token +
                              '\r\nContent-Length: 100\r\n\r\n{"act').encode())
                reply = b''
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        reply += chunk
                except socket.timeout:
                    self.fail('The server kept a half-sent request open for 6 s.')
            self.assertLess(time.monotonic() - started, 5)
        finally:
            handler.timeout = shipped

    def test_unsupported_methods_and_paths(self):
        for method in ('PUT', 'DELETE', 'PATCH'):
            status, _ = self.call(method, '/api/status')
            self.assertGreaterEqual(status, 400)
        for path in ('/../../etc/passwd', '/index.html', '/api', '/api/', '/api/status/../actions',
                     '/' + 'a' * 5000):
            status, _ = self.call('GET', path)
            self.assertIn(status, (403, 404, 414), path)

    # --- file access ----------------------------------------------------------------------------------
    def test_file_and_open_refuse_everything_outside_the_record_folders(self):
        (self.repo.root / 'data/runs/r1').mkdir(parents=True)
        (self.repo.root / 'data/runs/r1/notes.md').write_text('ok\n')
        (self.repo.root / 'data/runs/r1/big.log').write_bytes(b'x' * (1024 * 1024 + 1))
        (self.repo.root / 'data/runs/r1/script.py').write_text('print(1)\n')
        os.symlink(self.repo.root / 'tools/amr_console/approvals.yaml', self.repo.root / 'data/runs/r1/link.yaml')
        os.symlink('/etc', self.repo.root / 'data/runs/r1/etc')
        status, body = self.call('GET', '/api/file?path=data/runs/r1/notes.md')
        self.assertEqual((200, 'ok\n'), (status, body['text']))
        for path in ('/etc/passwd', '../../etc/passwd', 'data/runs/../../tools/amr_console/approvals.yaml',
                     'data/runs/%2e%2e/%2e%2e/tools/amr_console/approvals.yaml', 'data/runs/r1/notes.md%00.py',
                     'data/runs/r1/big.log', 'data/runs/r1/script.py', 'data/runs/r1/link.yaml',
                     'data/runs/r1/etc/passwd', 'data/runs/r1', 'data/runs/r1/missing.md', '', '~/.bashrc',
                     'tools/amr_console/approvals.yaml', 'data/environment.txt'):
            status, body = self.call('GET', '/api/file?path=' + path)
            self.assertIn(status, (403, 404, 422), (path, body))
            self.assertNotIn('proposed_system_places', json.dumps(body))
        for path in ('/etc', '../..', 'data/runs/r1/etc', 'data/runs/r1/link.yaml', None, 5, ['data']):
            status, body = self.call('POST', '/api/open', {'path': path})
            self.assertIn(status, (403, 404, 422), (path, body))

    # --- parameters --------------------------------------------------------------------------------
    def test_hostile_parameters_are_refused_before_any_command_is_built(self):
        cases = [('map.start', {'world': 'Demo office (18 × 12 m)'}),   # a label is not an id
                 ('map.start', {'world': '../../etc'}),
                 ('map.start', {'world': 'proposed_system; rm -rf ~'}),
                 ('map.explore', {'world': 'tb3_sandbox', 'method': 'ours'}),  # no exact map
                 ('map.explore', {'world': 'proposed_system', 'method': 'ours --out /'}),
                 ('nav.start', {'map': 'proposed_system', 'controller': 'mppi $(reboot)'}),
                 ('nav.start', {'map': 'proposed_system', 'controller': 'mppi', 'extra': 1})]
        for action_id, params in cases:
            status, body = self.call('POST', '/api/preview', {'action_id': action_id, 'params': params})
            self.assert_schema_error(status, body, 422)

    def test_session_parameters_reject_injection_and_nonfinite_numbers(self):
        supervisor = self.start_navigation()
        for name in ('a; rm -rf ~', '../escape', 'UPPER', 'ab', 'a' * 41, 'map name', '', None, 5, 'é_map'):
            # map.save needs a mapping session; the name check must still refuse first or block.
            status, body = self.call('POST', '/api/preview', {'action_id': 'map.save', 'params': {'name': name}})
            self.assertIn(status, (409, 422), (name, body))
        for raw in ('{"action_id": "nav.goal", "params": {"x": NaN, "y": 0, "yaw": 0}}',
                    '{"action_id": "nav.goal", "params": {"x": Infinity, "y": 0, "yaw": 0}}',
                    '{"action_id": "nav.goal", "params": {"x": 1e999, "y": 0, "yaw": 0}}',
                    '{"action_id": "nav.goal", "params": {"x": "1", "y": 0, "yaw": 0}}',
                    '{"action_id": "nav.goal", "params": {"x": true, "y": 0, "yaw": 0}}',
                    '{"action_id": "nav.goal", "params": {"x": 500, "y": 500, "yaw": 0}}'):
            status, body = self.call('POST', '/api/preview', raw=raw)
            self.assert_schema_error(status, body, 422)
        for text in (None, 5, ['go'], {'to': 'office'}, '', '   ', 'x' * 301, 'go\x00', 'go\nto', 'go\x1b[2J'):
            status, body = self.call('POST', '/api/preview',
                                     {'action_id': 'commands.typed', 'params': {'text': text}})
            self.assert_schema_error(status, body, 422)
        # Shell characters in typed text are data: the preview is an operation, never a shell command.
        values, preview = self.preview('commands.typed', text='go to `reboot` $(rm -rf ~); storage')
        self.assertEqual('operation', preview['kind'])
        self.assertIsNone(preview['blocked_reason'])
        self.assertTrue(supervisor.started)

    def test_drive_values_are_clamped_and_hostile_ones_refused(self):
        values, preview = self.preview('map.start')
        self.assertEqual(200, self.run_action('map.start', values, preview['spec_id'])[0])
        supervisor = self.supervisors.last
        for raw in ('{"linear_x": NaN, "angular_z": 0}', '{"linear_x": Infinity, "angular_z": 0}',
                    '{"linear_x": "fast", "angular_z": 0}', '{"linear_x": true, "angular_z": 0}',
                    '{"linear_x": null, "angular_z": 0}', '{"linear_x": [1], "angular_z": 0}', '{}'):
            status, body = self.call('POST', '/api/teleop', raw=raw)
            self.assert_schema_error(status, body, 422)
        status, body = self.call('POST', '/api/teleop', {'linear_x': 1e300, 'angular_z': -1e300})
        self.assertEqual(200, status, body)
        sent = [data for kind, data in supervisor.controls if kind == 'teleop'][-1]
        self.assertLessEqual(abs(sent['linear_x']), 0.26)
        self.assertLessEqual(abs(sent['angular_z']), 1.82)

    # --- reviewed specs and races ---------------------------------------------------------------------
    def test_spec_ids_cannot_be_forged_replayed_or_moved_to_another_action(self):
        values, preview = self.preview('map.start')
        other_values, other = self.preview('nav.start', map='proposed_system')
        for action_id, params, spec_id in (('map.start', values, 's_' + '0' * 32),
                                           ('map.start', values, None),
                                           ('map.start', values, other['spec_id']),
                                           ('map.start', {'world': 'tb3_sandbox'}, preview['spec_id'])):
            status, body = self.run_action(action_id, params, spec_id)
            self.assert_schema_error(status, body, 409)
        self.assertEqual(200, self.run_action('map.start', values, preview['spec_id'])[0])
        self.backend.stop()
        status, body = self.run_action('map.start', values, preview['spec_id'])
        self.assert_schema_error(status, body, 409)
        self.assertEqual(1, len(self.supervisors.instances))

    def test_twenty_simultaneous_runs_start_exactly_one_session(self):
        previews = [self.preview('map.start') for _ in range(10)] + \
                   [self.preview('nav.start', map='proposed_system') for _ in range(10)]
        barrier = threading.Barrier(len(previews))
        results = []

        def go(index):
            values, preview = previews[index]
            action_id = 'map.start' if index < 10 else 'nav.start'
            barrier.wait()
            results.append(self.run_action(action_id, values, preview['spec_id'])[0])

        threads = [threading.Thread(target=go, args=(i,)) for i in range(len(previews))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(1, results.count(200), results)
        self.assertEqual(len(previews) - 1, results.count(409), results)
        self.assertEqual(1, len(self.supervisors.instances))

    def test_two_typed_commands_at_once_move_the_robot_only_once(self):
        supervisor = self.start_navigation()
        supervisor.respond_to_operations = False
        first = self.preview('commands.typed', text='go to the storage')
        second = self.preview('commands.typed', text='go to the cafeteria')
        replies = {}

        def send(key, item):
            replies[key] = self.run_action('commands.typed', item[0], item[1]['spec_id'])

        one = threading.Thread(target=send, args=('first', first))
        one.start()
        self.assertTrue(self.wait_for(lambda: any(k == 'operation' for k, _ in supervisor.controls)))
        status, body = self.run_action('commands.typed', second[0], second[1]['spec_id'])
        self.assert_schema_error(status, body, 409)
        self.assertEqual(1, sum(1 for k, _ in supervisor.controls if k == 'operation'))
        # The page can see that an operation is in progress.
        status, body = self.call('GET', '/api/status')
        self.assertEqual(200, status)
        self.assertTrue(body['session']['operation_running'], body)
        request_id = next(d['request_id'] for k, d in supervisor.controls if k == 'operation')
        supervisor.complete_operation(request_id)
        one.join(timeout=5)
        self.assertEqual(200, replies['first'][0], replies)
        status, body = self.call('GET', '/api/status')
        self.assertFalse(body['session']['operation_running'], body)
        # And the next request is no longer blocked.
        _, again = self.preview('commands.typed', text='go to the cafeteria')
        self.assertIsNone(again['blocked_reason'])

    def test_status_and_stop_answer_while_the_robot_is_driving(self):
        supervisor = self.start_navigation()
        supervisor.respond_to_operations = False
        values, preview = self.preview('commands.typed', text='go to the storage')
        reply = {}
        sender = threading.Thread(target=lambda: reply.setdefault(
            'r', self.run_action('commands.typed', values, preview['spec_id'])))
        sender.start()
        self.assertTrue(self.wait_for(lambda: any(k == 'operation' for k, _ in supervisor.controls)))
        started = time.monotonic()
        self.assertEqual(200, self.call('GET', '/api/status')[0])
        self.assertEqual(200, self.call('GET', '/api/actions')[0])
        self.assertLess(time.monotonic() - started, 2)
        status, body = self.call('POST', '/api/stop', {})
        self.assertEqual(200, status, body)
        sender.join(timeout=5)
        self.assertFalse(sender.is_alive())
        self.assertEqual(409, reply['r'][0], reply)        # the drive request ends as "session ended"
        self.assertEqual('session_ended', reply['r'][1]['error'])
        self.assertEqual('idle', self.backend.status()['state'])
        # A new session can start straight away.
        self.start_navigation()

    def test_stop_twice_and_stop_when_idle_are_harmless(self):
        for _ in range(2):
            status, body = self.call('POST', '/api/stop', {})
            self.assertEqual(200, status, body)
        self.start_navigation()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.call('POST', '/api/stop', {})[0]))
                   for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([200] * 5, results)
        self.assertEqual('idle', self.backend.status()['state'])

    # --- event streams ------------------------------------------------------------------------------
    def test_many_event_streams_do_not_starve_the_api(self):
        sockets = []
        try:
            for _ in range(100):
                sock = socket.create_connection(self.server.server_address, timeout=5)
                sock.sendall(('GET /api/events?token=' + self.token + ' HTTP/1.1\r\nHost: x\r\n\r\n').encode())
                sockets.append(sock)
            self.assertIn(b'200', sockets[-1].recv(64))
            started = time.monotonic()
            status, _ = self.call('GET', '/api/status')
            self.assertEqual(200, status)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            for sock in sockets:
                sock.close()
        # Closed streams leave the server healthy.
        self.assertEqual(200, self.call('GET', '/api/status')[0])


if __name__ == '__main__':
    unittest.main()
