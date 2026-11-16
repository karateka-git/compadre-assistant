CREATE TABLE families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    owner_app_user_id INTEGER NOT NULL,
    next_item_number INTEGER NOT NULL DEFAULT 1 CHECK (next_item_number > 0),
    created_at TEXT NOT NULL,
    -- Создание семьи и первой записи членства завершает одна транзакция.
    FOREIGN KEY (id, owner_app_user_id) REFERENCES family_members (family_id, app_user_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE family_members (
    family_id INTEGER NOT NULL,
    app_user_id INTEGER NOT NULL UNIQUE,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (family_id, app_user_id),
    FOREIGN KEY (family_id) REFERENCES families (id) ON DELETE CASCADE,
    -- Удаление профиля не должно скрыто менять членство или лишать семью владельца.
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE NO ACTION
);

CREATE TABLE family_invites (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL,
    created_by_app_user_id INTEGER,
    code_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (family_id) REFERENCES families (id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_app_user_id) REFERENCES app_users (id) ON DELETE SET NULL
);

CREATE INDEX idx_family_invites_family_id ON family_invites (family_id);
CREATE INDEX idx_family_invites_created_by ON family_invites (created_by_app_user_id);

CREATE TABLE family_shopping_items (
    family_id INTEGER NOT NULL,
    number INTEGER NOT NULL CHECK (number > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    status TEXT NOT NULL CHECK (status IN ('needed', 'bought')),
    created_by_app_user_id INTEGER,
    created_at TEXT NOT NULL,
    bought_by_app_user_id INTEGER,
    bought_at TEXT,
    PRIMARY KEY (family_id, number),
    FOREIGN KEY (family_id) REFERENCES families (id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_app_user_id) REFERENCES app_users (id) ON DELETE SET NULL,
    FOREIGN KEY (bought_by_app_user_id) REFERENCES app_users (id) ON DELETE SET NULL
);

CREATE INDEX idx_family_shopping_items_created_by ON family_shopping_items (created_by_app_user_id);
CREATE INDEX idx_family_shopping_items_bought_by ON family_shopping_items (bought_by_app_user_id);
