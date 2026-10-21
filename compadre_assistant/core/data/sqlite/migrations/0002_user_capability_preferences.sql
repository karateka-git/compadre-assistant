-- Явные личные настройки; отсутствие строки означает default из каталога кода.
CREATE TABLE user_capability_preferences (
    app_user_id INTEGER NOT NULL,
    capability_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (app_user_id, capability_id),
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE
);
