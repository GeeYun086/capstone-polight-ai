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

-- policies 테이블은 백엔드가 없앴다(V12). 채울 코드가 없어 policy_id가 늘 null이었고,
-- 채우려면 정할 것(NOT NULL 불일치·display_name·status 전이·증권번호 암호화)이 많은데
-- 그게 필요한 기능이 로드맵에 없었다. 보험 정보는 analysis_results가 갖는다.
-- 아래 테이블들의 policy_id 컬럼은 남기고 FK만 뗐다(pg_mapper.COLUMNS 호환).

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
    CONSTRAINT fk_policy_documents_trip_id FOREIGN KEY (trip_id) REFERENCES trips(id)
);
CREATE INDEX idx_policy_documents_user_id ON policy_documents(user_id);
CREATE INDEX idx_policy_documents_policy_id ON policy_documents(policy_id);

-- ── 공용 약관 영역 (V7·V9) ──────────────────────────────────
--
-- 상품 1건당 한 벌. 같은 상품 가입자 전원이 공유한다. 사용자 영역(analysis_results,
-- coverage_items)과 테이블 단위로 분리했다. 근거: 백엔드 PR #36, docs/BACKEND_REPLY_4.md.

CREATE TABLE policy_terms (
    id UUID PRIMARY KEY,
    insurer_name VARCHAR(200) NOT NULL,
    product_name VARCHAR(200) NOT NULL,
    revision VARCHAR(100),
    effective_date DATE,
    verification_status VARCHAR(20) NOT NULL,
    source VARCHAR(20) NOT NULL,
    source_document_id UUID,
    owner_user_id UUID,
    file_hash VARCHAR(64),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT ck_policy_terms_verification CHECK (verification_status IN ('UNVERIFIED', 'VERIFIED')),
    CONSTRAINT ck_policy_terms_source CHECK (source IN ('OFFICIAL', 'USER_UPLOAD')),
    -- 주인 없는 UNVERIFIED 금지
    CONSTRAINT ck_policy_terms_owner CHECK (verification_status = 'VERIFIED' OR owner_user_id IS NOT NULL),
    CONSTRAINT fk_policy_terms_source_document_id FOREIGN KEY (source_document_id) REFERENCES policy_documents(id),
    CONSTRAINT fk_policy_terms_owner_user_id FOREIGN KEY (owner_user_id) REFERENCES users(id)
);
-- VERIFIED 는 (보험사, 상품, 개정판)마다 하나. NULL revision을 ''로 접어 중복 방지.
CREATE UNIQUE INDEX uk_policy_terms_verified_product
    ON policy_terms (insurer_name, product_name, COALESCE(revision, ''))
    WHERE verification_status = 'VERIFIED';

CREATE TABLE policy_terms_chunks (
    id UUID PRIMARY KEY,
    terms_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    source_content_type VARCHAR(30) NOT NULL,
    clause_type VARCHAR(30) NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    section_title VARCHAR(500),
    clause_path VARCHAR(300),
    coverage_category VARCHAR(100),
    summary TEXT,
    embedding vector(1536),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_policy_terms_chunks_terms_chunk_index UNIQUE (terms_id, chunk_index),
    CONSTRAINT fk_policy_terms_chunks_terms_id FOREIGN KEY (terms_id) REFERENCES policy_terms(id)
);
CREATE INDEX idx_policy_terms_chunks_terms ON policy_terms_chunks(terms_id, clause_path);

-- 약관상 보장 규칙. 면책·청구서류·세부한도가 매달리는 부모. limit_amount(정수)를
-- 일부러 두지 않았다 - 숫자로 확정된 금액은 가입 사실이라 coverage_items에만 있어야
-- 한다. 약관 예시값이 가입금액으로 읽히면 안 된다.
CREATE TABLE policy_terms_coverages (
    id UUID PRIMARY KEY,
    terms_id UUID NOT NULL,
    title VARCHAR(200) NOT NULL,
    subtitle VARCHAR(500),
    category VARCHAR(100),
    limit_label VARCHAR(100),
    conditions TEXT,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_policy_terms_coverages_terms_sort UNIQUE (terms_id, sort_order),
    CONSTRAINT fk_policy_terms_coverages_terms_id FOREIGN KEY (terms_id) REFERENCES policy_terms(id)
);
CREATE INDEX idx_policy_terms_coverages_terms ON policy_terms_coverages(terms_id);

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
    -- V8: 증권 분석 결과를 어느 공용 약관에 연결했는지. insurerName/productName으로
    -- 백엔드가 matching해 채운다. 못 찾으면(NONE) null.
    matched_terms_id UUID,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uk_analysis_results_document_id UNIQUE (document_id),
    CONSTRAINT fk_analysis_results_document_id FOREIGN KEY (document_id) REFERENCES policy_documents(id),
    CONSTRAINT fk_analysis_results_matched_terms_id FOREIGN KEY (matched_terms_id) REFERENCES policy_terms(id)
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
    -- V9: 이 가입 담보가 어느 약관 보장 규칙에 연결되는지. 백엔드가 담보명 매칭으로
    -- 채운다(EXACT/QUALIFIED). 못 찾으면 null이고 화면에서 면책·서류가 빈다.
    terms_coverage_id UUID,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_coverage_items_analysis_result_id FOREIGN KEY (analysis_result_id) REFERENCES analysis_results(id),
    CONSTRAINT fk_coverage_items_terms_coverage_id FOREIGN KEY (terms_coverage_id) REFERENCES policy_terms_coverages(id)
);
CREATE INDEX idx_coverage_items_analysis_sort ON coverage_items(analysis_result_id, sort_order);

