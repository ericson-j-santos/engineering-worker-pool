"""Authenticated, fenced PostgreSQL CAS extension for EWP issue 61.

The extension is incremental: postgres_authority_v1.sql and authority_meta stay
unchanged.  The operator installs v2 explicitly; runtime publishers and readers
use separate authenticated database roles and only SECURITY DEFINER functions.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from app.postgres_authority import (
    AuthorityUnavailable, PostgresAuthority, _need, psycopg, sql,
)

CAS_EXTENSION_VERSION = 2
_MIGRATION = Path(__file__).with_name("migrations") / "postgres_authority_v2_cas.sql"
_ROLLBACK = Path(__file__).with_name("migrations") / "postgres_authority_v2_cas_rollback.sql"
_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _migration_sha256() -> str:
    return hashlib.sha256(_MIGRATION.read_bytes()).hexdigest()


def _render(authority: PostgresAuthority) -> str:
    raw = _MIGRATION.read_text(encoding="utf-8")
    _need(raw.count("__AUTHORITY_SCHEMA__") > 10
          and raw.count("__TEMPLATE_SHA256__") == 1
          and raw.count("__LOCK_KEY__") == 3,
          "cas_migration_template_invalid")
    return (raw.replace("__AUTHORITY_SCHEMA__", authority.schema)
               .replace("__TEMPLATE_SHA256__", _migration_sha256())
               .replace("__LOCK_KEY__", str(authority.lock_key)))


def _role(value: str, code: str) -> str:
    _need(isinstance(value, str) and _ROLE.fullmatch(value) is not None, code)
    return value


class PostgresCasV2:
    """Explicit operator migration plus least-privilege publisher/reader ports."""

    def __init__(self, authority: PostgresAuthority):
        _need(isinstance(authority, PostgresAuthority), "cas_authority_invalid")
        self.authority = authority

    def migrate(self, *, publisher_role: str, reader_role: str,
                emitter_id: str, heartbeat_ttl_seconds: int) -> dict[str, Any]:
        publisher_role = _role(publisher_role, "cas_publisher_role_invalid")
        reader_role = _role(reader_role, "cas_reader_role_invalid")
        _need(publisher_role != reader_role, "cas_roles_not_separated")
        try:
            emitter_id = str(uuid.UUID(emitter_id))
        except (ValueError, TypeError, AttributeError):
            raise AuthorityUnavailable("cas_emitter_identity_invalid") from None
        _need(type(heartbeat_ttl_seconds) is int
              and 1 <= heartbeat_ttl_seconds <= 3600,
              "cas_heartbeat_policy_invalid")
        raw = self.authority._connect()
        try:
            raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE")
            raw.execute("SET LOCAL lock_timeout='8s'")
            raw.execute("SET LOCAL statement_timeout='30s'")
            raw.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)",
                        (self.authority.lock_key,))
            raw.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(
                sql.Identifier(self.authority.schema)))
            self.authority._verify_identity(raw)
            roles = raw.execute(
                "SELECT rolname,rolsuper,rolcanlogin FROM pg_catalog.pg_roles "
                "WHERE rolname=ANY(%s)", ([publisher_role, reader_role],)).fetchall()
            role_map = {row["rolname"]: row for row in roles}
            _need(set(role_map) == {publisher_role, reader_role}
                  and all(not row["rolsuper"] and row["rolcanlogin"]
                          for row in role_map.values()),
                  "cas_runtime_roles_invalid")
            relation = raw.execute(
                "SELECT pg_catalog.to_regclass(%s) AS relation",
                (self.authority.schema + ".cas_extension_meta_v2",)).fetchone()
            existing = None
            if relation["relation"] is not None:
                existing = raw.execute(
                    sql.SQL("SELECT extension_version,template_sha256 FROM {}."
                            "cas_extension_meta_v2 WHERE extension_name='fenced_cas'").format(
                                sql.Identifier(self.authority.schema))).fetchone()
            if existing is None:
                raw.execute(_render(self.authority), prepare=False)
            else:
                _need(existing["extension_version"] == CAS_EXTENSION_VERSION
                      and existing["template_sha256"] == _migration_sha256(),
                      "cas_extension_identity_mismatch")
            raw.execute(sql.SQL(
                "INSERT INTO {}.cas_publishers_v2(role_name,emitter_id,"
                "heartbeat_ttl_seconds) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING"
            ).format(sql.Identifier(self.authority.schema)),
                (publisher_role, emitter_id, heartbeat_ttl_seconds))
            raw.execute(sql.SQL(
                "INSERT INTO {}.cas_readers_v2(role_name) VALUES(%s) "
                "ON CONFLICT DO NOTHING"
            ).format(sql.Identifier(self.authority.schema)), (reader_role,))
            configured = raw.execute(sql.SQL(
                "SELECT role_name::text,emitter_id::text,heartbeat_ttl_seconds,enabled "
                "FROM {}.cas_publishers_v2 WHERE role_name=%s"
            ).format(sql.Identifier(self.authority.schema)), (publisher_role,)).fetchone()
            _need(configured is not None and configured["emitter_id"] == emitter_id
                  and configured["heartbeat_ttl_seconds"] == heartbeat_ttl_seconds
                  and configured["enabled"] is True,
                  "cas_publisher_configuration_conflict")
            for role in (publisher_role, reader_role):
                raw.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(self.authority.schema), sql.Identifier(role)))
            raw.execute(sql.SQL(
                "GRANT EXECUTE ON FUNCTION {}.issue_cas_attestation_v2(jsonb),"
                "{}.publish_fenced_cas_v2(uuid,bytea) TO {}"
            ).format(sql.Identifier(self.authority.schema),
                     sql.Identifier(self.authority.schema),
                     sql.Identifier(publisher_role)))
            raw.execute(sql.SQL(
                "GRANT EXECUTE ON FUNCTION {}.reconcile_cas_publication_v2"
                "(text,text,text,text,text,text,text) TO {}"
            ).format(sql.Identifier(self.authority.schema), sql.Identifier(reader_role)))
            for table in (
                "cas_extension_meta_v2", "cas_publishers_v2", "cas_readers_v2",
                "cas_objects_v2", "cas_attestations_v2",
                "cas_publication_receipts_v2",
            ):
                raw.execute(sql.SQL(
                    "REVOKE ALL ON TABLE {}.{} FROM {},{}"
                ).format(sql.Identifier(self.authority.schema), sql.Identifier(table),
                         sql.Identifier(publisher_role), sql.Identifier(reader_role)))
            raw.commit()
        except AuthorityUnavailable:
            raw.rollback()
            raise
        except psycopg.Error:
            raw.rollback()
            raise AuthorityUnavailable("cas_migration_failed") from None
        finally:
            raw.close()
        return self.verify(publisher_role=publisher_role, reader_role=reader_role)

    def verify(self, *, publisher_role: str, reader_role: str) -> dict[str, Any]:
        publisher_role = _role(publisher_role, "cas_publisher_role_invalid")
        reader_role = _role(reader_role, "cas_reader_role_invalid")
        raw = self.authority._connect()
        try:
            raw.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            raw.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(
                sql.Identifier(self.authority.schema)))
            self.authority._verify_identity(raw)
            meta = raw.execute(sql.SQL(
                "SELECT extension_version,template_sha256 FROM {}."
                "cas_extension_meta_v2 WHERE extension_name='fenced_cas'"
            ).format(sql.Identifier(self.authority.schema))).fetchone()
            _need(meta is not None and meta["extension_version"] == 2
                  and meta["template_sha256"] == _migration_sha256(),
                  "cas_extension_identity_mismatch")
            funcs = raw.execute(
                "SELECT p.proname,p.prosecdef,p.proconfig FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=%s AND p.proname=ANY(%s)",
                (self.authority.schema, [
                    "issue_cas_attestation_v2", "publish_fenced_cas_v2",
                    "reconcile_cas_publication_v2",
                ])).fetchall()
            _need(len(funcs) == 3 and all(row["prosecdef"] for row in funcs)
                  and all("search_path=pg_catalog" in (row["proconfig"] or [])
                          for row in funcs), "cas_functions_unverified")
            privileges = raw.execute(
                "SELECT has_function_privilege(%s,%s,'EXECUTE') AS pub_issue,"
                "has_function_privilege(%s,%s,'EXECUTE') AS pub_publish,"
                "has_function_privilege(%s,%s,'EXECUTE') AS pub_read,"
                "has_function_privilege(%s,%s,'EXECUTE') AS read_issue,"
                "has_function_privilege(%s,%s,'EXECUTE') AS read_publish,"
                "has_function_privilege(%s,%s,'EXECUTE') AS read_read",
                (publisher_role, f"{self.authority.schema}.issue_cas_attestation_v2(jsonb)",
                 publisher_role, f"{self.authority.schema}.publish_fenced_cas_v2(uuid,bytea)",
                 publisher_role, f"{self.authority.schema}.reconcile_cas_publication_v2(text,text,text,text,text,text,text)",
                 reader_role, f"{self.authority.schema}.issue_cas_attestation_v2(jsonb)",
                 reader_role, f"{self.authority.schema}.publish_fenced_cas_v2(uuid,bytea)",
                 reader_role, f"{self.authority.schema}.reconcile_cas_publication_v2(text,text,text,text,text,text,text)")).fetchone()
            _need(privileges["pub_issue"] and privileges["pub_publish"]
                  and not privileges["pub_read"] and not privileges["read_issue"]
                  and not privileges["read_publish"] and privileges["read_read"],
                  "cas_role_separation_unverified")
            raw.rollback()
        except AuthorityUnavailable:
            raw.rollback()
            raise
        except psycopg.Error:
            raw.rollback()
            raise AuthorityUnavailable("cas_verification_failed") from None
        finally:
            raw.close()
        return {"schema": self.authority.schema, "extension_version": 2,
                "template_sha256": _migration_sha256(),
                "publisher_role": publisher_role, "reader_role": reader_role}

    def issue_attestation(self, binding: Mapping[str, Any]) -> str:
        _need(isinstance(binding, Mapping), "cas_binding_invalid")
        raw = self.authority._connect()
        try:
            raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE")
            row = raw.execute(sql.SQL(
                "SELECT {}.issue_cas_attestation_v2(%s::jsonb) AS attestation_id"
            ).format(sql.Identifier(self.authority.schema)),
                (json.dumps(dict(binding), sort_keys=True, separators=(",", ":")),)).fetchone()
            raw.commit()
            return str(row["attestation_id"])
        except psycopg.Error:
            raw.rollback()
            raise AuthorityUnavailable("cas_attestation_rejected") from None
        finally:
            raw.close()

    def publish(self, attestation_id: str, payload: bytes) -> dict[str, Any]:
        try:
            attestation_id = str(uuid.UUID(attestation_id))
        except (ValueError, TypeError, AttributeError):
            raise AuthorityUnavailable("cas_attestation_invalid") from None
        _need(type(payload) is bytes and 0 < len(payload) <= 20000000,
              "cas_payload_invalid")
        raw = self.authority._connect()
        try:
            try:
                raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE")
                row = raw.execute(sql.SQL(
                    "SELECT {}.publish_fenced_cas_v2(%s,%s) AS created,"
                    "pg_catalog.pg_backend_pid() AS backend_pid"
                ).format(sql.Identifier(self.authority.schema)),
                    (attestation_id, payload)).fetchone()
            except psycopg.Error:
                raw.rollback()
                raise AuthorityUnavailable("cas_publish_rejected") from None
            try:
                raw.commit()
            except psycopg.Error:
                raise AuthorityUnavailable("cas_commit_unverified") from None
            return {"created": row["created"], "backend_pid": row["backend_pid"],
                    "artifact_ref": "cas://sha256/" + hashlib.sha256(payload).hexdigest()}
        finally:
            raw.close()

    def reconcile(self, *, task_id: str, effect_id: str, digest_hex: str,
                  repository: str, base_sha: str, source_sha: str,
                  produced_sha: str) -> dict[str, Any]:
        _need(_SHA256.fullmatch(digest_hex or "") is not None
              and all(_SHA40.fullmatch(value or "") is not None
                      for value in (base_sha, source_sha, produced_sha)),
              "cas_reconcile_input_invalid")
        raw = self.authority._connect()
        try:
            raw.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = raw.execute(sql.SQL(
                "SELECT * FROM {}.reconcile_cas_publication_v2"
                "(%s,%s,%s,%s,%s,%s,%s)"
            ).format(sql.Identifier(self.authority.schema)),
                (task_id, effect_id, digest_hex, repository,
                 base_sha, source_sha, produced_sha)).fetchone()
            raw.commit()
            return dict(row)
        except psycopg.Error:
            raw.rollback()
            raise AuthorityUnavailable("cas_reconcile_rejected") from None
        finally:
            raw.close()

    def rollback_empty(self) -> dict[str, Any]:
        raw = self.authority._connect()
        try:
            raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE")
            raw.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)",
                        (self.authority.lock_key,))
            raw.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(
                sql.Identifier(self.authority.schema)))
            self.authority._verify_identity(raw)
            counts = raw.execute(sql.SQL(
                "SELECT (SELECT count(*) FROM {}.cas_publication_receipts_v2) AS receipts,"
                "(SELECT count(*) FROM {}.cas_objects_v2) AS objects"
            ).format(sql.Identifier(self.authority.schema),
                     sql.Identifier(self.authority.schema))).fetchone()
            _need(counts["receipts"] == 0 and counts["objects"] == 0,
                  "cas_rollback_nonempty")
            rendered = _ROLLBACK.read_text(encoding="utf-8").replace(
                "__AUTHORITY_SCHEMA__", self.authority.schema)
            raw.execute(rendered, prepare=False)
            raw.commit()
        except AuthorityUnavailable:
            raw.rollback()
            raise
        except psycopg.Error:
            raw.rollback()
            raise AuthorityUnavailable("cas_rollback_failed") from None
        finally:
            raw.close()
        return self.authority.verify_schema()
