-- Редакция позиции защищает выбор категории в двух чатах, включая смену туда и обратно.
ALTER TABLE family_expense_items ADD COLUMN category_revision INTEGER NOT NULL DEFAULT 1 CHECK (category_revision >= 1);
