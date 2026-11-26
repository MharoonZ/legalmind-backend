"""Run trusted account operations from the backend terminal, never from public registration."""

import argparse
import asyncio
import getpass
import secrets
from datetime import timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import Database
from app.platform.models import Account, ResetToken, now
from app.platform.security import audit, digest, hash_password


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create-admin", "reset-link"])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Platform administrator")
    args = parser.parse_args()
    database = Database(get_settings())
    output = []
    try:
        async with database.session() as db:
            async with db.begin():
                user = await db.scalar(select(Account).where(Account.email == args.email.strip().casefold()))
                if args.action == "create-admin":
                    if user:
                        raise SystemExit("An account already uses this email.")
                    password = getpass.getpass("New administrator password (at least 12 characters): ")
                    if len(password) < 12 or password != getpass.getpass("Confirm password: "):
                        raise SystemExit("Password confirmation failed.")
                    user = Account(
                        email=args.email.strip().casefold(),
                        name=args.name,
                        role="admin",
                        password_hash=hash_password(password),
                    )
                    db.add(user)
                    await db.flush()
                    audit(db, user, "administrator_created", user.id)
                    output.append("Administrator created.")
                else:
                    if not user:
                        raise SystemExit("Account not found.")
                    token = secrets.token_urlsafe(48)
                    db.add(
                        ResetToken(user_id=user.id, token_hash=digest(token), expires_at=now() + timedelta(minutes=30))
                    )
                    audit(db, None, "reset_link_issued", user.id)
                    output.append("Deliver this one-use path privately after verifying the account owner's identity.")
                    output.append("/reset-password#token=" + token)
        for line in output:
            print(line)
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
