#!/usr/bin/env python3
"""Deterministic native-browser/MCP fixture with a separately grouped child."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def descendants():
    child = subprocess.Popen(
        [sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'],
        start_new_session=True)
    path = os.environ.get('SCHEDULED_TEST_PIDS')
    if path:
        with open(path, 'a') as stream:
            stream.write(f'{os.getpid()}\n{child.pid}\n')
            stream.flush()


if '--socket' in sys.argv:
    descendants()
    path = sys.argv[sys.argv.index('--socket') + 1]
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen(4)
        while True:
            client, _ = server.accept()
            with client:
                value = json.loads(client.makefile('rb').readline())
                client.sendall(json.dumps({'result': {'ok': True, 'fixture': 'browser'}}).encode() + b'\n')
                if value['action'] == 'close':
                    break
else:
    for line in sys.stdin:
        value = json.loads(line)
        if 'id' not in value:
            continue
        method = value['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'ping', 'description': 'Return the fixture answer.',
                                'inputSchema': {'type': 'object', 'properties': {}}}]}
        else:
            descendants()
            if os.environ.get('SCHEDULED_TEST_HANG') == '1':
                time.sleep(120)
            result = {'content': [{'type': 'text', 'text': 'fixture tool result'}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': value['id'], 'result': result}), flush=True)
