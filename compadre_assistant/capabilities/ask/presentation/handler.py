"""Представляет независимый вопрос и ожидаемые ошибки Ask любому каналу."""

from compadre_assistant.core.presentation.message_content import compose_content, code_span, text_content

from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelError, AskModelTimeoutError, EmptyQuestionError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CommandParameter, CapabilityDescriptor, CapabilityMode, CapabilityResult, HandlerContext,
    IncomingCapabilityPort, IncomingResponse, PendingInputContinuationPort,
    PendingInputResult, PendingInputOutcome, PendingInput,
)


class AskCapability(IncomingCapabilityPort, PendingInputContinuationPort):
    """Выполняет один вопрос без установки длительного режима чата."""

    descriptor = CapabilityDescriptor(
        "ask", CapabilityCommand("/ask", (CommandParameter("вопрос", "<вопрос>", False),),
                                 "один вопрос модели без истории"),
        persistent_mode=False,
        enabled_by_default=True,
    )

    def __init__(self, question: AskQuestion) -> None:
        """Принимает собранный независимый сценарий вопроса."""
        self._question = question

    def enter(
        self, context: HandlerContext, event: IncomingEvent, arguments: str
    ) -> CapabilityResult:
        """Отвечает на аргументы команды и сохраняет прежний режим чата.

        Args:
            context: Проверенный ядром пользователь.
            event: Исходное событие команды.
            arguments: Вопрос без имени команды.

        Returns:
            Ответ либо безопасное описание ожидаемой ошибки без перехода режима.
        """
        if not arguments.strip() and context.pending_inputs is not None:
            context.pending_inputs.begin(event.reply_address, "ask", "ask_question",
                                         app_user_id=context.app_user.id, value_label="вопрос для Ask")
            return CapabilityResult(IncomingResponse(text_content(
                "Введи один вопрос для модели следующим сообщением. "
                "Ожидание действует 10 минут. Для отмены введи /cancel."
            )), CapabilityMode.COMPLETE)
        def notify() -> None:
            """Подтверждает приём перед сетевым запросом через общий delivery port."""
            if context.delivery is not None:
                context.delivery.send(
                    event.reply_address, IncomingResponse(compose_content('Вопрос принят. Готовлю ответ…'))
                )

        try:
            response = IncomingResponse(text_content(self._question.execute(arguments, on_request=notify)))
        except EmptyQuestionError as error:
            response = IncomingResponse(compose_content('Напиши вопрос после команды: /ask <вопрос>.'), error)
        except QuestionTooLongError as error:
            response = IncomingResponse(compose_content(
                str(error),
                " Повтори ",
                code_span("/ask <вопрос>"),
                " с допустимым вопросом.",
            ), error)
        except AskModelTimeoutError as error:
            response = IncomingResponse(compose_content(
                'Модель не успела ответить. Попробуй повторить /ask позже.',
            ), error)
        except AskModelError as error:
            response = IncomingResponse(compose_content(
                'Не удалось получить ответ модели. Попробуй /ask позже.',
            ), error)
        return CapabilityResult(response, CapabilityMode.COMPLETE)

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
                                  lambda: self.enter(context, event, question).response)

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Предлагает явную команду при устаревшем сохранённом режиме Ask."""
        return CapabilityResult(
            IncomingResponse(compose_content(
                "Ask не является режимом чата. Чтобы задать один вопрос, вызови ",
                code_span("/ask <вопрос>"),
                ". Текущий режим не изменится.",
            )),
            CapabilityMode.COMPLETE,
        )
