"""Открывает режим администрирования и выдаёт отчёт о расходах."""

import logging

from compadre_assistant.capabilities.admin.domain.cost_report import GetLlmCostReport, InvalidReportTimezone, LlmCostReport
from compadre_assistant.capabilities.ask.domain.usage import NANORUBLES_PER_RUBLE, UsageTotals
from compadre_assistant.core.domain.users.entities import is_private_chat_address
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult, CommandParameter,
    ConfirmationQueryResult, HandlerContext, HelpSection, IncomingCapabilityPort, IncomingResponse,
    PendingConfirmationQueryPort, ReplyAction,
)
from compadre_assistant.core.presentation.message_content import TextSpan, TextStyle, code_span, compose_content, text_content


_LOGGER = logging.getLogger(__name__)
_ENTRY = "/admin"
_COSTS = "/llm_costs"
_COSTS_ACTION = ReplyAction("Показать расходы ИИ", _COSTS)
_PRIVATE_CHAT_HINT = "Открой личный чат с ботом, чтобы получить отчёт."
_NANORUBLES_PER_KOPECK = NANORUBLES_PER_RUBLE // 100  # Масштаб округления суммы отчёта.


class AdminCapability(IncomingCapabilityPort, PendingConfirmationQueryPort):
    """Подключает команды доверенного режима и их представление в чате."""

    descriptor = CapabilityDescriptor(
        "admin", CapabilityCommand(_ENTRY, (), "войти в режим администрирования"),
        CapabilityHelp(sections=(HelpSection("Отчёт о расходах", (
            CapabilityCommand(_COSTS, (CommandParameter("часовой пояс", "<timezone>", False),),
                              "показать расчётные расходы ИИ в личном чате"),
        )),)),
        enabled_by_default=True,
        requires_profile_access=True,
    )

    def __init__(self, cost_report: GetLlmCostReport) -> None:
        """Сохраняет use case чтения отчёта о расходах."""
        self._cost_report = cost_report

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Принимает явный вход в режим в личном чате.

        Args:
            context: Проверенный ядром профиль.
            event: Исходная команда входа и её адрес.
            arguments: Строка после команды, которая должна быть пустой.

        Returns:
            Приветствие режима или ответ с сохранением прежнего режима.
        """
        if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            return CapabilityResult(IncomingResponse(text_content(_PRIVATE_CHAT_HINT)), CapabilityMode.COMPLETE)
        if arguments:
            return CapabilityResult(IncomingResponse(
                compose_content("Используй ", code_span(_ENTRY), " без аргументов."),
                ValueError("Аргументы входа в режим администрирования"),
            ))
        return CapabilityResult(IncomingResponse(compose_content(
            "Режим администрирования включён. Чтобы получить отчёт, вызови ",
            code_span(_COSTS), ". Для выхода вызови ", code_span("/exit"), ".",
        ), suggested_actions=(_COSTS_ACTION,)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Показывает отчёт в исходном личном чате с сохранением режима.

        Args:
            context: Проверенный ядром профиль в активном режиме.
            event: Сообщение с автором и адресом исходного чата.

        Returns:
            Отчёт или подсказку. Вне личного чата завершает ошибочно сохранённый режим.
        """
        if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            return CapabilityResult(IncomingResponse(text_content(_PRIVATE_CHAT_HINT)), CapabilityMode.COMPLETE)
        parts = event.text.split()
        if not parts or parts[0] != _COSTS:
            return CapabilityResult(IncomingResponse(compose_content(
                "Чтобы получить отчёт, вызови ", code_span(_COSTS), ".",
            ), suggested_actions=(_COSTS_ACTION,)))
        if len(parts) > 2:
            return CapabilityResult(IncomingResponse(compose_content(
                "Укажи один часовой пояс. Например, ", code_span(f"{_COSTS} Europe/Moscow"), ".",
            )))
        try:
            report = self._cost_report.execute(parts[1] if len(parts) == 2 else None)
        except InvalidReportTimezone:
            return CapabilityResult(IncomingResponse(compose_content(
                "Часовой пояс не найден. Используй название IANA, например ",
                code_span(f"{_COSTS} Europe/Moscow"), ".",
            )))
        except Exception as error:
            _LOGGER.exception("Не удалось получить агрегаты расходов ИИ")
            return CapabilityResult(IncomingResponse(compose_content(
                "Отчёт временно недоступен. Повтори ", code_span(_COSTS), " позже.",
            ), error))
        return CapabilityResult(_report_response(report))

    def list_pending_confirmations(self, context: HandlerContext, origin: ReplyAddress) -> ConfirmationQueryResult:
        """Возвращает полный пустой список, поскольку режим не создаёт подтверждений."""
        return ConfirmationQueryResult("complete")


def _report_response(report: LlmCostReport) -> IncomingResponse:
    return IncomingResponse(compose_content(
        TextSpan("Расчётные расходы ИИ", TextStyle.EMPHASIS), "\nЧасовой пояс ", code_span(report.timezone), "\n\n",
        _report_line("Сегодня", report.today), "\n",
        _report_line("Текущий месяц", report.month), "\n",
        _report_line("Текущий год", report.year),
    ))


def _report_line(period: str, totals: UsageTotals) -> str:
    # Округляем половину копейки вверх после суммирования точных значений.
    kopecks = (totals.cost_nanorubles + _NANORUBLES_PER_KOPECK // 2) // _NANORUBLES_PER_KOPECK
    rubles, kopecks = divmod(kopecks, 100)
    return f"{period} — {rubles},{kopecks:02d} ₽. Запросов без оценки — {totals.unknown_count}."
