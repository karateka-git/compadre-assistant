"""Проверяет сводку SQLite и аннотации тегов без GitHub и VPS."""

from dataclasses import replace
import unittest
from unittest.mock import Mock

from scripts.release_sqlite_summary import (
    MIGRATIONS_PREFIX,
    InstallationSchemaEvidence,
    build_summary,
    expected_migration_at,
    expected_migration_from_manifest,
    read_installation_tag,
)


VERSION = "v3.2.0"
SHA = "a" * 40
OBJECT = "b" * 40
GENERATION = "c" * 32
MIGRATION = "0002_new_table.sql"


def _reference(kind: str, sha: str) -> dict:
    return {"ref": f"refs/tags/production/{VERSION}", "object": {"type": kind, "sha": sha}}


def _annotation(evidence: InstallationSchemaEvidence, **overrides) -> dict:
    return {"sha": OBJECT, "tag": f"production/{VERSION}", "message": evidence.message() + "\n",
            "object": {"type": "commit", "sha": SHA}} | overrides


class ReleaseSQLiteSummaryTests(unittest.TestCase):
    def test_rejects_unconfirmed_or_potentially_truncated_candidate_directory(self):
        path = MIGRATIONS_PREFIX + "0001_initial_schema.sql"
        entry = {"name": "0001_initial_schema.sql", "path": path, "type": "file"}
        api = Mock()
        for listing in ([entry] * 1000, [entry | {"type": "symlink"}], [entry | {"path": "elsewhere"}]):
            with self.subTest(entries=len(listing)), self.assertRaises(ValueError):
                api.get.return_value = listing
                expected_migration_at(api, SHA)
        api.get.assert_called_with(f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}", ref=SHA)

    def test_expected_migration_from_candidate_manifest(self):
        paths = [
            f"{MIGRATIONS_PREFIX}{MIGRATION}",
            f"{MIGRATIONS_PREFIX}README.md",
            f"{MIGRATIONS_PREFIX}tests/0009_sample.sql",
            "example/9999_ignore.sql",
            f"{MIGRATIONS_PREFIX}0001_initial_schema.sql",
        ]
        self.assertEqual(expected_migration_from_manifest(paths), MIGRATION)
        for wrong in (paths[:-1], [*paths, paths[-1]], [f"{MIGRATIONS_PREFIX}migration.sql"]):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                expected_migration_from_manifest(wrong)

    def test_annotation_from_receipt_is_stable_across_runtime_restart(self):
        receipt = {"schema_version": 1, "version": VERSION, "sha": SHA, "kind": "ordinary",
                   "release_id": 4, "run_id": 5, "run_attempt": 1, "generation_id": GENERATION,
                   "migration": MIGRATION, "runtime_id": "d" * 64}
        evidence = InstallationSchemaEvidence.from_receipt(receipt, VERSION, SHA)
        self.assertEqual(InstallationSchemaEvidence.from_message(evidence.message() + "\n"), evidence)
        self.assertEqual(InstallationSchemaEvidence.from_receipt(
            receipt | {"runtime_id": "e" * 64}, VERSION, SHA,
        ), evidence)
        for change in ({"runtime_id": "wrong"}, {"generation_id": "legacy"},
                       {"release_id": True}, {"sha": "f" * 40}, {"migration": "0000_wrong.sql"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                InstallationSchemaEvidence.from_receipt(receipt | change, VERSION, SHA)

    def test_noncanonical_tag_messages_rejected(self):
        message = InstallationSchemaEvidence(4, 5, 1, GENERATION, MIGRATION).message()
        for invalid in (message.replace('"version":1', '"version":2'),
                        message.replace('"version":1', '"version":true'),
                        message.replace('"version":1', '"version":1,"version":1'),
                        " " + message):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                InstallationSchemaEvidence.from_message(invalid)

    def test_summary_pending_legacy_and_accepted(self):
        pending = build_summary(VERSION, SHA, 4, 5, 1, MIGRATION, None)
        self.assertEqual(pending["installation_status"], "pending")
        self.assertIsNone(pending["applied_migration"])
        legacy = read_installation_tag(_reference("commit", SHA), VERSION, SHA)
        self.assertEqual(build_summary(VERSION, SHA, 4, 5, 1, MIGRATION, legacy),
                         pending | {"installation_status": "legacy_tag"})

        evidence = InstallationSchemaEvidence(4, 5, 1, GENERATION, MIGRATION)
        tag = read_installation_tag(_reference("tag", OBJECT), VERSION, SHA,
                                    lambda _: _annotation(evidence))
        self.assertEqual(tag.object_sha, OBJECT)
        self.assertEqual(build_summary(VERSION, SHA, 4, 5, 1, MIGRATION, tag),
                         pending | {"installation_status": "accepted", "generation_id": GENERATION,
                                    "applied_migration": MIGRATION, "installation_tag_object_sha": OBJECT})

    def test_conflicting_release_commit_tag_and_migrations_rejected(self):
        evidence = InstallationSchemaEvidence(4, 5, 1, GENERATION, MIGRATION)
        for changed in ({"sha": "f" * 40}, {"tag": "production/v3.1.0"},
                        {"object": {"type": "tag", "sha": SHA}},
                        {"object": {"type": "commit", "sha": "f" * 40}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                read_installation_tag(_reference("tag", OBJECT), VERSION, SHA,
                                      lambda _: _annotation(evidence, **changed))
        with self.assertRaises(ValueError):
            read_installation_tag(_reference("commit", "f" * 40), VERSION, SHA)
        for other in (replace(evidence, release_id=8), replace(evidence, run_attempt=2),
                      replace(evidence, migration="0003_other.sql")):
            with self.subTest(other=other):
                tag = read_installation_tag(_reference("tag", OBJECT), VERSION, SHA,
                                            lambda _: _annotation(other))
                with self.assertRaises(ValueError):
                    build_summary(VERSION, SHA, 4, 5, 1, MIGRATION, tag)


if __name__ == "__main__":
    unittest.main()
