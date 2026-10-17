"""Проверки отдельной CLI-команды миграций на временных SQLite-файлах."""

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import (
    MIGRATIONS_DIR, apply_migrations,
)
from compadre_assistant.presentation.cli.main import main, parse_args


class MigrateCommandTest(unittest.TestCase):
    """Проверяет выбор БД, отсутствие runtime и результат применения схемы."""

    def setUp(self) -> None:
        """Изолирует данные и запрещает загрузку runtime и внешние запросы."""
        directory = TemporaryDirectory(prefix="compadre-cli-migrate-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "nested" / "app.db"
        self.logger = Mock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("compadre_assistant.core.data.config.load_dotenv", None))
        stack.enter_context(patch("compadre_assistant.presentation.cli.main.configure_logging"))
        stack.enter_context(patch(
            "compadre_assistant.presentation.cli.main.logging.getLogger",
            return_value=self.logger,
        ))
        for target in (
            "compadre_assistant.presentation.cli.main.load_config",
            "compadre_assistant.presentation.cli.main.app_bootstrap.load_obsidian_profile",
            "compadre_assistant.presentation.cli.main.run_telegram_bot",
            "compadre_assistant.presentation.cli.main.run_vk_bot",
            "httpx.Client.send",
            "urllib.request.urlopen",
        ):
            stack.enter_context(patch(
                target, side_effect=AssertionError("Миграции не собирают runtime и не используют сеть"),
            ))

    def run_cli(self, args: list[str], env: dict[str, str]) -> int:
        """Вызывает настоящий main с изолированными CLI и окружением."""
        with patch.dict(os.environ, env, clear=True), patch(
            "sys.argv", ["compadre-assistant", *args],
        ):
            return main()

    def test_minimal_environment_initializes_database_and_repeat_preserves_data(self) -> None:
        """Только DATABASE_PATH достаточно; повтор не изменяет данные и журнал."""
        env = {"DATABASE_PATH": str(self.database)}
        self.assertEqual(self.run_cli(["--migrate"], env), 0)
        with connect_database(self.database) as connection:
            connection.execute("INSERT INTO app_users (display_name) VALUES ('Fixture')")
            connection.commit()
            before = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")]
        self.logger.reset_mock()
        self.assertEqual(self.run_cli(["--migrate"], env), 0)
        with connect_database(self.database) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")], before)
            self.assertEqual(connection.execute("SELECT display_name FROM app_users").fetchone()[0], "Fixture")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertIn("Database schema is up to date", str(self.logger.method_calls))

    def test_invalid_unrelated_settings_do_not_block_migrations(self) -> None:
        """Ошибки каналов и провайдеров не влияют на отдельную команду схемы."""
        env = {
            "DATABASE_PATH": str(self.database), "APP_DEBUG": "invalid",
            "ENABLED_CHANNELS": "unknown", "ENABLED_CAPABILITIES": "???",
            "TELEGRAM_PROXY_URL": "bad", "VK_GROUP_ID": "bad",
            "GITHUB_APP_ID": "bad", "OPENAI_API_KEY": "secret-sentinel",
            "ASK_BASE_URL": "bad", "EMBEDDING_DIMENSION": "bad",
        }
        self.assertEqual(self.run_cli(["--migrate"], env), 0)
        self.assertNotIn("secret-sentinel", str(self.logger.method_calls))

    def test_explicit_path_overrides_environment_without_loading_dotenv(self) -> None:
        """Явный путь не открывает БД окружения и не читает локальный dotenv."""
        other_database = self.root / "wrong.db"
        with patch(
            "compadre_assistant.core.data.config.load_dotenv",
            side_effect=AssertionError("Явный путь не требует dotenv"),
        ):
            self.assertEqual(self.run_cli(
                ["--migrate", "--database-path", str(self.database)],
                {"DATABASE_PATH": str(other_database)},
            ), 0)
        self.assertFalse(other_database.exists())
        self.assertTrue(self.database.exists())

    def test_environment_path_can_be_loaded_from_dotenv(self) -> None:
        """Без явного пути используется существующее соглашение загрузки dotenv."""
        def fill_environment() -> None:
            os.environ["DATABASE_PATH"] = str(self.database)

        with patch("compadre_assistant.core.data.config.load_dotenv", side_effect=fill_environment) as loader:
            self.assertEqual(self.run_cli(["--migrate"], {}), 0)
        loader.assert_called_once_with()

    def test_missing_path_returns_configuration_error(self) -> None:
        """Отсутствующий/пустой путь возвращает 2 до открытия SQLite."""
        for value in (None, "", "   "):
            with self.subTest(value=value):
                env = {} if value is None else {"DATABASE_PATH": value}
                self.assertEqual(self.run_cli(["--migrate"], env), 2)
        self.assertFalse(self.database.exists())

    def test_unavailable_path_and_corrupt_database_return_failure(self) -> None:
        """Файловая ошибка и не-SQLite файл дают код 1 и понятную диагностику."""
        invalid = self.root / "not-sqlite.db"
        invalid.write_bytes(b"this is not a sqlite database")
        for path in (self.root, invalid):
            with self.subTest(path=path):
                self.assertEqual(self.run_cli(["--migrate", "--database-path", str(path)], {}), 1)
        self.assertEqual(invalid.read_bytes(), b"this is not a sqlite database")

    def test_failed_pending_migration_returns_failure_and_rolls_back(self) -> None:
        """Ошибка второй миграции не оставляет её DDL/данные или запись журнала."""
        self.assertEqual(self.run_cli(["--migrate"], {"DATABASE_PATH": str(self.database)}), 0)
        migrations = self.root / "migrations"
        migrations.mkdir()
        initial = MIGRATIONS_DIR / "0001_initial_schema.sql"
        (migrations / initial.name).write_bytes(initial.read_bytes())
        (migrations / "0002_failing.sql").write_text(
            "CREATE TABLE partial_change (id INTEGER);\n"
            "INSERT INTO app_users (display_name) VALUES ('Rolled back');\n"
            "INSERT INTO missing_table VALUES (1);\n", encoding="utf-8",
        )
        with patch(
            "compadre_assistant.presentation.cli.main.apply_migrations",
            side_effect=lambda connection: apply_migrations(connection, migrations),
        ):
            self.assertEqual(self.run_cli(["--migrate"], {"DATABASE_PATH": str(self.database)}), 1)
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'partial_change'").fetchone())

    def test_argument_conflicts_are_rejected(self) -> None:
        """Миграции нельзя совмещать с каналами, другим режимом или чужими опциями."""
        for args in (
            ["--migrate", "--telegram-bot"], ["--migrate", "--vk-bot"],
            ["--migrate", "--healthcheck"], ["--migrate", "--channels", "telegram"],
            ["--database-path", str(self.database)],
        ):
            with self.subTest(args=args), patch("sys.argv", ["compadre-assistant", *args]), patch("sys.stderr"):
                with self.assertRaises(SystemExit) as error:
                    parse_args()
                self.assertEqual(error.exception.code, 2)

    def test_module_entrypoint_exits_after_migrations(self) -> None:
        """Настоящий subprocess без токенов создаёт схему и завершается при повторе."""
        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.update(PYTHONPATH=str(project_root), ENABLED_CHANNELS="invalid", APP_DEBUG="invalid")
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-m", "compadre_assistant", "--migrate", "--database-path", str(self.database)],
                cwd=self.root, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
