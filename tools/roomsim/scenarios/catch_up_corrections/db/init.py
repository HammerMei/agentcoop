"""Database start-up for the API service."""
import os

from db.engine import connect
from db.migrations import apply, pending


def init_db():
    """Open the connection pool. Migrations run only when MIGRATE=1 is set in the
    environment (the deploy pipeline sets it for the migration job, never for the
    API processes). Nothing here creates or recreates tables or schemas."""
    conn = connect(os.environ["DATABASE_URL"])
    if os.environ.get("MIGRATE") == "1":
        for migration in pending(conn):
            apply(conn, migration)
    return conn
