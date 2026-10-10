CREATE TABLE app_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE external_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    username TEXT,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    UNIQUE (channel, external_user_id)
);

-- Режим маршрутизации принадлежит ядру; предметные ожидания хранят возможности.
CREATE TABLE active_chat_modes (
    app_user_id INTEGER NOT NULL,
    channel TEXT NOT NULL CHECK (length(trim(channel)) > 0),
    chat_id TEXT NOT NULL CHECK (length(trim(chat_id)) > 0),
    capability_id TEXT CHECK (capability_id IS NULL OR length(trim(capability_id)) > 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_user_id, channel, chat_id),
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE
);

CREATE INDEX idx_external_identities_app_user_id
    ON external_identities (app_user_id);

CREATE TABLE identity_link_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE
);

CREATE INDEX idx_identity_link_tokens_app_user_id
    ON identity_link_tokens (app_user_id);
CREATE INDEX idx_identity_link_tokens_expires_at
    ON identity_link_tokens (expires_at);

CREATE TABLE identity_rebind_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_app_user_id INTEGER NOT NULL,
    origin_chat_id TEXT NOT NULL CHECK (length(trim(origin_chat_id)) > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'cancelled')),
    channel TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    target_app_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    FOREIGN KEY (source_app_user_id) REFERENCES app_users (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_identity_rebind_confirmations_active_identity
    ON identity_rebind_confirmations (channel, external_user_id) WHERE status = 'pending';

CREATE INDEX idx_identity_rebind_confirmations_expires_at
    ON identity_rebind_confirmations (expires_at);

CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    title TEXT,
    cleaned_text TEXT,
    text_hash TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    UNIQUE (app_user_id, normalized_url)
);

CREATE INDEX idx_articles_app_user_status
    ON articles (app_user_id, status);
CREATE INDEX idx_articles_app_user_text_hash
    ON articles (app_user_id, text_hash);

CREATE TABLE incoming_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    article_id INTEGER,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_text TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE SET NULL,
    UNIQUE (channel, chat_id, message_id)
);

CREATE INDEX idx_incoming_messages_app_user_id
    ON incoming_messages (app_user_id);
CREATE INDEX idx_incoming_messages_article_id
    ON incoming_messages (article_id);

CREATE TABLE analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL,
    llm_model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    result_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
);

CREATE INDEX idx_analysis_results_app_user_article_id
    ON analysis_results (app_user_id, article_id);
CREATE INDEX idx_analysis_results_article_id
    ON analysis_results (article_id);

CREATE TABLE processing_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER,
    article_id INTEGER,
    incoming_message_id INTEGER,
    stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE SET NULL,
    FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE SET NULL,
    FOREIGN KEY (incoming_message_id)
        REFERENCES incoming_messages (id) ON DELETE SET NULL
);

CREATE INDEX idx_processing_errors_app_user_id
    ON processing_errors (app_user_id);
CREATE INDEX idx_processing_errors_article_id
    ON processing_errors (article_id);
CREATE INDEX idx_processing_errors_incoming_message_id
    ON processing_errors (incoming_message_id);

CREATE TABLE github_accounts (
    app_user_id INTEGER PRIMARY KEY,
    github_user_id INTEGER NOT NULL,
    login TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    CHECK (github_user_id > 0),
    CHECK (length(trim(login)) > 0)
);

CREATE INDEX idx_github_accounts_github_user_id
    ON github_accounts (github_user_id);

CREATE TABLE github_installations (
    app_user_id INTEGER NOT NULL,
    installation_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_user_id, installation_id),
    FOREIGN KEY (app_user_id) REFERENCES github_accounts (app_user_id)
        ON DELETE CASCADE,
    CHECK (installation_id > 0)
);

CREATE INDEX idx_github_installations_installation_id
    ON github_installations (installation_id);

CREATE TABLE github_connection_attempts (
    app_user_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    CHECK (length(trim(owner)) > 0)
);

CREATE INDEX idx_github_connection_attempts_expires_at
    ON github_connection_attempts (expires_at);

CREATE TABLE obsidian_vaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL UNIQUE,
    installation_id INTEGER NOT NULL,
    repository_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    repository TEXT NOT NULL,
    branch TEXT NOT NULL,
    root_path TEXT NOT NULL DEFAULT '',
    head_commit_sha TEXT,
    tree_sha TEXT,
    head_etag TEXT,
    last_checked_at TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id, installation_id)
        REFERENCES github_installations (app_user_id, installation_id)
        ON DELETE CASCADE,
    UNIQUE (app_user_id, id),
    CHECK (repository_id > 0)
);

