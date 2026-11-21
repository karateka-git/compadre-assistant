"""Открывает режим администрирования и выдаёт отчёт о расходах."""

from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP, localcontext

from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostReportRow, CostReportStatus, GatewayCostReport, GetGatewayCostReport,
    InvalidReportTimezone, ReportPeriod, validate_report_timezone,
)
from compadre_assistant.core.domain.users.entities import AppUser, is_private_chat_address
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, ScreenDestination
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult, CommandParameter,
    ConfirmationQueryResult, HandlerContext, HelpSection, IncomingCapabilityPort, IncomingResponse,
    CANCEL_SECTION_ACTION, PendingConfirmationQueryPort, PendingInput, ReplyAction, SectionActionsQueryPort,
    PendingInputContinuationPort, PendingInputResult, PendingInputOutcome,
)
from compadre_assistant.core.presentation.message_content import TextSpan, TextStyle, code_span, compose_content, text_content


_ENTRY = "/admin"
_COSTS = "/llm_costs"
_TIMEZONE = "/llm_timezone"
_COSTS_ACTION = ReplyAction("Показать расходы ИИ", _COSTS, scope=ActionScope.SECTION)
_TIMEZONE_ACTION = ReplyAction("Часовой пояс", _TIMEZONE, scope=ActionScope.SECTION)
_ADMIN_ACTIONS = (
    _COSTS_ACTION, _TIMEZONE_ACTION, HELP_SECTION_ACTION,
    ReplyAction("Выйти из режима", "/exit", scope=ActionScope.SECTION,
                purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("main")),
)
_WAITING_ACTIONS = (CANCEL_SECTION_ACTION, HELP_SECTION_ACTION)
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


