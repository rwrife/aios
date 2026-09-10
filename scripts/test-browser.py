#!/usr/bin/env python3
"""Run as a normal user in an X11 session with aios-browser installed."""
import http.server
import json
import threading
from aios.browser import Browser

PAGE=b'''<!doctype html><title>AIOS Browser QA</title><h1>Browser controls test</h1><label>Name <input id="name"></label><button onclick="document.querySelector('#result').innerText='Hello '+document.querySelector('#name').value">Greet</button><p id="result"></p><a href="/next">Next page</a>'''
PAGE += b'<div style="height:1800px"></div><button>Bottom control</button><p>Bottom marker</p>'
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(PAGE if self.path=='/' else b'<title>Next page</title><h1>Navigation works</h1>')
    def log_message(self,*_): pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
browser=Browser()
try:
    url=f'http://127.0.0.1:{server.server_port}/'
    page=browser.act({'action':'open','url':url})
    assert page['title']=='AIOS Browser QA',page
    entry=next(c['element'] for c in page['controls'] if c['tag']=='input')
    page=browser.act({'action':'type','element':entry,'text':'AIOS'})
    button=next(c['element'] for c in page['controls'] if c['label']=='Greet')
    page=browser.act({'action':'click','element':button})
    assert 'Hello AIOS' in page['text'],page
    link=next(c['element'] for c in page['controls'] if c['label']=='Next page')
    page=browser.act({'action':'click','element':link})
    assert page['title']=='Next page',page
    assert browser.act({'action':'back'})['title']=='AIOS Browser QA'
    tabs=browser.act({'action':'tabs'})['tabs']; assert tabs==['main']
    browser.act({'action':'switch','tab':'main'})
    page=browser.act({'action':'navigate','url':url})
    assert not any(c['label']=='Bottom control' for c in page['controls'])
    for _ in range(5):
        page=browser.act({'action':'scroll','direction':'down'})
    assert 'Bottom marker' in page['text'],page
    assert any(c['label']=='Bottom control' for c in page['controls'])
    try:
        browser.act({'action':'navigate','url':'http://127.0.0.1:1/'})
    except RuntimeError as error:
        assert 'navigation failed' in str(error).lower(),error
    else:
        raise AssertionError('Failed navigation was reported as successful')
    print('PASS: themed WebEngine browser open, read, type, click, navigation and viewport scrolling',flush=True)
finally:
    browser.close(); server.shutdown()
