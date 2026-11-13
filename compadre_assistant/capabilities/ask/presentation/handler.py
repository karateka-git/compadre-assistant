"""Представляет независимый вопрос и ожидаемые ошибки Ask любому каналу."""

from compadre_assistant.core.presentation.message_content import compose_content, code_span, text_content

from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelError, AskModelTimeoutError, EmptyQuestionError, InvalidOcrImageError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ask.domain.use_cases.extract_image_text import ExtractImageText
from compadre_assistant.core.presentation.incoming.events import ImageAttachment, ImageLoadError, IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CommandParameter, CapabilityDescriptor, CapabilityHelp, CapabilityResult, HandlerContext,
    HelpSection,
    IncomingCapabilityPort, IncomingResponse, PendingInputContinuationPort,
    PendingInputResult, PendingInputOutcome, PendingInput, ReplyAction,
)


_OCR_COMMAND = "/get_text_from_image"
_OCR_INPUT_KIND = "ocr_image"
_OCR_ACTION = ReplyAction("Извлечь текст с фото", _OCR_COMMAND)


class AskCapability(IncomingCapabilityPort, PendingInputContinuationPort):
    """Открывает режим ИИ для независимых вопросов и распознавания текста."""

    descriptor = CapabilityDescriptor(
        "ask", CapabilityCommand("/ai", (), "войти в режим ИИ"),
        CapabilityHelp(sections=(HelpSection("Режим ИИ", (
            CapabilityCommand("/ask", (CommandParameter("вопрос", "<question>", False),),
                              "задать один вопрос модели без истории"),
            CapabilityCommand(_OCR_COMMAND, (), "получить текст с одного фото"),
        )),)),
        enabled_by_default=True,
        accepts_images=True,
    )

    def __init__(self, question: AskQuestion, extract_image_text: ExtractImageText) -> None:
        """Принимает сценарии текстового вопроса и распознавания изображения."""
        self._question = question
        self._extract_image_text = extract_image_text

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
                f"Используй /ai без аргументов. Затем отправь /ask или {_OCR_COMMAND} отдельным сообщением."
            ), ValueError("Аргументы команды входа в режим ИИ")))
        return CapabilityResult(IncomingResponse(compose_content(
            "Режим ИИ включён. Чтобы задать один вопрос, отправь ",
            code_span("/ask <question>"), " или ", code_span("/ask"),
            ". Чтобы получить текст с фото, отправь ", code_span(_OCR_COMMAND),
            ". Чтобы выйти из режима, вызови ", code_span("/exit"), ".",
        ), suggested_actions=(ReplyAction("Задать вопрос", "/ask"), _OCR_ACTION)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Принимает команду в активном режиме или подсказывает подходящее действие.

        Args:
            context: Допущенный пользователь и сервисы текущего чата.
            event: Сообщение в активном режиме ИИ.

        Returns:
            Ответ, запрос ожидаемого ввода или подсказка с сохранением режима.
        """
        if event.images:
            return CapabilityResult(IncomingResponse(compose_content(
                "Чтобы получить текст с фото, вызови ", code_span(_OCR_COMMAND),
                " и пришли фото следующим сообщением.",
            ), suggested_actions=(_OCR_ACTION,)))
        parts = event.text.split(maxsplit=1)
        if parts and parts[0] == _OCR_COMMAND:
            return CapabilityResult(self._begin_ocr(context, event, parts[1] if len(parts) > 1 else ""))
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

    def _begin_ocr(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> IncomingResponse:
        """Запоминает профиль и режим, в которых можно принять одно фото."""
        if arguments.strip():
            return IncomingResponse(compose_content("Используй ", code_span(_OCR_COMMAND), " без аргументов."))
        if context.pending_inputs is None:
            return IncomingResponse(text_content("Ожидание фото недоступно. Попробуй позже."))
        context.pending_inputs.begin(
            event.reply_address, "ask", _OCR_INPUT_KIND,
            app_user_id=context.app_user.id, required_mode="ask", accepts_images=True,
            value_label="фото для распознавания", retry_command=_OCR_COMMAND,
        )
        return IncomingResponse(compose_content(
            "Пришли одно фото или скриншот следующим сообщением. Ожидание действует 10 минут. "
            "Для отмены введи ", code_span("/cancel"), ".",
        ))

    def _recognize(self, attachment: ImageAttachment) -> IncomingResponse:
        """Преобразует результат распознавания в текст для исходного канала."""
        try:
            result = self._extract_image_text.execute(attachment.read_bytes())
        except (ImageLoadError, InvalidOcrImageError):
            failure = "Не удалось прочитать фото. "
        except AskModelTimeoutError:
            failure = "Модель не успела распознать текст. "
        except AskModelError:
            failure = "Не удалось распознать текст на фото. "
        else:
            return IncomingResponse(text_content(result.text if result.text is not None else "На фото нет текста."))
        return IncomingResponse(compose_content(
            failure, "Чтобы попробовать ещё раз, вызови ", code_span(_OCR_COMMAND), " и пришли фото.",
        ), suggested_actions=(_OCR_ACTION,))

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
        """Проверяет ожидаемый ввод и откладывает сетевой запрос до освобождения чата.

        Args:
            context: Повторно допущенный профиль и сервисы ответа.
            pending: Исходное ожидание с типом значения.
            event: Неизменённое сообщение пользователя.

        Returns:
            Retry при неверном значении либо complete с действием вне блокировки.
        """
        if pending.input_kind == _OCR_INPUT_KIND:
            if len(event.images) != 1:
                return PendingInputResult(IncomingResponse(compose_content(
                    "Пришли одно фото или скриншот либо введи ", code_span("/cancel"), ".",
                )), PendingInputOutcome.RETRY)
            attachment = event.images[0]
            return PendingInputResult(IncomingResponse(text_content("")), PendingInputOutcome.COMPLETE,
                                      lambda: self._recognize(attachment))
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
