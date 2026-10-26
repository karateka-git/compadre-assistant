-- Существующие профили без имени получают имя по умолчанию.
UPDATE app_users
SET display_name = 'Анатолий'
WHERE display_name IS NULL;
