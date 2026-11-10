"""Служебные команды: ожидание базы при запуске контейнера."""

from sqlalchemy.exc import OperationalError

from app import cli


def _failing(times: int):
    calls = {"n": 0}

    def ping() -> None:
        calls["n"] += 1
        if calls["n"] <= times:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    return ping, calls


def test_wait_db_succeeds_after_retries():
    ping, calls = _failing(2)
    sleeps: list[float] = []
    assert cli.wait_db(timeout=60, interval=1, ping=ping, sleep=sleeps.append) == 0
    assert calls["n"] == 3 and sleeps == [1, 1]


def test_wait_db_gives_up_after_timeout():
    ping, _ = _failing(10**6)
    assert cli.wait_db(timeout=0, interval=0, ping=ping, sleep=lambda s: None) == 1


def test_wait_db_real_database_is_reachable():
    assert cli.wait_db(timeout=5) == 0