class AdminCapability(IncomingCapabilityPort, PendingConfirmationQueryPort, SectionActionsQueryPort, PendingInputContinuationPort):
    """Подключает команды доверенного режима и их представление в чате."""

    descriptor = CapabilityDescriptor(
        "admin", CapabilityCommand(_ENTRY, (), "войти в режим администрирования"),
        CapabilityHelp(sections=(HelpSection("Отчёт о расходах", (
            CapabilityCommand(_COSTS, (
                CommandParameter("период", "<DAY|MONTH|YEAR>", False),
                CommandParameter("часовой пояс", "<timezone>", False),
            ), "показать расходы по статистике Timeweb"),
            CapabilityCommand(_TIMEZONE, (CommandParameter("часовой пояс", "<timezone>", False),),
                              "изменить часовой пояс текущего отчёта; без аргумента — ввести следующим сообщением"),
        )),)),
        enabled_by_default=True,
        requires_profile_access=True,
        private_chat_hint=_PRIVATE_CHAT_HINT,
        display_name="Администрирование",
    )

    def __init__(self, cost_report: GetGatewayCostReport) -> None:
        """Сохраняет use case чтения отчёта о расходах."""
        self._cost_report = cost_report
        # Выбор принадлежит ревизии режима, а не всему профилю или связанным чатам.
        self._reports: dict[tuple[int, ReplyAddress], tuple[int | None, str, ReportPeriod | None]] = {}

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
                text_content("Открой раздел администрирования без аргументов."),
                ValueError("Аргументы входа в режим администрирования"),
            ))
        self._reports.pop((context.app_user.id, event.reply_address), None)
        return CapabilityResult(IncomingResponse(text_content(
            "Режим администрирования включён. Выбери «Показать расходы ИИ» или часовой пояс отчёта."
        ), suggested_actions=_ADMIN_ACTIONS))

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Возвращает действия Admin в личном чате после проверки допуска ядром.

        Args:
            app_user: Допущенный доверенный профиль.
            address: Личный чат с активным режимом Admin.
            mode_revision: Проверенная ревизия режима.
            pending: Проверенное ожидание, если оно действует.

        Returns:
            Действия последнего отчёта либо отмена и помощь для ввода пояса.
        """
        if pending is not None and pending.owner_id == "admin":
            return _WAITING_ACTIONS
        timezone, _ = self._selection(app_user.id, address, mode_revision)
        return _admin_actions(timezone)

    def _selection(self, user_id: int, address: ReplyAddress, revision: int | None) -> tuple[str | None, ReportPeriod | None]:
        """Читает выбор только для текущего профиля, чата и ревизии режима."""
        saved = self._reports.get((user_id, address))
        return (saved[1], saved[2]) if saved is not None and saved[0] == revision else (None, None)

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
        if parts and parts[0] == _TIMEZONE:
            timezone, period = self._selection(context.app_user.id, event.reply_address, context.mode_revision)
            if len(parts) > 1:
                return CapabilityResult(self._report(context, event, " ".join(parts[1:]), period))
            if context.pending_inputs is None:
                return CapabilityResult(IncomingResponse(text_content("Ввод часового пояса сейчас недоступен. Попробуй позже."),
                                                         suggested_actions=_admin_actions(timezone)))
            context.pending_inputs.begin(
                event.reply_address, "admin", "report_timezone", app_user_id=context.app_user.id,
                required_mode="admin", value_label="часовой пояс IANA", retry_command=_TIMEZONE,
                cancel_only_menu=True, external_user_id=event.external_user_id,
                cancel_return_command=f"{_COSTS} {timezone}" if timezone else _COSTS,
            )
            return CapabilityResult(IncomingResponse(text_content(
                "Введи часовой пояс IANA, например Europe/Moscow или Asia/Omsk. Ожидание действует 10 минут."
            ), suggested_actions=_WAITING_ACTIONS))
        if not parts or parts[0] != _COSTS:
            timezone, _ = self._selection(context.app_user.id, event.reply_address, context.mode_revision)
            return CapabilityResult(IncomingResponse(text_content("Выбери «Показать расходы ИИ», чтобы получить отчёт."),
                                                     suggested_actions=_admin_actions(timezone)))
        arguments = parts[1:]
        period = ReportPeriod.__members__.get(arguments[0].upper()) if arguments else None
        if period is not None:
            arguments = arguments[1:]
        if len(arguments) > 1 or (arguments and arguments[0].upper() in ReportPeriod.__members__):
            timezone, _ = self._selection(context.app_user.id, event.reply_address, context.mode_revision)
            return CapabilityResult(IncomingResponse(text_content(
                "Не удалось распознать параметры отчёта. Открой сводку и выбери период или часовой пояс кнопкой."
            ), suggested_actions=_admin_actions(timezone)))
        return CapabilityResult(self._report(context, event, arguments[0] if arguments else None, period))

    def continue_input(self, context: HandlerContext, pending: PendingInput, event: IncomingEvent) -> PendingInputResult:
        """Принимает IANA-пояс, сохраняя исходное ожидание при ошибке.

        Args:
            context: Повторно допущенный профиль в активном Admin.
            pending: Ожидание пояса с первоначальным сроком.
            event: Обычный текст автора в исходном личном чате.

        Returns:
            Retry при неверном поясе либо отчёт вне блокировки ожидания.
        """
        if pending.input_kind != "report_timezone":
            return PendingInputResult(IncomingResponse(text_content("Ожидание недоступно. Начни ввод часового пояса заново."),
                                                       suggested_actions=_ADMIN_ACTIONS), PendingInputOutcome.COMPLETE)
        try:
            timezone = validate_report_timezone(event.text.strip())
        except InvalidReportTimezone:
            return PendingInputResult(IncomingResponse(text_content(
                "Часовой пояс не найден. Введи название IANA, например Europe/Moscow. Ожидание продолжается."
            ), suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)
        _, period = self._selection(context.app_user.id, event.reply_address, context.mode_revision)
        return PendingInputResult(IncomingResponse(text_content("")), PendingInputOutcome.COMPLETE,
                                  lambda: self._report(context, event, timezone, period))

    def _report(self, context: HandlerContext, event: IncomingEvent, timezone: str | None,
                period: ReportPeriod | None) -> IncomingResponse:
        """Читает статистику и запоминает выбор для навигации текущей ревизии."""
        try:
            report = self._cost_report.execute(timezone, period=period)
        except InvalidReportTimezone:
            current, _ = self._selection(context.app_user.id, event.reply_address, context.mode_revision)
            return IncomingResponse(text_content("Часовой пояс не найден. Выбери «Часовой пояс» и введи название IANA, например Europe/Moscow."),
                                    suggested_actions=_admin_actions(current))
        self._reports[(context.app_user.id, event.reply_address)] = (context.mode_revision, report.timezone, period)
        return _report_response(report, period)

    def list_pending_confirmations(self, context: HandlerContext, origin: ReplyAddress) -> ConfirmationQueryResult:
        """Возвращает полный пустой список, поскольку режим не создаёт подтверждений."""
        return ConfirmationQueryResult("complete")


def _admin_actions(timezone: str | None) -> tuple[ReplyAction, ...]:
    """Возвращает сводку с текущим поясом и общие действия раздела."""
    return ((replace(_COSTS_ACTION, command=f"{_COSTS} {timezone}"),) + _ADMIN_ACTIONS[1:]
            if timezone is not None else _ADMIN_ACTIONS)


def _report_response(report: GatewayCostReport, period: ReportPeriod | None) -> IncomingResponse:
    """Добавляет выбор периода с поясом именно этого отчёта."""
    if report.currency is None:
        return IncomingResponse(text_content(_STATUS_MESSAGES[CostReportStatus.SOURCE_UNCONFIGURED]),
                                suggested_actions=_admin_actions(report.timezone))
    actions = tuple(
        ReplyAction(label, f"{_COSTS} {option.value} {report.timezone}")
        for option, label in _PERIOD_LABELS.items() if option is not period
    )
    summary_command = f"{_COSTS} {report.timezone}"
    if period is not None:
        actions += (ReplyAction("Сводка", summary_command, purpose=ActionPurpose.NAVIGATION,
                                destination=ScreenDestination("admin_cost_summary")),)
    return IncomingResponse(compose_content(
        TextSpan("Расходы ИИ по статистике Timeweb", TextStyle.EMPHASIS),
        "\nЧасовой пояс ", code_span(report.timezone),
        "\nВалюта ", report.currency, ". Суммы до списания бонусов.\n\n",
        "\n".join(_report_line(row, report.currency) for row in report.rows),
    ), suggested_actions=actions + _admin_actions(report.timezone))


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
