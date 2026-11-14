"""Открывает режим администрирования и выдаёт отчёт о расходах."""

from decimal import Decimal, ROUND_HALF_UP, localcontext

from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostReportRow, CostReportStatus, GatewayCostReport, GetGatewayCostReport,
    InvalidReportTimezone, ReportPeriod,
)
from compadre_assistant.core.domain.users.entities import is_private_chat_address
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult, CommandParameter,
    ConfirmationQueryResult, HandlerContext, HelpSection, IncomingCapabilityPort, IncomingResponse,
    PendingConfirmationQueryPort, ReplyAction,
)
from compadre_assistant.core.presentation.message_content import TextSpan, TextStyle, code_span, compose_content, text_content


_ENTRY = "/admin"
_COSTS = "/llm_costs"
_COSTS_ACTION = ReplyAction("Показать расходы ИИ", _COSTS)
_PRIVATE_CHAT_HINT = "Открой личный чат с ботом, чтобы получить отчёт."
_PERIOD_LABELS = {
    ReportPeriod.DAY: "Сегодня",
    ReportPeriod.MONTH: "Текущий месяц",
    ReportPeriod.YEAR: "Текущий год",
}
_STATUS_MESSAGES = {
    CostReportStatus.PERIOD_UNAVAILABLE: "Полный период недоступен.",
    CostReportStatus.SOURCE_UNCONFIGURED: "Статистика Timeweb не настроена.",
    CostReportStatus.SOURCE_FAILED: "Статистика Timeweb временно недоступна.",
}


class AdminCapability(IncomingCapabilityPort, PendingConfirmationQueryPort):
    """Подключает команды доверенного режима и их представление в чате."""

    descriptor = CapabilityDescriptor(
        "admin", CapabilityCommand(_ENTRY, (), "войти в режим администрирования"),
        CapabilityHelp(sections=(HelpSection("Отчёт о расходах", (
            CapabilityCommand(_COSTS, (
                CommandParameter("период", "<DAY|MONTH|YEAR>", False),
                CommandParameter("часовой пояс", "<timezone>", False),
            ), "показать расходы по статистике Timeweb"),
        )),)),
        enabled_by_default=True,
        requires_profile_access=True,
    )

    def __init__(self, cost_report: GetGatewayCostReport) -> None:
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
        arguments = parts[1:]
        period = ReportPeriod.__members__.get(arguments[0].upper()) if arguments else None
        if period is not None:
            arguments = arguments[1:]
        if len(arguments) > 1 or (arguments and arguments[0].upper() in ReportPeriod.__members__):
            return CapabilityResult(IncomingResponse(compose_content(
                "Укажи период DAY, MONTH или YEAR, затем при необходимости часовой пояс. Например, ",
                code_span(f"{_COSTS} DAY Europe/Moscow"), ".",
            )))
        try:
            report = self._cost_report.execute(arguments[0] if arguments else None, period=period)
        except InvalidReportTimezone:
            return CapabilityResult(IncomingResponse(compose_content(
                "Часовой пояс не найден. Используй название IANA, например ",
                code_span(f"{_COSTS} Europe/Moscow"), ".",
            )))
        return CapabilityResult(_report_response(report))

    def list_pending_confirmations(self, context: HandlerContext, origin: ReplyAddress) -> ConfirmationQueryResult:
        """Возвращает полный пустой список, поскольку режим не создаёт подтверждений."""
        return ConfirmationQueryResult("complete")


def _report_response(report: GatewayCostReport) -> IncomingResponse:
    if report.currency is None:
        return IncomingResponse(text_content(_STATUS_MESSAGES[CostReportStatus.SOURCE_UNCONFIGURED]))
    return IncomingResponse(compose_content(
        TextSpan("Расходы ИИ по статистике Timeweb", TextStyle.EMPHASIS),
        "\nЧасовой пояс ", code_span(report.timezone),
        "\nВалюта ", report.currency, ". Суммы до списания бонусов.\n\n",
        "\n".join(_report_line(row, report.currency) for row in report.rows),
    ))


def _report_line(row: CostReportRow, currency: str) -> str:
    line = _cost_line(row, currency)
    if row.request_count is not None:
        return f"{line} Запросов: {row.request_count}."
    if row.amount is not None:
        return f"{line} Число запросов недоступно."
    return line


def _cost_line(row: CostReportRow, currency: str) -> str:
    label = _PERIOD_LABELS[row.period]
    if row.status in _STATUS_MESSAGES:
        return f"{label} — {_STATUS_MESSAGES[row.status]}"
    if row.status is CostReportStatus.COMPLETE and row.amount is not None:
        return f"{label} — {_format_amount(row.amount, currency)}."
    known = f"Известная сумма {_format_amount(row.amount, currency)}. " if row.amount is not None else ""
    return f"{label} — {known}Интервалов без стоимости — {row.unknown_intervals}."


def _format_amount(amount: Decimal, currency: str) -> str:
    # Для переноса разряда при округлении длинного целого нужна ещё одна цифра.
    with localcontext(prec=max(3, amount.adjusted() + 4)):
        rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    label = "₽" if currency == "RUB" else currency
    return f"{rounded:f}".replace(".", ",") + f" {label}"
