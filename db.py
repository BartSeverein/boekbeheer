"""
db.py — Connectie met de Supabase/Postgres-database.

Vereist SUPABASE_DB_URL in .env, bijvoorbeeld:
    postgresql://postgres:JOUW-WACHTWOORD@db.xxxxxxxxxxxx.supabase.co:5432/postgres

Je vindt deze connection string in je Supabase-project onder:
Project Settings -> Database -> Connection string -> URI
"""

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ["SUPABASE_DB_URL"]

SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


def get_connection():
    conn = psycopg2.connect(DB_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def init_db():
    """Voert schema_postgres.sql uit tegen de database (maakt tabellen als ze nog niet bestaan)."""
    schema_sql = SCHEMA_PATH.read_text()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Database geïnitialiseerd (tabellen aangemaakt indien nodig).")
