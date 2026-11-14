"""GET /reports: aggregates the reports table. Paginated since last week (page size
100); the aggregate query still scans the whole table per page."""


def list_reports(conn, page=0, size=100):
    rows = conn.execute("select * from reports order by created_at desc limit ? offset ?", (size, page * size))
    return list(rows)
