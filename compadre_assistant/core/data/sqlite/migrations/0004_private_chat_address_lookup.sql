-- Ускоряет точечную проверку владельца и неоднозначности адреса меню.
CREATE INDEX idx_external_identities_channel_chat
ON external_identities (channel, external_chat_id);
