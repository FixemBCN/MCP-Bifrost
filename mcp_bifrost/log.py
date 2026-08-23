"""Operational log writer for MCP-Bifrost patches."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager


class PatchLog:
    """SQLite-backed logger for patch operations."""

    def __init__(self, db_path: Path, session: str | None = None):
        """Initialize log with database path and session ID.

        Creates parent directories and schema if necessary.
        Sets WAL journal mode and NORMAL synchronous mode for performance.

        Args:
            db_path: Path to SQLite database file.
            session: Session identifier. Generated as uuid4 hex if None.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS patches (
              id           TEXT PRIMARY KEY,
              ts           TEXT NOT NULL,
              session      TEXT,
              grup         TEXT,
              op           TEXT NOT NULL,
              fitxer       TEXT NOT NULL,
              simbol       TEXT,
              start_byte   INTEGER,
              end_byte     INTEGER,
              estat        TEXT NOT NULL,
              porta        TEXT,
              blob_abans   TEXT,
              head_sha     TEXT,
              instruccio   TEXT,
              rationale    TEXT,
              src_b        INTEGER,
              out_b        INTEGER,
              in_b         INTEGER,
              resp_b       INTEGER,
              tin          INTEGER,
              tout         INTEGER,
              cache_hit    INTEGER,
              override     TEXT,
              ms           INTEGER
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_fitxer  ON patches(fitxer)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ts      ON patches(ts)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON patches(session)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_estat   ON patches(estat)")
        self.conn.commit()

        self.session = session if session is not None else uuid.uuid4().hex

    def record(self, **fields) -> str:
        """Insert a patch record.

        Generates id (uuid4 hex) and ts (ISO-8601 UTC) if not supplied.
        Fills session from instance.

        Args:
            **fields: Column values. Unknown columns raise ValueError.

        Returns:
            Inserted row id.

        Raises:
            ValueError: If unknown column names are supplied.
        """
        valid_columns = {
            'id', 'ts', 'session', 'grup', 'op', 'fitxer', 'simbol',
            'start_byte', 'end_byte', 'estat', 'porta', 'blob_abans',
            'head_sha', 'instruccio', 'rationale', 'src_b', 'out_b',
            'in_b', 'resp_b', 'tin', 'tout', 'cache_hit', 'override', 'ms'
        }
        unknown = set(fields.keys()) - valid_columns
        if unknown:
            raise ValueError(f"Unknown column(s): {', '.join(sorted(unknown))}")

        # Set defaults
        patch_id = fields.get('id') or uuid.uuid4().hex
        ts = fields.get('ts') or datetime.now(timezone.utc).isoformat()
        fields['id'] = patch_id
        fields['ts'] = ts
        if 'session' not in fields:
            fields['session'] = self.session

        # Build parameterized insert
        cols = list(fields.keys())
        placeholders = ', '.join(['?' for _ in cols])
        col_names = ', '.join(cols)

        query = f"INSERT INTO patches ({col_names}) VALUES ({placeholders})"
        values = [fields[col] for col in cols]

        self.conn.execute(query, values)
        self.conn.commit()

        return patch_id

    def get(self, patch_id: str) -> dict | None:
        """Retrieve a patch by id.

        Args:
            patch_id: The patch id.

        Returns:
            Dict of patch fields, or None if not found.
        """
        row = self.conn.execute(
            "SELECT * FROM patches WHERE id = ?",
            (patch_id,)
        ).fetchone()
        return dict(row) if row else None

    def by_file(self, path: str) -> list[dict]:
        """Retrieve all patches for a file, ordered by timestamp.

        Args:
            path: File path.

        Returns:
            List of patch dicts.
        """
        rows = self.conn.execute(
            "SELECT * FROM patches WHERE fitxer = ? ORDER BY ts",
            (path,)
        ).fetchall()
        return [dict(row) for row in rows]

    def session_patches(self, session: str | None = None, estat: str = "ok") -> list[dict]:
        """Retrieve patches for a session, ordered by timestamp descending.

        Descending order is used for rollback (most recent first).

        Args:
            session: Session id. Defaults to instance session.
            estat: Filter by status. Defaults to "ok".

        Returns:
            List of patch dicts ordered by ts DESC.
        """
        s = session if session is not None else self.session
        rows = self.conn.execute(
            "SELECT * FROM patches WHERE session = ? AND estat = ? ORDER BY ts DESC",
            (s, estat)
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        """Close the database connection."""
        self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
