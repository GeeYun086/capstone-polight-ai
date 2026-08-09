-- =======================================================
-- rag_service 계정 (운영 권한 구성을 로컬에서 재현)
--
-- 운영에서 Python은 Spring과 같은 계정을 쓰지 않는다.
-- policy_chunks만 쓰고, 나머지는 제한적으로 읽으며, DDL 권한은 없다.
--
-- 로컬에서도 같은 제약으로 개발하면 권한 문제를 지금 발견할 수 있다.
-- superuser로 개발하다가 운영에서 처음 막히면 원인 찾기가 어렵다.
-- =======================================================

CREATE ROLE rag_service WITH LOGIN PASSWORD 'rag_local_pw';

GRANT CONNECT ON DATABASE polight TO rag_service;
GRANT USAGE ON SCHEMA public TO rag_service;

-- RAG 산출물이므로 Python이 직접 쓴다
GRANT SELECT, INSERT, UPDATE, DELETE ON policy_chunks TO rag_service;

-- 분석 컨텍스트 확인용. 읽기만 한다.
GRANT SELECT ON analysis_results TO rag_service;
GRANT SELECT ON policy_documents TO rag_service;

-- 챗봇 멀티턴에서 대화 이력을 Python이 직접 읽는 방식으로 정해지면 필요해진다.
-- 아직 미확정이라 읽기만 열어둔다.
GRANT SELECT ON chat_sessions TO rag_service;
GRANT SELECT ON chat_messages TO rag_service;

-- 주지 않는 권한 (의도적)
--   users, policies, trips        수정 금지
--   coverage_items 계열           Spring이 콜백을 받아 저장한다
--   CREATE / ALTER / DROP TABLE   스키마 변경은 Flyway로만
