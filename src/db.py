"""Database access layer for retail-sql-analytics.

Two ways to reach PostgreSQL, in priority order:

1. ``DATABASE_URL`` environment variable -- any PostgreSQL you already run
   (this is the path a ``docker-compose up -d`` user takes), e.g.::

       set DATABASE_URL=postgresql://postgres:postgres@localhost:5432/retail

2. An embedded, throwaway PostgreSQL 16 server started by ``pgserver`` with its
   data directory inside the repository (``.pgdata/``, git-ignored).  This needs
   no Docker, no service, no password and no prior installation -- it is the
   primary documented path (``python run_all.py``).

The module deliberately exposes both a raw ``psycopg2`` connection (used by the
loader, which wants ``COPY``) and a pandas helper (used by the query runner).
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PGDATA_DIR = PROJECT_ROOT / ".pgdata"
SCHEMA_SQL = PROJECT_ROOT / "src" / "schema.sql"

DEFAULT_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


class Database:
    """Owns the lifecycle of the PostgreSQL instance used by this project.

    Usage::

        with Database() as db:
            conn = db.connect()
            ...

    If ``DATABASE_URL`` is set the instance is *external* and nothing is started
    or stopped.  Otherwise a local pgserver instance is started on ``.pgdata``
    and shut down again on exit.
    """

    def __init__(self, data_dir: Path | None = None, database_url: str | None = None):
        self.data_dir = Path(data_dir) if data_dir else PGDATA_DIR
        self.database_url = (database_url if database_url is not None
                             else DEFAULT_DATABASE_URL).strip()
        self._server = None          # pgserver handle, when embedded
        self.uri: str | None = None
        self.backend: str = "uninitialised"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> str:
        """Start (or adopt) a server and return its connection URI."""
        if self.uri:
            return self.uri

        if self.database_url:
            self.uri = self.database_url
            self.backend = "external DATABASE_URL"
            return self.uri

        try:
            import pgserver
        except ImportError as exc:  # pragma: no cover - environment problem
            raise RuntimeError(
                "pgserver is not installed and DATABASE_URL is not set.\n"
                "Install it with:  pip install -r requirements.txt\n"
                "Or point the project at an existing server:\n"
                "  set DATABASE_URL=postgresql://user:pass@host:5432/dbname"
            ) from exc

        self.data_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._server = pgserver.get_server(str(self.data_dir))
                self.uri = self._server.get_uri()
                self.backend = f"embedded pgserver ({self.data_dir})"
                return self.uri
            except Exception as exc:  # noqa: BLE001 - retry a transient lock
                last_error = exc
                stale = self.data_dir / "postmaster.pid"
                if stale.exists() and attempt < 3:
                    print(f"  ! pgserver start attempt {attempt} failed "
                          f"({exc.__class__.__name__}); removing stale "
                          f"postmaster.pid and retrying", file=sys.stderr)
                    try:
                        stale.unlink()
                    except OSError:
                        pass
                    time.sleep(1.5)
                else:
                    break
        raise RuntimeError(
            f"Could not start the embedded PostgreSQL server in {self.data_dir}: "
            f"{last_error}"
        )

    def stop(self) -> None:
        """Shut the embedded server down.  External URLs are left untouched."""
        if self._server is not None:
            try:
                self._server.cleanup()
            except Exception as exc:  # noqa: BLE001 - shutdown must never mask results
                print(f"  ! pgserver cleanup warning: {exc}", file=sys.stderr)
            finally:
                self._server = None
        self.uri = None

    def __enter__(self) -> "Database":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def connect(self):
        """Return a new psycopg2 connection to the managed server."""
        if not self.uri:
            self.start()
        conn = psycopg2.connect(self.uri)
        conn.autocommit = True
        return conn

    @contextmanager
    def cursor(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                yield cur
        finally:
            conn.close()

    def server_version(self) -> str:
        with self.cursor() as cur:
            cur.execute("SHOW server_version")
            return cur.fetchone()[0]

    def describe_target(self) -> str:
        """Human-readable, credential-free description of where we are."""
        with self.cursor() as cur:
            cur.execute("SELECT current_database(), inet_server_port()")
            dbname, port = cur.fetchone()
        return f"{dbname} @ 127.0.0.1:{port}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def execute_script(self, sql: str) -> None:
        """Run a multi-statement SQL script (schema.sql) as one transaction."""
        conn = self.connect()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_schema(self, schema_path: Path | None = None) -> None:
        path = Path(schema_path) if schema_path else SCHEMA_SQL
        self.execute_script(path.read_text(encoding="utf-8"))

    def fetch_dataframe(self, sql: str):
        """Execute one SELECT and return the full result as a DataFrame.

        PostgreSQL ``NUMERIC`` arrives as :class:`decimal.Decimal`, which pandas
        will not mix with floats in arithmetic.  Those columns are coerced to
        ``float64`` so that downstream analysis and CSV output behave normally.
        """
        import decimal

        import pandas as pd

        conn = self.connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description] if cur.description else []
        finally:
            conn.close()

        frame = pd.DataFrame(rows, columns=columns)
        for column in frame.columns:
            series = frame[column]
            if series.dtype == object and series.map(
                    lambda v: isinstance(v, decimal.Decimal)).any():
                frame[column] = series.astype("float64")
        return frame


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` comment lines so the SQL can be sent in one round trip.

    psycopg2 handles comments fine; this exists so that a query file is still a
    single clean statement after its explanatory header is removed.
    """
    return "\n".join(line for line in sql.splitlines()
                     if not line.strip().startswith("--"))


