import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer

Headers = dict[str, str]


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    # Real Postgres, not SQLite: advisory locks, arrays and SKIP LOCKED don't exist there.
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        os.environ["DATABASE_URL"] = pg.get_connection_url()
        os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        subprocess.run(["alembic", "upgrade", "head"], check=True)  # noqa: S607
        yield


@pytest.fixture(autouse=True)
async def clean(database: None) -> None:
    from sqlalchemy import text

    from webhooks.db import engine

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE tenants, api_keys, endpoints, events, deliveries"))


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
