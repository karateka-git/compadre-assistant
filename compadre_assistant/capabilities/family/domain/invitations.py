"""Одноразовые семейные приглашения и сценарии выдачи и вступления."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import choice
from typing import Protocol

from compadre_assistant.capabilities.family.domain.membership import Family, FamilyError


INVITATION_TTL = timedelta(hours=24)  # Срок от выдачи до первого успешного вступления.
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # Исключает похожие символы 0, 1, I и O.
_CODE_LENGTH = 16  # 80 бит случайности при ручном вводе кода.
_ISSUE_ATTEMPTS = 5  # Коллизия кода приводит к новой попытке генерации.


class InvalidInvitation(FamilyError):
    """Код неизвестен, введён неверно или больше не связан с семьёй."""


class InvitationUsed(FamilyError):
    """По приглашению уже успешно вступили."""


class InvitationExpired(FamilyError):
    """Срок действия приглашения истёк."""


class InvitationUnavailable(FamilyError):
    """В пределах числа попыток не удалось выдать новый код."""


@dataclass(frozen=True, slots=True)
class InvitationPeriod:
    """Хранит срок действия приглашения и момент успешного вступления."""

    created_at: datetime
    expires_at: datetime
    used_at: datetime | None

    def require_usable(self, now: datetime) -> None:
        """Проверяет, можно ли использовать выданное приглашение сейчас.

        Args:
            now: Время попытки вступить.

        Raises:
            InvitationUsed: Код уже погашен независимо от срока.
            InvalidInvitation: Часы показывают время до выдачи приглашения.
            InvitationExpired: Наступил или прошёл конец срока.
        """
        if self.used_at is not None:
            raise InvitationUsed("Приглашение уже использовано")
        if now < self.created_at:
            raise InvalidInvitation("Приглашение ещё не действует")
        if now >= self.expires_at:
            raise InvitationExpired("Срок приглашения истёк")


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """Возвращает код для передачи приглашённому и срок его действия."""

    family: Family
    code: str
    expires_at: datetime


class InvitationRepository(Protocol):
    """Хранит только проверочные значения и атомарно применяет приглашения."""

    def issue_invitation(
        self, app_user_id: int, family_id: int, code_digest: str, period: InvitationPeriod,
    ) -> Family | None:
        """Возвращает семью после выдачи, None при коллизии или FamilyAccessEnded при утрате членства."""
        ...

    def join_by_invitation(self, app_user_id: int, code_digest: str, now: datetime) -> Family:
        """В одной транзакции проверяет код, погашает его и добавляет участника.

        Args:
            app_user_id: Профиль, который хочет вступить.
            code_digest: Проверочное значение введённого кода.
            now: Время вступления.

        Returns:
            Семья, в которую вступил пользователь.

        Raises:
            InvalidInvitation: Код не связан с действующей семьёй.
            InvitationUsed: Код уже погашен.
            InvitationExpired: Истёк срок действия кода.
            AlreadyInFamily: Профиль уже состоит в семье.
        """
        ...


class ManageInvitations:
    """Выдаёт случайный код и обрабатывает ввод пользователя через семейный порт."""

    def __init__(
        self, repository: InvitationRepository, *,
        clock: Callable[[], datetime] | None = None,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        """Принимает хранилище, часы и при необходимости источник случайного кода.

        Args:
            repository: Хранилище проверочных значений и членства.
            clock: Источник текущего времени. Время без зоны считается UTC.
            code_factory: Генератор 16 символов из алфавита семейных приглашений.
        """
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._code_factory = code_factory or _generate_code

    def issue(self, app_user_id: int, family_id: int) -> IssuedInvitation:
        """Выдаёт приглашение действующему участнику указанной семьи.

        Args:
            app_user_id: Профиль приглашавшего участника.
            family_id: Семья, из меню которой начато действие.

        Returns:
            Одноразовый код, семью и срок действия.

        Raises:
            FamilyAccessEnded: Профиль больше не состоит в указанной семье.
            InvitationUnavailable: Не удалось получить уникальный код.
        """
        now = self._now()
        period = InvitationPeriod(now, now + INVITATION_TTL, None)
        for _ in range(_ISSUE_ATTEMPTS):
            code = self._code_factory()
            family = self._repository.issue_invitation(app_user_id, family_id, _digest(code), period)
            if family is not None:
                displayed_code = "-".join(code[index:index + 4] for index in range(0, len(code), 4))
                return IssuedInvitation(family, displayed_code, period.expires_at)
        raise InvitationUnavailable("Не удалось выдать уникальное приглашение")

    def join(self, app_user_id: int, code: str) -> Family:
        """Принимает приглашение с дефисами или без них и возвращает новую семью.

        Args:
            app_user_id: Профиль, вводящий код.
            code: Код приглашения из сообщения пользователя.

        Returns:
            Семья после успешного вступления.

        Raises:
            InvalidInvitation: Некорректный, неизвестный или недействительный код.
            InvitationUsed: Код уже погашен.
            InvitationExpired: Истёк срок действия кода.
            AlreadyInFamily: Профиль уже состоит в семье.
        """
        entered = code.strip()
        if not entered.isascii():
            raise InvalidInvitation("Некорректный код приглашения")
        normalized = entered.replace("-", "").upper()
        if len(normalized) != _CODE_LENGTH or any(char not in _ALPHABET for char in normalized):
            raise InvalidInvitation("Некорректный код приглашения")
        return self._repository.join_by_invitation(app_user_id, _digest(normalized), self._now())

    def _now(self) -> datetime:
        now = self._clock()
        return (now if now.tzinfo is not None else now.replace(tzinfo=UTC)).astimezone(UTC)


def _generate_code() -> str:
    return "".join(choice(_ALPHABET) for _ in range(_CODE_LENGTH))


def _digest(code: str) -> str:
    return sha256(code.encode("ascii")).hexdigest()
