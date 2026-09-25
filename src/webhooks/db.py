import os
from collections.abc import AsyncIterator

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql+asyncpg://...

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

# ponytail: single key; switch to MultiFernet when the encryption key itself needs rotating
_fernet = Fernet(os.environ["SECRET_ENCRYPTION_KEY"])


def encrypt(plain: str) -> bytes:
    return _fernet.encrypt(plain.encode())


def decrypt(token: bytes) -> str:
    return _fernet.decrypt(token).decode()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with Session() as session:
        yield session
