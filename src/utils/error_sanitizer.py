"""
src/utils/error_sanitizer.py
----------------------------
Sanitizes technical error messages and exception details before they are
returned to the client or rendered in the UI, ensuring that SQL queries,
database engine internals, and system stack traces are never exposed.
"""
import re
from typing import Any

SQL_LEAK_PATTERNS = [
    re.compile(r"\[SQL:.*?\]", re.IGNORECASE | re.DOTALL),
    re.compile(r"sqlite3\.\w+", re.IGNORECASE),
    re.compile(r"sqlalchemy\.\w+", re.IGNORECASE),
    re.compile(r"\(sqlite3\.\w+\)", re.IGNORECASE),
    re.compile(r"\[parameters:.*?\]", re.IGNORECASE | re.DOTALL),
]


def sanitize_error_detail(detail: Any) -> str:
    """
    Sanitizes an error detail string if it contains database leaks or internal traces,
    returning a clean, actionable Italian message.
    """
    if detail is None:
        return "Si è verificato un errore imprevisto durante l'elaborazione."

    detail_str = str(detail)
    lower = detail_str.lower()

    # Check for unique constraint violation
    if "unique constraint failed" in lower or "duplicate key" in lower:
        return "Impossibile completare l'operazione: esiste già un elemento con questo codice o identificativo."

    # Check for foreign key violation
    if "foreign key constraint failed" in lower or "violates foreign key" in lower:
        return "Impossibile completare l'operazione: l'elemento è collegato ad altri record a magazzino o nelle vendite."

    # Check for generic SQL / DB driver leaks
    has_sql_signature = (
        any(pattern.search(detail_str) for pattern in SQL_LEAK_PATTERNS)
        or "no such column" in lower
        or "no such table" in lower
        or "operationalerror" in lower
        or "integrityerror" in lower
        or "database is locked" in lower
    )

    if has_sql_signature:
        return "Si è verificato un errore durante l'accesso al database. Riprova più tardi o contatta il supporto."

    return detail_str

