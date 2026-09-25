import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine


def run(conn: Connection) -> None:
    context.configure(connection=conn, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as conn:
        await conn.run_sync(run)
        await conn.commit()
    await engine.dispose()


asyncio.run(main())
