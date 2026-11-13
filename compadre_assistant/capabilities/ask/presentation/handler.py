"""Представляет независимый вопрос и ожидаемые ошибки Ask любому каналу."""

from compadre_assistant.core.presentation.message_content import compose_content, code_span, text_content

from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelError, AskModelTimeoutError, EmptyQuestionError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CommandParameter, CapabilityDescriptor, CapabilityHelp, CapabilityResult, HandlerContext,
    HelpSection,
    IncomingCapabilityPort, IncomingResponse, PendingInputContinuationPort,
    PendingInputResult, PendingInputOutcome, PendingInput, ReplyAction,
)


class AskCapability(IncomingCapabilityPort, PendingInputContinuationPort):
    """Открывает режим ИИ и выполняет в нём отдельные вопросы без истории."""

    descriptor = CapabilityDescriptor(
        "ask", CapabilityCommand("/ai", (), "войти в режим ИИ"),
        CapabilityHelp(sections=(HelpSection("Режим ИИ", (
            CapabilityCommand("/ask", (CommandParameter("вопрос", "<question>", False),),
                              "задать один вопрос модели без истории"),
        )),)),
        enabled_by_default=True,
    )

    def __init__(self, question: AskQuestion) -> None:
        """Принимает собранный независимый сценарий вопроса."""
        self._question = question

    def enter(
        self, context: HandlerContext, event: IncomingEvent, arguments: str
    ) -> CapabilityResult:
        """Открывает режим ИИ в текущем чате.

        Args:
            context: Проверенный ядром пользователь.
            event: Исходное событие команды.
            arguments: Аргументы команды входа, которая вызывается без них.

        Returns:
            Приветствие режима либо ошибка аргумента без перехода режима.
        """
        if arguments.strip():
            return CapabilityResult(IncomingResponse(text_content(
                "Используй /ai без аргументов. Затем отправь /ask отдельным сообщением."
            ), ValueError("Аргументы команды входа в режим ИИ")))
        return CapabilityResult(IncomingResponse(compose_content(
            "Режим ИИ включён. Чтобы задать один вопрос, отправь ",
            code_span("/ask <question>"), " или ", code_span("/ask"),
            ". Чтобы выйти из режима, вызови ", code_span("/exit"), ".",
        ), suggested_actions=(ReplyAction("Задать вопрос", "/ask"),)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Принимает команду вопроса в активном режиме или подсказывает её.

        Args:
            context: Допущенный пользователь и сервисы текущего чата.
            event: Сообщение в активном режиме ИИ.

        Returns:
            Ответ, запрос ожидаемого ввода или подсказка с сохранением режима.
        """
        parts = event.text.split(maxsplit=1)
        if not parts or parts[0] != "/ask":
            return CapabilityResult(IncomingResponse(compose_content(
                "Чтобы задать вопрос модели, отправь ", code_span("/ask <question>"),
                " или ", code_span("/ask"), ".",
            ), suggested_actions=(ReplyAction("Задать вопрос", "/ask"),)))

        question = parts[1] if len(parts) > 1 else ""
        if not question.strip() and context.pending_inputs is not None:
            context.pending_inputs.begin(
                event.reply_address, "ask", "ask_question",
                app_user_id=context.app_user.id, required_mode="ask",
                value_label="вопрос для модели", retry_command="/ask",
            )
            return CapabilityResult(IncomingResponse(text_content(
                "Введи один вопрос для модели следующим сообщением. "
                "Ожидание действует 10 минут. Для отмены введи /cancel."
            )))
        return CapabilityResult(self._answer(context, event, question))

    def _answer(self, context: HandlerContext, event: IncomingEvent, question: str) -> IncomingResponse:
        """Выполняет один вопрос и преобразует ошибки модели в ответы чата."""
        def notify() -> None:
            """Подтверждает приём перед сетевым запросом через общий delivery port."""
            if context.delivery is not None:
                context.delivery.send(
                    event.reply_address, IncomingResponse(compose_content('Вопрос принят. Готовлю ответ…'))
                )

        try:
            response = IncomingResponse(text_content(self._question.execute(question, on_request=notify)))
        except EmptyQuestionError as error:
            response = IncomingResponse(compose_content('Напиши вопрос после команды: /ask <question>.'), error)
        except QuestionTooLongError as error:
            retry_content = compose_content(
                str(error), " Повтори ", code_span("/ask <question>"),
                " с допустимым вопросом.",
            )
            if context.pending_inputs is not None:
                retry_content = compose_content(
                    retry_content, " Чтобы ввести вопрос следующим сообщением, вызови ",
                    code_span("/ask"), ".",
                )
            response = IncomingResponse(
                retry_content, error,
                suggested_actions=(ReplyAction("Ввести вопрос заново", "/ask"),)
                if context.pending_inputs is not None else (),
            )
        except AskModelTimeoutError as error:
            response = IncomingResponse(compose_content(
                'Модель не успела ответить. Попробуй повторить /ask позже.',
            ), error)
        except AskModelError as error:
            response = IncomingResponse(compose_content(
                'Не удалось получить ответ модели. Попробуй /ask позже.',
            ), error)
        return response

    def continue_input(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent
    ) -> PendingInputResult:
        """Проверяет вопрос и откладывает запрос модели до освобождения чата.

        Args:
            context: Повторно допущенный профиль и сервисы ответа.
            pending: Исходное ожидание с типом значения.
            event: Неизменённое сообщение пользователя.

        Returns:
            Retry при неверном значении либо complete с действием вне блокировки.
        """
        if pending.input_kind != "ask_question":
            return PendingInputResult(IncomingResponse(text_content(
                "Ожидание вопроса недоступно. Повтори /ask."
            )), PendingInputOutcome.COMPLETE)
        try:
            question = self._question.validate_question(event.text)
        except EmptyQuestionError as error:
            return PendingInputResult(IncomingResponse(text_content(
                "Вопрос пуст. Отправь непустой вопрос ещё раз или введи /cancel."
            ), error), PendingInputOutcome.RETRY)
        except QuestionTooLongError as error:
            return PendingInputResult(IncomingResponse(text_content(
                str(error) + " Сократи вопрос и отправь его ещё раз или введи /cancel."
            ), error), PendingInputOutcome.RETRY)
        return PendingInputResult(IncomingResponse(text_content("")), PendingInputOutcome.COMPLETE,
                                  lambda: self._answer(context, event, question))
