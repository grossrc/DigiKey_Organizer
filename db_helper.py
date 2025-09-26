# db_helper.py
import os
from contextlib import closing
from typing import Iterable, Any
import psycopg2
from dotenv import load_dotenv

load_dotenv()  # loads .env from project root

def get_conn():
    # Option A: individual vars (safer if passwords have special chars)
    host = os.getenv("PGHOST", "localhost")
    port = int(os.getenv("PGPORT", "5432"))
    db   = os.getenv("PGDATABASE", "parts_DB")
    user = os.getenv("PGUSER", "murph")
    pwd  = os.getenv("PGPASSWORD", "password")

    conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pwd)
    conn.autocommit = False
    return conn

def run_query(cur, sql: str, params: Iterable[Any] | None = None):
    cur.execute(sql, params or ())
    if cur.description is not None:
        return cur.fetchall()
    return None
