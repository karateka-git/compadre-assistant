ALTER TABLE family_expense_items ADD COLUMN package_size TEXT;
ALTER TABLE family_expense_items ADD COLUMN amount_before_discount INTEGER
    CHECK (amount_before_discount IS NULL OR
           (typeof(amount_before_discount) = 'integer' AND category_id IS NOT NULL AND amount_before_discount >= amount));
