"""Protocol peer used by subscription integration tests; never contacts OpenAI."""
import json
import os
import subprocess
import sys
import time

scenario = os.environ.get('AIOS_FAKE_SCENARIO', '')
thread = 'test-thread'
tool_index = 0


TOOL_SEQUENCES = {
    'browser': [('browser', {'action': 'snapshot'})],
    'application': [('application', {'action': 'search', 'query': 'calculator'})],
    'mcp-error': [('mcp_fixture_echo', {'value': 'fail'})],
    'unknown-generic': [('shell', {'command': 'whoami'})],
    'generic-error': [('application', {'action': 'search', 'query': 'secret'})],
    'activate-narrow': [
        ('activate_skill', {'name': 'application-builder'}),
        ('browser', {'action': 'snapshot'}),
    ],
    'call-limit': [('application', {'action': 'search', 'query': str(index)})
                   for index in range(33)],
}


def send(value):
    print(json.dumps(value), flush=True)


def notify(method, params):
    send({'method': method, 'params': params})


def tool_request(index):
    tool, arguments = TOOL_SEQUENCES[scenario][index]
    send({'id': 'tool-request' if index == 0 else f'tool-request-{index}',
          'method': 'item/tool/call', 'params': {
              'threadId': thread, 'tool': tool, 'arguments': arguments}})


def finish_tool_scenario():
    messages = {
        'browser': 'Browser done',
        'application': 'Application done',
        'mcp-error': 'MCP recovered',
        'unknown-generic': 'Unknown tool recovered',
        'generic-error': 'Generic error recovered',
        'activate-narrow': 'Activation narrowed tools',
        'call-limit': 'Call limit missed',
    }
    notify('item/agentMessage/delta', {'threadId': thread, 'delta': messages[scenario]})
    notify('turn/completed', {'threadId': thread, 'turn': {'status': 'completed'}})


for line in sys.stdin:
    value = json.loads(line)
    with open(os.environ['AIOS_FAKE_LOG'], 'a') as log:
        log.write(json.dumps(value) + '\n')
    method = value.get('method')
    if method == 'initialized':
        continue
    if method is None:
        if scenario in TOOL_SEQUENCES:
            result = value.get('result', {})
            if scenario == 'mcp-error':
                assert result.get('success') is False
            if scenario == 'unknown-generic':
                assert result.get('success') is False
            if scenario == 'activate-narrow' and tool_index == 0:
                assert result.get('success') is True
            if scenario == 'activate-narrow' and tool_index == 1:
                assert result.get('success') is False
            tool_index += 1
            if tool_index < len(TOOL_SEQUENCES[scenario]):
                tool_request(tool_index)
            elif scenario != 'call-limit':
                finish_tool_scenario()
        elif scenario == 'server-request' and value.get('id') == 'server-request':
            notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'Hello 世界'})
            notify('turn/completed', {'threadId': thread, 'turn': {'status': 'completed'}})
        elif scenario == 'cross-thread-call' and value.get('id') == 'tool-request':
            notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'Cross-thread ignored'})
            notify('turn/completed', {'threadId': thread, 'turn': {'status': 'completed'}})
        continue
    result = {}
    if method == 'initialize':
        assert not os.environ.get('OPENAI_API_KEY')
        assert not os.environ.get('CODEX_API_KEY')
    elif method == 'account/read':
        result = {'account': None if scenario == 'signed-out' else
                  {'type': 'chatgpt', 'email': 'test@example.com', 'planType': 'plus', 'secret': 'do-not-emit'}}
    elif method == 'model/list':
        cursor = value['params'].get('cursor')
        result = {'data': [{'model': 'test-two' if cursor else 'test-one', 'displayName': 'Test'}],
                  'nextCursor': None if cursor else 'page-two'}
    elif method == 'account/rateLimits/read':
        result = {'rateLimits': {'primary': {'usedPercent': 25, 'resetsAt': 1800000000}}}
    elif method == 'account/login/start':
        result = {'loginId': 'login', 'authUrl': 'https://auth.openai.com/authorize',
                  'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'TEST-CODE'}
        # Deliberately notify before the RPC response to check event buffering.
        if scenario != 'wait-login':
            notify('account/login/completed', {'loginId': 'login', 'success': scenario != 'failed-login'})
    elif method == 'thread/start':
        assert value['params']['ephemeral'] is True
        assert value['params']['environments'] == []
        assert value['params']['sandbox'] == 'read-only'
        result = {'bad': True} if scenario == 'malformed-thread' else {'thread': {'id': thread}}
    elif method == 'turn/start':
        result = {'bad': True} if scenario == 'malformed-turn' else {'turn': {'id': 'turn'}}
    if scenario == 'rpc-error' and method == 'model/list':
        send({'id': value['id'], 'error': {'message': 'secret-provider-token'}})
    else:
        send({'id': value['id'], 'result': result})
    if method == 'turn/start':
        if scenario == 'disconnect':
            break
        if scenario == 'malformed-turn':
            notify('turn/completed', {'threadId': thread, 'turn': {'status': 'completed'}})
        if scenario in TOOL_SEQUENCES:
            tool_request(0)
        elif scenario == 'malformed-args':
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'tool': 'application', 'arguments': []}})
        elif scenario == 'malformed-tool':
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'tool': 7, 'arguments': {}}})
        elif scenario == 'malformed-params':
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': []})
        elif scenario == 'malformed-method':
            send({'id': 'tool-request', 'method': 7, 'params': {
                'threadId': thread, 'tool': 'application', 'arguments': {}}})
        elif scenario == 'oversized-args':
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'tool': 'application',
                'arguments': {'padding': 'x' * (24 * 1024)}}})
        elif scenario == 'oversized-delta':
            notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'x' * (256 * 1024 + 1)})
        elif scenario == 'non-string-delta':
            notify('item/agentMessage/delta', {'threadId': thread, 'delta': 7})
        elif scenario == 'malformed-completed':
            notify('turn/completed', {'threadId': thread, 'turn': {'status': 7}})
        elif scenario == 'cross-thread-call':
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': 'other-thread', 'tool': 'application', 'arguments': {}}})
        elif scenario == 'blocked-tool-response':
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'tool': 'application',
                'arguments': {'action': 'search', 'query': 'large'}}})
            # Stop reading while the adapter attempts a response larger than the pipe capacity.
            time.sleep(60)
        elif scenario == 'grandchild-holds-stdout':
            child = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(60)'],
                stdin=subprocess.DEVNULL,
            )
            path = os.environ.get('AIOS_FAKE_GRANDCHILD_PID')
            if path:
                with open(path, 'w', encoding='utf-8') as stream:
                    stream.write(str(child.pid))
            notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'Hello 世界'})
            time.sleep(60)
        elif scenario not in ('malformed-thread', 'malformed-turn'):
            if scenario == 'server-request':
                send({'id': 'server-request', 'method': 'unsupported/request',
                      'params': {'threadId': thread}})
            else:
                notify('item/agentMessage/delta', {'threadId': 'other-thread', 'delta': 'WRONG'})
                notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'Hello 世界'})
                if scenario != 'wait-turn':
                    notify('turn/completed', {'threadId': thread, 'turn': {
                        'status': 'failed' if scenario == 'failed-turn' else 'completed'}})
