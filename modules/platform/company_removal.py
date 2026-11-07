"""Permanently remove a company and everything that belongs to it.

Used by the platform admin to clear test companies. Many tables link to a company, most
without ON DELETE CASCADE, and some only through other tables (e.g. roster entries through
the employee). So the links are read from the database
itself and followed downwards: every row that points at a row being removed is removed
first. Self links and loops are cut by clearing the link (when it may be empty) instead.
Runs inside the caller's transaction: if anything fails, nothing is removed.
"""
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session


def _foreign_keys(db: Session) -> dict[str, list[tuple[str, str, str, bool]]]:
    """parent table -> [(child table, child column, parent column, child column nullable)]"""
    rows = db.execute(text("""
        SELECT kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name, c.is_nullable = 'YES'
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        JOIN information_schema.columns c
          ON c.table_schema = kcu.table_schema AND c.table_name = kcu.table_name AND c.column_name = kcu.column_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
    """)).fetchall()
    out: dict[str, list] = defaultdict(list)
    for child, col, parent, pcol, nullable in rows:
        out[parent].append((child, col, pcol, bool(nullable)))
    return out


def remove_company(db: Session, company_id: int) -> dict[str, int]:
    """Delete the company and all its data. Returns rows removed per table. Does not commit."""
    # Audit entries are append-only; this one platform maintenance task may purge the
    # removed company's entries (the removal itself is recorded as a platform event).
    db.execute(text("SELECT set_config('app.audit_purge', 'on', true)"))
    fks = _foreign_keys(db)
    removed: dict[str, int] = defaultdict(int)

    def purge(table: str, where: str, path: tuple[str, ...]) -> None:
        for child, col, pcol, nullable in fks.get(table, []):
            child_where = f'"{col}" IN (SELECT "{pcol}" FROM "{table}" WHERE {where})'
            if child == table or child in path:
                # self link or loop: unhook instead of following it forever
                if nullable:
                    db.execute(text(f'UPDATE "{child}" SET "{col}" = NULL WHERE {child_where}'))
                continue
            purge(child, child_where, path + (table,))
        res = db.execute(text(f'DELETE FROM "{table}" WHERE {where}'))
        if res.rowcount:
            removed[table] += res.rowcount

    purge("companies", f"id = {int(company_id)}", ())
    return dict(removed)
