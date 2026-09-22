"""Owner-scoped app-data storage contract tests (issue #163)."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from aios import app_data
from aios.app_data import (AppDataError, AppDataStore, AppScope, EphemeralExpired,
                           IntegrityProblem, Quota, QuotaExceeded, UnknownRecord,
                           VersionConflict)


class Clock:
    def __init__(self):
        self.value = 1000000.0

    def __call__(self):
        return self.value


def scope(owner=None, uid=1500):
    return AppScope(owner=owner, uid=uid, scope=str(uuid.uuid4()))


class AppDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "appdata"
        self.owner = str(uuid.uuid4())
        self.app = str(uuid.uuid4())

    def store(self, principal=None, now=None, quota=None):
        return AppDataStore(self.root, principal or scope(self.owner), quota=quota, now=now or Clock())

    # ------------------------------------------------------------ basics

    def test_roundtrip_kinds_and_metadata(self):
        store = self.store()
        saved = store.save(self.app, "documents", "first note")
        self.assertEqual(saved["size"], 10)
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["sha256"], hashlib.sha256(b"first note").hexdigest())
        loaded = store.read(self.app, "documents", saved["id"], with_data=True)
        self.assertEqual(loaded["data"], "first note")
        again = store.save(self.app, "documents", "second note", record_id=saved["id"])
        self.assertEqual(again["version"], 2)
        listed = store.list(self.app)
        self.assertEqual(len(listed["kinds"]["documents"]), 1)
        self.assertEqual(listed["kinds"]["documents"][0]["version"], 2)
        self.assertNotIn("data", listed["kinds"]["documents"][0])

    def test_bytes_roundtrip_and_nul_text_rejected(self):
        store = self.store()
        blob = bytes(range(256))
        saved = store.save(self.app, "records", blob)
        loaded = store.read(self.app, "records", saved["id"], with_data=True)
        self.assertEqual(base64.b64decode(loaded["data"]), blob)
        with self.assertRaises(ValueError):
            store.save(self.app, "state", "text with \x00 nul")

    def test_identity_is_uuid_only_and_no_paths(self):
        store = self.store()
        for bad in ("../../etc/passwd", "../shared", "/absolute", "notes-app", "", None):
            with self.subTest(app=bad), self.assertRaises(ValueError):
                store.save(bad, "documents", "x")
        saved = store.save(self.app, "documents", "x")
        for bad in ("../../x", "notes", None, 7):
            with self.subTest(record=bad), self.assertRaises(ValueError):
                store.read(self.app, "documents", bad)
        with self.assertRaises(ValueError):
            store.save(self.app, "secrets", "x")
        # The stored tree contains no path-shaped names; every stored subtree
        # is addressed by UUID identity.
        stored = [p for p in self.root.rglob("*.json")]
        self.assertTrue(stored)
        for path in stored:
            self.assertRegex(path.stem, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_optimistic_concurrency(self):
        store = self.store()
        saved = store.save(self.app, "state", "v1 data")
        conflict = None
        try:
            store.save(self.app, "state", "lost update", record_id=saved["id"], expected_version=42)
        except VersionConflict as error:
            conflict = error
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.current_version, 1)
        won = store.save(self.app, "state", "v2 data", record_id=saved["id"], expected_version=1)
        self.assertEqual(won["version"], 2)
        with self.assertRaises(VersionConflict):
            store.delete(self.app, "state", saved["id"], expected_version=1)

    def test_delete_and_unknown(self):
        store = self.store()
        saved = store.save(self.app, "documents", "temp")
        store.delete(self.app, "documents", saved["id"])
        with self.assertRaises(UnknownRecord):
            store.read(self.app, "documents", saved["id"])
        with self.assertRaises(UnknownRecord):
            store.delete(self.app, "documents", saved["id"])
        with self.assertRaises(UnknownRecord):
            store.read(self.app, "state", str(uuid.uuid4()))

    # ------------------------------------------------------------- quotas

    def test_record_quota_rejects_before_write(self):
        store = self.store(quota=Quota(max_record_bytes=16, max_total_bytes=1024, max_records=8))
        with self.assertRaises(QuotaExceeded):
            store.save(self.app, "documents", "x" * 17)
        self.assertEqual(store.list(self.app)["kinds"]["documents"], [])

    def test_total_and_count_quotas(self):
        quota = Quota(max_record_bytes=64, max_total_bytes=64, max_records=2)
        store = self.store(quota=quota)
        doc = store.save(self.app, "documents", "a" * 20)
        store.save(self.app, "records", "b" * 20)
        with self.assertRaises(QuotaExceeded):  # third distinct record exceeds the count quota
            store.save(self.app, "state", "c")
        # Growing an existing record past the total quota is refused...
        with self.assertRaises(QuotaExceeded):
            store.save(self.app, "documents", "a" * 64, record_id=doc["id"])
        # ...but an update that fits within both budgets lands.
        grown = store.save(self.app, "documents", "a" * 40, record_id=doc["id"])
        self.assertEqual(grown["version"], 2)
        self.assertEqual(store.list(self.app)["total_bytes"], 60)

    def test_overlong_data_is_rejected_not_truncated(self):
        store = self.store(quota=Quota(max_record_bytes=10, max_total_bytes=1024, max_records=8))
        with self.assertRaises(QuotaExceeded):
            store.save(self.app, "documents", "x" * 11)
        ok = store.save(self.app, "documents", "x" * 10)
        self.assertEqual(store.read(self.app, "documents", ok["id"])["size"], 10)

    # ------------------------------------------------------- isolation

    def test_same_app_name_across_owners_and_guests_is_partitioned(self):
        app = str(uuid.uuid4())
        alice = AppDataStore(self.root / "alice", scope(str(uuid.uuid4())))
        bob = AppDataStore(self.root / "bob", scope(str(uuid.uuid4())))
        guest = AppDataStore(self.root, scope(owner=None))
        saved = alice.save(app, "documents", "alice secret")
        self.assertEqual(
            alice.read(app, "documents", saved["id"], with_data=True)["data"], "alice secret")
        with self.assertRaises(UnknownRecord):
            bob.read(app, "documents", saved["id"])
        with self.assertRaises(UnknownRecord):
            guest.read(app, "documents", saved["id"])
        self.assertEqual(bob.list(app)["kinds"]["documents"], [])
        self.assertEqual(guest.list(app)["kinds"]["documents"], [])
        # Distinct roots keep secrets out of each other's tree entirely.
        for path in (self.root / "bob").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"alice secret", path.read_bytes())

    def test_guest_storage_is_ephemeral_on_close(self):
        guest_scope = scope(owner=None)
        guest = AppDataStore(self.root, guest_scope)
        saved = guest.save(self.app, "documents", "scratch note")
        self.assertEqual(guest.read(self.app, "documents", saved["id"])["size"], 12)
        guest.close()
        newcomer = AppDataStore(self.root, guest_scope)
        with self.assertRaises(UnknownRecord):
            newcomer.read(self.app, "documents", saved["id"])
        self.assertEqual(newcomer.list(self.app)["kinds"]["documents"], [])
        # An authenticated owner's data survives a guest close.
        owner = self.store()
        kept = owner.save(self.app, "documents", "durable")
        guest2 = AppDataStore(self.root, scope(owner=None))
        guest2.save(self.app, "documents", "guest scratch")
        guest2.close()
        self.assertEqual(owner.read(self.app, "documents", kept["id"], with_data=True)["data"], "durable")

    def test_guest_ttl_expiry_and_quarantine_on_failed_scrub(self):
        clock = Clock()
        ttl = Quota(max_record_bytes=1024, max_total_bytes=4096, max_records=16, ttl_seconds=60)
        guest_scope = scope(owner=None)
        guest = AppDataStore(self.root, guest_scope, quota=ttl, now=clock)
        saved = guest.save(self.app, "documents", "scratch")
        clock.value += 61
        with self.assertRaises(EphemeralExpired):
            guest.read(self.app, "documents", saved["id"])
        # Expiry scrub removed the guest tree.
        self.assertFalse(any((self.root / "guest").glob(guest_scope.scope)))

        guest2 = AppDataStore(self.root, scope(owner=None), quota=ttl, now=clock)
        guest2.save(self.app, "documents", "scratch two")
        clock.value += 61
        with mock.patch("aios.app_data.shutil.rmtree", side_effect=OSError("device busy")):
            with self.assertRaises(EphemeralExpired):
                guest2.read(self.app, "documents", "00000000-0000-0000-0000-000000000000")
        quarantined = list((self.root / "guest" / ".quarantine").iterdir())
        self.assertEqual(len(quarantined), 1)
        # Quarantined data is unreachable through the API and not silently gone.
        fresh = AppDataStore(self.root, scope(owner=None), quota=ttl, now=clock)
        self.assertEqual(fresh.list(self.app)["kinds"]["documents"], [])

    # ------------------------------------------------ fail-closed integrity

    def test_tampered_record_fails_closed(self):
        store = self.store()
        saved = store.save(self.app, "documents", "important")
        path = next((self.root / "apps" / self.owner / self.app).rglob(saved["id"] + ".json"))
        envelope = json.loads(path.read_text())
        envelope["data"] = "tampered"
        path.write_text(json.dumps(envelope))
        with self.assertRaises(IntegrityProblem):
            store.read(self.app, "documents", saved["id"])
        envelope2 = json.loads(path.read_text())
        envelope2["format"] = 99
        path.write_text(json.dumps(envelope2))
        with self.assertRaises(IntegrityProblem):
            store.read(self.app, "documents", saved["id"])
        # A corrupt record does not block listing other records.
        keep = store.save(self.app, "documents", "other")
        ids = [item["id"] for item in store.list(self.app)["kinds"]["documents"]]
        self.assertIn(keep["id"], ids)

    def test_stray_temp_files_recovered_without_data_loss(self):
        store = self.store()
        saved = store.save(self.app, "documents", "durable note")
        live = next(p for p in (self.root / "apps" / self.owner / self.app).iterdir() if p.name.startswith("v"))
        stray = live / "documents" / ".pending-crash"
        stray.write_text("half-written junk")
        store.recover_all()
        self.assertFalse(stray.exists())
        self.assertEqual(store.read(self.app, "documents", saved["id"], with_data=True)["data"], "durable note")

    # ------------------------------------------------------ migration

    def test_upgrade_rolls_forward_and_back_preserving_data(self):
        store = self.store()
        first = store.save(self.app, "documents", "hello")
        second = store.save(self.app, "records", b"\x01\x02")

        def upper(kind, record):
            if kind == "documents":
                return record["data"].upper()
            return None  # drop non-documents in the new layout

        result = store.upgrade(self.app, 2, upper)
        self.assertEqual(result["rollback_available"], "v1")
        self.assertEqual(store.read(self.app, "documents", first["id"], with_data=True)["data"], "HELLO")
        with self.assertRaises(UnknownRecord):
            store.read(self.app, "records", second["id"])
        store.rollback(self.app, 1)
        self.assertEqual(store.read(self.app, "documents", first["id"], with_data=True)["data"], "hello")
        self.assertEqual(store.read(self.app, "records", second["id"])["size"], 2)
        store.prune_preserved_upgrade(self.app)
        with self.assertRaises(IntegrityProblem):
            store.rollback(self.app, 2)

    def test_failed_migration_keeps_old_layout_authoritative(self):
        store = self.store()
        saved = store.save(self.app, "documents", "keep me")
        boom = {"n": 0}

        def explode(kind, record):
            boom["n"] += 1
            raise RuntimeError("migrator crashed")

        with self.assertRaises(RuntimeError):
            store.upgrade(self.app, 2, explode)
        # Fail-closed until recovery runs: a partially staged layout is ambiguous.
        with self.assertRaises(IntegrityProblem):
            store.read(self.app, "documents", saved["id"])
        self.assertEqual(store.recover_all(), [self.app])
        # Recovery discards the staging layout; the previous data is intact.
        self.assertEqual(store.read(self.app, "documents", saved["id"], with_data=True)["data"], "keep me")
        live = next(p for p in (self.root / "apps" / self.owner / self.app).iterdir()
                    if p.is_dir() and p.name.startswith("v"))
        self.assertEqual(live.name, "v1")
        self.assertFalse((self.root / "apps" / self.owner / self.app / "migration.json").exists())

    def test_interrupted_swap_completes_on_recovery(self):
        store = self.store()
        saved = store.save(self.app, "documents", "durable")
        store.upgrade(self.app, 2, lambda kind, record: record["data"])
        # Simulate a crash between symlink swap and journal completion by
        # re-writing the journal to the pre-swap state.
        app_root = self.root / "apps" / self.owner / self.app
        journal = app_root / "migration.json"
        journal.write_text(json.dumps({"from": 1, "to": 2, "status": "staged"}))
        store.recover_all()
        self.assertEqual(json.loads(journal.read_text())["status"], "complete")
        self.assertEqual(store.read(self.app, "documents", saved["id"], with_data=True)["data"], "durable")
        # Rollback still reaches the preserved layout after recovery.
        store.rollback(self.app, 1)
        self.assertEqual(store.read(self.app, "documents", saved["id"])["version"], 1)

    # ---------------------------------------------------- export/import

    def test_export_import_roundtrip(self):
        store = self.store()
        text = store.save(self.app, "documents", "note éxport")
        blob = store.save(self.app, "records", os.urandom(64))
        bundle = store.export(self.app)
        json.dumps(bundle)  # JSON-safe
        fresh = str(uuid.uuid4())
        target = self.store()
        report = target.import_bundle(fresh, {**bundle, "app": fresh})
        self.assertEqual(report["imported"], 2)
        self.assertEqual(target.read(fresh, "documents", text["id"], with_data=True)["data"], "note éxport")
        self.assertEqual(target.read(fresh, "records", blob["id"])["size"], 64)
        # Round-trip preserves exact payload hashes.
        reexport = target.export(fresh)
        by_id = {r["id"]: r["sha256"] for k in reexport["kinds"].values() for r in k}
        self.assertEqual(by_id[text["id"]], text["sha256"])
        self.assertEqual(by_id[blob["id"]], blob["sha256"])

    def test_into_empty_guard_and_replace_atomicity(self):
        store = self.store()
        saved = store.save(self.app, "documents", "original")
        bundle = store.export(self.app)
        bundle["app"] = self.app
        with self.assertRaises(IntegrityProblem):
            store.import_bundle(self.app, bundle, mode="into-empty")
        # Corrupt bundle is rejected in full before any filesystem change.
        broken = json.loads(json.dumps(bundle))
        broken["kinds"]["documents"][0]["data"] = "corrupted"
        with self.assertRaises(IntegrityProblem):
            store.import_bundle(self.app, broken, mode="replace")
        self.assertEqual(store.read(self.app, "documents", saved["id"], with_data=True)["data"], "original")
        # Replace swap preserves the prior layout as rollback material.
        other = self.store()
        changed = json.loads(json.dumps(bundle))
        report = other.import_bundle(self.app, changed, mode="replace")
        self.assertEqual(report["imported"], 1)
        self.assertEqual(other.read(self.app, "documents", saved["id"], with_data=True)["data"], "original")

    def test_bundle_validation_rejects_foreign_and_malformed(self):
        store = self.store()
        store.save(self.app, "documents", "x")
        bundle = store.export(self.app)
        with self.assertRaises(ValueError):
            store.import_bundle(str(uuid.uuid4()), bundle)
        for mutate in (
            lambda b: b.pop("app"),
            lambda b: b.__setitem__("format", "aios-appdata/2"),
            lambda b: b["kinds"].__setitem__("secrets", []),
            lambda b: b["kinds"]["documents"][0].pop("sha256"),
            lambda b: b["kinds"]["documents"][0].update({"id": "../../etc/pwned"}),
        ):
            broken = json.loads(json.dumps(bundle))
            mutate(broken)
            with self.subTest(bundle=mutate), self.assertRaises(ValueError):
                app_data.AppDataStore.validate_export(broken)


class PrincipalValidationTests(unittest.TestCase):
    def test_scope_identity_checks(self):
        with self.assertRaises(ValueError):
            AppScope(owner="alice", uid=1500, scope=str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            AppScope(owner=str(uuid.uuid4()), uid=1500, scope="session-1")
        with self.assertRaises(ValueError):
            AppScope(owner=str(uuid.uuid4()), uid="1500", scope=str(uuid.uuid4()))
        guest = AppScope(owner=None, uid=65534, scope=str(uuid.uuid4()))
        self.assertTrue(guest.guest)

    def test_store_requires_scope(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(ValueError):
            AppDataStore(tmp.name, {"owner": None})
        self.assertTrue(issubclass(AppDataError, ValueError))


if __name__ == "__main__":
    unittest.main()