CREATE INDEX idx_obsidian_vaults_installation_id
    ON obsidian_vaults (installation_id);
CREATE INDEX idx_obsidian_vaults_repository_id
    ON obsidian_vaults (repository_id);

CREATE TABLE obsidian_vault_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_vault_id INTEGER NOT NULL CHECK (base_vault_id > 0),
    app_user_id INTEGER NOT NULL,
    origin_channel TEXT NOT NULL CHECK (length(trim(origin_channel)) > 0),
    origin_chat_id TEXT NOT NULL CHECK (length(trim(origin_chat_id)) > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'cancelled')),
    action TEXT NOT NULL,
    installation_id INTEGER,
    repository_id INTEGER,
    owner TEXT,
    repository TEXT,
    branch TEXT,
    root_path TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    FOREIGN KEY (app_user_id, installation_id)
        REFERENCES github_installations (app_user_id, installation_id)
        ON DELETE CASCADE,
    CHECK (
        (
            action = 'replace'
            AND installation_id IS NOT NULL
            AND repository_id IS NOT NULL
            AND owner IS NOT NULL
            AND repository IS NOT NULL
            AND branch IS NOT NULL
            AND root_path IS NOT NULL
        )
        OR (
            action = 'disconnect'
            AND installation_id IS NULL
            AND repository_id IS NULL
            AND owner IS NULL
            AND repository IS NULL
            AND branch IS NULL
            AND root_path IS NULL
        )
    )
);

CREATE UNIQUE INDEX idx_obsidian_vault_confirmations_active_resource
    ON obsidian_vault_confirmations (app_user_id) WHERE status = 'pending';

CREATE INDEX idx_obsidian_vault_confirmations_expires_at
    ON obsidian_vault_confirmations (expires_at);

CREATE TABLE obsidian_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    title TEXT,
    markdown TEXT NOT NULL,
    frontmatter TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id, vault_id)
        REFERENCES obsidian_vaults (app_user_id, id) ON DELETE CASCADE,
    UNIQUE (vault_id, path),
    UNIQUE (app_user_id, id),
    UNIQUE (app_user_id, vault_id, id)
);

CREATE INDEX idx_obsidian_notes_app_user_vault
    ON obsidian_notes (app_user_id, vault_id);
CREATE INDEX idx_obsidian_notes_vault_blob_sha
    ON obsidian_notes (vault_id, blob_sha);

CREATE TABLE obsidian_vault_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id, vault_id)
        REFERENCES obsidian_vaults (app_user_id, id) ON DELETE CASCADE,
    UNIQUE (vault_id, path),
    UNIQUE (vault_id, position),
    UNIQUE (app_user_id, id),
    CHECK (position >= 0)
);

CREATE INDEX idx_obsidian_vault_instructions_app_user_vault
    ON obsidian_vault_instructions (app_user_id, vault_id);
CREATE INDEX idx_obsidian_vault_instructions_vault_blob_sha
    ON obsidian_vault_instructions (vault_id, blob_sha);

