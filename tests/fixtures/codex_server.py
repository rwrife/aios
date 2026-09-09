"""Protocol peer used by subscription integration tests; never contacts OpenAI."""
import json
import os
import sys

scenario = os.environ.get('AIOS_FAKE_SCENARIO', '')
thread = 'test-thread'


def send(value):
    print(json.dumps(value), flush=True)


def notify(method, params):
    send({'method': method, 'params': params})


for line in sys.stdin:
    value = json.loads(line)
    with open(os.environ['AIOS_FAKE_LOG'], 'a') as log:
        log.write(json.dumps(value) + '\n')
    method = value.get('method')
    if method == 'initialized':
        continue
    if method is None:
        notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'Browser done'})
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
        result = {'thread': {'id': thread}}
    elif method == 'turn/start':
        result = {'turn': {'id': 'turn'}}
    if scenario == 'rpc-error' and method == 'model/list':
        send({'id': value['id'], 'error': {'message': 'secret-provider-token'}})
    else:
        send({'id': value['id'], 'result': result})
    if method == 'turn/start':
        if scenario == 'disconnect':
            break
        if scenario in ('browser', 'unknown-tool'):
            send({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'tool': 'browser' if scenario == 'browser' else 'shell',
                'arguments': {'action': 'snapshot'}}})
        else:
            notify('item/agentMessage/delta', {'threadId': 'other-thread', 'delta': 'WRONG'})
            notify('item/agentMessage/delta', {'threadId': thread, 'delta': 'Hello 世界'})
            if scenario != 'wait-turn':
                notify('turn/completed', {'threadId': thread, 'turn': {
                    'status': 'failed' if scenario == 'failed-turn' else 'completed'}})
