-- =======================================================
-- 로컬 개발/테스트용 스키마 사본
--
-- ⚠️ 이 파일은 스키마의 원본이 아니다.
-- policy_chunks를 포함한 모든 테이블의 소유자는 Spring 프로젝트이고,
-- 변경은 Flyway 마이그레이션으로만 이뤄진다.
-- 여기 있는 SQL은 백엔드가 확정한 DDL을 그대로 복사한 것으로,
-- 로컬에서 INSERT/검색을 검증하기 위한 용도다.
-- 백엔드에서 DDL이 바뀌면 이 파일도 맞춰 갱신해야 한다.
-- =======================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id UUID PRIMARY KEY,
    email VARCHAR(100),
    password_hash VARCHAR(255),
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(30),
    avatar_emoji VARCHAR(10),
    passport_name VARCHAR(100),
    passport_no_encrypted VARCHAR(500),
    nationality_code VARCHAR(10),
    provider VARCHAR(30) NOT NULL,
    provider_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_users_provider_provider_id UNIQUE (provider, provider_id)
);

CREATE TABLE trips (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    title VARCHAR(100) NOT NULL,
    country_code VARCHAR(10) NOT NULL,
    country_name VARCHAR(100) NOT NULL,
    city_name VARCHAR(100),
    flag_emoji VARCHAR(10),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_trips_user_id FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE policies (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    trip_id UUID NOT NULL,
    insurer_name VARCHAR(200) NOT NULL,
    product_name VARCHAR(200) NOT NULL,
    policy_number_encrypted VARCHAR(500),
    display_name VARCHAR(200) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL,
    coverage_score INTEGER,
    coverage_count INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_policies_user_id FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_policies_trip_id FOREIGN KEY (trip_id) REFERENCES trips(id)
);
CREATE INDEX idx_policies_user_id ON policies(user_id);
CREATE INDEX idx_policies_trip_id ON policies(trip_id);

CREATE TABLE policy_documents (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    trip_id UUID,
    policy_id UUID,
    original_filename VARCHAR(255) NOT NULL,
    stored_file_path VARCHAR(500) NOT NULL,
    content_type VARCHAR(100),
    file_size BIGINT,
    parse_status VARCHAR(20) NOT NULL,
    uploaded_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_policy_documents_user_id FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_policy_documents_trip_id FOREIGN KEY (trip_id) REFERENCES trips(id),
    CONSTRAINT fk_policy_documents_policy_id FOREIGN KEY (policy_id) REFERENCES policies(id)
);
CREATE INDEX idx_policy_documents_user_id ON policy_documents(user_id);
CREATE INDEX idx_policy_documents_policy_id ON policy_documents(policy_id);

CREATE TABLE analysis_results (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL,
    policy_id UUID,
    summary TEXT,
    raw_result_json TEXT,
    accuracy_score REAL,
    status VARCHAR(20) NOT NULL,
    embedding_model VARCHAR(100),
    embedding_dimension INTEGER,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    failure_reason TEXT,
    analyzed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_analysis_results_document_id UNIQUE (document_id),
    CONSTRAINT fk_analysis_results_document_id FOREIGN KEY (document_id) REFERENCES policy_documents(id),
    CONSTRAINT fk_analysis_results_policy_id FOREIGN KEY (policy_id) REFERENCES policies(id)
);
CREATE INDEX idx_analysis_results_policy_id ON analysis_results(policy_id);
CREATE INDEX idx_analysis_results_status ON analysis_results(status);

CREATE TABLE coverage_items (
    id UUID PRIMARY KEY,
    analysis_result_id UUID NOT NULL,
    title VARCHAR(200) NOT NULL,
    subtitle VARCHAR(500),
    category VARCHAR(100),
    limit_label VARCHAR(100),
    is_covered BOOLEAN NOT NULL,
    coverage_status VARCHAR(20) NOT NULL,
    limit_amount BIGINT,
    limit_currency VARCHAR(10),
    conditions TEXT,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_coverage_items_analysis_result_id FOREIGN KEY (analysis_result_id) REFERENCES analysis_results(id)
);
CREATE INDEX idx_coverage_items_analysis_sort ON coverage_items(analysis_result_id, sort_order);

CREATE TABLE coverage_detail_items (
    id UUID PRIMARY KEY,
    coverage_item_id UUID NOT NULL,
    title VARCHAR(200) NOT NULL,
    subtitle VARCHAR(500),
    is_covered BOOLEAN NOT NULL,
    sort_order INTEGER NOT NULL,
    CONSTRAINT fk_coverage_detail_items_coverage_item_id FOREIGN KEY (coverage_item_id) REFERENCES coverage_items(id)
);
CREATE INDEX idx_coverage_detail_items_coverage_sort ON coverage_detail_items(coverage_item_id, sort_order);

CREATE TABLE sub_coverage_limits (
    id UUID PRIMARY KEY,
    coverage_item_id UUID NOT NULL,
    label VARCHAR(100) NOT NULL,
    value VARCHAR(200) NOT NULL,
    limit_amount BIGINT,
    limit_currency VARCHAR(10),
    description VARCHAR(500),
    sort_order INTEGER NOT NULL,
    CONSTRAINT fk_sub_coverage_limits_coverage_item_id FOREIGN KEY (coverage_item_id) REFERENCES coverage_items(id)
);
CREATE INDEX idx_sub_coverage_limits_coverage_sort ON sub_coverage_limits(coverage_item_id, sort_order);

CREATE TABLE required_documents (
    id UUID PRIMARY KEY,
    coverage_item_id UUID NOT NULL,
    document_name VARCHAR(200) NOT NULL,
    is_mandatory BOOLEAN NOT NULL,
    sort_order INTEGER NOT NULL,
    CONSTRAINT fk_required_documents_coverage_item_id FOREIGN KEY (coverage_item_id) REFERENCES coverage_items(id)
);
CREATE INDEX idx_required_documents_coverage_sort ON required_documents(coverage_item_id, sort_order);

CREATE TABLE exclusion_conditions (
    id UUID PRIMARY KEY,
    coverage_item_id UUID NOT NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    source_text TEXT,
    severity VARCHAR(20) NOT NULL,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_exclusion_conditions_coverage_item_id FOREIGN KEY (coverage_item_id) REFERENCES coverage_items(id)
);
CREATE INDEX idx_exclusion_conditions_coverage_sort ON exclusion_conditions(coverage_item_id, sort_order);

CREATE TABLE policy_chunks (
    id UUID PRIMARY KEY,
    analysis_result_id UUID NOT NULL,
    user_id UUID NOT NULL,
    trip_id UUID,
    policy_id UUID,
    document_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    source_content_type VARCHAR(30) NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    section_title VARCHAR(500),
    clause_path VARCHAR(300),
    coverage_category VARCHAR(100),
    clause_type VARCHAR(30) NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    embedding vector(1536),
    char_count INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_policy_chunks_analysis_chunk_index UNIQUE (analysis_result_id, chunk_index),
    CONSTRAINT fk_policy_chunks_analysis_result_id FOREIGN KEY (analysis_result_id) REFERENCES analysis_results(id),
    CONSTRAINT fk_policy_chunks_user_id FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_policy_chunks_trip_id FOREIGN KEY (trip_id) REFERENCES trips(id),
    CONSTRAINT fk_policy_chunks_policy_id FOREIGN KEY (policy_id) REFERENCES policies(id),
    CONSTRAINT fk_policy_chunks_document_id FOREIGN KEY (document_id) REFERENCES policy_documents(id)
);
CREATE INDEX idx_policy_chunks_user_trip ON policy_chunks(user_id, trip_id);
CREATE INDEX idx_policy_chunks_user_policy ON policy_chunks(user_id, policy_id);
CREATE INDEX idx_policy_chunks_user_document ON policy_chunks(user_id, document_id);

CREATE TABLE coverage_item_sources (
    id UUID PRIMARY KEY,
    coverage_item_id UUID NOT NULL,
    policy_chunk_id UUID NOT NULL,
    source_role VARCHAR(30) NOT NULL,
    quote_text TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_coverage_item_sources_item_chunk_role UNIQUE (coverage_item_id, policy_chunk_id, source_role),
    CONSTRAINT fk_coverage_item_sources_coverage_item_id FOREIGN KEY (coverage_item_id) REFERENCES coverage_items(id),
    CONSTRAINT fk_coverage_item_sources_policy_chunk_id FOREIGN KEY (policy_chunk_id) REFERENCES policy_chunks(id)
);
CREATE INDEX idx_coverage_item_sources_policy_chunk_id ON coverage_item_sources(policy_chunk_id);

CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    trip_id UUID,
    policy_id UUID,
    title VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL,
    started_at TIMESTAMP NOT NULL,
    last_active_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_chat_sessions_user_id FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_chat_sessions_trip_id FOREIGN KEY (trip_id) REFERENCES trips(id),
    CONSTRAINT fk_chat_sessions_policy_id FOREIGN KEY (policy_id) REFERENCES policies(id)
);
CREATE INDEX idx_chat_sessions_user_id ON chat_sessions(user_id);

