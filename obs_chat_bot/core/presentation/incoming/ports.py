from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.domain.users.entities import AppUser


@dataclass(frozen=True, slots=True)
class OnboardingState:
    """Описывает готовность подключаемой возможности для нового пользователя."""

    ready: bool
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class HelpSection:
    """Описывает раздел справки без зависимости от конкретной возможности."""

    title: str
    commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityHelp:
    """Дополняет общую справку командами и инструкциями возможности."""

    sections: tuple[HelpSection, ...] = ()
    registration_description: str | None = None
    instructions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IncomingResponse:
    """Передаёт каналу готовый текст без предметной модели результата."""

    text: str
    error: Exception | None = None


ChannelCompletionHandler = Callable[[IncomingResponse], None]


class OnboardingReplyKind(StrEnum):
    """Выбирает этап общей регистрации для текста подключённой возможности."""

    START = "start"  # Повторное приветствие готового профиля.
    REQUIRED = "required"  # Профилю ещё нужна настройка возможности.
    ALREADY_REGISTERED = "already_registered"  # Повторная регистрация готового профиля.
    NAME_SAVED = "name_saved"  # Имя сохранено, настройка возможности продолжается.


class IncomingResult(Protocol):
    """Описывает минимальный ответ возможности для общего входа."""

    type: StrEnum
    app_user: AppUser | None
    error: Exception | None


class IncomingCapabilityPort(Protocol):
    """Принимает сообщение уже идентифицированного пользователя."""

    def onboarding_state(self, app_user: AppUser) -> OnboardingState:
        """Возвращает готовность возможности для завершения регистрации.

        Args:
            app_user: Идентифицированный пользователь приложения.

        Returns:
            Нейтральную готовность и краткое описание состояния.
        """
        ...

    def onboarding_reply(self, kind: OnboardingReplyKind, app_user: AppUser) -> str:
        """Возвращает готовый текст этапа регистрации или профиля.

        Args:
            kind: Тип общего сценария регистрации.
            app_user: Идентифицированный пользователь.

        Returns:
            Готовый текст ответа для канала.
        """
        ...

    def help(self) -> CapabilityHelp:
        """Возвращает дополнительные разделы общей справки."""
        ...

    def confirmation_missing_reply(self) -> str:
        """Возвращает подсказку при подтверждении без зарегистрированного профиля."""
        ...

    def status(self, app_user: AppUser) -> IncomingResult:
        """Возвращает состояние возможности для общей команды статуса.

        Args:
            app_user: Идентифицированный пользователь приложения.
        """
        ...

    def handle(
        self,
        app_user: AppUser,
        event: IncomingEvent,
        completion_handler: Callable[[IncomingResult], None] | None = None,
    ) -> IncomingResult:
        """Обрабатывает предметный текст или команду и вызывает callback.

        Args:
            app_user: Идентифицированный пользователь приложения.
            event: Нейтральное событие исходного чата.
            completion_handler: Доставка фонового результата, если она нужна.

        Returns:
            Структурированный ответ возможности.
        """
        ...