-- 자식 4종 (V9): 부모가 coverage_items -> policy_terms_coverages 로 바뀌었다.
-- 면책·서류·세부한도·세부항목은 가입자의 사실이 아니라 약관의 보장 규칙이다.
-- 컬럼 구조는 그대로이고 coverage_item_id -> terms_coverage_id 로만 바뀐다.
-- 타임스탬프는 exclusion_conditions 에만 있다(백엔드 스키마 그대로).

CREATE TABLE coverage_detail_items (
    id UUID PRIMARY KEY,
    terms_coverage_id UUID NOT NULL,
    title VARCHAR(200) NOT NULL,
    subtitle VARCHAR(500),
    is_covered BOOLEAN NOT NULL,
    sort_order INTEGER NOT NULL,
    CONSTRAINT fk_coverage_detail_items_terms_coverage_id FOREIGN KEY (terms_coverage_id) REFERENCES policy_terms_coverages(id)
);
CREATE INDEX idx_coverage_detail_items_terms_sort ON coverage_detail_items(terms_coverage_id, sort_order);

CREATE TABLE sub_coverage_limits (
    id UUID PRIMARY KEY,
    terms_coverage_id UUID NOT NULL,
    label VARCHAR(100) NOT NULL,
    value VARCHAR(200) NOT NULL,
    limit_amount BIGINT,
    limit_currency VARCHAR(10),
    description VARCHAR(500),
    sort_order INTEGER NOT NULL,
    CONSTRAINT fk_sub_coverage_limits_terms_coverage_id FOREIGN KEY (terms_coverage_id) REFERENCES policy_terms_coverages(id)
);
CREATE INDEX idx_sub_coverage_limits_terms_sort ON sub_coverage_limits(terms_coverage_id, sort_order);

CREATE TABLE required_documents (
    id UUID PRIMARY KEY,
    terms_coverage_id UUID NOT NULL,
    document_name VARCHAR(200) NOT NULL,
    is_mandatory BOOLEAN NOT NULL,
    sort_order INTEGER NOT NULL,
    CONSTRAINT fk_required_documents_terms_coverage_id FOREIGN KEY (terms_coverage_id) REFERENCES policy_terms_coverages(id)
);
CREATE INDEX idx_required_documents_terms_sort ON required_documents(terms_coverage_id, sort_order);

CREATE TABLE exclusion_conditions (
    id UUID PRIMARY KEY,
    terms_coverage_id UUID NOT NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    source_text TEXT,
    severity VARCHAR(20) NOT NULL,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_exclusion_conditions_terms_coverage_id FOREIGN KEY (terms_coverage_id) REFERENCES policy_terms_coverages(id)
);
CREATE INDEX idx_exclusion_conditions_terms_sort ON exclusion_conditions(terms_coverage_id, sort_order);

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
    CONSTRAINT fk_policy_chunks_document_id FOREIGN KEY (document_id) REFERENCES policy_documents(id)
);
CREATE INDEX idx_policy_chunks_user_trip ON policy_chunks(user_id, trip_id);
CREATE INDEX idx_policy_chunks_user_policy ON policy_chunks(user_id, policy_id);
CREATE INDEX idx_policy_chunks_user_document ON policy_chunks(user_id, document_id);
-- V6: document_id 단독 인덱스. 스코프 필터에 user_id가 없어 복합 인덱스가 안 쓰인다.
CREATE INDEX idx_policy_chunks_document_id ON policy_chunks(document_id);

-- 근거 조항 (V9): coverage_item_sources -> policy_terms_coverage_sources.
-- 양쪽 끝이 공용 영역으로 옮겨졌다. 규칙(terms_coverage)과 청크(terms_chunk)는
-- 같은 약관(terms_id)에 속해야 한다.
CREATE TABLE policy_terms_coverage_sources (
    id UUID PRIMARY KEY,
    terms_coverage_id UUID NOT NULL,
    terms_chunk_id UUID NOT NULL,
    source_role VARCHAR(30) NOT NULL,
    quote_text TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT ck_policy_terms_coverage_sources_role CHECK (source_role IN ('PRIMARY', 'CONDITION', 'EXCLUSION', 'LIMIT', 'PROCEDURE', 'REQUIRED_DOCUMENT', 'DEFINITION')),
    CONSTRAINT uk_policy_terms_coverage_sources UNIQUE (terms_coverage_id, terms_chunk_id, source_role),
    CONSTRAINT fk_ptcs_terms_coverage_id FOREIGN KEY (terms_coverage_id) REFERENCES policy_terms_coverages(id),
    CONSTRAINT fk_ptcs_terms_chunk_id FOREIGN KEY (terms_chunk_id) REFERENCES policy_terms_chunks(id)
);
CREATE INDEX idx_ptcs_terms_chunk_id ON policy_terms_coverage_sources(terms_chunk_id);

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
    CONSTRAINT fk_chat_sessions_trip_id FOREIGN KEY (trip_id) REFERENCES trips(id)
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