CREATE TABLE chat_messages (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    sender VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    response_type VARCHAR(30) NOT NULL,
    metadata_json TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_chat_messages_session_id FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
);
CREATE INDEX idx_chat_messages_session_created ON chat_messages(session_id, created_at);

CREATE TABLE emergency_contacts (
    id UUID PRIMARY KEY,
    country_code VARCHAR(10) NOT NULL,
    type VARCHAR(30) NOT NULL,
    name VARCHAR(200) NOT NULL,
    phone VARCHAR(50),
    description VARCHAR(500),
    insurer_name VARCHAR(200),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_emergency_contacts_country_type ON emergency_contacts(country_code, type);

CREATE TABLE notifications (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    type VARCHAR(30) NOT NULL,
    title VARCHAR(200) NOT NULL,
    body VARCHAR(500) NOT NULL,
    deep_link VARCHAR(500),
    read_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_notifications_user_id FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_notifications_user_created ON notifications(user_id, created_at);

CREATE TABLE notification_preferences (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    policy_expiry_enabled BOOLEAN NOT NULL,
    renewal_enabled BOOLEAN NOT NULL,
    analysis_done_enabled BOOLEAN NOT NULL,
    push_enabled BOOLEAN NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_notification_preferences_user_id UNIQUE (user_id),
    CONSTRAINT fk_notification_preferences_user_id FOREIGN KEY (user_id) REFERENCES users(id)
);
