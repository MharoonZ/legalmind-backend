"""Diagnose database connection problems step by step, with timings.

    python scripts/check_db.py                # 30 s limit per step
    python scripts/check_db.py --timeout 60
    python scripts/check_db.py --no-ssl       # try without TLS

Reads DATABASE_URL from .env, then tests DNS -> TCP -> TLS + login -> query.
The first step that fails or is slow is your problem. The app itself hides the
real error behind "TimeoutError", which this script does not.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import get_settings  # noqa: E402


def step(label: str, started: float) -> None:
    print(f"  ok  {label:<32} {time.perf_counter() - started:6.2f}s")


async def main(timeout: float, use_ssl: bool) -> int:
    settings = get_settings()
    url = make_url(settings.database_url)
    host, port = url.host, url.port or 5432
    print(f"Host: {host}:{port}  user: {url.username}  db: {url.database}")
    print(f"SSL: {'require' if use_ssl else 'off'}  step limit: {timeout:.0f}s\n")

    if not host:
        print("DATABASE_URL has no host. Check .env.")
        return 1

    t = time.perf_counter()
    try:
        ip = socket.gethostbyname(host)
    except OSError as exc:
        print(f"FAIL DNS lookup: {exc}")
        return 1
    step(f"DNS -> {ip}", t)

    t = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
    except OSError as exc:
        print(f"FAIL TCP connect: {exc}\n-> port blocked by ISP/firewall, or project paused.")
        return 1
    step("TCP connect", t)

    t = time.perf_counter()
    try:
        conn = await asyncpg.connect(
            user=url.username,
            password=url.password,
            host=host,
            port=port,
            database=url.database,
            ssl="require" if use_ssl else None,
            timeout=timeout,
            statement_cache_size=0,  # required by the Supabase pooler
        )
    except asyncio.TimeoutError:
        print(f"FAIL TLS + login: no answer within {timeout:.0f}s")
        print("-> try --timeout 60, or a phone hotspot to rule out your ISP.")
        return 1
    except Exception as exc:  # noqa: BLE001 - we want the real message
        print(f"FAIL TLS + login: {exc.__class__.__name__}: {exc}")
        message = str(exc).lower()
        if "password authentication" in message:
            print("-> wrong password, or user must be postgres.<PROJECT_REF> on the pooler.")
        elif "tenant or user not found" in message:
            print("-> project ref in the username is wrong, or wrong pooler region host.")
        elif "ssl" in message:
            print("-> try again with --no-ssl, or set DB_SSL=false in .env.")
        return 1
    step("TLS + login", t)

    try:
        t = time.perf_counter()
        await conn.fetchval("SELECT 1")
        step("SELECT 1 (one round trip)", t)

        t = time.perf_counter()
        has_vector = await conn.fetchval(
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        )
        step("pg_extension query", t)
        print(f"\nvector extension installed: {'yes' if has_vector else 'no (alembic will create it)'}")

        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename IN ('legal_files','file_pages','corpus_revisions','provisions','alembic_version')"
        )
        found = sorted(row["tablename"] for row in tables)
        print(f"legalmind tables present: {found or 'none (clean database)'}")
    finally:
        await conn.close()

    print("\nDatabase is reachable. Set DB_CONNECT_TIMEOUT above the slowest step (e.g. 20).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--no-ssl", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.timeout, not args.no_ssl)))
