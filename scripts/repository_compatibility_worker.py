"""Внешний worker: импортирует только выбранный точный checkout и его tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.util
import importlib.machinery
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import sys
import sysconfig
from tempfile import TemporaryDirectory
import threading
import traceback
import unittest


class CheckoutImports(importlib.abc.MetaPathFinder):
    """Не допускает загрузку project/tests/helpers из installed или чужого checkout."""

    def __init__(self, checkout: Path) -> None:
        """Запоминает корни исходников и имена прежних helpers до первого импорта."""
        self.checkout = checkout
        self.names = {"compadre_assistant", "document_chunker", "tests", "scripts"}
        self.names.update(p.stem for p in (checkout / "tests").glob("*.py"))
        self.origins: dict[str, str] = {}

    def check(self, name: str, origin: str | None) -> None:
        """Проверяет фактический origin контролируемого модуля.

        Raises:
            ImportError: Модуль разрешился вне выбранного checkout.
        """
        if name.split(".")[0] not in self.names:
            return
        if not origin or not Path(origin).resolve().is_relative_to(self.checkout):
            raise ImportError(f"import leakage: {name}: {origin}")
        self.origins[name] = str(Path(origin).resolve())

    def find_spec(self, fullname: str, path=None, target=None):
        """Проверяет origin до исполнения Python модуля выбранного проекта."""
        if fullname.split(".")[0] not in self.names:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            raise ImportError(f"missing checkout module: {fullname}")
        # Namespace packages допустимы только с каталогами этого checkout.
        if spec.origin is None and spec.submodule_search_locations:
            if any(not Path(p).resolve().is_relative_to(self.checkout) for p in spec.submodule_search_locations):
                raise ImportError(f"namespace import leakage: {fullname}")
        else:
            self.check(fullname, spec.origin)
        return spec

    def verify_loaded(self) -> None:
        """Повторяет проверку реальных __file__ после тестов, включая helpers."""
        for name, module in tuple(sys.modules.items()):
            if getattr(module, "__file__", None):
                self.check(name, module.__file__)


def assert_database(connection: sqlite3.Connection, names: list[str]) -> str:
    """Проверяет журнал, FK/integrity и возвращает fingerprint текущей схемы."""
    actual = [row[0] for row in connection.execute("SELECT name FROM schema_migrations ORDER BY version")]
    if actual != names:
        raise AssertionError(f"migration journal mismatch: {actual} != {names}")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise AssertionError("integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise AssertionError("foreign_key_check failed")
    schema = [tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name")]
    return hashlib.sha256(json.dumps(schema).encode()).hexdigest()


def run_tests(modules: list[str], external_tests: Path | None = None) -> dict:
    """Исполняет неизменные unittest и сохраняет IDs и раздельные ошибки."""
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    if external_tests is not None:
        spec = importlib.util.spec_from_file_location("repository_compatibility_cases", external_tests)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load external compatibility tests: {external_tests}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))

    def ids(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                yield from ids(test)
            else:
                yield test.id()

    test_ids = list(ids(suite))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return {"passed": result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
            "tests_run": result.testsRun, "test_ids": test_ids,
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "skips": [(test.id(), reason) for test, reason in result.skipped]}


def seed_database(database: Path) -> dict:
    """Создаёт upgrade fixture старым API на прежних миграциях."""
    from compadre_assistant.core.data.sqlite.connection import connect_database
    from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
    from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository
    from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteExternalIdentityRepository
    from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
    from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
    from compadre_assistant.core.domain.users.entities import IncomingIdentity
    from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
    from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
    from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import SQLiteArticleAnalysisResultRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import SQLiteIncomingMessageRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_repository import SQLiteGitHubInstallationRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import SQLiteObsidianVaultRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import SQLiteVaultInstructionRepository
    from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
    from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
    from compadre_assistant.capabilities.obsidian.domain.articles.incoming_messages import ArticleIncomingMessage
    from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote, VaultInstruction

    with connect_database(database) as connection:
        apply_migrations(connection)
        # Связанные строки создаются старым API до подстановки SQL кандидата.
        user = SQLiteAppUserRepository(connection).create(display_name="Compatibility fixture")
        identity = SQLiteExternalIdentityRepository(connection).create(
            app_user_id=user.id, identity=IncomingIdentity("telegram", "42", "42", username="previous"),
        )
        SQLiteUserCapabilityPreferenceRepository(connection).upsert(
            UserCapabilityPreference(user.id, "obsidian", True)
        )
        SQLiteChatModeRepository(connection).save(
            ChatMode(ChatModeKey(user.id, "telegram", "42"), "obsidian")
        )
        article = SQLiteArticleRepository(connection).create(Article(
            app_user_id=user.id, source_url="https://example.org/previous",
            normalized_url="https://example.org/previous", title="Previous article",
            cleaned_text="Previous text", text_hash="previous-hash",
        ))
        analysis = SQLiteArticleAnalysisResultRepository(connection).save(ArticleAnalysisResult(
            article_id=article.id, app_user_id=user.id, llm_model="previous-model",
            prompt_version="v1", result_text="Previous summary",
        ))
        incoming = SQLiteIncomingMessageRepository(connection).save(ArticleIncomingMessage(
            channel="telegram", chat_id="42", message_id="previous-message",
            text="https://example.org/previous", app_user_id=user.id,
        ))
        SQLiteIncomingMessageRepository(connection).link_to_article(
            incoming_message_id=incoming.id, article_id=article.id,
        )
        connection.execute(
            "INSERT INTO github_accounts (app_user_id, github_user_id, login) VALUES (?, ?, ?)",
            (user.id, 701, "previous-user"),
        )
        connection.commit()
        SQLiteGitHubInstallationRepository(connection).replace_for_user(
            app_user_id=user.id, installation_ids={101},
        )
        vault = SQLiteObsidianVaultRepository(connection).replace(ObsidianVault(
            app_user_id=user.id, installation_id=101, repository_id=501,
            owner="octocat", repository="notes", branch="main",
        ))
        note = SQLiteVaultNoteRepository(connection).upsert(VaultNote(
            app_user_id=user.id, vault_id=vault.id, path="previous.md",
            blob_sha="previous-blob", markdown="# Previous note",
        ))
        instruction = SQLiteVaultInstructionRepository(connection).replace_for_vault(
            app_user_id=user.id, vault_id=vault.id,
            instructions=(VaultInstruction(app_user_id=user.id, vault_id=vault.id,
                position=0, path="AGENTS.md.txt", blob_sha="previous-rule",
                content="Previous instruction"),),
        )[0]
        fingerprint = assert_database(connection, sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql")))
    return {"passed": True, "fixture_scope": "user/identity/preference/chat/article/analysis/incoming/account/installation/vault/note/instruction",
            "user_id": user.id, "identity_id": identity.id,
            "article_id": article.id, "analysis_id": analysis.id,
            "incoming_id": incoming.id, "vault_id": vault.id,
            "note_id": note.id, "instruction_id": instruction.id,
            "schema_fingerprint": fingerprint}


def candidate_migrations(database: Path, previous_sql: Path) -> dict:
    """Проверяет fresh/upgrade, точные SQL bytes и старые смысловые поля."""
    from compadre_assistant.core.data.sqlite.connection import connect_database
    from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
    from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository
    from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteExternalIdentityRepository
    from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
    from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
    from compadre_assistant.core.domain.users.entities import IncomingIdentity
    from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
    from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
    from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import SQLiteArticleAnalysisResultRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import SQLiteIncomingMessageRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
    from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import SQLiteVaultInstructionRepository
    from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
    from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
    from compadre_assistant.capabilities.obsidian.domain.articles.incoming_messages import ArticleIncomingMessage
    from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultNote, VaultInstruction

    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    previous_names = sorted(p.name for p in previous_sql.glob("*.sql"))
    if not previous_names or names[:len(previous_names)] != previous_names:
        raise AssertionError("previous SQL is not candidate prefix")
    for name in previous_names:
        if (previous_sql / name).read_bytes() != (MIGRATIONS_DIR / name).read_bytes():
            raise AssertionError(f"released SQL bytes changed: {name}")
    with TemporaryDirectory(prefix="candidate-fresh-") as temporary:
        with connect_database(Path(temporary) / "fresh.db") as connection:
            apply_migrations(connection)
            fingerprint = assert_database(connection, names)
            if apply_migrations(connection):
                raise AssertionError("fresh migrations are not idempotent")
    with connect_database(database) as connection:
        assert_database(connection, previous_names)
        before = [tuple(row) for row in connection.execute("SELECT id, display_name FROM app_users")]
        if len(before) != 1 or before[0][1] != "Compatibility fixture":
            raise AssertionError("previous seed missing")
        completed = apply_migrations(connection)
        if [m.name for m in completed] != names[len(previous_names):]:
            raise AssertionError("unexpected pending migrations")
        upgraded_fingerprint = assert_database(connection, names)
        after = [tuple(row) for row in connection.execute("SELECT id, display_name FROM app_users")]
        if before != after or apply_migrations(connection):
            raise AssertionError("upgrade lost fixture or is not idempotent")
        old_article = SQLiteArticleRepository(connection).find_by_normalized_url(
            "https://example.org/previous", before[0][0])
        old_vault = connection.execute(
            "SELECT id, repository FROM obsidian_vaults WHERE app_user_id = ?", (before[0][0],)
        ).fetchone()
        old_note = SQLiteVaultNoteRepository(connection).get_by_path(
            app_user_id=before[0][0], vault_id=old_vault[0], path="previous.md"
        ) if old_vault else None
        if not old_article or old_article.title != "Previous article" or old_article.text_hash != "previous-hash":
            raise AssertionError("upgrade lost old article semantics")
        if not old_vault or old_vault[1] != "notes" or not old_note or old_note.markdown != "# Previous note":
            raise AssertionError("upgrade lost old vault/note semantics")
        old_identity = SQLiteExternalIdentityRepository(connection).find(
            IncomingIdentity("telegram", "42", "42"))
        old_preferences = SQLiteUserCapabilityPreferenceRepository(connection).get_for_user(before[0][0])
        old_mode = SQLiteChatModeRepository(connection).get(
            ChatModeKey(before[0][0], "telegram", "42"))
        old_analysis = SQLiteArticleAnalysisResultRepository(connection).get_latest_for_article(old_article.id)
        old_incoming = SQLiteIncomingMessageRepository(connection).find_by_external_id(
            channel="telegram", chat_id="42", message_id="previous-message")
        old_instructions = SQLiteVaultInstructionRepository(connection).list_for_vault(
            app_user_id=before[0][0], vault_id=old_vault[0])
        if (not old_identity or old_identity.username != "previous"
                or old_preferences != (UserCapabilityPreference(before[0][0], "obsidian", True),)
                or not old_mode or old_mode.capability_id != "obsidian"
                or not old_analysis or old_analysis.result_text != "Previous summary"
                or not old_incoming or old_incoming.article_id != old_article.id
                or len(old_instructions) != 1 or old_instructions[0].content != "Previous instruction"):
            raise AssertionError("upgrade lost core or Obsidian linked semantics")
        candidate_user = SQLiteAppUserRepository(connection).create(display_name="Candidate fixture")
        SQLiteExternalIdentityRepository(connection).create(
            app_user_id=candidate_user.id,
            identity=IncomingIdentity("vk", "77", "77", username="candidate"),
        )
        SQLiteUserCapabilityPreferenceRepository(connection).upsert(
            UserCapabilityPreference(candidate_user.id, "obsidian", False))
        SQLiteChatModeRepository(connection).save(
            ChatMode(ChatModeKey(candidate_user.id, "vk", "77"), "ask"))
        candidate_article = SQLiteArticleRepository(connection).create(Article(
            app_user_id=candidate_user.id, source_url="https://example.org/candidate",
            normalized_url="https://example.org/candidate", title="Candidate article",
            cleaned_text="Candidate text", text_hash="candidate-hash",
        ))
        candidate_analysis = SQLiteArticleAnalysisResultRepository(connection).save(ArticleAnalysisResult(
            article_id=candidate_article.id, app_user_id=candidate_user.id,
            llm_model="candidate-model", prompt_version="v2",
            result_text="Candidate summary",
        ))
        candidate_incoming = SQLiteIncomingMessageRepository(connection).save(ArticleIncomingMessage(
            channel="vk", chat_id="77", message_id="candidate-message",
            text="https://example.org/candidate", app_user_id=candidate_user.id,
        ))
        SQLiteIncomingMessageRepository(connection).link_to_article(
            incoming_message_id=candidate_incoming.id, article_id=candidate_article.id)
        candidate_note = SQLiteVaultNoteRepository(connection).upsert(VaultNote(
            app_user_id=before[0][0], vault_id=old_vault[0], path="candidate.md",
            blob_sha="candidate-blob", markdown="# Candidate note",
        ))
        candidate_instruction = SQLiteVaultInstructionRepository(connection).replace_for_vault(
            app_user_id=before[0][0], vault_id=old_vault[0],
            instructions=(VaultInstruction(app_user_id=before[0][0], vault_id=old_vault[0],
                position=0, path="AGENTS.md.txt", blob_sha="candidate-rule",
                content="Candidate instruction"),),
        )[0]
        assert_database(connection, names)
    tests = run_tests(["tests.test_migration_runner"])
    return {"passed": tests["passed"], "fixture_scope": "user/identity/preference/chat/article/analysis/incoming/account/installation/vault/note/instruction; full table coverage pending",
            "fresh_fingerprint": fingerprint, "upgraded_fingerprint": upgraded_fingerprint,
            "candidate_user_id": candidate_user.id, "candidate_article_id": candidate_article.id,
            "candidate_analysis_id": candidate_analysis.id,
            "candidate_incoming_id": candidate_incoming.id,
            "candidate_note_id": candidate_note.id,
            "candidate_instruction_id": candidate_instruction.id,
            "migration_names": names, "candidate_tests": tests}


def old_behavior(modules: list[str], external_tests: Path | None = None,
                 database: Path | None = None) -> dict:
    """Наблюдает реальный старый apply_migrations, не заменяя его или assertions."""
    from compadre_assistant.core.data.sqlite import migration_runner

    names = sorted(p.name for p in migration_runner.MIGRATIONS_DIR.glob("*.sql"))
    fingerprints: set[str] = set()
    failures: list[str] = []
    calls = 0

    def observe(frame, event, arg):
        nonlocal calls
        if event != "return" or frame.f_code is not migration_runner.apply_migrations.__code__:
            return
        if Path(frame.f_locals["migrations_dir"]).resolve() != migration_runner.MIGRATIONS_DIR.resolve():
            return
        try:
            fingerprints.add(assert_database(frame.f_locals["connection"], names))
            calls += 1
        except Exception as error:
            failures.append(str(error))

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        tests = run_tests(modules, external_tests)
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
    if database is not None:
        from compadre_assistant.core.data.sqlite.connection import connect_database
        from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository
        from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteExternalIdentityRepository
        from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
        from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
        from compadre_assistant.core.domain.users.entities import IncomingIdentity
        from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
        from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
        from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
        from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import SQLiteArticleAnalysisResultRepository
        from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import SQLiteIncomingMessageRepository
        from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
        from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import SQLiteVaultInstructionRepository
        from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultNote, VaultInstruction
        with connect_database(database) as connection:
            users = SQLiteAppUserRepository(connection)
            candidate = connection.execute(
                "SELECT id FROM app_users WHERE display_name = 'Candidate fixture'"
            ).fetchone()
            vault = connection.execute(
                "SELECT id, app_user_id FROM obsidian_vaults WHERE repository = 'notes'"
            ).fetchone()
            if not candidate or users.get_by_id(candidate[0]).display_name != "Candidate fixture":
                raise AssertionError("previous API cannot read candidate user")
            article = SQLiteArticleRepository(connection).find_by_normalized_url(
                "https://example.org/candidate", candidate[0]
            )
            note = SQLiteVaultNoteRepository(connection).get_by_path(
                app_user_id=vault[1], vault_id=vault[0], path="candidate.md"
            ) if vault else None
            if not article or article.title != "Candidate article" or not note or note.markdown != "# Candidate note":
                raise AssertionError("previous API cannot read candidate records")
            identity = SQLiteExternalIdentityRepository(connection).find(
                IncomingIdentity("vk", "77", "77"))
            preference = SQLiteUserCapabilityPreferenceRepository(connection).get_for_user(candidate[0])
            mode = SQLiteChatModeRepository(connection).get(ChatModeKey(candidate[0], "vk", "77"))
            analysis = SQLiteArticleAnalysisResultRepository(connection).get_latest_for_article(article.id)
            incoming = SQLiteIncomingMessageRepository(connection).find_by_external_id(
                channel="vk", chat_id="77", message_id="candidate-message")
            instruction = SQLiteVaultInstructionRepository(connection).list_for_vault(
                app_user_id=vault[1], vault_id=vault[0])
            if (not identity or identity.username != "candidate"
                    or preference != (UserCapabilityPreference(candidate[0], "obsidian", False),)
                    or not mode or mode.capability_id != "ask"
                    or not analysis or analysis.result_text != "Candidate summary"
                    or not incoming or incoming.article_id != article.id
                    or len(instruction) != 1 or instruction[0].content != "Candidate instruction"):
                raise AssertionError("previous API cannot read candidate linked semantics")
            users.update_display_name(app_user_id=candidate[0], display_name="Previous updated candidate")
            SQLiteExternalIdentityRepository(connection).touch(
                IncomingIdentity("vk", "77", "77", username="previous-updated"))
            SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                UserCapabilityPreference(candidate[0], "obsidian", True))
            SQLiteChatModeRepository(connection).save(
                ChatMode(ChatModeKey(candidate[0], "vk", "77"), "obsidian"))
            updated = SQLiteArticleRepository(connection).update_content(
                article_id=article.id, title="Previous updated article", cleaned_text="Updated text",
                text_hash="updated-hash", status=article.status,
            )
            if updated is None or updated.title != "Previous updated article":
                raise AssertionError("previous API cannot update candidate article")
            SQLiteVaultNoteRepository(connection).upsert(VaultNote(
                app_user_id=vault[1], vault_id=vault[0], path="candidate.md",
                blob_sha="previous-updated-blob", markdown="# Previous updated note",
            ))
            SQLiteVaultInstructionRepository(connection).replace_for_vault(
                app_user_id=vault[1], vault_id=vault[0],
                instructions=(VaultInstruction(app_user_id=vault[1], vault_id=vault[0],
                    position=0, path="AGENTS.md.txt", blob_sha="previous-updated-rule",
                    content="Previous updated instruction"),),
            )
            assert_database(connection, names)
    return {"passed": tests["passed"] and calls > 0 and not failures,
            "old_tests": tests, "candidate_schema_observations": calls,
            "schema_fingerprints": sorted(fingerprints), "schema_errors": failures,
            "migration_names": names,
            "coverage": "107-method audit matrix plus external cases; local mechanism, not trusted admission"}


def main() -> int:
    """Настраивает изолированный import path до первого импорта проекта/tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--mode", choices=("seed", "candidate", "behavior", "verify"), required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--previous-sql", type=Path)
    parser.add_argument("--test-modules", nargs="+", default=[])
    parser.add_argument("--compatibility-tests", type=Path)
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    result = {"passed": False}
    guard = CheckoutImports(checkout)
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode or sys.prefix == sys.base_prefix:
            raise RuntimeError("worker requires an isolated venv and -I -B")
        configuration = Path(sys.prefix) / "pyvenv.cfg"
        if "include-system-site-packages = false" not in configuration.read_text(encoding="utf-8").lower():
            raise RuntimeError("system site packages are forbidden")
        environment = Path(sys.prefix).resolve()
        base = Path(sys.base_prefix).resolve()
        standard_library = Path(sysconfig.get_path("stdlib")).resolve()
        for entry in sys.path:
            path = Path(entry).resolve()
            allowed = (path.is_relative_to(environment) or path == base
                       or path.is_relative_to(standard_library)
                       or path == base / "DLLs"
                       or path.name == f"python{sys.version_info.major}{sys.version_info.minor}.zip"
                       and path.parent in {base, standard_library.parent})
            if not entry or not allowed or ("site-packages" in path.parts and not path.is_relative_to(environment)):
                raise RuntimeError(f"unexpected initial import path: {entry}")
        # -I исключает cwd/PYTHONPATH/user site; добавляем только выбранные исходники.
        sys.path[:0] = [str(checkout), str(checkout / "tests")]
        expected_path = list(sys.path)
        sys.meta_path.insert(0, guard)
        guard.verify_loaded()
        if args.mode == "seed":
            result = seed_database(args.database)
        elif args.mode == "candidate":
            result = candidate_migrations(args.database, args.previous_sql)
        elif args.mode == "behavior":
            result = old_behavior(args.test_modules, args.compatibility_tests, args.database)
        else:
            from compadre_assistant.core.data.sqlite.connection import connect_database
            from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR
            from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository, SQLiteExternalIdentityRepository
            from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
            from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
            from compadre_assistant.core.domain.users.entities import IncomingIdentity
            from compadre_assistant.core.domain.chat_modes import ChatModeKey
            from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
            from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
            from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import SQLiteArticleAnalysisResultRepository
            from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import SQLiteIncomingMessageRepository
            from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
            from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import SQLiteVaultInstructionRepository
            with connect_database(args.database) as connection:
                names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
                fingerprint = assert_database(connection, names)
                user = connection.execute("SELECT id, display_name FROM app_users WHERE display_name = 'Previous updated candidate'").fetchone()
                if not user or SQLiteAppUserRepository(connection).get_by_id(user[0]).display_name != "Previous updated candidate":
                    raise AssertionError("candidate cannot read previous user update")
                article = SQLiteArticleRepository(connection).find_by_normalized_url(
                    "https://example.org/candidate", user[0])
                identity = SQLiteExternalIdentityRepository(connection).find(
                    IncomingIdentity("vk", "77", "77"))
                preference = SQLiteUserCapabilityPreferenceRepository(connection).get_for_user(user[0])
                mode = SQLiteChatModeRepository(connection).get(ChatModeKey(user[0], "vk", "77"))
                analysis = SQLiteArticleAnalysisResultRepository(connection).get_latest_for_article(article.id) if article else None
                incoming = SQLiteIncomingMessageRepository(connection).find_by_external_id(
                    channel="vk", chat_id="77", message_id="candidate-message")
                vault = connection.execute("SELECT id, app_user_id FROM obsidian_vaults WHERE repository = 'notes'").fetchone()
                note = SQLiteVaultNoteRepository(connection).get_by_path(
                    app_user_id=vault[1], vault_id=vault[0], path="candidate.md") if vault else None
                instructions = SQLiteVaultInstructionRepository(connection).list_for_vault(
                    app_user_id=vault[1], vault_id=vault[0]) if vault else []
                if (not article or article.title != "Previous updated article" or article.text_hash != "updated-hash"
                        or not identity or identity.username != "previous-updated"
                        or preference != (UserCapabilityPreference(user[0], "obsidian", True),)
                        or not mode or mode.capability_id != "obsidian"
                        or not analysis or analysis.result_text != "Candidate summary"
                        or not incoming or incoming.article_id != article.id
                        or not note or note.markdown != "# Previous updated note"
                        or len(instructions) != 1 or instructions[0].content != "Previous updated instruction"):
                    raise AssertionError("candidate cannot read previous updates")
                result = {"passed": True, "schema_fingerprint": fingerprint,
                          "verified_user_id": user[0], "verified_article": article.title,
                          "verified_identity": identity.username,
                          "verified_note": note.markdown,
                          "verified_instruction": instructions[0].content}
        guard.verify_loaded()
        if sys.path != expected_path:
            raise RuntimeError("tests changed isolated import path")
    except Exception as error:
        traceback.print_exc()
        result = {"passed": False, "error": str(error)}
    result["environment"] = {"executable": sys.executable, "prefix": sys.prefix,
                             "python": sys.version, "sqlite": sqlite3.sqlite_version,
                             "dependencies": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()),
                             "sys_path": sys.path}
    result["imports"] = guard.origins
    args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
