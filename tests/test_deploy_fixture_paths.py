"""Проверяет узкую модель owner и неизменный production selector guard."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from tests.deploy_fixture_paths import FixtureStatePath


class FixturePathsTests(unittest.TestCase):
    """Отделяет моделируемый root owner от настоящих filesystem инвариантов."""

    def test_fixture_changes_only_selector_uid_and_gid(self):
        """Alias, mode, inode и все прочие файлы сохраняют реальную metadata."""
        for name in ("active-db.json", "other.json"):
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                path = Path(temporary) / name
                path.write_text("{}", encoding="utf-8")
                fields = list(path.lstat())
                fields[4:6] = [1001, 1002]
                original = os.stat_result(fields)
                with patch.object(Path, "lstat", return_value=original):
                    observed = FixtureStatePath(path).lstat()
                expected = fields.copy()
                if name == "active-db.json":
                    expected[4:6] = [0, 0]
                self.assertEqual(tuple(observed), tuple(expected))

    def test_production_selector_still_rejects_nonroot_mode_and_alias(self):
        """Включает POSIX условие без изменения os.name/pathlib и без root прав."""
        state = {"protocol_version": 2, "transition_id": "a" * 32, "admission_may_have_started": False}
        for uid, mode, links in ((1001, 0o100600, 1), (0, 0o100644, 1), (0, 0o100600, 2)):
            with self.subTest(uid=uid, mode=mode, links=links), TemporaryDirectory() as temporary:
                host = SimpleNamespace(state_dir=Path(temporary))
                info = SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=0, st_nlink=links)
                # Подменяется ссылка os только в проверяемом модуле: pathlib и
                # файловая система Windows остаются в своём штатном режиме.
                with patch.object(switch, "os", SimpleNamespace(name="posix")), patch.object(Path, "lstat", return_value=info):
                    with self.assertRaisesRegex(ValueError, "Selector не является приватным"):
                        switch.read_selected(host, state)
