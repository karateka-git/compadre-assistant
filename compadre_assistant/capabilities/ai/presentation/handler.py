"""Обрабатывает команды режима ИИ и формирует ответы для канала."""

from collections.abc import Callable

from compadre_assistant.core.presentation.message_content import text_content

from compadre_assistant.capabilities.ai.domain.errors import (
    AiModelError, AiModelTimeoutError, EmptyQuestionError, InvalidOcrImageError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ai.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ai.domain.use_cases.extract_image_text import ExtractImageText
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, ScreenDestination
from compadre_assistant.core.presentation.incoming.events import ImageAttachment, ImageLoadError, IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, CancellableAction, CapabilityCommand, CapabilityDescriptor, CapabilityHelp,
    CapabilityResult, CommandParameter, HandlerContext, HelpSection, IncomingCapabilityPort,
    IncomingResponse, PendingInput, PendingInputContinuationPort, PendingInputOutcome, PendingInputResult,
    ReplyAction, SectionActionsQueryPort,
)


_OCR_COMMAND = "/get_text_from_image"
_OCR_INPUT_KIND = "ocr_image"
_ASK_ACTION = ReplyAction("Задать вопрос", "/ask", scope=ActionScope.SECTION)
_OCR_ACTION = ReplyAction("Извлечь текст с фото", _OCR_COMMAND, scope=ActionScope.SECTION)
_AI_ACTIONS = (
    _ASK_ACTION, _OCR_ACTION,
    ReplyAction("Выйти из режима", "/exit", scope=ActionScope.SECTION,
                purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("main")),
    HELP_SECTION_ACTION,
)
_WAITING_ACTIONS = (CANCEL_SECTION_ACTION, HELP_SECTION_ACTION)
_QUESTION_RECEIPT = IncomingResponse(text_content("Вопрос принят. Готовлю ответ…"), suggested_actions=_WAITING_ACTIONS)


