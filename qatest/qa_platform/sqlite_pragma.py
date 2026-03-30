from __future__ import annotations

from django.db.backends.signals import connection_created
from django.dispatch import receiver


def setup_sqlite_pragmas() -> None:
    return


@receiver(connection_created)
def _on_connection_created(sender, connection, **kwargs):
    try:
        if getattr(connection, "vendor", "") != "sqlite":
            return
    except Exception:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        return

