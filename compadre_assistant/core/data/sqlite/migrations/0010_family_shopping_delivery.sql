-- Перед передачей в API отправитель сохраняет попытку. После сбоя процесса
-- такая попытка имеет неопределённый результат и автоматически не повторяется.
ALTER TABLE family_shopping_batches ADD COLUMN dispatching INTEGER NOT NULL DEFAULT 0
    CHECK (dispatching IN (0, 1));
ALTER TABLE family_shopping_batches ADD COLUMN retry_at TEXT;
CREATE INDEX idx_family_shopping_batches_ready ON family_shopping_batches(due_at, id)
    WHERE dispatching = 0;
