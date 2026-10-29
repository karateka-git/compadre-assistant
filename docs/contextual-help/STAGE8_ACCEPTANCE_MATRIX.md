# Матрица приёмки этапа 8

Матрица связывает сценарии [этапа 8](PLAN.md#этап-8-проверить-сценарии-и-оформление) с автоматическими проверками и ожидаемым результатом в личных чатах Telegram и VK. Она задаёт пробы для согласованного окна по [порядку передачи polling](STAGE3_HANDOFF.md); фактические наблюдения и дату нужно записать отдельно. Эталон веток ответа — [карта ответов](RESPONSE_MAP.md), [ядро](CORE.md), [Ask](ASK.md) и [Obsidian](OBSIDIAN.md).

## Подготовить безопасную пробу

Начинайте клиентские пробы только после preflight, согласования окна и переключения обоих polling по `STAGE3_HANDOFF.md` с учётом `../PRODUCTION.md`. Используйте свои личные чаты. В таблице команды приведены как примеры действий, а не как разрешение запускать ботов или менять production. Не пересылайте в отчёт коды привязки, token, сообщения модели и личные данные. Для повторяемости отмечайте канал, состояние профиля, серверную и личную доступность возможностей, активный режим и дату.

Проверку старого профиля с `NULL`-именем проводите **только на отдельной development-БД**: подготавливайте профиль до миграции `0003`, применяйте миграции и проверяйте присвоение «Анатолий» и сохранность identity. Не создавайте такой профиль в production. Автоматическое доказательство — `test_migration_runner.py::test_existing_profiles_receive_default_name_without_losing_data`.

## Сопоставить сценарии

| Состояние и действие в личном чате | Ожидаемый ответ и меню в Telegram и VK | Автоматические доказательства | Клиентская граница |
| --- | --- | --- | --- |
| Неизвестная identity: `/help`, затем `/register`, затем `/help`, `/cancel` | До регистрации только `/register` и `/link`, допускается прямой `/link <code>`; после `/register` запрос имени, профиль ещё не создан; `/help` напоминает об ожидании, `/cancel` его снимает. Меню соответствует доступным текстовым командам. | `test_contextual_help.py::test_unknown_and_registration_overlay`; `test_command_menu.py::test_unknown_registration_and_link_overlay`; `test_profile_obsidian.py::test_cancelled_or_restarted_registration_leaves_no_profile`; `test_menu_lifecycle.py::test_registration_link_modes_access_input_failure_and_restart` | Реальное отображение меню и подсказок до регистрации. Для существующего личного чата состояние неизвестной identity нужно получать штатным тестовым профилем, без удаления production-данных. |
| Новая регистрация: `/register` → допустимое имя → `/help`; отдельно неверное имя | Профиль и связь создаются после допустимого имени; ответ обращается по имени. Неверное имя даёт правила исправления, ожидание сохраняет исходный срок. `/help` переходит к основному списку. | `test_text_command_sequences.py`; `test_pending_input.py`; `test_process_incoming_message.py::test_execute_saves_registration_name_before_vault`; `test_menu_lifecycle.py` | Вид кириллицы и обновление меню после принятия имени. Не повторять на уже зарегистрированном production-чате. |
| Готовый профиль в `main`: `/help`, `/status`, обычный текст | `/help` показывает общие команды, `/capabilities`, `/link_code` и только доступные входы. Внутренних команд Obsidian, общего `/confirm` без ожидания и `/cancel` без ввода нет. Обычный текст предлагает выбрать возможность. | `test_contextual_help.py::test_main_lists_entries_without_subject_commands`; `test_command_menu.py::test_main_uses_catalog_and_executable_input_entries`; `test_incoming_boundary.py::test_plain_text_and_url_without_mode_only_show_hint` | Фактические список Telegram при `/`, постоянное меню VK и переносы строк. |
| Доступный Obsidian: `/obsidian`, `/help`, `/vault_status`, `/exit` | Вход описывает хранилище заметок (vault), доступные команды и `/exit`; `/help` содержит команды Obsidian и доступный одноразовый `/ask`. После `/exit` этот чат снова в `main`. `/vault_status` только читает состояние. | `test_contextual_help.py::test_active_mode_hides_other_entries_but_preserves_manual_access`; `test_command_menu.py::test_active_mode_is_subject_commands_exit_and_effective_ask`; `test_chat_modes.py`; `test_profile_obsidian.py` | Вид длинной справки и меню после смены режима; фактическое состояние GitHub зависит от окружения. |
| Разовый Ask поверх `main` или Obsidian: `/ask` → короткий нейтральный вопрос; отдельно `/ask <question>` | `/ask` запрашивает вопрос следующим сообщением; допустимый вопрос запускает один ответ, возможна предварительная квитанция. Активный режим не меняется, `/help` во время ожидания показывает его и `/cancel`. | `test_ask_channels.py::test_registered_questions_are_independent_and_do_not_select_mode`; `test_capability_pending_input.py::test_ask_retry_ttl_direct_and_mode`; `test_contextual_help.py::test_ask_overlay_and_unknown_link_keep_base_context` | Реальный ответ модели, время доставки, оформление и разбиение. Вопрос отправлять только в согласованном окне, поскольку он вызывает модель. |
| Лично выключенная или серверно недоступная возможность: `/help`, попытка входа; затем штатное восстановление доступности | Недоступный вход скрыт или объясняет причину; сохранённый режим не исполняет предметный текст. После восстановления доступности сохранённый режим снова работает без потери данных. | `test_contextual_help.py::test_unavailable_modes_and_effective_alternatives`; `test_command_menu.py::test_unavailable_modes_only_offer_exit_and_alternatives`; `test_personal_processor_admission.py::test_disable_command_is_shared_and_reenable_restores_saved_mode`; `test_menu_reconciliation.py::test_server_availability_changes_on_reconfigured_startup` | Изменение серверной доступности проверять только в контролируемой конфигурации; в клиенте можно безопасно проверить личное `/disable` и `/enable` при согласованном профиле. |
| Сохранённый режим и соседние чаты: войти в Obsidian в одном чате, `/help` в другом, перезапустить обработчик | Режим хранится отдельно для адреса чата и канала, переживает перезапуск; Ask не становится активным режимом. Устаревший сохранённый `ask` даёт `/exit` и рабочие входы. | `test_chat_modes.py::test_mode_survives_new_connection`; `test_chat_modes.py::test_dispatcher_restores_mode_without_parsing_subject_text`; `test_contextual_help.py::test_saved_one_shot_is_not_an_active_mode`; `test_menu_lifecycle.py` | Клиентский перезапуск и два канала требуют контролируемого окна; не имитировать его на production ради одиночной пробы. |
| Команда без аргумента: `/name`, `/link`, `/capabilities`, `/reanalyze` в Obsidian; затем `/help` и `/cancel` | Команда запрашивает конкретное значение, `/help` сохраняет ожидание и показывает `/cancel`; отмена действует только в текущем чате. Неверное значение допускает повтор до исходного срока; после 10 минут значение не исполняется. | `test_core_pending_input.py`; `test_capability_pending_input.py`; `test_pending_input.py`; `test_command_menu.py::test_pending_expiration_and_context_validation_match_help` | Для безопасной пробы не вводить реальный код связи и ID статьи. `/reanalyze` не завершать ID без отдельного решения о повторном анализе. |
| Есть одно или несколько адресных подтверждений: `/help`, `/confirm`, точная команда `... no` из актуального ответа | `/help` показывает конкретные действия; голая `/confirm` только перечисляет их при полном снимке. При неполном снимке известные адресные действия помечены как неполный список; неподтверждённое действие не выполняется. Короткие «да»/«нет» неоднозначны при нескольких ожиданиях. | `test_confirmation_snapshot.py`; `test_contextual_help.py::test_confirmations_complete_incomplete_and_pre_profile`; `test_command_menu.py::test_confirm_requires_registered_complete_nonempty_snapshot`; `test_text_command_sequences.py` | Формирование предметного ожидания может менять внешнее состояние. Для клиентской пробы использовать уже подготовленное обратимое действие и выбирать `no`; не вводить `yes` ради вида кнопки. |
| Inline-кнопка актуального ответа, повторное нажатие и устаревшая кнопка | Кнопка выбирает ту же точную текстовую команду один раз. Повтор и выбор после смены профиля, режима, доступа или истечения срока не исполняются. Текстовый путь остаётся доступен при ошибке меню или очистки кнопки. Постоянное меню VK остаётся после inline-выбора. | `test_action_adapter_flow.py`; `test_response_action_selection.py`; `test_telegram_bot.py::test_invalid_or_repeated_callback_and_edit_error_keep_response`; `test_vk_bot.py::test_message_event_repeated_and_foreign_choice_never_execute_twice`; `test_menu_lifecycle.py::test_confirmation_menu_tracks_complete_snapshot_and_text_path` | Доставка callback клиентом, вид старой кнопки и постоянного меню VK подтверждаются только вручную. Для пробы выбирать отмену безопасного ожидания. |
| Длинный ответ с кириллицей, emoji и переменным текстом | Части приходят по порядку без потери текста; клавиатура прикреплена к последней части. Telegram экранирует разметку, VK сохраняет корректный `format_data` или отправляет обычный текст при неподдерживаемом сочетании. | `test_message_rendering.py`; `test_channel_rendering_delivery.py`; `test_telegram_bot.py::test_keyboard_attaches_to_last_chunk_only`; `test_vk_bot.py::test_vk_inline_keyboard_is_attached_only_to_last_chunk_and_preserves_text` | Реальные отображение, доставка и порядок в клиентах остаются ручной проверкой шагов 8.4–8.5. Не отправлять синтетический длинный текст в production без отдельной согласованной пробы. |

## Проверить каждый канал

1. В Telegram после переключения polling открыть свой личный диалог: вызвать `/help`, посмотреть список при вводе `/`, пройти применимые строки матрицы и сравнить текст с меню. Для безопасной проверки ввода использовать `/name` → `/cancel`; для Ask — нейтральный вопрос только если доступен. Записать вид inline-кнопки и результат повторного нажатия на заранее подготовленном отменяемом действии.
2. В VK в своём личном диалоге повторить применимые текстовые шаги; проверить постоянное меню до и после выбора inline-кнопки, переносы, `format_data` и видимую устаревшую кнопку. Текстовый `/help` и адресная команда должны оставаться рабочими без кнопок.
3. Для каждой выполненной строки записать дату, канал, исходное состояние, ожидаемое и фактическое поведение. Если состояние не подготовлено безопасно, отметить «не проверено в клиенте» с причиной; автоматический тест не заменяет это наблюдение. Расхождения передать шагу 8.6, а возврат обоих polling подтвердить отдельно по шагу 8.7.

## Оставшиеся пробелы

Клиентские пробы 29.10.2026 по текущей локальной сборке и причины пропусков
записаны в [результатах этапа 8](STAGE8_CLIENT_RESULTS.md). В Telegram и VK
подтверждены контекстная справка, меню, регистрация, выбор возможности,
Obsidian, Ask, оформление, inline-кнопки и порядок длинных ответов. После
исправлений проверены VK callback, широкая постоянная клавиатура, команды
`/vault_*` и английские подсказки аргументов. Реальное адресное подтверждение,
длинный ответ с кнопкой и некоторые ветки ошибок остаются без клиентской пробы;
их автоматические доказательства и причины приведены в журнале. Production
polling не возвращён по решению владельца.

### Автоматическая проверка шага 8.2 — 29.10.2026

Проверка выполнена в worktree `contextual-help` ветки
`codex/contextual-help-implementation`, без запуска ботов и доступа к VPS.
Адресный прогон восьми модулей — 107 тестов, `OK`; полный
`unittest discover -s tests` — 982 теста, `OK (skipped=2)`.
Оба прогона использовали
`C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe`:
собственного `.venv` в worktree нет. Системный Python не использовался.
Новые проверки дополняют перечисленные выше гарантии, а не заменяют
клиентскую приёмку:

- `test_text_command_sequences.py::test_variable_name_and_model_output_remain_literal_in_both_channels`
  проводит имя профиля и длинный ответ Ask через настоящие use cases и оба
  renderer: HTML-подобный текст, entities, кириллица, emoji и combining marks
  сохраняются буквально и в исходном порядке.
- `test_text_command_sequences.py::test_text_confirmation_makes_visible_old_button_unusable`
  создаёт запрос отключения хранилища, завершает его текстовым отказом и
  отклоняет старую кнопку. Новая кнопка отказа работает. Выявлен и исправлен
  дефект: уточнённая подпись публичного снимка мешала связать начальную кнопку
  Obsidian. Сопоставление теперь использует `action_id` и точную команду;
  каноническое действие снимка сохраняется для проверки перед исполнением.
- `test_response_action_selection.py::test_confirmation_label_may_differ_but_id_and_command_must_match`
  проверяет допустимое различие подписей и запрещает подмену ID или команды.
- `test_channel_rendering_delivery.py::test_invalid_keyboard_preserves_all_parts_and_text_commands`
  проверяет превышение числа кнопок и длины подписи Telegram/VK. Исправлено
  исчезновение всего ответа при ошибке локальной сериализации клавиатуры:
  адаптер отправляет все части с адресными командами без кнопок. Этот fallback
  выполняется до обращения к API; сетевой сбой не запускает повторную отправку
  всего ответа обычным текстом.

Следующий шаг — согласованная подготовка окна по `STAGE3_HANDOFF.md`.
Эти проверки не подтверждают отображение HTML и `format_data`, вид составных
emoji, фактический порядок сетевой доставки, сохранение постоянного меню VK
или поведение callback в приложениях пользователя. Для них остаются шаги
8.4–8.5; дата автоматического прогона не является датой клиентской приёмки.

### Клиентские наблюдения, которые ещё нужны

- Автоматические тесты используют fake-адаптеры и не доказывают вид меню, `format_data`, разбиение и callback в реальных клиентах или сетевую доставку. Эти пробы относятся к шагам 8.4–8.5.
- Старый профиль без имени проверен миграционным тестом, но клиентская проба требует отдельной development-БД. Production-профиль с `NULL`-именем для этого не нужен.
- Неполный снимок подтверждений и серверное отключение воспроизводятся тестами; безопасная клиентская проба возможна только при заранее подготовленном контролируемом состоянии. Не считать их проверенными в клиентах без отдельной записи.
