-- Идентификатор сообщения имеет смысл только внутри канала и чата.
CREATE TABLE family_shopping_add_receipts (
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    PRIMARY KEY (channel, chat_id, message_id)
);

-- Номера позиций в семье возрастают и не выдаются повторно. Диапазон сохраняет
-- границы подборки при удалении товаров и запоздавшем запуске отправителя.
CREATE TABLE family_shopping_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    first_number INTEGER NOT NULL CHECK (first_number > 0),
    last_number INTEGER NOT NULL CHECK (last_number >= first_number),
    FOREIGN KEY (family_id, recipient_id)
        REFERENCES family_members(family_id, app_user_id) ON DELETE CASCADE
);
CREATE INDEX idx_family_shopping_batches_recipient
    ON family_shopping_batches(family_id, recipient_id, id DESC);
CREATE INDEX idx_family_shopping_batches_due ON family_shopping_batches(due_at, id);
