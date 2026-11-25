ALTER TABLE family_expense_categories ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1));
ALTER TABLE family_expense_categories ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1);

-- Одни и те же начальные значения для обновляемых и будущих семей.
CREATE VIEW family_expense_category_defaults (name, name_key) AS VALUES
    ('Продукты', 'продукты'),
    ('Кафе и доставка еды', 'кафе и доставка еды'),
    ('Транспорт', 'транспорт'),
    ('Дом и быт', 'дом и быт'),
    ('Здоровье', 'здоровье'),
    ('Одежда', 'одежда'),
    ('Дети', 'дети'),
    ('Питомцы', 'питомцы'),
    ('Досуг', 'досуг'),
    ('Связь и подписки', 'связь и подписки'),
    ('Прочее', 'прочее');

INSERT INTO family_expense_categories (family_id, name, name_key)
SELECT families.id, defaults.name, defaults.name_key
FROM families CROSS JOIN family_expense_category_defaults AS defaults WHERE 1
ON CONFLICT(family_id, name_key) DO NOTHING;

-- Только создание семьи заполняет каталог. Переименование или скрытие его не пересоздаёт.
CREATE TRIGGER family_initial_expense_categories AFTER INSERT ON families
BEGIN
    INSERT INTO family_expense_categories (family_id, name, name_key)
    SELECT NEW.id, name, name_key FROM family_expense_category_defaults;
END;
