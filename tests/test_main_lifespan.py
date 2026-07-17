import asyncio

from src import main


class RecordingSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def exec(self, statement):
        return self


class RecordingRedis:
    def delete(self, key):
        return None

    def sadd(self, key, *values):
        return None


class DummyBlacklistService:
    def __init__(self, db_session, redis_client):
        self.db_session = db_session
        self.redis_client = redis_client

    def sync_to_redis(self):
        assert hasattr(self.db_session, "exec")
        assert self.redis_client is not None
        return set()


def test_lifespan_uses_real_session_and_redis(monkeypatch):
    monkeypatch.setattr(main, "db_init", lambda: None)
    monkeypatch.setattr(main, "BlacklistService", DummyBlacklistService, raising=False)
    monkeypatch.setattr(main, "Session", lambda *args, **kwargs: RecordingSession(), raising=False)
    monkeypatch.setattr(main, "get_redis_client", lambda: RecordingRedis(), raising=False)

    async def run_lifespan():
        async with main.lifespan(main.app):
            pass

    asyncio.run(run_lifespan())
