"""Bootstrap tenants and API keys: python -m webhooks.cli create-tenant NAME"""

import argparse
import asyncio

from webhooks.auth import new_api_key
from webhooks.db import Session
from webhooks.models import Tenant


async def create_tenant(name: str) -> None:
    async with Session() as session:
        tenant = Tenant(name=name)
        session.add(tenant)
        await session.flush()
        row, key = new_api_key(tenant.id)
        session.add(row)
        await session.commit()
    print(f"tenant_id={tenant.id}\napi_key={key}  (shown once, store it now)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="webhooks.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-tenant").add_argument("name")
    args = parser.parse_args()
    asyncio.run(create_tenant(args.name))


if __name__ == "__main__":
    main()
