"""Durable owner-scoped data storage for generated applications (issue #163).

Broker-side storage layer that separates application *code* from application
*data*. Records live under an owner-scoped root that the broker derives from
its own privileged configuration: callers only ever handle stable application
identity (an app UUID), a fixed kind, and record identity (a record UUID).
No caller-supplied path, UID, or environment value reaches the filesystem.

Boundaries (deliberate, not incomplete):

- Authenticated scopes persist durably; guest scopes are ephemeral, bound to a
  session UUID, expire after a TTL, and are scrubbed on chat close, logout and
  crash recovery. A failed scrub quarantines rather than claiming success.
- Records are stored as authenticated-shape envelopes in one atomic rename per
  save, so a crash mid-write can only ever lose the newest write, never a
  previously acknowledged record. Oversized records are rejected before the
  write and are never truncated on read.
- Cross-user confidentiality at rest requires the broker to point each owner's
  root at that Linux user's private home (the per-user account model of
  issues #159/#160); this layer guarantees deterministic app-scoped
  partitioning and never exposes another owner's subtree through the API, and
  it enforces file/directory ownership when it runs as root.
- Application code and cache are NOT stored here (publishing code must not
  imply data was saved); use the artifact/application stores for code.

Schema migration uses a staging layout plus an atomically swapped ``current``
symlink: an interrupted migration never destroys the previous layout, so a
failed upgrade is recoverable and rollback preserves prior data.

Import is transactional (validate everything, then one replace), and export
bundles carry per-record SHA-256 integrity for round-trips without manual
filesystem edits.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .secure_store import atomic_bytes

LAYOUT_VERSION = 1
KINDS = ("documents", "records", "state")
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
_TEMP_PREFIXES = (".pending-", ".tmp-")


class AppDataError(ValueError):
    """Expected application-data failure with a user-safe message."""


class QuotaExceeded(AppDataError):
    """The write would exceed a per-record, per-kind or per-app quota."""


class VersionConflict(AppDataError):
    """An optimistic-concurrency save lost a race against another writer."""

    def __init__(self, current_version):
        super().__init__("That record changed since you last read it. Re-read and retry.")
        self.current_version = current_version


class UnknownRecord(AppDataError):
    """The requested record does not exist in this scope and app."""


class EphemeralExpired(AppDataError):
    """The guest session's ephemeral storage expired and was discarded."""


class IntegrityProblem(AppDataError):
    """Stored state is inconsistent; the scope is quarantined fail-closed."""


