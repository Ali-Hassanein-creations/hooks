import os
from collections.abc import AsyncIterator

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql+asyncpg://...

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

# Comma-separated keys: the first encrypts, any of them decrypts. To rotate, put the new
# key first and keep the old one until existing secrets have been re-encrypted.
_fernet = MultiFernet([Fernet(k) for k in os.environ["SECRET_ENCRYPTION_KEY"].split(",")])


def encrypt(plain: str) -> bytes:
    return _fernet.encrypt(plain.encode())


def decrypt(token: bytes) -> str:
    return _fernet.decrypt(token).decode()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with Session() as session:
        yield session
