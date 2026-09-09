"""Serialized session state machine shared by the daemon and deterministic tests."""
import hashlib
import secrets
import time
import uuid

from .authority import Capabilities, RULES, pin_record, verify_pin
from .identity import Fusion, identity_id
from .isolation import application
from .journal import Journal


class Sessions:
    def __init__(self, isolation, store, clock=time.monotonic, wall=time.time,
                 privacy_timeout=3, suspension_timeout=30, anonymous_timeout=120):
        if not 0 < privacy_timeout < suspension_timeout or anonymous_timeout <= 0:
            raise ValueError("Invalid presence timeouts")
        self.isolation, self.store, self.clock, self.wall = isolation, store, clock, wall
        self.privacy_timeout, self.suspension_timeout = privacy_timeout, suspension_timeout
        self.anonymous_timeout = anonymous_timeout
        self.fusion = Fusion(clock, ttl=privacy_timeout)
        self.capabilities = Capabilities(clock)
        self.owner = self.lease = self.work = self.root = self.journal = None
        self.uid = None
        self.shield = False
        self.fault = False
        self.last_presence = self.last_activity = clock()
        self.challenge = None
        self.verified_until = 0
        self.manual_owner = None
        self.manual_until = 0
        self.pending_restoration = []
        self.enrollment_blocked = False
        self._reconcile_enrollment()

    def _reconcile_enrollment(self):
        pending = self.store.get('pending-enrollment')
        if not pending:
            return
        owner = identity_id(pending['identity'])
        # An allocation without a committed principal mapping is never adopted,
        # reformatted or deleted automatically. Existing profiles remain usable.
        if not self.isolation.provisioned(owner):
            self.enrollment_blocked = True
            return
        record = pending['record']
        names = self.store.get('identities', {})
        if any(key != owner and value.casefold() == record['name'].casefold()
               for key, value in names.items()):
            raise PermissionError('Enrollment index conflict requires administrator recovery')
        existing = self.store.get('identity-' + owner)
        if existing is not None and existing != record:
            raise PermissionError('Enrollment record conflict requires administrator recovery')
        self.store.put('identity-' + owner, record)
        names[owner] = record['name']
        self.store.put('identities', names)
        # Persist a tombstone atomically so a power loss cannot replay stale PIN
        # state after subsequent verification/recovery updates.
        self.store.put('pending-enrollment', None)
        self.enrollment_blocked = False

    def _candidate(self):
        if self.manual_owner and self.clock() < self.manual_until:
            return self.manual_owner
        return self.fusion.current()

    def _audit(self, kind, allowed):
        # Deliberately omit request bodies, tokens, PINs, names and resources.
        audit = self.store.get('audit', [])
        audit.append({'time': self.wall(), 'kind': kind, 'allowed': allowed})
        self.store.put('audit', audit[-1000:])

    def evidence(self, tracks):
        self.fusion.feed(tracks)
        candidate = self.fusion.current()
        if self.owner and candidate == self.owner and self.fusion.reason != 'conflict':
            self.last_presence = self.clock()
            # After shielding, an explicit activation is needed for a new lease.
        elif self.owner and (self.fusion.reason in ('conflict', 'ambiguous') or
                            (candidate is not None and candidate != self.owner)):
            self._shield()
        self.tick()

    def _shield(self):
        self.shield = True
        self.capabilities.revoke()
        self.challenge = None
        self.verified_until = 0
        self.manual_owner = None
        self.manual_until = 0

    def tick(self):
        if not self.enrollment_blocked:
            self._reconcile_enrollment()
        now = self.clock()
        if self.manual_owner and now >= self.manual_until:
            self._shield()
        candidate = self._candidate()
        if self.owner and candidate == self.owner and self.manual_owner == self.owner:
            self.last_presence = now
        if self.owner:
            if candidate != self.owner and now - self.last_presence >= self.privacy_timeout:
                self._shield()
            if now - self.last_presence >= self.suspension_timeout:
                self.suspend()
        elif self.root and now - self.last_activity >= self.anonymous_timeout:
            self.suspend()
        if self.challenge and self.challenge['expires'] <= now:
            self.challenge = None

    def status(self):
        self.tick()
        return {'authority': 'verified' if self.owner and not self.shield and
                self.clock() < self.verified_until else 'recognized' if self.owner and
                not self.shield else 'anonymous', 'shield': self.shield,
                'fault': self.fault, 'personal': bool(self.owner),
                'lease': self.lease if not self.shield else None,
                'reason': self.fusion.reason, 'session': self.work if not self.shield else None}

    def _present(self):
        self.tick()
        if self.fault or self.shield or not self.owner or self._candidate() != self.owner:
            raise PermissionError("Personal presence required")

    def anonymous(self):
        self.tick()
        if self.owner:
            raise PermissionError("Suspend personal work before switching context")
        if self.fault:
            raise PermissionError("Workspace cleanup needs administrator attention")
        if self.root is None:
            self.root, self.uid = self.isolation.anonymous()
            self.lease = str(uuid.uuid4())
        self.last_activity = self.clock()
        return self.lease

    def activate(self, title=None, session=None):
        self.tick()
        manual_owner, deadline = self.manual_owner, self.manual_until
        result = self._activate_for(self._candidate(), title, session)
        if manual_owner:
            self.manual_owner, self.manual_until = manual_owner, deadline
            self.verified_until = deadline
        return result

    def activate_verified(self, owner, pin, title=None, session=None):
        self.tick()
        owner = self._resolve_owner(owner)
        record = self.store.get('identity-' + owner)
        if not record:
            raise PermissionError('Verification failed or temporarily locked')
        valid = verify_pin(record['pin'], pin, self.wall())
        self.store.put('identity-' + owner, record)
        self._audit('pin-session', valid)
        if not valid:
            raise PermissionError('Verification failed or temporarily locked')
        if title is None and session is None:
            if self.owner and self.owner != owner:
                raise PermissionError('Suspend current personal work first')
            if self.root:
                self.suspend()
            self._open_catalog(owner)
            work = None
        else:
            work = self._activate_for(owner, title, session)
        self.manual_owner = owner
        self.manual_until = self.verified_until = self.clock() + 120
        return work

    def _resolve_owner(self, owner):
        if not isinstance(owner, str) or len(owner) > 80:
            raise ValueError('Enter a profile name')
        try:
            identity_id(owner)
        except ValueError:
            owners = [key for key, name in self.store.get('identities', {}).items()
                      if name.casefold() == owner.strip().casefold()]
            if len(owners) != 1:
                raise PermissionError('Verification failed or temporarily locked')
            owner = owners[0]
        return owner

    def _activate_for(self, candidate, title, session):
        if self.fusion.reason in ('conflict', 'ambiguous'):
            raise PermissionError('Resolve conflicting identity evidence first')
        if not candidate or self.fault:
            raise PermissionError("Unambiguous local recognition required")
        if self.owner and self.owner != candidate:
            raise PermissionError("Sessions never change owners")
        if self.store.get('identity-' + candidate) is None:
            raise PermissionError("Identity is not enrolled")
        if (title is None) == (session is None):
            raise ValueError("Choose a new title or existing session")
        if self.root:
            self.suspend()
        root, uid = self.isolation.activate(candidate)
        try:
            journal = Journal(root, candidate)
            if session:
                journal.get(session)
                journal.transition(session, 'active')
                work = session
            else:
                work = journal.create(title)
        except Exception:
            if 'journal' in locals():
                journal.close()
            self.isolation.release(candidate, root)
            raise
        self.root, self.uid, self.journal, self.work = root, uid, journal, work
        self.owner, self.lease = candidate, str(uuid.uuid4())
        self.shield = False
        self.last_presence = self.clock()
        try:
            manifests = journal.manifests(work)
            if getattr(self.isolation, 'requires_display', False):
                self.pending_restoration = manifests
            else:
                for app, arguments in manifests:
                    self.isolation.launch(work, root, uid, app, arguments)
        except Exception:
            self.suspend()
            raise
        self._audit('activation', True)
        return work

    def launch(self, app, arguments):
        application(app, arguments)
        if self.owner:
            self._present()
            if self.work is None:
                raise PermissionError("Start or resume a work session first")
        else:
            self.anonymous()
        self.isolation.launch(self.work or self.lease, self.root, self.uid, app, arguments)
        if self.journal:
            self.journal.manifest(self.work, app, arguments)
        self.last_activity = self.clock()

    def suspend(self):
        self._shield()
        self.pending_restoration = []
        if self.root is None:
            self.shield = False
            return
        try:
            self.isolation.stop(self.work or self.lease)
            if self.journal:
                if self.work and self.journal.get(self.work)['status'] == 'active':
                    self.journal.transition(self.work, 'suspended')
                self.journal.close()
                self.journal = None
            self.isolation.release(self.owner, self.root)
        except Exception:
            self.fault = True
            raise
        self.owner = self.lease = self.work = self.root = self.uid = None
        self.shield = False

    def acquire_display(self):
        if self.owner:
            self._present()
        else:
            self.anonymous()
        from .display import DescriptorReply
        return DescriptorReply(self.isolation.display(self.lease, self.uid), {'lease': self.lease})

    def display_ready(self, lease):
        if self.owner:
            self._present()
        if self.shield or self.fault or not self.root or lease != self.lease:
            raise PermissionError('Display lease expired')
        self.isolation.display_ready(self.uid)
        pending, self.pending_restoration = self.pending_restoration, []
        try:
            for app, arguments in pending:
                self.isolation.launch(self.work, self.root, self.uid, app, arguments)
        except Exception:
            self.suspend()
            raise

    def list_work(self, query):
        self.tick()
        if self.owner is None:
            candidate = self.fusion.current()
            if self.fault or not candidate or self.store.get('identity-' + candidate) is None:
                raise PermissionError("Unambiguous local recognition required")
            if self.root is not None:
                raise PermissionError("Suspend anonymous work before browsing personal sessions")
            self._open_catalog(candidate)
        self._present()
        return self.journal.search(query)

    def _open_catalog(self, candidate):
        if self.fault or self.fusion.reason in ('conflict', 'ambiguous'):
            raise PermissionError('Personal workspace unavailable')
        root, uid = self.isolation.activate(candidate)
        try:
            journal = Journal(root, candidate)
        except Exception:
            self.isolation.release(candidate, root)
            raise
        self.owner, self.root, self.uid, self.journal = candidate, root, uid, journal
        self.lease = str(uuid.uuid4())
        self.last_presence = self.clock()
        self.shield = False

    def message(self, role, content):
        self._present()
        self.journal.message(self.work, role, content)

    def history(self, before=None):
        self._present()
        return self.journal.history(self.work, before)

    def summarize(self, summary):
        self._present()
        self.journal.summarize(self.work, summary)

    def document(self, path, content=None):
        self._present()
        if self.work is None:
            raise PermissionError('Start or resume a work session first')
        from . import artifacts
        if content is None:
            return artifacts.read(self.root, path)
        result = artifacts.save(self.root, self.uid, path, content)
        self.journal.artifact(self.work, result['path'], result['sha256'])
        return result

    def request_capability(self, operation, resource):
        self._present()
        if operation not in RULES:
            raise PermissionError("Unsupported operation")
        if not isinstance(resource, str) or not 1 <= len(resource) <= 256:
            raise ValueError("Invalid resource")
        if operation == 'system.install' and not self.store.get('identity-' + self.owner).get('admin', False):
            raise PermissionError("Administrator required")
        self.challenge = {'id': secrets.token_urlsafe(24), 'owner': self.owner, 'lease': self.lease,
                          'operation': operation, 'resource': resource, 'expires': self.clock() + 30}
        # Scope must be displayed by trusted UI, not summarized by the LLM.
        return {k: v for k, v in self.challenge.items() if k != 'lease'}

    def verify(self, challenge, pin, confirmed):
        self._present()
        item = self.challenge
        self.challenge = None
        if not item or item['id'] != challenge or item['expires'] <= self.clock() or (
            item['owner'], item['lease']) != (self.owner, self.lease):
            raise PermissionError("Verification expired")
        record = self.store.get('identity-' + self.owner)
        valid = verify_pin(record['pin'], pin, self.wall())
        self.store.put('identity-' + self.owner, record)
        self._audit('pin', valid)
        if not valid:
            raise PermissionError("Verification failed or temporarily locked")
        operation = item['operation']
        if RULES[operation][0] == 'transaction_confirmed' and confirmed is not True:
            raise PermissionError("Explicit transaction confirmation required")
        authority = 'transaction_confirmed' if confirmed is True else 'verified'
        token = self.capabilities.issue(self.owner, self.lease, operation, item['resource'], authority)
        self.verified_until = self.clock() + 120
        return token

    def use(self, token, operation, resource):
        self._present()
        self.capabilities.use(token, self.owner, self.lease, operation, resource)
        self._audit(operation, True)

    def profiles(self):
        return [{'id': key, 'name': name} for key, name in
                sorted(self.store.get('identities', {}).items(), key=lambda item: item[1].casefold())][:128]

    def profile_status(self):
        self.tick()
        detected = self.fusion.current()
        owner = self.owner if self.owner and not self.shield else detected
        if self.shield or self.fusion.reason in ('conflict', 'ambiguous'):
            owner = None
        record = self.store.get('identity-' + owner) if owner else None
        return {'id': owner if record else None, 'name': record['name'] if record else '',
                'photo': record.get('photo', '') if record and detected == owner else '',
                'detected': bool(record and detected == owner), 'reason': self.fusion.reason}

    def enroll(self, name, pin, consent, templates, photo=None):
        self._reconcile_enrollment()
        if self.enrollment_blocked:
            raise PermissionError('Interrupted allocation requires administrator recovery')
        if self.owner or self.fault:
            raise PermissionError('Enrollment requires an anonymous context')
        if consent is not True or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("A display name and explicit consent are required")
        # Enrollment cannot select or merge an existing principal by spoken name.
        names = self.store.get('identities', {})
        if name.strip().casefold() in (value.casefold() for value in names.values()):
            raise PermissionError("Use the recovery process for an existing name")
        manual = templates is None
        if manual:
            templates = {'face': [], 'voice': []}
        if not isinstance(templates, dict) or set(templates) != {'face', 'voice'}:
            raise ValueError("Face and speaker templates required")
        from .identity import cosine
        for samples in templates.values():
            if not isinstance(samples, list) or not (manual or 3 <= len(samples) <= 12):
                raise ValueError("Collect multiple enrollment samples")
            for sample in samples:
                if not isinstance(sample, list) or not 16 <= len(sample) <= 2048:
                    raise ValueError("Invalid embedding")
                cosine(sample, sample)
        owner = str(uuid.uuid4())
        from .portraits import portrait
        image = portrait(photo)
        recovery = secrets.token_urlsafe(32)
        record = {'name': name.strip(), 'pin': pin_record(pin), 'templates': templates,
                  'recovery': hashlib.sha256(recovery.encode()).hexdigest(), 'admin': False,
                  'biometric_consent': not manual, 'photo': image}
        # The encrypted intent contains only the PIN verifier and recovery hash,
        # never the entered PIN or the one-time recovery secret.
        self.store.put('pending-enrollment', {'identity': owner, 'record': record})
        self.isolation.provision(owner)
        self._reconcile_enrollment()
        return {'identity': owner, 'recovery': recovery}

    def recover(self, owner, recovery, new_pin):
        self.tick()
        owner = self._resolve_owner(owner)
        if self.owner and self.owner != owner:
            raise PermissionError('Suspend current personal work first')
        record = self.store.get('identity-' + owner)
        if not isinstance(recovery, str) or len(recovery) > 128 or not record or not secrets.compare_digest(
            record['recovery'], hashlib.sha256(recovery.encode()).hexdigest()
        ):
            raise PermissionError("Recovery failed")
        if self.owner == owner:
            self.suspend()
        replacement = secrets.token_urlsafe(32)
        record['pin'] = pin_record(new_pin)
        record['recovery'] = hashlib.sha256(replacement.encode()).hexdigest()
        self.store.put('identity-' + owner, record)
        return replacement
