ALTER TABLE app_users ADD COLUMN notification_channel TEXT
    CHECK (notification_channel IN ('telegram', 'vk'));

-- Дату создания перенесённой identity нельзя использовать как дату регистрации.
CREATE VIEW sole_notification_channel AS
    SELECT app_user_id, MIN(channel) AS channel
    FROM external_identities
    WHERE channel IN ('telegram', 'vk')
    GROUP BY app_user_id
    HAVING COUNT(DISTINCT channel) = 1;

UPDATE app_users SET notification_channel = (
    SELECT channel FROM sole_notification_channel WHERE app_user_id = app_users.id
);

-- Поддерживаем выбор в той же транзакции при всех способах изменения привязки.
CREATE TRIGGER notification_channel_on_link AFTER INSERT ON external_identities
WHEN NEW.channel IN ('telegram', 'vk')
BEGIN
    UPDATE app_users SET notification_channel = (
        SELECT channel FROM sole_notification_channel WHERE app_user_id = NEW.app_user_id
    ) WHERE id = NEW.app_user_id AND notification_channel IS NULL;
END;

CREATE TRIGGER notification_channel_on_transfer AFTER UPDATE OF app_user_id ON external_identities
WHEN OLD.app_user_id <> NEW.app_user_id AND NEW.channel IN ('telegram', 'vk')
BEGIN
    UPDATE app_users SET notification_channel = (
        SELECT channel FROM sole_notification_channel WHERE app_user_id = app_users.id
    ) WHERE id IN (OLD.app_user_id, NEW.app_user_id) AND
        (notification_channel IS NULL OR NOT EXISTS (
            SELECT 1 FROM external_identities
            WHERE app_user_id = app_users.id AND channel = app_users.notification_channel
        ));
END;

CREATE TRIGGER notification_channel_on_unlink AFTER DELETE ON external_identities
WHEN OLD.channel IN ('telegram', 'vk')
BEGIN
    UPDATE app_users SET notification_channel = (
        SELECT channel FROM sole_notification_channel WHERE app_user_id = OLD.app_user_id
    ) WHERE id = OLD.app_user_id AND (notification_channel IS NULL OR NOT EXISTS (
        SELECT 1 FROM external_identities
        WHERE app_user_id = OLD.app_user_id AND channel = app_users.notification_channel
    ));
END;
