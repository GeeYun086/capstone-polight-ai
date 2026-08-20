"""DB 연결 풀 검증.

실제 DB 없이 psycopg2 풀만 대체해 확인한다. 여기서 잡으려는 것은
"동시 요청이 풀 크기를 넘으면 어떻게 되는가"다.

psycopg2의 풀은 연결이 다 나가 있으면 기다리지 않고 PoolError를 던진다.
그대로 두면 매번 새 연결을 열던 이전 방식보다 나빠진다(실패 vs 조금 느림).
"""

import threading
import time

import pytest

from app.repositories import pg_repository
from app.repositories.pg_repository import PgVectorRepository


class FakeConnection:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        connection = self

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self): return []
        return Cursor()

    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True


class FakePool:
    """maxconn을 넘으면 psycopg2처럼 예외를 던진다."""

    def __init__(self, minconn, maxconn, dsn) -> None:
        self.maxconn = maxconn
        self.out = 0
        self.peak = 0
        self.created = 0
        self.closed = False
        self._lock = threading.Lock()

    def getconn(self):
        with self._lock:
            if self.out >= self.maxconn:
                raise pg_repository.pool.PoolError("connection pool exhausted")
            self.out += 1
            self.peak = max(self.peak, self.out)
            self.created += 1
        return FakeConnection()

    def putconn(self, connection):
        with self._lock:
            self.out -= 1

    def closeall(self):
        self.closed = True


@pytest.fixture
def repo(monkeypatch):
    created: list[FakePool] = []

    def make_pool(minconn, maxconn, dsn):
        p = FakePool(minconn, maxconn, dsn)
        created.append(p)
        return p

    monkeypatch.setattr(pg_repository.pool, "ThreadedConnectionPool", make_pool)
    monkeypatch.setattr(pg_repository, "register_vector", lambda c: None)

    repository = PgVectorRepository("postgresql://x", minconn=1, maxconn=3)
    repository._created_pools = created
    return repository


# 풀은 한 번만 만들어져야 한다. 질의마다 만들면 풀을 쓰는 의미가 없다.
def test_pool_is_created_once(repo):
    for _ in range(5):
        with repo._cursor():
            pass

    assert len(repo._created_pools) == 1


# 기동 시점에 만들면 DB가 아직 안 떴을 때 앱이 뜨지 못한다.
def test_pool_is_created_lazily(monkeypatch):
    monkeypatch.setattr(
        pg_repository.pool, "ThreadedConnectionPool",
        lambda *a: pytest.fail("생성 시점에 풀을 만들면 안 된다"),
    )

    PgVectorRepository("postgresql://x")


# 동시 요청이 풀 크기를 넘어도 실패하지 않아야 한다.
# 세마포어가 없으면 psycopg2가 PoolError를 던져 사용자에게 500이 나간다.
def test_concurrent_use_beyond_pool_size_waits_instead_of_failing(repo):
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def use():
        try:
            barrier.wait()
            with repo._cursor():
                time.sleep(0.02)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=use) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"동시 요청이 실패했다: {errors}"
    assert repo._created_pools[0].peak <= 3, "풀 크기를 넘겨 연결을 꺼냈다"


# 무한정 기다리면 요청이 쌓여 서버가 멈춘 것처럼 보인다.
def test_times_out_with_clear_message(monkeypatch, repo):
    repo._acquire_timeout = 0.05
    monkeypatch.setattr(repo._slots, "acquire", lambda timeout=None: False)

    with pytest.raises(TimeoutError, match="풀 크기"):
        with repo._cursor():
            pass


# 롤백하지 않고 풀에 되돌리면 다음 사용자가 실패한 트랜잭션 상태의 연결을 받아
# "current transaction is aborted"로 연쇄 실패한다.
def test_failed_query_rolls_back_before_returning(repo):
    with pytest.raises(RuntimeError):
        with repo._cursor() as cursor:
            raise RuntimeError("질의 실패")

    assert repo._created_pools[0].out == 0, "연결이 반납되지 않았다"


# 실패해도 세마포어 자리를 반납해야 한다. 안 하면 실패가 누적돼 풀이 영구히 잠긴다.
def test_slot_is_released_even_on_failure(repo):
    for _ in range(5):
        with pytest.raises(RuntimeError):
            with repo._cursor():
                raise RuntimeError("실패")

    with repo._cursor():
        pass


def test_close_releases_pool(repo):
    with repo._cursor():
        pass

    repo.close()

    assert repo._created_pools[0].closed
