-- Режим чата и личный доступ используют ID ai. Ревизии и выбор сохраняются.
UPDATE active_chat_modes SET capability_id = 'ai' WHERE capability_id = 'ask';
UPDATE user_capability_preferences SET capability_id = 'ai' WHERE capability_id = 'ask';
