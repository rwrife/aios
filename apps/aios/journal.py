"""Per-owner work journal. The broker owns the DB; apps receive only artifacts."""
import json
import sqlite3
import time
import uuid
from pathlib import Path


class Journal:
    def __init__(self, root, owner):
        self.owner = owner
        path = Path(root) / 'journal.sqlite3'
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Unsupported journal version")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS work_sessions (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, updated REAL NOT NULL, summary TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS session_events (
            id INTEGER PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS application_manifests (
            session TEXT NOT NULL, app TEXT NOT NULL, arguments TEXT NOT NULL,
            PRIMARY KEY(session, app, arguments));
          CREATE TABLE IF NOT EXISTS artifacts (
            session TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
            PRIMARY KEY(session, path));
          PRAGMA user_version=1;
        ''')
        # Processes cannot survive a broker restart as authorized work.
        with self.db:
            self.db.execute("UPDATE work_sessions SET status='suspended' WHERE status='active'")

    def close(self):
        self.db.close()

    def create(self, title):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("Enter a short session title")
        session = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO work_sessions VALUES (?,?,?,?,?,?)',
                            (session, self.owner, title.strip(), 'active', time.time(), ''))
            self._event(session, 'created', {})
        return session

    def get(self, session):
        row = self.db.execute('SELECT * FROM work_sessions WHERE id=? AND owner=?',
                              (session, self.owner)).fetchone()
        if not row or row['status'] == 'deleted':
            raise PermissionError("Session unavailable")
        return dict(row)

    def _event(self, session, kind, payload):
        self.db.execute('INSERT INTO session_events(session,kind,payload,created) VALUES (?,?,?,?)',
                        (session, kind, json.dumps(payload), time.time()))

    def transition(self, session, status):
        current = self.get(session)['status']
        allowed = {'active': ('suspended',), 'suspended': ('active', 'archived', 'deleted'),
                   'archived': ('active', 'deleted')}
        if status not in allowed.get(current, ()):
            raise ValueError("Invalid session transition")
        with self.db:
            self.db.execute('UPDATE work_sessions SET status=?,updated=? WHERE id=?',
                            (status, time.time(), session))
            self._event(session, status, {})

    def search(self, query=''):
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Invalid search")
        return [dict(row) for row in self.db.execute('''SELECT * FROM work_sessions
            WHERE owner=? AND status!='deleted' AND (instr(lower(title),lower(?))>0
            OR instr(lower(summary),lower(?))>0) ORDER BY updated DESC LIMIT 50''',
            (self.owner, query, query))]

    def message(self, session, role, content):
        if self.get(session)['status'] != 'active':
            raise PermissionError("Session is not active")
        if role not in ('user', 'assistant') or not isinstance(content, str) or len(content) > 32768:
            raise ValueError("Invalid message")
        with self.db:
            self._event(session, 'message', {'role': role, 'content': content})

    def manifest(self, session, app, arguments):
        self.get(session)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO application_manifests VALUES (?,?,?)',
                            (session, app, json.dumps(arguments)))
            self._event(session, 'application', {'app': app})

    def manifests(self, session):
        self.get(session)
        return [(row['app'], json.loads(row['arguments'])) for row in self.db.execute(
            'SELECT * FROM application_manifests WHERE session=?', (session,))]
