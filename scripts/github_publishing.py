"""Предоставляет явный POST-клиент GitHub для публикации Git refs и Releases."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

from scripts.full_ci_controller import GitHub


class PublishingError(RuntimeError):
    """Означает, что GitHub не подтвердил создание объекта."""


class PublishingGitHub(GitHub):
    """Дополняет читающий GitHub-клиент атомарным созданием объектов."""

    def post(self, path: str, payload: dict[str, object]) -> dict:
        """Создаёт объект GitHub API и отвергает сетевую ошибку или плохой ответ.

        Args:
            path: Путь REST API относительно репозитория.
            payload: Тело запроса в формате JSON.

        Returns:
            Полученный JSON-объект.

        Raises:
            PublishingError: Запрос не завершился подтверждённым созданием.
        """
        request = Request(self.root + path, data=json.dumps(payload).encode("utf-8"),
                          method="POST", headers={
                              "Accept": "application/vnd.github+json",
                              "Authorization": f"Bearer {self.token}",
                              "X-GitHub-Api-Version": "2026-03-10",
                              "Content-Type": "application/json",
                              "User-Agent": "compadre-release-publisher",
                          })
        try:
            with urlopen(request, timeout=20) as response:
                if response.status != 201:
                    raise PublishingError(f"GitHub API не подтвердил создание {path}: HTTP {response.status}")
                result = json.load(response)
        except Exception as error:
            raise PublishingError(f"GitHub API не подтвердил создание {path}: {error}") from error
        if not isinstance(result, dict):
            raise PublishingError(f"GitHub API вернул некорректный объект: {path}")
        return result
