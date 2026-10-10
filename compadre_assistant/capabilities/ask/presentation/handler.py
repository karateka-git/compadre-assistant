"""Представляет независимый вопрос и ожидаемые ошибки Ask любому каналу."""

from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelError, AskModelTimeoutError, EmptyQuestionError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityDescriptor, CapabilityMode, CapabilityResult, HandlerContext,
    IncomingCapabilityPort, IncomingResponse,
)


class AskCapability(IncomingCapabilityPort):
    """Выполняет один вопрос без установки длительного режима чата."""

    descriptor = CapabilityDescriptor(
        "ask", "/ask", "один вопрос модели без истории: /ask <вопрос>",
        persistent_mode=False,
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
        def notify() -> None:
            """Подтверждает приём перед сетевым запросом через общий delivery port."""
            if context.delivery is not None:
                context.delivery.send(
                    event.reply_address, IncomingResponse("Вопрос принят. Готовлю ответ…")
                )

        try:
            response = IncomingResponse(self._question.execute(arguments, on_request=notify))
        except EmptyQuestionError as error:
            response = IncomingResponse("Напиши вопрос после команды: /ask <вопрос>.", error)
        except QuestionTooLongError as error:
            response = IncomingResponse(str(error), error)
        except AskModelTimeoutError as error:
            response = IncomingResponse("Модель не успела ответить. Попробуй повторить /ask позже.", error)
        except AskModelError as error:
            response = IncomingResponse("Не удалось получить ответ модели. Попробуй /ask позже.", error)
        return CapabilityResult(response, CapabilityMode.COMPLETE)

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Предлагает явную команду при устаревшем сохранённом режиме Ask."""
        return CapabilityResult(
            IncomingResponse("Для вопроса используй /ask <вопрос>."),
            CapabilityMode.COMPLETE,
        )
