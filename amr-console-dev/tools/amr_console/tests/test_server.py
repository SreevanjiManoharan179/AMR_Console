"""Real loopback HTTP with a process-free backend fixture; no ROS launches."""
import http.client
import json
import threading
import unittest

from engine import ConsoleBackend
from fixtures import ENVIRONMENT, FakeClock, FakeSupervisors, FixtureRepo
from server import make_server
from test_support import assert_guard_active


def setUpModule():
    assert_guard_active()


class ServerTests(unittest.TestCase):
    token = 'a' * 32

    def setUp(self):
        self.repo = FixtureRepo()
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
        self.backend.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.repo.close()

    def request(self, method, path, payload=None, token=token, raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=4)
        headers = {'X-Console-Token': token} if token is not None else {}
        body = raw
        if payload is not None:
            body = json.dumps(payload)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status, content_type = response.status, response.getheader('Content-Type')
        body = response.read().decode()
        connection.close()
        return status, content_type, body

    def json_request(self, method, path, payload=None, token=token, raw=None):
        status, content_type, body = self.request(method, path, payload, token, raw)
        self.assertIn('application/json', content_type)
        return status, json.loads(body)

    def test_server_binds_only_loopback(self):
        self.assertEqual('127.0.0.1', self.server.server_address[0])

    def test_frontend_is_exact_supplied_file_and_needs_no_token(self):
        status, content_type, body = self.request('GET', '/', token=None)
        self.assertEqual(200, status)
        self.assertIn('text/html', content_type)
        expected = (self.repo.root / 'tools/amr_console/web/index.html').read_text()
        self.assertEqual(expected, body)
        self.assertNotIn('/api/handoff', body)        # the handoff route was removed

    def test_all_get_api_routes_need_token_and_include_reason(self):
        for path in ('/api/status', '/api/actions', '/api/results',
                     '/api/file?path=data/logs/console/x/run.json', '/api/events', '/api/unknown'):
            for token in (None, 'expired'):
                with self.subTest(path=path, token=token):
                    status, error = self.json_request('GET', path, token=token)
                    self.assertEqual(403, status)
                    self.assertEqual('bad_token', error['error'])
                    self.assertTrue(error['reason'])

    def test_all_post_api_routes_need_token_before_mutation(self):
        for path in ('/api/preview', '/api/run', '/api/stop', '/api/continue',
                     '/api/teleop', '/api/windows/arrange', '/api/open', '/api/unknown'):
            with self.subTest(path=path):
                status, error = self.json_request('POST', path, {}, token=None)
                self.assertEqual(403, status)
                self.assertTrue(error['reason'])
        self.assertFalse(self.supervisors.instances)

    def test_events_require_query_token_even_with_header(self):
        for path in ('/api/events', '/api/events?token=wrong'):
            status, error = self.json_request('GET', path)
            self.assertEqual(403, status)
            self.assertEqual('bad_token', error['error'])

    def test_removed_handoff_route_is_not_found(self):
        status, error = self.json_request('GET', '/api/handoff')
        self.assertEqual(404, status)
        self.assertEqual('not_found', error['error'])
        self.assertFalse(hasattr(self.backend, 'handoff'))

    def test_authenticated_routes_and_exact_prepared_spec(self):
        status, actions = self.json_request('GET', '/api/actions')
        self.assertEqual(200, status)
        ids = [action['id'] for action in actions['actions']]
        self.assertIn('map.start', ids)
        for removed in ('trials.one', 'trials.visual', 'trials.study', 'nav.stage1'):
            self.assertNotIn(removed, ids)
        status, preview = self.json_request('POST', '/api/preview',
                                            {'action_id': 'map.start', 'params': {}})
        self.assertEqual(200, status)
        self.assertIsNone(preview['blocked_reason'])
        status, result = self.json_request('POST', '/api/run',
                                           {'action_id': 'map.start', 'params': {},
                                            'spec_id': preview['spec_id'], 'confirm_text': None})
        self.assertEqual(200, status)
        self.assertTrue(result['run_id'])
        self.assertEqual(preview['command'], self.supervisors.last.spec['command'])
        status, info = self.json_request('GET', '/api/status')
        self.assertEqual(200, status)
        self.assertEqual('running', info['state'])
        status, cleanup = self.json_request('POST', '/api/stop', {})
        self.assertEqual(200, status)
        self.assertEqual(0, cleanup['survivors'])

    def test_api_interlock_and_validation_errors_keep_schema(self):
        cases = [('/api/preview', {'action_id': 'missing', 'params': {}}, 404),
                 ('/api/preview', {'action_id': 'env.check', 'params': {'bad': True}}, 422),
                 ('/api/run', {'action_id': 'env.check', 'params': {}}, 409),
                 ('/api/teleop', {'linear_x': .1, 'angular_z': 0}, 409),
                 ('/api/continue', {}, 409)]
        for path, payload, expected in cases:
            with self.subTest(path=path, payload=payload):
                status, error = self.json_request('POST', path, payload)
                self.assertEqual(expected, status)
                self.assertTrue(error['error'])
                self.assertTrue(error['reason'])

    def test_invalid_json_and_nonobject_requests_are_422(self):
        for body in ('{broken', '[]', 'null'):
            with self.subTest(body=body):
                status, error = self.json_request('POST', '/api/preview', raw=body)
                self.assertEqual(422, status)
                self.assertTrue(error['reason'])

    def test_arrangement_reports_actual_reason(self):
        status, result = self.json_request('POST', '/api/windows/arrange',
                                          {'layout': 'gazebo-left-rviz-right'})
        self.assertEqual(200, status)
        self.assertFalse(result['supported'])
        self.assertTrue(result['reason'])

    def test_file_reading_and_results_use_allowed_repo_paths(self):
        self.repo.write('data/logs/console/fixture/cleanup.json', '{"survivors": 0}')
        status, result = self.json_request('GET', '/api/file?path=data/logs/console/fixture/cleanup.json')
        self.assertEqual(200, status)
        self.assertIn('survivors', result['text'])
        self.repo.write('docs/archive/old.md', 'old docs record')
        for path in ('docs/../tools/amr_console/approvals.yaml', 'docs/archive/old.md',
                     'data/environment.txt', 'data/runs/../../tools/amr_console/approvals.yaml'):
            with self.subTest(path=path):
                status, error = self.json_request('GET', '/api/file?path=' + path)
                self.assertIn(status, (403, 404, 422))
                self.assertTrue(error['reason'])
        for path in ('docs/archive/old.md', 'data/environment.txt'):
            with self.subTest(open=path):
                status, error = self.json_request('POST', '/api/open', {'path': path})
                self.assertEqual(404, status)
                self.assertEqual('bad_path', error['error'])

    def test_sse_sends_json_data_frame_without_waiting_for_keepalive(self):
        self.backend.events.publish({'type': 'log', 'level': 'info', 'text': 'fixture output', 'ts': 1})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=4)
        connection.request('GET', '/api/events?token=' + self.token)
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.assertIn('text/event-stream', response.getheader('Content-Type'))
        payload = None
        for _ in range(3):
            line = response.readline().decode().strip()
            if line.startswith('data: '):
                payload = json.loads(line[6:])
                break
        self.assertIsNotNone(payload)
        self.assertIn('type', payload)
        connection.close()


class BusyPortTests(unittest.TestCase):
    def test_a_second_console_says_the_port_is_taken_before_starting_anything(self):
        # Both menu apps use 127.0.0.1:8765; the second one must say why it cannot start.
        import contextlib
        import io
        import socket
        import sys
        import unittest.mock
        import server
        holder = socket.socket()
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        out = io.StringIO()
        try:
            with unittest.mock.patch.object(sys, 'argv', ['server.py', '--port', str(port)]), \
                    unittest.mock.patch.object(server, 'ConsoleBackend') as backend, \
                    contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stopped:
                server.main()
        finally:
            holder.close()
        self.assertEqual(1, stopped.exception.code)
        backend.assert_not_called()
        self.assertIn(f'Port {port} is already in use', out.getvalue())
        self.assertIn('another console is running', out.getvalue())