CREATE TABLE obsidian_note_tags (
    app_user_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (note_id, tag),
    FOREIGN KEY (app_user_id, note_id)
        REFERENCES obsidian_notes (app_user_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_obsidian_note_tags_app_user_tag
    ON obsidian_note_tags (app_user_id, tag);

CREATE TABLE obsidian_note_wikilinks (
    app_user_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (note_id, target),
    FOREIGN KEY (app_user_id, note_id)
        REFERENCES obsidian_notes (app_user_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_obsidian_note_wikilinks_app_user_target
    ON obsidian_note_wikilinks (app_user_id, target);

CREATE TABLE obsidian_note_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    note_path TEXT NOT NULL,
    chunk_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    heading_path TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id, vault_id)
        REFERENCES obsidian_vaults (app_user_id, id) ON DELETE CASCADE,
    FOREIGN KEY (app_user_id, vault_id, note_id)
        REFERENCES obsidian_notes (app_user_id, vault_id, id) ON DELETE CASCADE,
    UNIQUE (note_id, chunk_key),
    UNIQUE (note_id, position),
    UNIQUE (app_user_id, id),
    UNIQUE (app_user_id, vault_id, id),
    CHECK (position >= 0),
    CHECK (part_index >= 0),
    CHECK (length(trim(note_path)) > 0),
    CHECK (length(trim(chunk_key)) > 0),
    CHECK (length(trim(text)) > 0),
    CHECK (length(trim(content_hash)) > 0)
);

CREATE INDEX idx_obsidian_note_chunks_app_user_vault
    ON obsidian_note_chunks (app_user_id, vault_id);
CREATE INDEX idx_obsidian_note_chunks_note_position
    ON obsidian_note_chunks (note_id, position);
CREATE INDEX idx_obsidian_note_chunks_vault_content_hash
    ON obsidian_note_chunks (vault_id, content_hash);

CREATE TABLE obsidian_chunk_index_states (
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    index_signature TEXT NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_user_id, vault_id),
    FOREIGN KEY (app_user_id, vault_id)
        REFERENCES obsidian_vaults (app_user_id, id) ON DELETE CASCADE,
    CHECK (length(trim(index_signature)) > 0)
);

CREATE TABLE obsidian_note_chunk_index_states (
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    source_blob_sha TEXT NOT NULL,
    index_signature TEXT NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (note_id),
    FOREIGN KEY (app_user_id, vault_id, note_id)
        REFERENCES obsidian_notes (app_user_id, vault_id, id) ON DELETE CASCADE,
    CHECK (length(trim(source_blob_sha)) > 0),
    CHECK (length(trim(index_signature)) > 0)
);

CREATE INDEX idx_obsidian_note_chunk_states_app_user_vault
    ON obsidian_note_chunk_index_states (app_user_id, vault_id);

CREATE TABLE obsidian_chunk_embeddings (
    chunk_id INTEGER PRIMARY KEY,
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    document_model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (app_user_id, vault_id, chunk_id)
        REFERENCES obsidian_note_chunks (app_user_id, vault_id, id)
        ON DELETE CASCADE,
    CHECK (length(trim(document_model)) > 0),
    CHECK (dimension > 0),
    CHECK (length(trim(content_hash)) > 0),
    CHECK (typeof(vector) = 'blob'),
    CHECK (length(vector) = dimension * 4)
);

CREATE INDEX idx_obsidian_chunk_embeddings_app_user_vault
    ON obsidian_chunk_embeddings (app_user_id, vault_id);
CREATE INDEX idx_obsidian_chunk_embeddings_vault_profile
    ON obsidian_chunk_embeddings (
        vault_id,
        document_model,
        dimension,
        content_hash
    );

CREATE TABLE obsidian_embedding_index_states (
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    chunk_index_signature TEXT NOT NULL,
    document_model TEXT NOT NULL,
    query_model TEXT NOT NULL,
    dimension INTEGER,
    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_user_id, vault_id),
    FOREIGN KEY (app_user_id, vault_id)
        REFERENCES obsidian_vaults (app_user_id, id) ON DELETE CASCADE,
    CHECK (length(trim(chunk_index_signature)) > 0),
    CHECK (length(trim(document_model)) > 0),
    CHECK (length(trim(query_model)) > 0),
    CHECK (dimension IS NULL OR dimension > 0)
);

