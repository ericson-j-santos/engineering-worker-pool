"""PostgreSQL persistence for the existing Worker Pool state machine.

All lifecycle methods are inherited from WorkerPoolStore / WorkOrchestrator.
One transaction-scoped lock per database/schema preserves BEGIN IMMEDIATE
semantics, including lane capacity, fair ordering, fencing and checkpoints.
Runtime construction verifies an existing schema; only the explicit initialize
command performs DDL. No SQLite file, snapshot, queue mirror or automatic retry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.store import ConflictError, SHA40, WorkerPoolStore
from app.work import WorkOrchestrator

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None

SCHEMA_VERSION = 1
_SCHEMA = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_MIGRATION = Path(__file__).with_name("migrations") / "postgres_authority_v1.sql"
_REQUIRED_TABLES = {
    "authority_meta", "workers", "repository_lanes",
    "worker_repository_affinity", "tasks", "quarantine", "pool_control",
    "task_attempts", "task_checkpoints", "effect_reconciliations", "work_runs",
}
_REQUIRED_INDEXES = {
    "idx_worker_active", "idx_workspace_active",
    "idx_task_checkpoints_generation", "idx_task_checkpoints_effect",
}
_REQUIRED_TRIGGERS = {"guard_fenced_task_admission", "guard_fenced_claim"}


class AuthorityUnavailable(RuntimeError):
    """Bounded public error; never include a DSN or driver exception."""


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise AuthorityUnavailable(code)


def read_private_connection_file(path: Path) -> str:
    """Read a stable owner-only POSIX file, without weakening the Windows gate."""
    _need(os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
          "authority_credentials_acl_unverified")
    _need(path.is_absolute(), "authority_credentials_file_invalid")
    descriptor = None
    try:
        before = path.lstat()
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
              and before.st_uid == os.getuid() and before.st_mode & 0o077 == 0,
              "authority_credentials_file_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            opened = os.fstat(handle.fileno())
            content = handle.read(8193)
            after = os.fstat(handle.fileno())
        final = path.lstat()
    except OSError:
        raise AuthorityUnavailable("authority_credentials_file_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                value.st_ctime_ns, value.st_uid, value.st_mode, value.st_nlink)
    _need(identity(before) == identity(opened) == identity(after) == identity(final)
          and 0 < len(content) <= 8192, "authority_credentials_file_changed")
    try:
        value = content.decode("utf-8").strip()
    except UnicodeError:
        raise AuthorityUnavailable("authority_credentials_file_invalid") from None
    _need(bool(value) and "\x00" not in value and "\n" not in value and "\r" not in value,
          "authority_credentials_file_invalid")
    return value


def compile_pool_sql(statement: str, parameter_count: int) -> str:
    """Translate this repository's qmark SQL; values stay driver parameters.

    Quoted strings/identifiers, comments and dollar strings are preserved.
    This is a narrow DB-API adapter, not a query or state-machine rewriter.
    """
    _need(isinstance(statement, str) and bool(statement.strip()),
          "authority_statement_invalid")
    ignored = re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", statement, re.I)
    if ignored:
        statement = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", statement,
                           count=1, flags=re.I).rstrip().removesuffix(";")
        statement += " ON CONFLICT DO NOTHING"
    output: list[str] = []
    position = count = 0
    while position < len(statement):
        char = statement[position]
        end = None
        if char in ("'", '"'):
            end = position + 1
            while end < len(statement):
                if statement[end] == char:
                    if end + 1 < len(statement) and statement[end + 1] == char:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            else:
                raise AuthorityUnavailable("authority_statement_invalid")
        elif statement.startswith("--", position):
            found = statement.find("\n", position)
            end = len(statement) if found < 0 else found + 1
        elif statement.startswith("/*", position):
            found = statement.find("*/", position + 2)
            _need(found >= 0, "authority_statement_invalid")
            end = found + 2
        elif char == "$":
            tag = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", statement[position:])
            if tag:
                found = statement.find(tag[0], position + len(tag[0]))
                _need(found >= 0, "authority_statement_invalid")
                end = found + len(tag[0])
        if end is not None:
            fragment = statement[position:end]
            output.append(fragment.replace("%", "%%") if parameter_count else fragment)
            position = end
            continue
        if (statement.startswith("t.rowid", position)
                and (position == 0 or not re.match(r"[A-Za-z0-9_]", statement[position - 1]))
                and (position + 7 == len(statement)
                     or not re.match(r"[A-Za-z0-9_]", statement[position + 7]))):
            output.append("t.__pool_fifo")
            position += 7
            continue
        if char == "?":
            output.append("%s")
            count += 1
        elif char == "%":
            output.append("%%" if parameter_count else "%")
        else:
            _need(char != ";" or not statement[position + 1:].strip(),
                  "authority_multiple_statements_rejected")
            output.append(char)
        position += 1
    _need(count == parameter_count, "authority_parameter_count_invalid")
    return "".join(output)


class _Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @staticmethod
    def _row(row):
        if row is None:
            return None
        # PostgreSQL needs an explicit insertion sequence for SQLite's rowid.
        # It remains an internal ordering column, absent from public v1 tasks.
        return {key: value for key, value in row.items() if key != "__pool_fifo"}

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]


class _Connection:
    def __init__(self, authority: "PostgresAuthority", *, read_only: bool = False):
        self.authority = authority
        self._raw = authority._connect()
        self._read_only = read_only
        self._started = False
        self._write = False
        self._closed = False

    def _begin(self, *, write: bool) -> None:
        _need(not self._started and not self._closed,
              "authority_transaction_state_invalid")
        _need(not (write and self._read_only), "authority_read_only")
        try:
            self._raw.execute(
                "BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE" if write else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self._started = True
            self._raw.execute("SET LOCAL lock_timeout = '8s'")
            self._raw.execute("SET LOCAL statement_timeout = '30s'")
            self._raw.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(self.authority.schema)))
            if write:
                _need(getattr(self.authority._local, "writer", None) is None,
                      "authority_nested_write_rejected")
                self._raw.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)",
                                  (self.authority.lock_key,))
            self.authority._verify_identity(self._raw)
            self._write = write
            if write:
                self.authority._local.writer = self
        except AuthorityUnavailable:
            self.rollback()
            raise
        except psycopg.Error:
            self.rollback()
            raise AuthorityUnavailable("authority_transaction_unavailable") from None

    def execute(self, statement: str, parameters=None) -> _Cursor | None:
        normalized = statement.strip().upper()
        if normalized == "BEGIN IMMEDIATE":
            self._begin(write=True)
            return None
        if normalized == "BEGIN":
            self._begin(write=False)
            return None
        if not self._started:
            self._begin(write=False)
        values = tuple(parameters) if parameters is not None else ()
        compiled = compile_pool_sql(statement, len(values))
        try:
            return _Cursor(self._raw.execute(compiled, values if values else None))
        except psycopg.IntegrityError:
            raise ConflictError("authority_integrity_conflict") from None
        except psycopg.Error:
            raise AuthorityUnavailable("authority_database_operation_failed") from None

    def server_time(self) -> datetime:
        _need(self._started and not self._closed,
              "authority_transaction_state_invalid")
        try:
            # Resolve each lease check on the server, after acquiring the lock.
            return self._raw.execute("SELECT pg_catalog.clock_timestamp() AS now").fetchone()["now"]
        except psycopg.Error:
            raise AuthorityUnavailable("authority_clock_unavailable") from None

    def commit(self) -> None:
        try:
            self._raw.commit()
        except psycopg.Error:
            # The caller must read back; never retry an ambiguous mutation.
            raise AuthorityUnavailable("authority_commit_unverified") from None
        finally:
            self._release_local()
            self._started = False

    def _release_local(self) -> None:
        if getattr(self.authority._local, "writer", None) is self:
            self.authority._local.writer = None

    def rollback(self) -> None:
        try:
            if not self._closed:
                self._raw.rollback()
        except psycopg.Error:
            pass
        finally:
            self._release_local()
            self._started = False

    def close(self) -> None:
        if not self._closed:
            self.rollback()
            self._raw.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class PostgresAuthority:
    def __init__(
        self, connection_parameters: Mapping[str, str], *,
        schema: str = "worker_pool", authority_id: str,
        allow_insecure_loopback: bool = False,
    ):
        _need(psycopg is not None, "postgres_driver_not_installed")
        _need(bool(_SCHEMA.fullmatch(schema))
              and schema not in {"public", "information_schema"}
              and not schema.startswith("pg_"), "authority_schema_invalid")
        try:
            _need(str(uuid.UUID(authority_id)) == authority_id,
                  "authority_identity_invalid")
        except (ValueError, TypeError, AttributeError):
            raise AuthorityUnavailable("authority_identity_invalid") from None
        options = dict(connection_parameters)
        allowed = {"host", "port", "dbname", "user", "password", "sslmode",
                   "sslrootcert", "channel_binding", "connect_timeout"}
        _need(not set(options) - allowed
              and all(isinstance(value, str) and "\x00" not in value
                      for value in options.values()), "authority_connection_invalid")
        host = options.get("host", "")
        _need(bool(_HOST.fullmatch(host)) and ".." not in host
              and bool(options.get("dbname")) and bool(options.get("user")),
              "authority_connection_invalid")
        try:
            _need(1 <= int(options.get("port", "5432")) <= 65535,
                  "authority_connection_invalid")
        except ValueError:
            raise AuthorityUnavailable("authority_connection_invalid") from None
        insecure_fixture = allow_insecure_loopback and host == "127.0.0.1"
        _need(insecure_fixture or options.get("sslmode") == "verify-full",
              "authority_tls_identity_unverified")
        if not insecure_fixture:
            _need(bool(options.get("password")) and bool(options.get("sslrootcert")),
                  "authority_connection_invalid")
        if "channel_binding" in options:
            _need(options["channel_binding"] in {"prefer", "require"},
                  "authority_connection_invalid")
        try:
            connect_timeout = int(options.pop("connect_timeout", "8"))
        except ValueError:
            raise AuthorityUnavailable("authority_connection_invalid") from None
        _need(1 <= connect_timeout <= 30, "authority_connection_invalid")
        self.connect_timeout = connect_timeout
        self._parameters = options
        self.schema = schema
        self.authority_id = authority_id
        self.lock_key = int.from_bytes(hashlib.sha256(
            ("engineering-worker-pool/v1:" + schema).encode()).digest()[:8],
            "big", signed=True)
        self._local = threading.local()
        self._schema_bytes = _MIGRATION.read_bytes()
        self.schema_sha256 = hashlib.sha256(self._schema_bytes).hexdigest()

    @classmethod
    def from_file(cls, path: Path, *, schema: str = "worker_pool",
                  authority_id: str) -> "PostgresAuthority":
        _need(psycopg is not None, "postgres_driver_not_installed")
        value = read_private_connection_file(Path(path))
        try:
            parameters = conninfo_to_dict(value)
        except psycopg.Error:
            raise AuthorityUnavailable("authority_connection_invalid") from None
        return cls(parameters, schema=schema, authority_id=authority_id)

    def _connect(self):
        try:
            return psycopg.connect(
                **self._parameters, autocommit=True, row_factory=dict_row,
                connect_timeout=self.connect_timeout, prepare_threshold=None,
                application_name="engineering-worker-pool")
        except psycopg.Error:
            raise AuthorityUnavailable("authority_connection_unavailable") from None

    def _verify_identity(self, raw) -> None:
        row = raw.execute(
            "SELECT authority_id,schema_version,schema_sha256 "
            "FROM authority_meta WHERE singleton=1").fetchone()
        _need(row is not None and row["authority_id"] == self.authority_id,
              "authority_identity_mismatch")
        _need(row["schema_version"] == SCHEMA_VERSION
              and row["schema_sha256"] == self.schema_sha256,
              "authority_schema_version_mismatch")

    def connection(self) -> _Connection:
        return _Connection(self)

    def read_connection(self) -> _Connection:
        return _Connection(self, read_only=True)

    @contextmanager
    def transaction(self) -> Iterator[_Connection]:
        db = self.connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def clock(self) -> datetime:
        writer = getattr(self._local, "writer", None)
        if writer is not None:
            return writer.server_time()
        with self.read_connection() as db:
            db.execute("BEGIN")
            return db.server_time()

    def verify_schema(self) -> dict[str, Any]:
        with self.read_connection() as db:
            tables = db.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=? AND table_type='BASE TABLE'",
                (self.schema,)).fetchall()
            _need(_REQUIRED_TABLES <= {row["table_name"] for row in tables},
                  "authority_schema_incomplete")
            indexes = db.execute(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=?",
                (self.schema,)).fetchall()
            index_map = {row["indexname"]: row["indexdef"] for row in indexes}
            _need(_REQUIRED_INDEXES <= set(index_map)
                  and all("UNIQUE INDEX" in index_map[name] for name in
                          ("idx_worker_active", "idx_workspace_active")),
                  "authority_indexes_unverified")
            triggers = db.execute(
                "SELECT t.tgname,t.tgenabled,p.proconfig "
                "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_proc p ON p.oid=t.tgfoid "
                "WHERE n.nspname=? AND c.relname='tasks' AND NOT t.tgisinternal",
                (self.schema,)).fetchall()
            enabled = set()
            for row in triggers:
                settings = [item.replace('"', "") for item in (row["proconfig"] or [])]
                if (row["tgenabled"] == "O"
                        and "search_path=" + self.schema + ", pg_catalog" in settings):
                    enabled.add(row["tgname"])
            _need(_REQUIRED_TRIGGERS <= enabled, "authority_fence_guards_unverified")
            fifo = db.execute(
                "SELECT is_identity FROM information_schema.columns "
                "WHERE table_schema=? AND table_name='tasks' AND column_name='__pool_fifo'",
                (self.schema,)).fetchone()
            _need(fifo is not None and fifo["is_identity"] == "YES",
                  "authority_queue_order_unverified")
        return {"backend": "postgres", "authority_id": self.authority_id,
                "schema": self.schema, "schema_version": SCHEMA_VERSION,
                "schema_sha256": self.schema_sha256}

    def initialize(self) -> dict[str, Any]:
        """Explicit operator migration into an empty schema, never a queue copy."""
        raw = self._connect()
        try:
            raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE")
            raw.execute("SET LOCAL lock_timeout = '8s'")
            raw.execute("SET LOCAL statement_timeout = '30s'")
            raw.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (self.lock_key,))
            raw.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(self.schema)))
            raw.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                sql.Identifier(self.schema)))
            existing = raw.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s",
                (self.schema,)).fetchall()
            names = {row["tablename"] for row in existing}
            if names:
                _need("authority_meta" in names, "authority_schema_not_empty")
                self._verify_identity(raw)
            else:
                raw.execute(self._schema_bytes.decode("utf-8"), prepare=False)
                for function in (
                    "guard_fenced_task_admission_fn", "guard_fenced_claim_fn",
                ):
                    raw.execute(sql.SQL(
                        "ALTER FUNCTION {}.{}() SET search_path TO {}, pg_catalog"
                    ).format(sql.Identifier(self.schema), sql.Identifier(function),
                             sql.Identifier(self.schema)))
                raw.execute(
                    "INSERT INTO authority_meta(singleton,authority_id,schema_version,"
                    "schema_sha256) VALUES(1,%s,%s,%s)",
                    (self.authority_id, SCHEMA_VERSION, self.schema_sha256))
            raw.commit()
        except AuthorityUnavailable:
            raw.rollback()
            raise
        except psycopg.Error:
            raw.rollback()
            raise AuthorityUnavailable("authority_migration_failed") from None
        finally:
            raw.close()
        return self.verify_schema()


class PostgresWorkerPoolStore(WorkerPoolStore):
    """The existing lifecycle, with PostgreSQL connection/clock/init hooks only."""
    def __init__(
        self, authority: PostgresAuthority, *,
        heartbeat_ttl_seconds: int = 90, default_lease_seconds: int = 120,
        default_max_attempts: int = 3, progress_stall_seconds: int = 300,
        expected_rules_sha: str | None = None,
    ):
        if min(heartbeat_ttl_seconds, default_lease_seconds, default_max_attempts,
               progress_stall_seconds) < 1:
            raise ValueError("timeouts e tentativas devem ser positivos")
        normalized = (expected_rules_sha or "").strip().lower()
        if normalized and not SHA40.fullmatch(normalized):
            raise ValueError("expected_rules_sha inválido")
        self.authority = authority
        self.db_path = None
        self.clock = authority.clock
        self.heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self.default_lease_seconds = default_lease_seconds
        self.default_max_attempts = default_max_attempts
        self.progress_stall_seconds = progress_stall_seconds
        self.expected_rules_sha = normalized or None
        self._init()

    def _db(self):
        return self.authority.connection()

    def _init(self):
        self.authority.verify_schema()


class PostgresWorkOrchestrator(WorkOrchestrator):
    def __init__(self, authority: PostgresAuthority,
                 pool_store: PostgresWorkerPoolStore):
        _need(pool_store.authority is authority, "work_authority_mismatch")
        self.authority = authority
        self.db_path = None
        self.pool_store = pool_store
        self.clock = authority.clock
        self._init()

    def _db(self):
        return self.authority.connection()

    def _init(self):
        self.authority.verify_schema()


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit PostgreSQL authority lifecycle")
    parser.add_argument("action", choices=("initialize", "check"))
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--schema", default="worker_pool")
    parser.add_argument("--authority-id", required=True)
    args = parser.parse_args()
    try:
        authority = PostgresAuthority.from_file(
            args.database_url_file, schema=args.schema, authority_id=args.authority_id)
        evidence = (authority.initialize() if args.action == "initialize"
                    else authority.verify_schema())
    except AuthorityUnavailable as exc:
        print(json.dumps({"result": "blocked", "code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"result": "ok", **evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
