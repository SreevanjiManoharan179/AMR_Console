#!/usr/bin/env python3
"""Loopback-only standard-library HTTP/SSE AMR console, contract v1.2."""
import argparse
import errno
import sys
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from engine import ConsoleBackend
from errors import ConsoleError
from events import stream_frames
import runtime

MAX_REQUEST = 64 * 1024
# One short spoken request: 15 s of 16 kHz mono 16-bit WAV is about 0.5 MB.
MAX_AUDIO = 1024 * 1024
# A client that stops sending mid-request is dropped after this many seconds, so it cannot hold a
# server thread forever. Event streams are unaffected: the server writes to them, it never waits.
REQUEST_TIMEOUT_S = 20


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, _format, *_args):
        # Request URLs include the SSE token: do not log them.
        pass

    def send_json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def authenticate(self, split):
        supplied = (parse_qs(split.query).get('token', [''])[0] if split.path == '/api/events'
                    else self.headers.get('X-Console-Token', ''))
        # Compare bytes: a non-ASCII token is simply wrong, not a server error.
        if not hmac.compare_digest(str(supplied).encode('utf-8', 'surrogateescape'), self.server.token.encode()):
            raise ConsoleError(403, 'bad_token',
                "The console link's token is wrong or expired. Open the link printed when the server started.")

    def read_body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ConsoleError(422, 'bad_json', 'Content-Length must be a valid integer.')
        if length < 0 or length > MAX_REQUEST:
            raise ConsoleError(422, 'bad_json', 'The JSON request must be at most 64 KB.')
        try:
            value = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        except (ValueError, UnicodeError):
            raise ConsoleError(422, 'bad_json', 'Send a valid JSON object.')
        if not isinstance(value, dict):
            raise ConsoleError(422, 'bad_json', 'Send a JSON object.')
        return value

    def read_audio(self):
        # A refused body is left unread: close the connection after the reply so it cannot be
        # mistaken for the next request.
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'audio/wav':
            self.close_connection = True
            raise ConsoleError(415, 'speech_type', 'Send the recording as audio/wav.')
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.close_connection = True
            raise ConsoleError(411, 'speech_length', 'The recording needs a Content-Length.')
        if length <= 0 or length > MAX_AUDIO:
            self.close_connection = True
            raise ConsoleError(413, 'speech_size', 'The recording must be 1 MB at most.')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ConsoleError(422, 'speech_size', 'The recording arrived incomplete.')
        return raw

    def do_GET(self):
        try:
            split = urlsplit(self.path)
            if split.path == '/':
                raw = (self.server.backend.root / 'tools/amr_console/web/index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(raw)
                return
            if not split.path.startswith('/api/'):
                raise ConsoleError(404, 'not_found', 'That console path does not exist.')
            self.authenticate(split)
            backend = self.server.backend
            if split.path == '/api/events':
                self.event_stream()
                return
            methods = {'/api/status': backend.status, '/api/actions': backend.actions,
                       '/api/results': backend.results, '/api/office': backend.office.snapshot}
            if split.path == '/api/file':
                value = backend.file(parse_qs(split.query).get('path', [''])[0])
            elif split.path in methods:
                value = methods[split.path]()
            else:
                raise ConsoleError(404, 'not_found', 'That console endpoint does not exist.')
            self.send_json(value)
        except ConsoleError as exc:
            self.send_json(exc.as_dict(), exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_json({'error': 'backend_fault', 'reason': f'Backend request failed: {exc}; inspect the server terminal.'}, 500)

    def do_POST(self):
        self.body_read = False
        try:
            split = urlsplit(self.path)
            if not split.path.startswith('/api/'):
                raise ConsoleError(404, 'not_found', 'That console path does not exist.')
            self.authenticate(split)
            backend = self.server.backend
            if split.path == '/api/office/speech':
                # A recording, not JSON: written down on this computer and returned as text only.
                query = parse_qs(split.query)
                raw = self.read_audio()
                self.body_read = True
                self.send_json(backend.office.transcribe(raw, query.get('language', ['auto'])[0],
                                                         query.get('hint', [None])[0]))
                return
            body = self.read_body()
            self.body_read = True
            if split.path == '/api/preview':
                value = backend.preview(body.get('action_id'), body.get('params', {}))
            elif split.path == '/api/run':
                value = backend.run(body.get('action_id'), body.get('params', {}),
                                    body.get('confirm_text'), body.get('spec_id'))
            elif split.path == '/api/stop':
                value = backend.stop()
            elif split.path == '/api/continue':
                value = backend.continue_run()
            elif split.path == '/api/teleop':
                value = backend.teleop(body.get('linear_x'), body.get('angular_z'))
            elif split.path == '/api/open':
                value = backend.open(body.get('path'))
            elif split.path == '/api/windows/arrange':
                if body.get('layout') != 'gazebo-left-rviz-right':
                    raise ConsoleError(422, 'layout', 'Choose gazebo-left-rviz-right.')
                value = arrange_windows(backend)
            elif split.path == '/api/office/service':
                if body.get('action') == 'start':
                    value = backend.office.start(body.get('mode', 'robot'))
                elif body.get('action') == 'stop':
                    value = backend.office.stop_service()
                else:
                    raise ConsoleError(422, 'office_action', 'Choose start or stop.')
            elif split.path == '/api/office/request':
                value = backend.office.submit(body.get('text'), body.get('requester'),
                                              body.get('language'), body.get('place'))
            elif split.path == '/api/office/reply':
                value = backend.office.reply(body.get('input_id'), body.get('text'))
            elif split.path == '/api/office/cancel':
                value = backend.office.cancel()
            else:
                raise ConsoleError(404, 'not_found', 'That console endpoint does not exist.')
            self.send_json(value)
        except ConsoleError as exc:
            if not self.body_read:
                # Refused before its body was read (a wrong token, say): close the connection after
                # the reply, so the unread body is never taken for the next request.
                self.close_connection = True
            self.send_json(exc.as_dict(), exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_json({'error': 'backend_fault', 'reason': f'Backend request failed: {exc}; inspect the server terminal.'}, 500)

    def event_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            cursor = int(self.headers.get('Last-Event-ID', '0'))
        except ValueError:
            cursor = 0
        try:
            for frame in stream_frames(self.server.backend.events, cursor):
                if self.server.closing.is_set():
                    break
                self.wfile.write(frame.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True


def arrange_windows(backend):
    return backend.arrange()


class ConsoleHTTPServer(ThreadingHTTPServer):
    # Room for a burst of page requests and event streams; the default backlog of 5 resets them.
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # A browser that closes a tab or drops off the network mid-reply is normal: no traceback.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(backend, token, port=0):
    server = ConsoleHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.backend, server.token = backend, token
    server.closing = threading.Event()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be within 0–65535')
    token = secrets.token_hex(16)
    try:
        server = make_server(None, token, args.port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        # Every AMR console (this one and the earlier lab console) uses this port, and only one console
        # may drive the simulation at a time. Say so plainly, before anything else has started.
        print(f'Port {args.port} is already in use: another console is running (every AMR console uses '
              'this port). Close that console window first, then start this one again.', flush=True)
        sys.exit(1)
    backend = server.backend = ConsoleBackend()
    print(f'AMR Console: http://127.0.0.1:{server.server_port}/?token={token}', flush=True)
    shutting_down = threading.Event()
    def shutdown(_sig, _frame):
        if shutting_down.is_set():
            return
        shutting_down.set()
        def finish():
            try:
                backend.stop()
            except Exception as exc:
                print(f'Cleanup requires review: {exc}', flush=True)
            backend.office.shutdown()
            server.closing.set()
            server.shutdown()
        threading.Thread(target=finish, daemon=True).start()
    # Ctrl+C, a normal kill, or closing the terminal window: STOP the session in order, then exit.
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, shutdown)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.closing.set()
        server.server_close()


if __name__ == '__main__':
    main()
