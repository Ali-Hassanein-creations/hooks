import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer

Headers = dict[str, str]

# Real Postgres, not SQLite: advisory locks, arrays and SKIP LOCKED don't exist there.
# Started in pytest_sessionstart, i.e. before test modules are imported, because
# webhooks.db reads DATABASE_URL at import time.
_postgres = PostgresContainer("postgres:16-alpine", driver="asyncpg")


def pytest_sessionstart(session: pytest.Session) -> None:
    _postgres.start()
    os.environ["DATABASE_URL"] = _postgres.get_connection_url()
    os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


def pytest_sessionfinish(session: pytest.Session) -> None:
    _postgres.stop()


@pytest.fixture(autouse=True)
async def clean() -> None:
    from sqlalchemy import text

    from webhooks.db import engine

    async with engine.begin() as conn:
        # Every table references tenants, so CASCADE empties them all.
        await conn.execute(text("TRUNCATE tenants CASCADE"))


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from webhooks.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def make_tenant() -> Callable[[], Awaitable[Headers]]:
    async def make() -> Headers:
        from webhooks.auth import new_api_key
        from webhooks.db import Session
        from webhooks.models import Tenant

        async with Session() as session:
            tenant = Tenant(name="t")
            session.add(tenant)
            await session.flush()
            row, key = new_api_key(tenant.id)
            session.add(row)
            await session.commit()
        return {"Authorization": f"Bearer {key}"}

    return make