class AiCapability(IncomingCapabilityPort, PendingInputContinuationPort, SectionActionsQueryPort):
    """Открывает режим ИИ для независимых вопросов и распознавания текста."""

    descriptor = CapabilityDescriptor(
        "ai", CapabilityCommand("/ai", (), "войти в режим ИИ"),
        CapabilityHelp(sections=(HelpSection("Режим ИИ", (
            CapabilityCommand("/ask", (CommandParameter("вопрос", "<question>", False),),
                              "задать один вопрос модели без истории"),
            CapabilityCommand(_OCR_COMMAND, (), "получить текст с одного фото"),
        )),)),
        enabled_by_default=True,
        accepts_images=True,
        display_name="Режим ИИ",
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
                "Открой режим ИИ и выбери действие кнопкой."
            ), ValueError("Аргументы команды входа в режим ИИ")))
        return CapabilityResult(IncomingResponse(text_content(
            "Режим ИИ включён. Здесь можно задать вопрос или распознать текст на фото.",
        ), suggested_actions=_AI_ACTIONS))

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Возвращает действия ИИ для проверенного состояния текущего чата.

        Args:
            app_user: Допущенный профиль.
            address: Чат с активным режимом ИИ.
            mode_revision: Проверенная ревизия режима.
            pending: Проверенное ожидание, если оно действует.

        Returns:
            Действия режима или отмена в ожидании значения.
        """
        return _WAITING_ACTIONS if pending is not None and pending.cancel_only_menu else _AI_ACTIONS

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Принимает команду в активном режиме или подсказывает подходящее действие.

        Args:
            context: Допущенный пользователь и сервисы текущего чата.
            event: Сообщение в активном режиме ИИ.

        Returns:
            Ответ, запрос ожидаемого ввода или подсказка с сохранением режима.
        """
        if event.images:
            return CapabilityResult(IncomingResponse(text_content(
                "Выбери «Извлечь текст с фото» и пришли фото следующим сообщением.",
            ), suggested_actions=(_OCR_ACTION, HELP_SECTION_ACTION)))
        parts = event.text.split(maxsplit=1)
        if parts and parts[0] == _OCR_COMMAND:
            return CapabilityResult(self._begin_ocr(context, event, parts[1] if len(parts) > 1 else ""))
        if not parts or parts[0] != "/ask":
            return CapabilityResult(IncomingResponse(text_content(
                "Выбери «Задать вопрос» и пришли свой вопрос следующим сообщением.",
            ), suggested_actions=(_ASK_ACTION, HELP_SECTION_ACTION)))

        question = parts[1] if len(parts) > 1 else ""
        if not question.strip() and context.pending_inputs is not None:
            return CapabilityResult(self._begin_input(context, event, image=False))
        try:
            question = self._question.validate_question(question)
        except EmptyQuestionError as error:
            return CapabilityResult(IncomingResponse(text_content(
                "Выбери «Задать вопрос» и пришли непустой вопрос следующим сообщением."
            ), error, suggested_actions=(_ASK_ACTION, HELP_SECTION_ACTION)))
        except QuestionTooLongError as error:
            return CapabilityResult(IncomingResponse(
                text_content(str(error) + " Сократи вопрос и начни ввод заново."), error,
                suggested_actions=(_ASK_ACTION, HELP_SECTION_ACTION)
                if context.pending_inputs is not None else (HELP_SECTION_ACTION,),
            ))
        return CapabilityResult(_QUESTION_RECEIPT, cancellable_action=self._question_action(question))

    def _begin_ocr(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> IncomingResponse:
        """Запоминает профиль и режим, в которых можно принять одно фото."""
        if arguments.strip():
            return IncomingResponse(text_content("Выбери «Извлечь текст с фото» и пришли одно фото следующим сообщением."),
                                    suggested_actions=(_OCR_ACTION, HELP_SECTION_ACTION))
        return self._begin_input(context, event, image=True)

    def _begin_input(self, context: HandlerContext, event: IncomingEvent, *, image: bool) -> IncomingResponse:
        """Начинает ввод автора, сохраняя чужое ожидание в групповом чате."""
        pending_inputs = context.pending_inputs
        if pending_inputs is None:
            return IncomingResponse(text_content("Ожидание ввода недоступно. Попробуй позже."),
                                    suggested_actions=(HELP_SECTION_ACTION,))
        with pending_inputs.lock(event.reply_address):
            current = pending_inputs.get(event.reply_address)
            if (current is not None and current.external_user_id not in (None, event.external_user_id)
                    and not pending_inputs.expired(current)):
                return IncomingResponse(text_content(
                    "В этом чате ожидается ввод от другого участника. Дождись завершения его шага.",
                ), suggested_actions=(HELP_SECTION_ACTION,))
            pending_inputs.begin(
                event.reply_address, self.descriptor.capability_id, _OCR_INPUT_KIND if image else "ask_question",
                app_user_id=context.app_user.id, required_mode=self.descriptor.capability_id, accepts_images=image,
                value_label="фото для распознавания" if image else "вопрос для модели",
                retry_command=_OCR_COMMAND if image else "/ask", cancel_only_menu=True,
                external_user_id=event.external_user_id,
            )
        prompt = "Пришли одно фото или скриншот" if image else "Введи один вопрос для модели"
        return IncomingResponse(text_content(prompt + " следующим сообщением. Ожидание действует 10 минут."),
                                suggested_actions=_WAITING_ACTIONS)

    def _recognize(self, attachment: ImageAttachment, is_cancelled: Callable[[], bool]) -> IncomingResponse:
        """Преобразует результат распознавания в текст для исходного канала."""
        try:
            data = attachment.read_bytes()
            if is_cancelled():
                return IncomingResponse(text_content(""))
            result = self._extract_image_text.execute(data)
        except (ImageLoadError, InvalidOcrImageError):
            failure = "Не удалось прочитать фото. "
        except AiModelTimeoutError:
            failure = "Модель не успела распознать текст. "
        except AiModelError:
            failure = "Не удалось распознать текст на фото. "
        else:
            return IncomingResponse(text_content(result.text if result.text is not None else "На фото нет текста."),
                                    suggested_actions=_AI_ACTIONS)
        return IncomingResponse(text_content(failure + "Начни распознавание заново и пришли фото."),
                                suggested_actions=(_OCR_ACTION, HELP_SECTION_ACTION))

    def _question_action(self, question: str) -> CancellableAction:
        """Передаёт исполнителю только уже проверенный вопрос."""
        def answer(is_cancelled: Callable[[], bool]) -> IncomingResponse:
            if is_cancelled():
                return IncomingResponse(text_content(""))
            return self._answer(question)

        return answer

    def _answer(self, question: str) -> IncomingResponse:
        """Выполняет проверенный вопрос и преобразует ошибки модели в ответы чата."""
        try:
            response = IncomingResponse(text_content(self._question.execute(question)), suggested_actions=_AI_ACTIONS)
        except AiModelTimeoutError as error:
            response = IncomingResponse(text_content('Модель не успела ответить. Попробуй задать вопрос позже.'),
                                        error, suggested_actions=(_ASK_ACTION, HELP_SECTION_ACTION))
        except AiModelError as error:
            response = IncomingResponse(text_content('Не удалось получить ответ модели. Попробуй задать вопрос позже.'),
                                        error, suggested_actions=(_ASK_ACTION, HELP_SECTION_ACTION))
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
                return PendingInputResult(IncomingResponse(text_content(
                    "Пришли одно фото или скриншот. Для отмены нажми «Отмена».",
                ), suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)
            attachment = event.images[0]
            receipt = IncomingResponse(text_content("Фото получено. Распознаю текст."), suggested_actions=_WAITING_ACTIONS)
            return PendingInputResult(receipt, PendingInputOutcome.COMPLETE,
                                      cancellable_action=lambda is_cancelled: self._recognize(attachment, is_cancelled))
        if pending.input_kind != "ask_question":
            return PendingInputResult(IncomingResponse(text_content(
                "Ожидание вопроса недоступно. Начни ввод заново."
            ), suggested_actions=(_ASK_ACTION, HELP_SECTION_ACTION)), PendingInputOutcome.COMPLETE)
        try:
            question = self._question.validate_question(event.text)
        except EmptyQuestionError as error:
            return PendingInputResult(IncomingResponse(text_content(
                "Вопрос пуст. Пришли непустой вопрос или нажми «Отмена»."
            ), error, suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)
        except QuestionTooLongError as error:
            return PendingInputResult(IncomingResponse(text_content(
                str(error) + " Сократи вопрос и пришли его ещё раз или нажми «Отмена»."
            ), error, suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)
        return PendingInputResult(_QUESTION_RECEIPT, PendingInputOutcome.COMPLETE,
                                  cancellable_action=self._question_action(question))