def _is_rule(line: str) -> bool:
    """True for the decorative ``====`` / ``----`` separator lines."""
    stripped = line.strip()
    return len(stripped) >= 3 and set(stripped) <= set("=- ")


def _is_section_heading(line: str) -> bool:
    """True for the shouty section labels such as ``WHY IT MATTERS``."""
    stripped = line.strip()
    return len(stripped) > 3 and stripped.isupper() and not stripped.endswith(".")


def extract_question(header_lines: list[str]) -> str:
    """Pull just the prose under ``BUSINESS QUESTION`` out of a comment header.

    The SQL files carry a long explanatory header; only the business question
    belongs in the report, so everything after it is discarded at the next
    section heading or blank line.
    """
    start = None
    for index, line in enumerate(header_lines):
        if line.strip().upper().startswith("BUSINESS QUESTION"):
            start = index + 1
            break

    if start is None:
        # No marker: fall back to the whole header minus decoration.
        return " ".join(ln.strip() for ln in header_lines
                        if ln.strip() and not _is_rule(ln))

    parts: list[str] = []
    for line in header_lines[start:]:
        stripped = line.strip()
        if not stripped:
            if parts:
                break          # a blank line closes the question paragraph
            continue
        if _is_rule(stripped) or _is_section_heading(stripped):
            break
        parts.append(stripped)
    return " ".join(parts)


def read_query(path: Path) -> tuple[str, str]:
    """Return ``(business_question, sql)`` for a query file.

    The first contiguous block of leading ``--`` comment lines is treated as the
    file header; the business question is extracted from it and the remainder is
    the executable SQL.
    """
    text = Path(path).read_text(encoding="utf-8")
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = True
    for line in text.splitlines():
        stripped = line.strip()
        if in_header and stripped.startswith("--"):
            header_lines.append(stripped.lstrip("-").strip())
        elif in_header and not stripped:
            if header_lines:
                header_lines.append("")
        else:
            in_header = False
            body_lines.append(line)

    sql = "\n".join(body_lines).strip()
    return extract_question(header_lines), sql


def query_files(sql_dir: Path | None = None) -> list[Path]:
    directory = Path(sql_dir) if sql_dir else PROJECT_ROOT / "sql"
    return sorted(p for p in directory.glob("*.sql") if p.name[:2].isdigit())
