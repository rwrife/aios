"""Real runtime daemon with a deterministic subprocess in place of llama."""
import http.server
import json
import os
import signal
import subprocess
import sys
import time

from aios import local_runtime


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if any('LOCAL_HANG' in item.get('content', '') for item in request['messages']):
            time.sleep(120)
        event = {'choices': [{'delta': {'content': 'Local background answer'}, 'finish_reason': 'stop'}]}
        self.wfile.write(('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
        self.wfile.flush()


class FixtureModel(local_runtime.ModelProcess):
    def start(self, identity):
        self.stop()
        parent = os.getpid()
        self.process = subprocess.Popen(
            [sys.executable, __file__, 'model'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=lambda: local_runtime._die_with_parent(parent))
        with open(os.environ['SCHEDULED_TEST_MODEL_PIDS'], 'a') as stream:
            stream.write(str(self.process.pid) + '\n')
        self.identity = identity
        self.started = time.monotonic()
        self.last_probe = 0
        self.ready = False


if sys.argv[-1] == 'model':
    http.server.ThreadingHTTPServer(('127.0.0.1', 8080), Handler).serve_forever()
else:
    service = local_runtime.RuntimeService(model=FixtureModel())
    signal.signal(signal.SIGTERM, lambda *_: setattr(service, 'stopping', True))
    service.serve_forever()
