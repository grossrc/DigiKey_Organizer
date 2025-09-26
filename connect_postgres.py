from __future__ import annotations

import os
from contextlib import closing
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv


def get_conn():
    # Load env vars from .env if present
    load_dotenv()
    host = os.getenv("PGHOST", "localhost")
    port = int(os.getenv("PGPORT", "5432"))
    db   = os.getenv("PGDATABASE", "DigiKey_Parts_DB")
    user = os.getenv("PGUSER", "postgres")
    pwd  = os.getenv("PGPASSWORD", "admin")

    conn = psycopg2.connect(
        host=host, port=port, dbname=db, user=user, password=pwd
    )
    conn.autocommit = False  # we'll commit explicitly
    return conn


def run_query(cur, sql: str, params: Iterable[Any] | None = None):
    cur.execute(sql, params or ())
    # Only return rows if it's a SELECT
    if cur.description is not None:
        return cur.fetchall()
    return None


def main():
    with closing(get_conn()) as conn:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Basic sanity checks
            ver = run_query(cur, "SHOW server_version;")[0]["server_version"]
            who = run_query(cur, "SELECT current_user AS user, current_database() AS db;")[0]
            print(f"Connected to PostgreSQL {ver} as {who['user']} on database {who['db']}")

            # Create a demo table
            # run_query(cur, )

            # Insert a couple of rows safely with parameters
            #run_query(cur, "INSERT INTO demo_items (name, qty) VALUES (%s, %s)", ("apples", 3))

            # Commit our work
            conn.commit()

if __name__ == "__main__":
    main()