CREATE VIRTUAL TABLE obsidian_note_chunks_fts USING fts5 (
    app_user_id UNINDEXED,
    vault_id UNINDEXED,
    chunk_id UNINDEXED,
    title,
    path,
    tags,
    headings,
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER trg_obsidian_note_chunks_fts_insert
AFTER INSERT ON obsidian_note_chunks
BEGIN
    INSERT INTO obsidian_note_chunks_fts (
        rowid,
        app_user_id,
        vault_id,
        chunk_id,
        title,
        path,
        tags,
        headings,
        text
    )
    VALUES (
        NEW.id,
        NEW.app_user_id,
        NEW.vault_id,
        NEW.id,
        COALESCE((SELECT title FROM obsidian_notes WHERE id = NEW.note_id), ''),
        NEW.note_path,
        COALESCE((
            SELECT group_concat(tag, ' ')
            FROM (
                SELECT tag
                FROM obsidian_note_tags
                WHERE note_id = NEW.note_id
                ORDER BY position
            )
        ), ''),
        NEW.heading_path,
        NEW.text
    );
END;

CREATE TRIGGER trg_obsidian_note_chunks_fts_update
AFTER UPDATE OF note_path, heading_path, text ON obsidian_note_chunks
WHEN OLD.note_path IS NOT NEW.note_path
    OR OLD.heading_path IS NOT NEW.heading_path
    OR OLD.text IS NOT NEW.text
BEGIN
    UPDATE obsidian_note_chunks_fts
    SET path = NEW.note_path,
        headings = NEW.heading_path,
        text = NEW.text
    WHERE rowid = NEW.id;
END;

CREATE TRIGGER trg_obsidian_note_chunks_fts_delete
AFTER DELETE ON obsidian_note_chunks
BEGIN
    DELETE FROM obsidian_note_chunks_fts WHERE rowid = OLD.id;
END;

CREATE TRIGGER trg_obsidian_notes_fts_title_update
AFTER UPDATE OF title ON obsidian_notes
WHEN OLD.title IS NOT NEW.title
BEGIN
    UPDATE obsidian_note_chunks_fts
    SET title = COALESCE(NEW.title, '')
    WHERE rowid IN (
        SELECT id FROM obsidian_note_chunks WHERE note_id = NEW.id
    );
END;

CREATE TRIGGER trg_obsidian_note_tags_fts_insert
AFTER INSERT ON obsidian_note_tags
BEGIN
    UPDATE obsidian_note_chunks_fts
    SET tags = COALESCE((
        SELECT group_concat(tag, ' ')
        FROM (
            SELECT tag
            FROM obsidian_note_tags
            WHERE note_id = NEW.note_id
            ORDER BY position
        )
    ), '')
    WHERE rowid IN (
        SELECT id FROM obsidian_note_chunks WHERE note_id = NEW.note_id
    );
END;

CREATE TRIGGER trg_obsidian_note_tags_fts_delete
AFTER DELETE ON obsidian_note_tags
BEGIN
    UPDATE obsidian_note_chunks_fts
    SET tags = COALESCE((
        SELECT group_concat(tag, ' ')
        FROM (
            SELECT tag
            FROM obsidian_note_tags
            WHERE note_id = OLD.note_id
            ORDER BY position
        )
    ), '')
    WHERE rowid IN (
        SELECT id FROM obsidian_note_chunks WHERE note_id = OLD.note_id
    );
END;

CREATE TABLE obsidian_vault_sync_leases (
    app_user_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (app_user_id, vault_id)
        REFERENCES obsidian_vaults (app_user_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_obsidian_vault_sync_leases_expires_at
    ON obsidian_vault_sync_leases (expires_at);

CREATE TABLE obsidian_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_channel TEXT NOT NULL CHECK (length(trim(origin_channel)) > 0),
    origin_chat_id TEXT NOT NULL CHECK (length(trim(origin_chat_id)) > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    execution_expires_at TEXT,
    app_user_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL,
    analysis_id INTEGER NOT NULL,
    vault_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    target_path TEXT,
    proposed_markdown TEXT,
    base_commit_sha TEXT NOT NULL,
    base_tree_sha TEXT NOT NULL,
    target_blob_sha TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    applied_commit_sha TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
    FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    FOREIGN KEY (analysis_id) REFERENCES analysis_results (id) ON DELETE CASCADE,
    CHECK (action IN ('add', 'update', 'skip')),
    CHECK (status IN ('pending', 'executing', 'cancelled', 'conflict', 'applied')),
    CHECK (length(trim(reasoning)) > 0),
    CHECK (length(trim(base_commit_sha)) > 0),
    CHECK (length(trim(base_tree_sha)) > 0),
    CHECK (
        (action = 'skip' AND target_path IS NULL AND proposed_markdown IS NULL
            AND target_blob_sha IS NULL)
        OR
        (action = 'add' AND target_path IS NOT NULL
            AND proposed_markdown IS NOT NULL AND target_blob_sha IS NULL)
        OR
        (action = 'update' AND target_path IS NOT NULL
            AND proposed_markdown IS NOT NULL AND target_blob_sha IS NOT NULL)
    ),
    CHECK (
        (status = 'applied' AND completed_at IS NOT NULL)
        OR (status != 'applied' AND applied_commit_sha IS NULL)
    )
);

-- Один цикл одной статьи; разные статьи профиля ожидают независимо.
CREATE UNIQUE INDEX idx_obsidian_proposals_one_active_per_article
    ON obsidian_proposals (app_user_id, article_id)
    WHERE status IN ('pending', 'executing');
CREATE INDEX idx_obsidian_proposals_origin
    ON obsidian_proposals (app_user_id, origin_channel, origin_chat_id, status);
CREATE INDEX idx_obsidian_proposals_app_user_article
    ON obsidian_proposals (app_user_id, article_id, id DESC);
CREATE INDEX idx_obsidian_proposals_status
    ON obsidian_proposals (status, updated_at);

-- Lease полного article flow исключает конкурирующие анализы одной ссылки.
CREATE TABLE obsidian_article_flow_leases (
    app_user_id INTEGER NOT NULL,
    normalized_url TEXT NOT NULL,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (app_user_id, normalized_url),
    FOREIGN KEY (app_user_id) REFERENCES app_users (id) ON DELETE CASCADE
);