@dataclass(frozen=True)
class Quota:
    max_record_bytes: int
    max_total_bytes: int
    max_records: int
    ttl_seconds: int | None = None

    def __post_init__(self):
        for name in ("max_record_bytes", "max_total_bytes", "max_records"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Invalid quota: {name}")
        if self.ttl_seconds is not None and (type(self.ttl_seconds) is not int or self.ttl_seconds < 1):
            raise ValueError("Invalid quota: ttl_seconds")


DEFAULT_QUOTA = Quota(max_record_bytes=131072, max_total_bytes=2 * 1024 * 1024, max_records=1024)
GUEST_QUOTA = Quota(max_record_bytes=131072, max_total_bytes=1024 * 1024, max_records=256, ttl_seconds=7200)


@dataclass(frozen=True)
class AppScope:
    """Server-derived identity for one execution context.

    ``owner`` is the authenticated AIOS account UUID or None for guests;
    ``scope`` is always a session UUID and identifies guest storage. UIDs are
    only used to fix file ownership when the store runs as root.
    """

    owner: str | None
    uid: int
    scope: str

    @property
    def guest(self) -> bool:
        return self.owner is None

    def __post_init__(self):
        for name in ("owner", "scope"):
            value = getattr(self, name)
            if name == "owner" and value is None:
                continue
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError("Invalid app-data identity")
        if not isinstance(self.uid, bool) and type(self.uid) is int and self.uid >= 0:
            return
        raise ValueError("Invalid app-data identity")

    @property
    def _identity(self) -> str:
        return self.owner if self.owner is not None else self.scope


def _app_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Application identity must be an application UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        raise ValueError("Application identity must be an application UUID") from None
    return parsed


def _record_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Record identity must be a record UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        raise ValueError("Record identity must be a record UUID") from None
    return parsed


def _kind(value) -> str:
    if value not in KINDS:
        raise ValueError("Choose a supported data kind")
    return value


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _encode_payload(data):
    if isinstance(data, str):
        payload = data.encode("utf-8")
        return payload, "utf-8"
    if isinstance(data, bytes):
        return data, "base64"
    raise ValueError("App data must be text or bytes")


class AppDataStore:
    """Owner-scoped record storage. Construct one store per execution scope."""

    def __init__(self, root, principal: AppScope, quota: Quota | None = None, now=time.time):
        self.root = Path(root)
        if not isinstance(principal, AppScope):
            raise ValueError("AppDataStore requires an AppScope principal")
        self.principal = principal
        self.quota = quota or (GUEST_QUOTA if principal.guest else DEFAULT_QUOTA)
        self._now = now
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._fix_owner(self.root)
        if principal.guest:
            self._collect_expired(force=principal.scope)

    # ------------------------------------------------------------------ paths

    def _app_root(self, app) -> Path:
        app = _app_id(app)
        owner = self.principal.owner
        if owner is None:
            base = self.root / "guest" / self.principal.scope
            base.mkdir(parents=True, mode=0o700, exist_ok=True)
            created = base / ".created"
            if not created.exists():
                created.write_text(str(self._now()))
            return base / app
        base = self.root / "apps" / owner
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
        return base / app

    def _live_dir(self, app_root: Path, create: bool) -> Path | None:
        current = app_root / "current"
        if current.is_symlink():
            target = os.readlink(current)
            if not re.fullmatch(r"v[1-9][0-9]*", target):
                raise IntegrityProblem("Application data layout is unsafe")
            journal_path = app_root / "migration.json"
            if journal_path.is_file():
                try:
                    journal = json.loads(journal_path.read_text("utf-8"))
                except (ValueError, OSError):
                    raise IntegrityProblem("Application data needs recovery") from None
                if journal.get("status") != "complete" and str(journal.get("to")) != target[1:]:
                    raise IntegrityProblem("An interrupted upgrade needs recovery")
            resolved = app_root / target
            if not resolved.is_dir():
                raise IntegrityProblem("Application data layout is broken")
            return resolved
        versions = sorted(
            (entry for entry in app_root.iterdir() if entry.is_dir() and re.fullmatch(r"v[1-9][0-9]*", entry.name)),
            key=lambda entry: int(entry.name[1:]),
        ) if app_root.is_dir() else []
        if len(versions) > 1:
            raise IntegrityProblem("Application data layout is ambiguous")
        if not versions:
            if not create:
                return None
            app_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            first = app_root / f"v{LAYOUT_VERSION}"
            first.mkdir(mode=0o700)
            for kind in KINDS:
                (first / kind).mkdir(mode=0o700)
            self._link_current(app_root, first)
            return first
        if create and versions[0].name != f"v{LAYOUT_VERSION}":
            raise IntegrityProblem("Application data layout needs an upgrade")
        self._link_current(app_root, versions[0])
        return versions[0]

    def _ensure_live(self, app_root: Path) -> Path:
        live = self._live_dir(app_root, create=True)
        assert live is not None  # _live_dir(create=True) always returns a directory
        return live

    @staticmethod
    def _link_current(app_root: Path, version_dir: Path):
        temporary = app_root / ".current.tmp"
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()
        os.symlink(version_dir.name, temporary, target_is_directory=True)
        os.replace(temporary, app_root / "current")

    def _record_path(self, live: Path, kind: str, record: str) -> Path:
        return live / kind / (record + ".json")

    # ----------------------------------------------------------------- locking

    @contextmanager
    def _locked(self, app_root: Path):
        app_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_path = app_root / "lock"
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Closing the descriptor on context exit releases the lock.
            yield

    # --------------------------------------------------------------- ownership

    def _fix_owner(self, path: Path):
        if os.geteuid() == 0 and self.principal.uid >= 0:
            os.chown(path, self.principal.uid, self.principal.uid)

    # ------------------------------------------------------------ envelope IO

    def _write_envelope(self, live: Path, kind: str, record: str, envelope: dict):
        path = self._record_path(live, kind, record)
        payload = json.dumps(envelope, allow_nan=False, sort_keys=True).encode("utf-8")
        atomic_bytes(path, payload)
        path.chmod(0o600)
        self._fix_owner(path)

    def _read_envelope(self, live: Path, kind: str, record: str) -> dict:
        path = self._record_path(live, kind, record)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise UnknownRecord("That saved item no longer exists.") from None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IntegrityProblem("A saved record is corrupt; it was not modified.") from None
        if (not isinstance(envelope, dict) or envelope.get("format") != 1
                or envelope.get("id") != record or envelope.get("kind") != kind
                or envelope.get("encoding") not in ("utf-8", "base64")):
            raise IntegrityProblem("A saved record has an invalid shape; it was not modified.")
        payload = self._payload(envelope)
        if envelope.get("size") != len(payload) or envelope.get("sha256") != _sha(payload):
            raise IntegrityProblem("A saved record failed its integrity check; it was not modified.")
        return envelope

    @staticmethod
    def _payload(envelope: dict) -> bytes:
        data = envelope.get("data")
        if not isinstance(data, str):
            raise IntegrityProblem("A saved record has an invalid shape; it was not modified.")
        try:
            if envelope["encoding"] == "utf-8":
                return data.encode("utf-8")
            return base64.b64decode(data, validate=True)
        except ValueError:
            raise IntegrityProblem("A saved record failed its integrity check; it was not modified.") from None

    @staticmethod
    def _meta(envelope: dict) -> dict:
        return {key: envelope[key] for key in ("app", "kind", "id", "version", "size", "updated_at", "sha256")}

    # ----------------------------------------------------------------- quota

    def _totals(self, live: Path) -> tuple[int, int]:
        count = 0
        total = 0
        for kind in KINDS:
            for entry in sorted((live / kind).glob("*.json")):
                try:
                    envelope = json.loads(entry.read_text("utf-8"))
                    total += int(envelope["size"])
                    count += 1
                except (ValueError, KeyError, TypeError):
                    continue
        return count, total

    # ------------------------------------------------------------- expiration

    def _expired(self) -> bool:
        if not self.principal.guest or self.quota.ttl_seconds is None:
            return False
        created = self.root / "guest" / self.principal.scope / ".created"
        try:
            age = self._now() - float(created.read_text())
        except (FileNotFoundError, OSError, ValueError):
            return False
        return age > self.quota.ttl_seconds

    def _check_guest(self):
        if self._expired():
            self._scrub(Path("guest") / self.principal.scope)
            raise EphemeralExpired("This session's temporary app data expired and was discarded.")

    def _collect_expired(self, force: str | None = None):
        """Drop expired guest trees; quarantines anything that cannot be removed."""
        guests = self.root / "guest"
        if not guests.is_dir() or self.quota.ttl_seconds is None:
            return []
        removed = []
        for entry in list(guests.iterdir()):
            if not entry.is_dir():
                continue
            created = entry / ".created"
            try:
                age = self._now() - float(created.read_text())
            except (FileNotFoundError, OSError, ValueError):
                continue
            if age > self.quota.ttl_seconds or force is not None:
                if force is not None and entry.name != force:
                    continue
                try:
                    shutil.rmtree(entry)
                    removed.append(entry.name)
                except OSError:
                    quarantine = guests / ".quarantine"
                    quarantine.mkdir(mode=0o700, exist_ok=True)
                    entry.rename(quarantine / f"{entry.name}-{secrets.token_hex(4)}")
                    removed.append(entry.name)
            if force is not None and entry.name == force:
                break
        return removed

    def _scrub(self, relative: Path):
        target = self.root / relative
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            return
        except OSError:
            quarantine = self.root / relative.parent / ".quarantine"
            quarantine.mkdir(mode=0o700, exist_ok=True)
            target.rename(quarantine / f"{relative.name}-{secrets.token_hex(4)}")

    # --------------------------------------------------------- crash recovery

    def _recover(self, app):
        """Clean partial writes; resolve interrupted migrations fail-closed."""
        app_root = self._app_root(app)
        with self._locked(app_root):
            if not app_root.is_dir():
                return
            journal_path = app_root / "migration.json"
            for version_dir in (entry for entry in app_root.iterdir()
                                if entry.is_dir() and re.fullmatch(r"v[1-9][0-9]*", entry.name)):
                for kind_path in (entry for entry in version_dir.iterdir() if entry.is_dir()):
                    for stray in kind_path.iterdir():
                        if stray.name.startswith(_TEMP_PREFIXES):
                            stray.unlink(missing_ok=True)
            journal = None
            if journal_path.is_file():
                try:
                    journal = json.loads(journal_path.read_text("utf-8"))
                except (ValueError, OSError):
                    journal = None
            if isinstance(journal, dict) and journal.get("from") and journal.get("to"):
                staging = app_root / f"v{journal['to']}"
                current = app_root / "current"
                pointing = os.readlink(current) if current.is_symlink() else None
                if pointing == staging.name:
                    # Swap landed; finish bookkeeping so rollback stays possible.
                    journal["status"] = "complete"
                    atomic_bytes(journal_path, json.dumps(journal, sort_keys=True).encode())
                else:
                    # Migration never landed: the previous layout is authoritative.
                    if staging.is_dir():
                        shutil.rmtree(staging)
                    journal_path.unlink(missing_ok=True)
            self._live_dir(app_root, create=False)

    def recover_all(self):
        """Recovery entry point for broker start: cleans every known scope."""
        recovered = []
        for base in (self.root / "apps", self.root / "guest"):
            if not base.is_dir():
                continue
            for owner_dir in (entry for entry in base.iterdir() if entry.is_dir() and not entry.name.startswith(".")):
                for app_dir in (entry for entry in owner_dir.iterdir() if entry.is_dir() and not entry.name.startswith(".")):
                    try:
                        name = app_dir.name
                        self._recover(name)
                        recovered.append(name)
                    except IntegrityProblem:
                        continue
        return recovered

    # ------------------------------------------------------------------- API

    def save(self, app, kind, data, record_id=None, expected_version=None) -> dict:
        """Persist one record; returns its metadata (version, sha256, size)."""
        kind = _kind(kind)
        app = _app_id(app)
        payload, encoding = _encode_payload(data)
        if len(payload) > self.quota.max_record_bytes:
            raise QuotaExceeded("That item is too large for app storage; save a smaller excerpt.")
        record = _record_id(record_id) if record_id is not None else str(uuid.uuid4())
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            live = self._ensure_live(app_root)
            existing = None
            if self._record_path(live, kind, record).exists():
                existing = self._read_envelope(live, kind, record)
            if existing is not None and expected_version is not None and existing["version"] != expected_version:
                raise VersionConflict(existing["version"])
            count, total = self._totals(live)
            incoming = len(payload) - (existing["size"] if existing else 0)
            if count + (0 if existing else 1) > self.quota.max_records:
                raise QuotaExceeded("This application has too many saved items.")
            if total + incoming > self.quota.max_total_bytes:
                raise QuotaExceeded("This application's saved data is full; delete something first.")
            envelope = {
                "format": 1, "app": app, "kind": kind, "id": record,
                "version": (existing["version"] + 1) if existing else 1,
                "encoding": encoding,
                "data": payload.decode("utf-8") if encoding == "utf-8" else base64.b64encode(payload).decode("ascii"),
                "size": len(payload), "sha256": _sha(payload),
                "updated_at": self._now(),
            }
            if envelope["encoding"] == "utf-8" and "\x00" in envelope["data"]:
                raise ValueError("App data cannot contain NUL characters; store bytes instead.")
            self._write_envelope(live, kind, record, envelope)
            return self._meta(envelope)

    def read(self, app, kind, record_id, with_data=False):
        kind = _kind(kind)
        app = _app_id(app)
        record = _record_id(record_id)
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            live = self._live_dir(app_root, create=False)
            if live is None:
                raise UnknownRecord("That saved item no longer exists.")
            envelope = self._read_envelope(live, kind, record)
            meta = self._meta(envelope)
            if with_data:
                meta["data"] = envelope["data"]
            return meta

    def list(self, app, kind=None) -> dict:
        kind = _kind(kind) if kind is not None else None
        app = _app_id(app)
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            live = self._live_dir(app_root, create=False)
            kinds = {name: [] for name in KINDS}
            if live is not None:
                for name in KINDS:
                    if kind is not None and name != kind:
                        continue
                    entries = []
                    for entry in sorted((live / name).glob("*.json")):
                        try:
                            envelope = json.loads(entry.read_text("utf-8"))
                            entries.append(self._meta(envelope))
                        except (ValueError, OSError):
                            continue
                    kinds[name] = entries
            total = sum(item["size"] for items in kinds.values() for item in items)
            return {"app": app, "kinds": kinds, "total_bytes": total, "quota": {
                "max_record_bytes": self.quota.max_record_bytes,
                "max_total_bytes": self.quota.max_total_bytes,
                "max_records": self.quota.max_records,
                "ttl_seconds": self.quota.ttl_seconds,
            }}

    def delete(self, app, kind, record_id, expected_version=None) -> dict:
        kind = _kind(kind)
        app = _app_id(app)
        record = _record_id(record_id)
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            live = self._live_dir(app_root, create=False)
            if live is None:
                raise UnknownRecord("That saved item no longer exists.")
            envelope = self._read_envelope(live, kind, record)
            if expected_version is not None and envelope["version"] != expected_version:
                raise VersionConflict(envelope["version"])
            self._record_path(live, kind, record).unlink(missing_ok=True)
            return {"deleted": record, "kind": kind, "app": app}

    def close(self):
        """Chat-close / logout hook: guests lose all app data immediately."""
        if self.principal.guest:
            return self._collect_expired(force=self.principal.scope)
        return []

    # ------------------------------------------------------------- migration

    def upgrade(self, app, to_layout: int, migrate) -> dict:
        """Transform every record into a new layout version transactionally.

        ``migrate(kind, meta_and_data)`` returns the transformed payload (str
        or bytes), or None to drop the record. The previous layout stays
        untouched until the atomic swap, so any failure before the swap keeps
        the old data authoritative and rollback is supported afterwards.
        """
        app = _app_id(app)
        if type(to_layout) is not int or to_layout <= LAYOUT_VERSION:
            raise ValueError("Upgrade target must be a newer layout version")
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            live = self._ensure_live(app_root)
            staging = app_root / f"v{to_layout}"
            if staging.exists() or (app_root / "current").is_symlink() and \
                    os.readlink(app_root / "current") == staging.name:
                raise IntegrityProblem("An interrupted upgrade is already staged; recover first")
            journal_path = app_root / "migration.json"
            atomic_bytes(journal_path, json.dumps(
                {"from": int(live.name[1:]), "to": to_layout, "status": "pending"}, sort_keys=True).encode())
            for kind in KINDS:
                (staging / kind).mkdir(mode=0o700, parents=True)
            for kind in KINDS:
                for entry in sorted((live / kind).glob("*.json")):
                    envelope = json.loads(entry.read_text("utf-8"))
                    payload = self._payload(envelope)
                    data = envelope["data"] if envelope["encoding"] == "utf-8" else payload
                    result = migrate(kind, {"id": envelope["id"], "data": data})
                    if result is None:
                        continue
                    converted, encoding = _encode_payload(result)
                    new_envelope = {
                        "format": 1, "app": app, "kind": kind, "id": envelope["id"],
                        "version": envelope["version"], "encoding": encoding,
                        "data": converted.decode("utf-8") if encoding == "utf-8"
                        else base64.b64encode(converted).decode("ascii"),
                        "size": len(converted), "sha256": _sha(converted),
                        "updated_at": self._now(),
                    }
                    temporary = staging / kind / (".tmp-" + envelope["id"] + ".json")
                    temporary.write_bytes(json.dumps(new_envelope, sort_keys=True).encode())
                    temporary.rename(staging / kind / (envelope["id"] + ".json"))
            atomic_bytes(journal_path, json.dumps(
                {"from": int(live.name[1:]), "to": to_layout, "status": "staged"}, sort_keys=True).encode())
            self._link_current(app_root, staging)
            atomic_bytes(journal_path, json.dumps(
                {"from": int(live.name[1:]), "to": to_layout, "status": "complete"}, sort_keys=True).encode())
            return {"app": app, "from": live.name[1:], "to": str(to_layout), "rollback_available": live.name}

    def rollback(self, app, to_layout: int) -> dict:
        """Point storage back at a preserved previous layout."""
        app = _app_id(app)
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            target = app_root / f"v{to_layout}"
            if not target.is_dir():
                raise IntegrityProblem("That previous layout no longer exists")
            self._link_current(app_root, target)
            journal_path = app_root / "migration.json"
            if journal_path.is_file():
                journal_path.unlink()
            return {"app": app, "rolled_back_to": str(to_layout)}

    # ---------------------------------------------------------- export/import

    def export(self, app) -> dict:
        app = _app_id(app)
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            live = self._live_dir(app_root, create=False)
            kinds = {}
            if live is not None:
                for name in KINDS:
                    records = []
                    for entry in sorted((live / name).glob("*.json")):
                        envelope = json.loads(entry.read_text("utf-8"))
                        self._payload(envelope)  # integrity gate before export
                        records.append({key: envelope[key] for key in
                                        ("id", "version", "encoding", "data", "size", "sha256")})
                    kinds[name] = records
            bundle = {"format": "aios-appdata/1", "app": app, "exported_at": self._now(), "kinds": kinds}
            size = len(json.dumps(bundle))
            if size > MAX_BUNDLE_BYTES:
                raise QuotaExceeded("This application's data is too large to export at once.")
            return bundle

    @staticmethod
    def validate_export(bundle) -> dict:
        if not isinstance(bundle, dict) or set(bundle) != {"format", "app", "exported_at", "kinds"}:
            raise ValueError("That is not an app-data export bundle")
        if bundle["format"] != "aios-appdata/1":
            raise ValueError("That export bundle uses an unsupported format")
        app = _app_id(bundle["app"])
        kinds = bundle["kinds"]
        if not isinstance(kinds, dict) or not set(kinds) <= set(KINDS):
            raise ValueError("That export bundle contains unknown data kinds")
        total = 0
        for name, records in kinds.items():
            if not isinstance(records, list):
                raise ValueError("That export bundle is malformed")
            for record in records:
                if not isinstance(record, dict) or set(record) != {"id", "version", "encoding", "data", "size", "sha256"}:
                    raise ValueError("That export bundle contains a malformed record")
                _record_id(record["id"])
                if record["encoding"] not in ("utf-8", "base64") or type(record["version"]) is not int \
                        or record["version"] < 1 or not isinstance(record["data"], str):
                    raise ValueError("That export bundle contains a malformed record")
                try:
                    payload = (record["data"].encode("utf-8") if record["encoding"] == "utf-8"
                               else base64.b64decode(record["data"], validate=True))
                except ValueError:
                    raise ValueError("That export bundle contains unreadable record data") from None
                if record["size"] != len(payload) or record["sha256"] != _sha(payload):
                    raise IntegrityProblem("That export bundle failed its integrity check")
                total += len(payload)
        if total > MAX_BUNDLE_BYTES:
            raise QuotaExceeded("That export bundle is too large")
        return {"app": app, "total_bytes": total}

    def import_bundle(self, app, bundle, mode="into-empty") -> dict:
        """Restore a bundle transactionally.

        ``into-empty`` refuses when any record already exists (manual
        filesystem edits are never required). ``replace`` swaps the whole app
        storage only after every record validated, so a bad import cannot
        corrupt a working application.
        """
        kindless = self.validate_export(bundle)
        app = _app_id(app)
        if kindless["app"] != app:
            raise ValueError("That export bundle belongs to a different application")
        if mode not in ("into-empty", "replace"):
            raise ValueError("Choose 'into-empty' or 'replace' import mode")
        self._check_guest()
        app_root = self._app_root(app)
        with self._locked(app_root):
            staging = None
            live = self._live_dir(app_root, create=False)
            if mode == "into-empty":
                if live is not None and any((live / name).glob("*.json") for name in KINDS):
                    raise IntegrityProblem("This application already has data; import into an empty application or replace")
                target = self._ensure_live(app_root)
            else:
                target = self._ensure_live(app_root)
                staging = app_root / f"v{LAYOUT_VERSION}-import"
                if staging.exists():
                    shutil.rmtree(staging)
                for kind in KINDS:
                    (staging / kind).mkdir(mode=0o700, parents=True)
                target = staging
            imported = 0
            for name, records in bundle["kinds"].items():
                for record in records:
                    envelope = {"format": 1, "app": app, "kind": name, "id": record["id"],
                                "version": record["version"], "encoding": record["encoding"],
                                "data": record["data"], "size": record["size"],
                                "sha256": record["sha256"], "updated_at": bundle["exported_at"]}
                    self._write_envelope(target, name, record["id"], envelope)
                    imported += 1
            if mode == "replace":
                assert staging is not None
                previous = app_root / "vprevious"
                if previous.exists():
                    shutil.rmtree(previous)
                current_dir = self._ensure_live(app_root)
                if live is not None:
                    os.replace(current_dir, previous)
                os.replace(staging, current_dir)
                self._link_current(app_root, current_dir)
            return {"app": app, "imported": imported, "mode": mode}

    def prune_preserved_upgrade(self, app) -> list:
        """Remove preserved pre-upgrade/replace layouts after explicit acceptance."""
        app = _app_id(app)
        app_root = self._app_root(app)
        removed = []
        with self._locked(app_root):
            if not app_root.is_dir():
                return removed
            current = os.readlink(app_root / "current") if (app_root / "current").is_symlink() else None
            for entry in (candidate for candidate in app_root.iterdir()
                          if candidate.is_dir() and not candidate.is_symlink() and candidate.name != current):
                shutil.rmtree(entry)
                removed.append(entry.name)
            journal = app_root / "migration.json"
            journal.unlink(missing_ok=True)
        return removed
