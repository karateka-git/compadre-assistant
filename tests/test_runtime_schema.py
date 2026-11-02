"""Проверки закрытого допуска будущей схемы и CLI без polling."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, MigrationError, apply_migrations
from compadre_assistant.core.data.sqlite.runtime_schema import (
    ADMISSION_ENV, LAUNCH_ENV, migration_sql_fingerprint, prepare_runtime_schema,
    schema_fingerprint, validate_compatible_schema, validate_release_pair,
)
from compadre_assistant.presentation.cli.main import initialize_database, main, run_process_url_command
from compadre_assistant.core.data.config import AppConfig


class RuntimeSchemaTest(unittest.TestCase):
    """Сверяет точную пару, структуру, журнал и отсутствие SQL-побочных эффектов."""

    def setUp(self) -> None:
        """Создаёт только локальную синтетическую будущую БД и тестовый допуск."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "app.db"
        with connect_database(self.database) as db:
            apply_migrations(db)
            base = [list(row) for row in db.execute("SELECT version, name FROM schema_migrations ORDER BY version")]
            db.execute("CREATE TABLE future_data (value TEXT)")
            db.execute("INSERT INTO future_data VALUES ('preserve')")
            db.execute("INSERT INTO schema_migrations (version, name) VALUES (5, '0005_future.sql')")
            db.commit()
            fingerprint = schema_fingerprint(db)
        self.context = dict(base_sha="a" * 40, base_image_id="sha256:" + "b" * 64,
                            candidate_sha="c" * 40, candidate_image_id="sha256:" + "d" * 64,
                            controller_sha="e" * 40, verification_id="f" * 64)
        self.admission = dict(self.context, format_version=1, base_journal=base,
                              target_journal=base + [[5, "0005_future.sql"]],
                              base_sql_sha256=migration_sql_fingerprint(), schema_sha256=fingerprint)
        self.admission_path = self.root / "admission.json"
        self.launch_path = self.root / "launch.json"
        self.admission_path.write_text(json.dumps(self.admission), encoding="utf-8")
        self.launch_path.write_text(json.dumps(self.context), encoding="utf-8")
        self.env = {ADMISSION_ENV: str(self.admission_path), LAUNCH_ENV: str(self.launch_path)}

    def test_exact_admission_repeat_parallel_and_no_unknown_sql(self) -> None:
        """Повторные и параллельные старты сохраняют данные, журнал и структуру."""
        def run() -> list:
            with connect_database(self.database) as db:
                statements = []
                db.set_trace_callback(statements.append)
                result = validate_compatible_schema(db, self.admission, self.context)
                self.assertFalse(any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")) for sql in statements))
                self.assertEqual(db.execute("SELECT value FROM future_data").fetchone()[0], "preserve")
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: run(), range(4))), [[], [], [], []])

    def test_mismatched_identity_or_contract_is_rejected_without_ddl(self) -> None:
        """Иная пара, SQL digest, история, формат и fingerprint закрывают допуск."""
        changes = [(key, "0" * (64 if key == "verification_id" else 40)) for key in ("base_sha", "candidate_sha", "controller_sha", "verification_id")]
        changes += [("base_image_id", "sha256:" + "0" * 64), ("candidate_image_id", "sha256:" + "0" * 64),
                    ("schema_sha256", "0" * 64), ("base_sql_sha256", "0" * 64), ("format_version", True),
                    ("target_journal", self.admission["base_journal"]),
                    ("target_journal", self.admission["base_journal"] + [[6, "0006_future.sql"]]),
                    ("target_journal", self.admission["base_journal"] + [[5, "0005_other.sql"]]),
                    ("base_journal", [[1, "0001_other.sql"]])]
        for key, value in changes:
            with self.subTest(key=key, value=value), connect_database(self.database) as db:
                before = list(db.iterdump())
                admission = copy.deepcopy(self.admission)
                admission[key] = value
                statements = []
                db.set_trace_callback(statements.append)
                with self.assertRaises(MigrationError):
                    validate_compatible_schema(db, admission, self.context)
                db.set_trace_callback(None)
                self.assertEqual(list(db.iterdump()), before)
                self.assertFalse(any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")) for sql in statements))

    def test_same_journal_changed_schema_and_missing_journal_fail(self) -> None:
        """DDL без новой версии и пустая БД не получают права по старому допуску."""
        with connect_database(self.database) as db:
            db.execute("ALTER TABLE future_data ADD COLUMN extra TEXT")
            with self.assertRaises(MigrationError):
                validate_compatible_schema(db, self.admission, self.context)
        with sqlite3.connect(":memory:") as db:
            with self.assertRaises(MigrationError):
                validate_compatible_schema(db, self.admission, self.context)
            self.assertEqual(db.execute("SELECT * FROM sqlite_schema").fetchall(), [])

    def test_strict_mode_and_migrate_ignore_runtime_admission(self) -> None:
        """Без допуска новая схема отвергается; --migrate остаётся строгим с допуском."""
        with patch.dict(os.environ, {}, clear=True), connect_database(self.database) as db:
            with self.assertRaises(MigrationError):
                prepare_runtime_schema(db)
        with patch.dict(os.environ, self.env, clear=True):
            self.assertTrue(initialize_database(self.database, Mock()))
            with patch("sys.argv", ["app", "--migrate", "--database-path", str(self.database)]):
                self.assertEqual(main(), 1)

    def test_invalid_file_or_partial_context_has_no_fallback(self) -> None:
        """Ошибочный JSON, дубли, неизвестные поля и неполный контекст закрывают startup."""
        payloads = ["{", "[]", '{"format_version":1,"format_version":1}', json.dumps({**self.admission, "allow_unknown": True})]
        for payload in payloads:
            self.admission_path.write_text(payload, encoding="utf-8")
            with patch.dict(os.environ, self.env, clear=True), connect_database(self.database) as db:
                with self.assertRaises(MigrationError):
                    prepare_runtime_schema(db)
        with patch.dict(os.environ, {LAUNCH_ENV: str(self.launch_path)}, clear=True), connect_database(self.database) as db:
            with self.assertRaises(MigrationError):
                prepare_runtime_schema(db)
        incomplete = dict(self.admission)
        del incomplete["verification_id"]
        with self.assertRaises(MigrationError):
            validate_release_pair(incomplete, self.context)

    def test_process_url_uses_admission_before_use_case(self) -> None:
        """Прямой process-url проходит тот же допуск до предметной обработки."""
        result = Mock(error=None)
        with patch.dict(os.environ, self.env, clear=True), patch(
            "compadre_assistant.presentation.cli.main.process_url_for_cli", return_value=result,
        ) as process:
            self.assertEqual(run_process_url_command(database_path=self.database, source_url="https://example.com", logger=Mock()), 0)
            process.assert_called_once()
            self.launch_path.write_text("{}", encoding="utf-8")
            process.reset_mock()
            self.assertEqual(run_process_url_command(database_path=self.database, source_url="https://example.com", logger=Mock()), 1)
            process.assert_not_called()

    def test_channel_cli_refuses_before_polling_and_accepts_exact_pair(self) -> None:
        """Оба входа каналов проверяют допуск перед передачей управления adapter."""
        for channel in ("telegram", "vk"):
            config = AppConfig(app_env="test", database_path=self.database, telegram_bot_token="", enabled_capabilities=())
            prefix = "compadre_assistant.presentation.cli.main."
            with self.subTest(channel=channel), patch.dict(os.environ, self.env, clear=True), patch(
                "sys.argv", ["app", f"--{channel}-bot"],
            ), patch(prefix + "load_config", return_value=config), patch(
                prefix + "app_bootstrap.load_obsidian_profile", return_value=None,
            ), patch(prefix + "check_runtime_configuration", return_value=True), patch(
                prefix + f"run_{channel}_bot_command", return_value=0,
            ) as runner:
                self.launch_path.write_text(json.dumps(self.context), encoding="utf-8")
                self.assertEqual(main(), 0)
                runner.assert_called_once()
                runner.reset_mock()
                self.launch_path.write_text("{}", encoding="utf-8")
                self.assertEqual(main(), 1)
                runner.assert_not_called()

    def test_validation_holds_lock_across_journal_and_fingerprint(self) -> None:
        """Параллельный writer не может изменить схему между двумя сверками."""
        import compadre_assistant.core.data.sqlite.runtime_schema as runtime_schema
        original = runtime_schema.schema_fingerprint
        with connect_database(self.database) as db, closing(sqlite3.connect(self.database, timeout=0)) as writer:
            def fingerprint(connection):
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("ALTER TABLE future_data ADD COLUMN concurrent TEXT")
                return original(connection)
            with patch.object(runtime_schema, "schema_fingerprint", side_effect=fingerprint):
                self.assertEqual(validate_compatible_schema(db, self.admission, self.context), [])
            self.assertFalse(db.in_transaction)

    def test_changed_local_sql_and_incomplete_local_history_fail(self) -> None:
        """Допуск не переносится на изменённые байты SQL или другой набор файлов."""
        migrations = self.root / "migrations"
        migrations.mkdir()
        for source in MIGRATIONS_DIR.glob("*.sql"):
            (migrations / source.name).write_bytes(source.read_bytes())
        first = next(migrations.glob("0001_*.sql"))
        first.write_bytes(first.read_bytes() + b"\n-- modified\n")
        with connect_database(self.database) as db:
            with self.assertRaises(MigrationError):
                validate_compatible_schema(db, self.admission, self.context, migrations)
            first.unlink()
            with self.assertRaises(MigrationError):
                validate_compatible_schema(db, self.admission, self.context, migrations)
