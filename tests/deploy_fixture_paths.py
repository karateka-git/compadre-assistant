"""Моделирует только владельца selector для non-root controller fixtures."""

import os
from pathlib import Path


class FixtureStatePath(type(Path())):
    """Сохраняет настоящие файлы/mode/inode и root ownership только selector.

    Класс используется исключительно fake Host тестов. Runtime получает
    обычный Path и продолжает требовать настоящего root:root владельца.
    """

    def lstat(self):
        """Читает реальный lstat, моделируя uid/gid приватного selector."""
        info = super().lstat()
        if self.name != "active-db.json":
            return info
        fields = list(info)
        fields[4:6] = [0, 0]
        return os.stat_result(fields)
