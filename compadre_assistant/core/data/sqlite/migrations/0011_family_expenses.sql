ALTER TABLE families ADD COLUMN expense_timezone TEXT NOT NULL DEFAULT 'Asia/Omsk';

CREATE TABLE family_expense_categories (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    name_key TEXT NOT NULL CHECK (length(name_key) > 0),
    UNIQUE (family_id, name_key),
    UNIQUE (family_id, id)
);

CREATE TABLE family_expense_products (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    name_key TEXT NOT NULL CHECK (length(name_key) > 0),
    UNIQUE (family_id, name_key),
    UNIQUE (family_id, id)
);

CREATE TABLE family_expenses (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    author_id INTEGER REFERENCES app_users(id) ON DELETE SET NULL,
    operation_id TEXT NOT NULL CHECK (length(operation_id) > 0),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    purchase_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    description TEXT,
    receipt_total INTEGER CHECK (receipt_total IS NULL OR
        (typeof(receipt_total) = 'integer' AND receipt_total >= 0)),
    UNIQUE (family_id, operation_id),
    UNIQUE (family_id, id)
);
CREATE INDEX family_expenses_period ON family_expenses(family_id, purchase_date, id);

CREATE TABLE family_expense_items (
    family_id INTEGER NOT NULL,
    expense_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),
    category_id INTEGER,
    product_id INTEGER,
    amount INTEGER NOT NULL CHECK (typeof(amount) = 'integer'),
    quantity TEXT,
    unit TEXT,
    unit_price INTEGER CHECK (unit_price IS NULL OR
        (typeof(unit_price) = 'integer' AND unit_price >= 0)),
    PRIMARY KEY (expense_id, position),
    FOREIGN KEY (family_id, expense_id) REFERENCES family_expenses(family_id, id) ON DELETE CASCADE,
    FOREIGN KEY (family_id, category_id) REFERENCES family_expense_categories(family_id, id),
    FOREIGN KEY (family_id, product_id) REFERENCES family_expense_products(family_id, id),
    -- Отсутствующая категория обозначает единственную корректировку итога чека.
    CHECK ((category_id IS NOT NULL AND amount >= 0) OR
        (category_id IS NULL AND product_id IS NULL AND amount <> 0
         AND quantity IS NULL AND unit IS NULL AND unit_price IS NULL))
);
CREATE UNIQUE INDEX family_expense_adjustment ON family_expense_items(expense_id)
    WHERE category_id IS NULL;
CREATE INDEX family_expense_category_items ON family_expense_items(family_id, category_id, product_id);
CREATE INDEX family_expense_product_items ON family_expense_items(family_id, product_id, expense_id);
