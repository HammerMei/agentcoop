"""Forward-only SQL migrations under db/sql/. There is no schema sync: a table that
is dropped by hand stays dropped until a new migration recreates it."""


def pending(conn):
    applied = {row[0] for row in conn.execute("select name from schema_migrations")}
    return [m for m in sorted(_migrations()) if m not in applied]


def apply(conn, name):
    conn.execute(open(f"db/sql/{name}.sql").read())
    conn.execute("insert into schema_migrations (name) values (?)", (name,))


def _migrations():
    import os
    return [f[:-4] for f in os.listdir("db/sql") if f.endswith(".sql")]
