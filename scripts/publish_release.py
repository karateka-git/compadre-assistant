"""Проверяет и по явному запросу публикует точную версию GitHub Release."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import sys
from urllib.parse import quote

from scripts.ci_repository_compatibility import DeploymentReleaseTagEvidence, ReleaseTagEvidence, resolve_release_tag
from scripts.check_hotfix_pairs import HotfixError, check_release, check_release_history
from scripts.create_release_branch import (
    EXPECTED_REPOSITORY, ReleaseBranchError, checked_main_head, repository_from_origin,
)
from scripts.full_ci_admission import (
    Admission, AdmissionError, MAIN_WORKFLOW, RELEASE_WORKFLOW, SHA_PATTERN,
    select_successful_run,
)
from scripts.full_ci_controller import ControllerError, GitHub, VERSION_PATTERN, tag_commit
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.predeploy_compatibility import EvidenceAPI, check, verify_deployment_base, verify_evidence
from scripts.release_sqlite_summary import EXPECTED_MIGRATION_LABEL, expected_migration_at


class PublishError(RuntimeError):
    """Означает, что опубликованная версия не может быть создана безопасно."""


@dataclass
class _ImmutableReleaseReads:
    """Хранит проверенные данные точных Git SHA в рамках одной публикации."""

    parents: dict[str, tuple[str, str]] = field(default_factory=dict)
    migrations: dict[str, str] = field(default_factory=dict)
    archives: dict[tuple[int, str], bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedRelease:
    """Хранит проверенные реквизиты будущего выпуска."""

    version: str
    branch: str
    sha: str
    admission: Admission
    pairs: tuple[tuple[int, int | None, str], ...]
    body: str
    change: str
    notes: str
    make_latest: str
    compatibility: DeploymentReleaseTagEvidence
    previous_patch_sha: str | None
    github_history: bool
    _reads: _ImmutableReleaseReads = field(repr=False, compare=False)


def _sha(value: object, label: str) -> str:
    """Проверяет полный SHA в обязательном поле GitHub API."""
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise PublishError(f"Некорректный SHA: {label}")
    return value


def _version(tag: str) -> tuple[int, int, int]:
    """Разбирает каноническую версию без ведущих нулей."""
    match = VERSION_PATTERN.fullmatch(tag)
    if not match or any(len(part) > 1 and part.startswith("0") for part in match.groups()):
        raise PublishError("Требуется канонический тег vMAJOR.MINOR.PATCH")
    return tuple(map(int, match.groups()))


def _published_versions(api: GitHub) -> dict[tuple[int, int, int], dict]:
    """Получает все опубликованные версии и отвергает неоднозначную историю."""
    versions: dict[tuple[int, int, int], dict] = {}
    for item in api.pages("/releases"):
        if not isinstance(item, dict) or not isinstance(item.get("tag_name"), str):
            raise PublishError("Некорректный список GitHub Releases")
        if item.get("draft") or item.get("prerelease"):
            continue
        version = _version(item["tag_name"])
        if version in versions or not item.get("published_at"):
            raise PublishError("Неоднозначная история опубликованных версий")
        versions[version] = item
    return versions


def _unused_name(api: GitHub, version: str) -> None:
    """Проверяет отсутствие тега и Release, включая draft и prerelease."""
    refs = api.pages("/git/matching-refs/tags/v")
    if any(not isinstance(ref, dict) or not isinstance(ref.get("ref"), str) for ref in refs):
        raise PublishError("Некорректный список Git tags")
    if any(ref["ref"] == f"refs/tags/{version}" for ref in refs):
        raise PublishError("Тег уже существует; версия не переиспользуется")
    releases = api.pages("/releases")
    if any(not isinstance(item, dict) or not isinstance(item.get("tag_name"), str) for item in releases):
        raise PublishError("Некорректный список GitHub Releases")
    if any(item["tag_name"] == version for item in releases):
        raise PublishError("GitHub Release уже существует; версия не переиспользуется")


def _protection(api: GitHub, version: str) -> None:
    """Требует действующей защиты тега и включённой неизменности Release."""
    immutable = api.get("/immutable-releases")
    if not isinstance(immutable, dict) or immutable.get("enabled") is not True:
        raise PublishError("Immutable Releases не включены")
    _tag_protection(api, version)


def _tag_protection(api: GitHub, version: str, *, require_visible_bypass: bool = True) -> None:
    """Требует активный tag ruleset для выбранной версии.

    Args:
        api: Клиент GitHub для чтения правил репозитория.
        version: Проверяемый tag выпуска.
        require_visible_bypass: Требовать явный пустой bypass list. Публичный
            API скрывает это поле; selector компенсирует его проверкой
            immutable Release и точного SHA.
    """
    rulesets = api.pages("/rulesets")
    protected = False
    for summary in rulesets:
        if not isinstance(summary, dict) or type(summary.get("id")) is not int:
            raise PublishError("Некорректный список GitHub rulesets")
        if summary.get("target") != "tag" or summary.get("enforcement") != "active":
            continue
        rule = api.get(f"/rulesets/{summary['id']}")
        if not isinstance(rule, dict) or rule.get("target") != "tag" or rule.get("enforcement") != "active":
            raise PublishError("Некорректное tag ruleset")
        names = rule.get("conditions", {}).get("ref_name", {})
        if (not isinstance(names, dict) or names.get("exclude") != [] or
                not isinstance(names.get("include"), list)):
            raise PublishError("Неоднозначные условия tag ruleset")
        # Принимаем лишь понятные широкие шаблоны; сложный fnmatch не угадываем.
        if not any(pattern in ("refs/tags/*", "refs/tags/v*", f"refs/tags/{version}")
                   for pattern in names["include"]):
            continue
        types = {entry.get("type") for entry in rule.get("rules", []) if isinstance(entry, dict)}
        bypass = rule.get("bypass_actors")
        acceptable_bypass = (bypass == [] if require_visible_bypass else
                             ("bypass_actors" not in rule or bypass == []))
        if {"update", "deletion"} <= types and acceptable_bypass:
            protected = True
    if not protected:
        if require_visible_bypass:
            raise PublishError("Нет активного tag ruleset v* с запретом update/deletion без bypass")
        raise PublishError("Нет активного tag ruleset v* с запретом update/deletion")


def _check_number(parts: tuple[int, int, int], change: str,
                  history: dict[tuple[int, int, int], dict]) -> None:
    """Проверяет последовательность версий внутри поддерживаемой линии."""
    major, minor, patch = parts
    if parts in history:
        raise PublishError("Версия уже опубликована")
    if not history:
        if change != "initial" or parts != (1, 0, 0):
            raise PublishError("Первым выпуском должен быть v1.0.0")
        return
    line = [version for version in history if version[:2] == parts[:2]]
    if line:
        if change != "patch" or patch != max(item[2] for item in line) + 1:
            raise PublishError("Для существующей линии требуется следующий PATCH")
        return
    highest_major, highest_minor, _ = max(history)
    if patch != 0 or not (
        (change == "minor" and major == highest_major and minor == highest_minor + 1) or
        (change == "major" and major == highest_major + 1 and minor == 0)
    ):
        raise PublishError("Новая линия требует следующего MINOR или MAJOR с PATCH=0")


def _candidate(api: GitHub, branch: str, sha: str, *, github_history: bool = False) -> None:
    """Сверяет удалённую вершину и checkout для публикации с локальной проверкой."""
    from scripts.create_release_branch import git
    if api.repository != EXPECTED_REPOSITORY:
        raise PublishError("Репозиторий origin или API не совпадает с проектом")
    if not github_history:
        if repository_from_origin() != EXPECTED_REPOSITORY:
            raise PublishError("Репозиторий origin или API не совпадает с проектом")
        if git("status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise PublishError("Для публикации нужен чистый checkout")
        if git("rev-parse", "HEAD").stdout.strip() != sha:
            raise PublishError("HEAD не соответствует candidate SHA")
    ref = api.get(f"/git/ref/heads/{branch}")
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        raise PublishError("Некорректный ref релизной ветки")
    if _sha(ref["object"].get("sha"), branch) != sha:
        raise PublishError("Candidate не является текущим tip релизной ветки")


def _policy_pin(api: GitHub) -> str:
    """Читает внешний reviewed pin независимо от данных кандидата."""
    name = "COMPATIBILITY_POLICY_SHA"
    variable = api.get(f"/actions/variables/{name}")
    if not isinstance(variable, dict) or variable.get("name") != name:
        raise PublishError("GitHub не подтвердил внешний CI policy pin")
    return _sha(variable.get("value"), name)


def _compatibility(api: GitHub, admission: Admission, reads: _ImmutableReleaseReads) -> DeploymentReleaseTagEvidence:
    """Берёт данные аннотации только из полного доказательства точной попытки CI."""
    from scripts.deployment_acceptance import AcceptedBaseline

    pin = _policy_pin(api)
    with check("SAFE_PUBLICATION_EVIDENCE"):
        result = verify_evidence(EvidenceAPI(api.repository, api.token), admission, pin, None,
                                 verified_archives=reads.archives)
        baseline = AcceptedBaseline(**result["baseline"])
        return DeploymentReleaseTagEvidence(baseline.sha, result["policy_sha"], admission.run_id,
                                            admission.run_attempt, result["schema_fingerprint"], baseline)


def _previous_patch_sha(api: GitHub, parts: tuple[int, int, int],
                        history: dict[tuple[int, int, int], dict]) -> str:
    """Читает текущий тег непосредственно предшествующего выпуска линии."""
    major, minor, patch = parts
    previous = history.get((major, minor, patch - 1))
    if previous is None:
        raise PublishError("Не подтверждён предыдущий выпуск PATCH")
    return tag_commit(api, previous["tag_name"])


def _patch_pairs(api: GitHub, branch: str, sha: str, baseline: str, *,
                 github_history: bool, reads: _ImmutableReleaseReads) -> tuple[tuple[int, int | None, str], ...]:
    """Проверяет текущие пары PR для фазы публикации."""
    if github_history:
        return tuple(check_release_history(api, branch, sha, baseline, commit_parents=reads.parents))
    return tuple(check_release(api, branch, sha, baseline, require_full_ci=False))


def prepare(api: GitHub, version: str, sha: str, change: str, notes: str, *,
            github_history: bool = False) -> PreparedRelease:
    """Проверяет номер, защиту, историю, парные PR и полный CI точного SHA.

    Args:
        api: Клиент GitHub API для выбранного репозитория.
        version: Канонический тег vMAJOR.MINOR.PATCH.
        sha: Полный SHA кандидата.
        change: Тип версии initial, major, minor или patch.
        notes: Подготовленное человеком описание изменений.
        github_history: Проверять историю PATCH через GitHub для production runner.

    Returns:
        Все реквизиты для публикации без скрытых проверок.

    Raises:
        PublishError: Допуск или защита не доказаны.
    """
    return _prepare(api, version, sha, change, notes, github_history=github_history and change == "patch",
                    reads=_ImmutableReleaseReads())


def _prepare(api: GitHub, version: str, sha: str, change: str, notes: str, *,
             github_history: bool, reads: _ImmutableReleaseReads) -> PreparedRelease:
    """Строит допуск на свежих данных и переиспользует проверенные Git-объекты."""
    major, minor, patch = parts = _version(version)
    if not SHA_PATTERN.fullmatch(sha):
        raise PublishError("Требуется полный 40-символьный SHA")
    check_notes(notes)
    branch = f"release/{major}.{minor}"
    _candidate(api, branch, sha, github_history=github_history)
    history = _published_versions(api)
    _check_number(parts, change, history)
    _unused_name(api, version)
    _protection(api, version)
    previous_patch_sha = None
    if change == "patch":
        previous_patch_sha = _previous_patch_sha(api, parts, history)
        pairs = _patch_pairs(api, branch, sha, previous_patch_sha, github_history=github_history, reads=reads)
        admission = select_successful_run(api, api.repository, RELEASE_WORKFLOW, sha, branch)
    else:
        checked_main_head(api, sha)
        pairs = ()
        admission = select_successful_run(api, api.repository, MAIN_WORKFLOW, sha, "main")
    if admission is None:
        raise PublishError("Нет успешного полного CI для точного SHA")
    compatibility = _compatibility(api, admission, reads)
    try:
        if sha not in reads.migrations:
            reads.migrations[sha] = expected_migration_at(api, sha)
        expected_migration = reads.migrations[sha]
    except ValueError as error:
        raise PublishError(f"Не подтверждена ожидаемая миграция SQLite: {error}") from error
    _candidate(api, branch, sha, github_history=github_history)
    _unused_name(api, version)
    run_url = f"https://github.com/{api.repository}/actions/runs/{admission.run_id}/attempts/{admission.run_attempt}"
    pair_lines = "\n".join(
        (f"- release https://github.com/{api.repository}/pull/{release_number}, "
         f"main https://github.com/{api.repository}/pull/{main_number}; "
         f"main merge `{main_sha}` подтверждён в main")
        if main_number is not None else
        (f"- release https://github.com/{api.repository}/pull/{release_number}; "
         f"main commit https://github.com/{api.repository}/commit/{main_sha} "
         "подтверждён в main")
        for release_number, main_number, main_sha in pairs
    )
    body = (f"{notes.strip()}\n\nТип выпуска: обычный\nCommit: `{sha}`\nВетка: `{branch}`\n"
            f"Полный CI: {run_url}\n{EXPECTED_MIGRATION_LABEL} `{expected_migration}`\n")
    if pair_lines:
        body += f"\nПарные PR исправлений:\n{pair_lines}\n"
    make_latest = "true" if not history or parts > max(history) else "false"
    return PreparedRelease(version, branch, sha, admission, pairs, body,
                           change, notes, make_latest, compatibility, previous_patch_sha, github_history, reads)


def check_notes(notes: str) -> None:
    """Отклоняет пустые notes и дублирование служебных полей манифеста."""
    if not notes.strip():
        raise PublishError("Release notes не должны быть пустыми")
    reserved = ("Тип выпуска:", "Commit:", "Ветка:", "Полный CI:", EXPECTED_MIGRATION_LABEL)
    if any(line.startswith(reserved) for line in notes.splitlines()):
        raise PublishError("Release notes содержат служебное поле манифеста")


def _require_published_tag(api: GitHub, prepared: PreparedRelease, object_sha: str) -> None:
    """Подтверждает неизменность объекта тега и реквизитов допуска."""
    reference = api.get(f"/git/ref/tags/{quote(prepared.version, safe='')}")
    if not isinstance(reference, dict) or reference.get("ref") != f"refs/tags/{prepared.version}":
        raise PublishError("GitHub не подтвердил ref аннотированного тега")
    sha, actual_object, message = resolve_release_tag(
        reference, prepared.version, lambda tag_sha: api.get(f"/git/tags/{tag_sha}"),
    )
    if (actual_object != object_sha or sha != prepared.sha or message is None
            or ReleaseTagEvidence.from_message(message) != prepared.compatibility):
        raise PublishError("Аннотированный тег не совпадает с проверенным допуском")


def publish(api: PublishingGitHub, prepared: PreparedRelease) -> str:
    """Создаёт аннотированный tag ref и затем опубликованный GitHub Release.

    Args:
        api: Клиент GitHub с правом создания тега и Release.
        prepared: Результат свежего допуска.

    Returns:
        Подтверждённая ссылка на опубликованный Release.

    Raises:
        PublishError: Создание или повторная сверка не подтверждены. GitHub
            не объединяет tag ref и Release в одну транзакцию: тег после
            частичного сбоя остаётся занятым и требует ручного разбора.
    """
    if _prepare(api, prepared.version, prepared.sha, prepared.change, prepared.notes,
                github_history=prepared.github_history, reads=prepared._reads) != prepared:
        raise PublishError("Допуск изменился после подготовки выпуска")
    object_sha = create_annotated_tag(api, prepared.version, prepared.sha, prepared.compatibility)
    _require_published_tag(api, prepared, object_sha)
    _candidate(api, prepared.branch, prepared.sha, github_history=prepared.github_history)
    if prepared.previous_patch_sha is not None:
        history = _published_versions(api)
        parts = _version(prepared.version)
        _check_number(parts, prepared.change, history)
        previous = _previous_patch_sha(api, parts, history)
        if previous != prepared.previous_patch_sha:
            raise PublishError("Предыдущий тег PATCH изменился после создания тега")
        if _patch_pairs(api, prepared.branch, prepared.sha, previous, github_history=prepared.github_history,
                        reads=prepared._reads) != prepared.pairs:
            raise PublishError("Условия парных PR изменились после создания тега")
    else:
        checked_main_head(api, prepared.sha)
    if select_successful_run(api, api.repository, prepared.admission.workflow,
                             prepared.sha, prepared.admission.branch) != prepared.admission:
        raise PublishError("Допуск CI изменился после создания тега")
    if _policy_pin(api) != prepared.compatibility.policy_sha:
        raise PublishError("Внешний CI policy pin изменился после создания тега")
    verify_deployment_base(api, asdict(prepared.compatibility.baseline), prepared.compatibility.previous_sha)
    _protection(api, prepared.version)
    release = api.post("/releases", {
        "tag_name": prepared.version, "target_commitish": prepared.sha,
        "name": prepared.version, "body": prepared.body,
        "draft": False, "prerelease": False, "make_latest": prepared.make_latest,
    })
    if (release.get("tag_name") != prepared.version or release.get("draft") is not False or
            release.get("prerelease") is not False or type(release.get("id")) is not int or
            not isinstance(release.get("html_url"), str)):
        raise PublishError("Создание Release не подтверждено; версию разбирать вручную")
    confirmed = api.get(f"/releases/tags/{quote(prepared.version, safe='')}")
    if (not isinstance(confirmed, dict) or confirmed.get("id") != release["id"] or
            confirmed.get("tag_name") != prepared.version or
            confirmed.get("target_commitish") != prepared.sha or
            confirmed.get("body") != prepared.body or
            confirmed.get("draft") is not False or
            confirmed.get("prerelease") is not False or
            confirmed.get("immutable") is not True or
            not confirmed.get("published_at")):
        raise PublishError("Опубликованный Release не совпадает с подготовленным")
    _require_published_tag(api, prepared, object_sha)
    return release["html_url"]


def create_annotated_tag(api: PublishingGitHub, version: str, sha: str, evidence: ReleaseTagEvidence) -> str:
    """Публикует объект и ref аннотированного тега, сверяя оба ответа GitHub.

    Args:
        api: Клиент доверенного писателя.
        version: Свободное имя версии.
        sha: Commit проверенного кандидата.
        evidence: Аннотация CI для этой установки.

    Returns:
        SHA опубликованного объекта тега.

    Raises:
        PublishError: Ответ не подтверждает записанную связь.
        PublishingError: GitHub не подтвердил запись.
    """
    tag = api.post("/git/tags", {"tag": version, "message": evidence.message(), "object": sha, "type": "commit"})
    object_sha = _sha(tag.get("sha"), "объект аннотированного тега")
    reference = {"object": {"type": "tag", "sha": object_sha}}
    target, _, message = resolve_release_tag(reference, version, lambda _: tag)
    if target != sha or message is None or ReleaseTagEvidence.from_message(message) != evidence:
        raise PublishError("Создание объекта аннотированного тега не подтверждено")
    ref = f"refs/tags/{version}"
    tag = api.post("/git/refs", {"ref": ref, "sha": object_sha})
    if (tag.get("ref") != ref or tag.get("object", {}).get("type") != "tag"
            or tag["object"].get("sha") != object_sha):
        raise PublishError("Создание тега не подтверждено; версию разбирать вручную")
    return object_sha


def main() -> None:
    """Запускает проверку, dry-run или явную публикацию после допуска."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="новая версия vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный SHA tip релизной ветки")
    parser.add_argument("--change", required=True, choices=("initial", "major", "minor", "patch"))
    parser.add_argument("--notes-file", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="проверить без записи")
    mode.add_argument("--dry-run", action="store_true", help="проверить и показать Release notes")
    mode.add_argument("--publish", action="store_true", help="создать tag и GitHub Release")
    args = parser.parse_args()
    try:
        notes = args.notes_file.read_text(encoding="utf-8")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        result = prepare(api, args.version, args.sha, args.change, notes)
        print(f"Допуск: {result.version} -> {result.sha}; {result.admission.workflow} "
              f"run={result.admission.run_id}, attempt={result.admission.run_attempt}")
        if args.dry_run:
            print(result.body)
        if args.publish:
            print(f"Опубликовано: {publish(api, result)}")
    except (OSError, AdmissionError, ControllerError, HotfixError, ReleaseBranchError,
            PublishError, PublishingError, KeyError,
            TypeError, ValueError) as error:
        print(f"Release: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
