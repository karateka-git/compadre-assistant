"""Назначение действий и ограничения их показа без знания конкретного канала."""

from dataclasses import dataclass
from enum import StrEnum


class ActionScope(StrEnum):
    """Определяет, относится действие к сообщению или текущему разделу."""

    SCREEN = "screen"  # Выбор и переход сохраняют контекст показанного сообщения.
    SECTION = "section"  # Доступное действие текущего раздела, в VK — нижнее меню.


class ActionPurpose(StrEnum):
    """Передаёт смысл действия без разбора его команды адаптером."""

    ACTION = "action"  # Обычное действие или вход в сценарий.
    SELECTION = "selection"  # Адресный выбор предмета из показанного снимка.
    NAVIGATION = "navigation"  # Переход к указанному экрану и его странице.
    CANCEL = "cancel"  # Завершение ожидаемого ввода с сохранением режима.


class SectionMenuDelivery(StrEnum):
    """Определяет, требуется ли сверять меню при содержательном ответе."""

    RECONCILE = "reconcile"  # Сверить текущие действия; пустой набор снимает меню.
    PRESERVE = "preserve"  # Не запрашивать новый набор и оставить доставленное меню.


@dataclass(frozen=True, slots=True)
class ScreenDestination:
    """Называет экран и страницу перехода внутри владельца сценария.

    Команда действия остаётся единственным путём исполнения. Эти данные
    описывают контекст перехода; адаптер не строит по ним новую команду.
    """

    screen: str
    page: int | None = None  # None — экран без пагинации, иначе номер с единицы.

    def __post_init__(self) -> None:
        """Отклоняет неопределённую цель или недопустимый номер страницы."""
        if not self.screen or self.screen != self.screen.strip():
            raise ValueError("screen destination must have a nonempty name")
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("screen destination page must be a positive integer")


@dataclass(frozen=True, slots=True)
class ActionLayoutLimits:
    """Передаёт владельцу экрана вместимость inline-проекции до создания текста.

    Значения задаёт адаптер, а страницу и номера выбирает владелец сценария.
    Нулевое ограничение означает недоступность кнопок: текстовый путь остаётся
    у владельца. Лимит подписи не разрешает сокращать предметный текст экрана.
    """

    max_buttons: int = 0
    max_rows: int = 0
    max_columns: int = 0
    max_label_length: int = 0

    def __post_init__(self) -> None:
        """Отклоняет отрицательные и нецелые ограничения."""
        values = (self.max_buttons, self.max_rows, self.max_columns, self.max_label_length)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("action layout limits must be nonnegative integers")

    def selection_capacity(self, *, reserved_rows: int = 0, reserved_buttons: int = 0) -> int:
        """Возвращает число выбираемых позиций при одной позиции в ряду.

        Args:
            reserved_rows: Ряды переходов и других служебных действий.
            reserved_buttons: Число служебных кнопок, включая общие ряды.

        Returns:
            Вместимость страницы выбора; ноль требует текстового пути.
            Владелец ограничивает результат своим размером страницы и не
            меняет соответствие уже показанных номеров предметным целям.

        Raises:
            ValueError: Резерв отрицателен или не является целым числом.
        """
        if any(type(value) is not int or value < 0 for value in (reserved_rows, reserved_buttons)):
            raise ValueError("reserved layout space must be nonnegative integers")
        if not self.max_columns or not self.max_label_length:
            return 0
        return max(0, min(self.max_buttons - reserved_buttons, self.max_rows - reserved_rows))
