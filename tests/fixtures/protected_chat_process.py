import json
import sys


print(json.dumps({'type': 'ready', 'config': {
    'mode': 'remote', 'model': 'fixture', 'model_path': '',
    'subscription_model': '', 'theme_color': 'blue',
    'reduced_motion': False, 'voice_mode': 'remote', 'voice_url': ''}}), flush=True)
for raw in sys.stdin:
    value = json.loads(raw)
    action = value.get('action')
    if action == 'send':
        content = value['messages'][-1]['content']
        if content == 'SCHEDULE':
            print(json.dumps({'type': 'scheduled_jobs', 'id': 'relay',
                              'request': {'action': 'health'}}), flush=True)
            response = json.loads(sys.stdin.readline())
            print(json.dumps({'type': 'token', 'text': response['response']['status']}), flush=True)
        else:
            print(json.dumps({'type': 'token', 'text': 'PRIVATE REPLY'}), flush=True)
        print(json.dumps({'type': 'done'}), flush=True)
    elif action == 'stop':
        print(json.dumps({'type': 'error', 'text': 'Stopped'}), flush=True)
