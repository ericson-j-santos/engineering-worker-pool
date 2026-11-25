-- Incremental CAS extension v2 for the existing PostgreSQL authority.
-- The v1 migration and authority_meta identity remain byte-for-byte unchanged.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE TABLE cas_extension_meta_v2(
  extension_name TEXT PRIMARY KEY CHECK(extension_name='fenced_cas'),
  extension_version BIGINT NOT NULL CHECK(extension_version=2),
  template_sha256 TEXT NOT NULL CHECK(template_sha256 ~ '^[0-9a-f]{64}$'),
  installed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE cas_publishers_v2(
  role_name NAME PRIMARY KEY,
  emitter_id UUID NOT NULL UNIQUE,
  heartbeat_ttl_seconds BIGINT NOT NULL CHECK(heartbeat_ttl_seconds BETWEEN 1 AND 3600),
  enabled BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE cas_readers_v2(
  role_name NAME PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE cas_objects_v2(
  sha256_hex TEXT PRIMARY KEY CHECK(sha256_hex ~ '^[0-9a-f]{64}$'),
  payload BYTEA NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 20000000),
  size_bytes BIGINT NOT NULL CHECK(size_bytes BETWEEN 1 AND 20000000),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  retain_until TIMESTAMPTZ NOT NULL DEFAULT (clock_timestamp()+interval '30 days'),
  CHECK(size_bytes=octet_length(payload))
);

CREATE TABLE cas_attestations_v2(
  attestation_id UUID PRIMARY KEY,
  task_id TEXT NOT NULL,
  effect_id TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL CHECK(artifact_sha256 ~ '^[0-9a-f]{64}$'),
  binding_sha256 TEXT NOT NULL CHECK(binding_sha256 ~ '^[0-9a-f]{64}$'),
  binding_json JSONB NOT NULL,
  issuer_role NAME NOT NULL,
  emitter_id UUID NOT NULL,
  lease_generation BIGINT NOT NULL CHECK(lease_generation>=1),
  attempt_id TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  checkpoint_digest TEXT NOT NULL CHECK(checkpoint_digest ~ '^[0-9a-f]{64} BIGINT NOT NULL CHECK(heartbeat_ttl_seconds BETWEEN 1 AND 3600),
  expires_at TIMESTAMPTZ NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(task_id,effect_id),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
);

CREATE TABLE cas_publication_receipts_v2(
  task_id TEXT NOT NULL,
  effect_id TEXT NOT NULL,
  attestation_id UUID NOT NULL UNIQUE REFERENCES cas_attestations_v2(attestation_id),
  binding_sha256 TEXT NOT NULL,
  sha256_hex TEXT NOT NULL REFERENCES cas_objects_v2(sha256_hex),
  repository TEXT NOT NULL,
  base_sha TEXT NOT NULL,
  source_sha TEXT NOT NULL,
  produced_sha TEXT NOT NULL,
  published_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(task_id,effect_id)
);

INSERT INTO cas_extension_meta_v2(extension_name,extension_version,template_sha256)
VALUES('fenced_cas',2,'__TEMPLATE_SHA256__');

CREATE FUNCTION issue_cas_attestation_v2(b JSONB)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog SET statement_timeout='15s' AS $fn$
DECLARE
  p __AUTHORITY_SCHEMA__.cas_publishers_v2%ROWTYPE;
  t __AUTHORITY_SCHEMA__.tasks%ROWTYPE;
  a __AUTHORITY_SCHEMA__.task_attempts%ROWTYPE;
  w __AUTHORITY_SCHEMA__.workers%ROWTYPE;
  c __AUTHORITY_SCHEMA__.task_checkpoints%ROWTYPE;
  old_a __AUTHORITY_SCHEMA__.cas_attestations_v2%ROWTYPE;
  now_at TIMESTAMPTZ;
  expires TIMESTAMPTZ;
  bh TEXT;
  new_id UUID;
BEGIN
  IF b IS NULL OR jsonb_typeof(b)<>'object' OR NOT (b ?& ARRAY[
    'authority_id','emitter_id','task_id','effect_id','repository','issue_number',
    'request_id','correlation_id','base_sha','source_sha','produced_sha','worker_id',
    'lease_token','lease_generation','attempt_id','rules_sha','checkpoint_id',
    'checkpoint_digest','artifact_sha256'])
  THEN RAISE EXCEPTION 'cas_input_invalid' USING ERRCODE='P6101'; END IF;
  IF b->>'base_sha' !~ '^[0-9a-f]{40}$' OR b->>'source_sha' !~ '^[0-9a-f]{40}$'
     OR b->>'produced_sha' !~ '^[0-9a-f]{40}$' OR b->>'rules_sha' !~ '^[0-9a-f]{40}$'
     OR b->>'artifact_sha256' !~ '^[0-9a-f]{64}$'
     OR b->>'checkpoint_digest' !~ '^[0-9a-f]{64}$'
     OR b->>'emitter_id' !~ '^[0-9a-f-]{36}$'
  THEN RAISE EXCEPTION 'cas_binding_invalid' USING ERRCODE='P6101'; END IF;

  PERFORM pg_advisory_xact_lock(__LOCK_KEY__::bigint);
  SELECT * INTO p FROM __AUTHORITY_SCHEMA__.cas_publishers_v2
   WHERE role_name=session_user::name AND enabled;
  IF NOT FOUND OR p.emitter_id::text IS DISTINCT FROM b->>'emitter_id'
  THEN RAISE EXCEPTION 'cas_publisher_denied' USING ERRCODE='42501'; END IF;

  IF NOT EXISTS(SELECT 1 FROM __AUTHORITY_SCHEMA__.authority_meta m
    WHERE m.singleton=1 AND m.authority_id=b->>'authority_id'
      AND m.schema_version=1)
    OR NOT EXISTS(SELECT 1 FROM __AUTHORITY_SCHEMA__.pool_control pc
      WHERE pc.control_key='fenced_cutover' AND pc.enabled=1)
  THEN RAISE EXCEPTION 'cas_authority_invalid' USING ERRCODE='P6101'; END IF;

  SELECT * INTO t FROM __AUTHORITY_SCHEMA__.tasks
   WHERE task_id=b->>'task_id' FOR UPDATE;
  IF NOT FOUND OR t.state IS DISTINCT FROM 'running'
    OR t.fencing_required IS DISTINCT FROM 1
    OR t.repository IS DISTINCT FROM b->>'repository'
    OR t.issue_number::text IS DISTINCT FROM b->>'issue_number'
    OR t.request_id IS DISTINCT FROM b->>'request_id'
    OR t.correlation_id IS DISTINCT FROM b->>'correlation_id'
    OR t.base_sha IS DISTINCT FROM b->>'base_sha'
    OR (t.produced_sha IS NOT NULL AND t.produced_sha IS DISTINCT FROM b->>'produced_sha')
    OR t.builder_worker_id IS DISTINCT FROM b->>'worker_id'
    OR t.leased_by IS DISTINCT FROM b->>'worker_id'
    OR t.lease_token IS NULL OR t.lease_token IS DISTINCT FROM b->>'lease_token'
    OR t.lease_generation::text IS DISTINCT FROM b->>'lease_generation'
    OR t.attempt_id IS DISTINCT FROM b->>'attempt_id'
  THEN RAISE EXCEPTION 'cas_task_fence_invalid' USING ERRCODE='P6101'; END IF;

  SELECT * INTO a FROM __AUTHORITY_SCHEMA__.task_attempts
   WHERE attempt_id=t.attempt_id FOR UPDATE;
  IF NOT FOUND OR a.task_id IS DISTINCT FROM t.task_id
    OR a.lease_generation IS DISTINCT FROM t.lease_generation
    OR a.worker_id IS DISTINCT FROM t.leased_by
    OR a.role IS DISTINCT FROM 'builder' OR a.ended_at IS NOT NULL
  THEN RAISE EXCEPTION 'cas_attempt_invalid' USING ERRCODE='P6101'; END IF;

  SELECT * INTO w FROM __AUTHORITY_SCHEMA__.workers
   WHERE worker_id=t.leased_by FOR UPDATE;
  IF NOT FOUND OR w.role IS DISTINCT FROM 'builder'
    OR w.profile IS DISTINCT FROM 'NORMAL'
    OR w.gateway_ok IS DISTINCT FROM 1 OR w.state_validated IS DISTINCT FROM 1
    OR w.rules_sha IS DISTINCT FROM b->>'rules_sha'
  THEN RAISE EXCEPTION 'cas_worker_invalid' USING ERRCODE='P6101'; END IF;

  SELECT * INTO c FROM __AUTHORITY_SCHEMA__.task_checkpoints
   WHERE task_id=t.task_id AND lease_generation=t.lease_generation
   ORDER BY sequence DESC LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR c.checkpoint_id IS DISTINCT FROM b->>'checkpoint_id'
    OR c.attempt_id IS DISTINCT FROM t.attempt_id
    OR c.effect_id IS NULL OR c.effect_id IS DISTINCT FROM b->>'effect_id'
    OR c.effect_state IS DISTINCT FROM 'pending'
    OR c.artifact_sha256 IS DISTINCT FROM b->>'artifact_sha256'
    OR c.content_digest IS DISTINCT FROM b->>'checkpoint_digest'
    OR c.correlation_id IS DISTINCT FROM t.correlation_id
  THEN RAISE EXCEPTION 'cas_checkpoint_invalid' USING ERRCODE='P6101'; END IF;

  now_at:=clock_timestamp();
  expires:=least(t.lease_expires_at::timestamptz,a.lease_expires_at::timestamptz);
  IF expires<=now_at OR w.last_heartbeat_at::timestamptz<now_at-make_interval(secs=>p.heartbeat_ttl_seconds)
    OR w.last_heartbeat_at::timestamptz>now_at
  THEN RAISE EXCEPTION 'cas_lease_or_heartbeat_expired' USING ERRCODE='P6101'; END IF;

  bh:=encode(public.digest(convert_to((b-'lease_token')::text,'UTF8'),'sha256'),'hex');
  SELECT * INTO old_a FROM __AUTHORITY_SCHEMA__.cas_attestations_v2
   WHERE task_id=t.task_id AND effect_id=c.effect_id;
  IF FOUND THEN
    IF old_a.binding_sha256 IS DISTINCT FROM bh
      OR old_a.artifact_sha256 IS DISTINCT FROM b->>'artifact_sha256'
    THEN RAISE EXCEPTION 'cas_attestation_replay_conflict' USING ERRCODE='P6101'; END IF;
    RETURN old_a.attestation_id;
  END IF;

  new_id:=gen_random_uuid();
  INSERT INTO __AUTHORITY_SCHEMA__.cas_attestations_v2(
    attestation_id,task_id,effect_id,artifact_sha256,binding_sha256,binding_json,
    issuer_role,emitter_id,lease_generation,attempt_id,checkpoint_id,
    checkpoint_digest,lease_token_sha256,heartbeat_ttl_seconds,expires_at)
  VALUES(new_id,t.task_id,c.effect_id,b->>'artifact_sha256',bh,b-'lease_token',
    session_user::name,p.emitter_id,t.lease_generation,t.attempt_id,c.checkpoint_id,
    c.content_digest,encode(public.digest(convert_to(b->>'lease_token','UTF8'),'sha256'),'hex'),
    p.heartbeat_ttl_seconds,expires);
  RETURN new_id;
END
$fn$;

CREATE FUNCTION publish_fenced_cas_v2(attestation UUID,payload_bytes BYTEA)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog SET statement_timeout='15s' AS $fn$
DECLARE
  p __AUTHORITY_SCHEMA__.cas_publishers_v2%ROWTYPE;
  x __AUTHORITY_SCHEMA__.cas_attestations_v2%ROWTYPE;
  t __AUTHORITY_SCHEMA__.tasks%ROWTYPE;
  a __AUTHORITY_SCHEMA__.task_attempts%ROWTYPE;
  w __AUTHORITY_SCHEMA__.workers%ROWTYPE;
  c __AUTHORITY_SCHEMA__.task_checkpoints%ROWTYPE;
  r __AUTHORITY_SCHEMA__.cas_publication_receipts_v2%ROWTYPE;
  old_payload BYTEA;
  old_retention TIMESTAMPTZ;
  now_at TIMESTAMPTZ;
BEGIN
  IF attestation IS NULL OR payload_bytes IS NULL
    OR octet_length(payload_bytes) NOT BETWEEN 1 AND 20000000
  THEN RAISE EXCEPTION 'cas_input_invalid' USING ERRCODE='P6101'; END IF;

  PERFORM pg_advisory_xact_lock(__LOCK_KEY__::bigint);
  SELECT * INTO p FROM __AUTHORITY_SCHEMA__.cas_publishers_v2
   WHERE role_name=session_user::name AND enabled;
  IF NOT FOUND THEN RAISE EXCEPTION 'cas_publisher_denied' USING ERRCODE='42501'; END IF;
  SELECT * INTO x FROM __AUTHORITY_SCHEMA__.cas_attestations_v2
   WHERE attestation_id=attestation AND issuer_role=session_user::name
     AND emitter_id=p.emitter_id FOR UPDATE;
  IF NOT FOUND OR encode(public.digest(payload_bytes,'sha256'),'hex')
      IS DISTINCT FROM x.artifact_sha256
  THEN RAISE EXCEPTION 'cas_attestation_invalid' USING ERRCODE='P6101'; END IF;

  SELECT * INTO t FROM __AUTHORITY_SCHEMA__.tasks WHERE task_id=x.task_id FOR UPDATE;
  SELECT * INTO a FROM __AUTHORITY_SCHEMA__.task_attempts
    WHERE attempt_id=x.attempt_id FOR UPDATE;
  SELECT * INTO w FROM __AUTHORITY_SCHEMA__.workers
    WHERE worker_id=t.leased_by FOR UPDATE;
  SELECT * INTO c FROM __AUTHORITY_SCHEMA__.task_checkpoints
    WHERE task_id=x.task_id AND checkpoint_id=x.checkpoint_id FOR UPDATE;
  now_at:=clock_timestamp();
  IF t.state IS DISTINCT FROM 'running' OR t.fencing_required IS DISTINCT FROM 1
    OR t.lease_generation IS DISTINCT FROM x.lease_generation
    OR t.attempt_id IS DISTINCT FROM x.attempt_id OR t.leased_by IS NULL
    OR encode(public.digest(convert_to(t.lease_token,'UTF8'),'sha256'),'hex')
       IS DISTINCT FROM x.lease_token_sha256
    OR a.attempt_id IS NULL OR a.ended_at IS NOT NULL
    OR a.task_id IS DISTINCT FROM t.task_id OR a.worker_id IS DISTINCT FROM t.leased_by
    OR w.worker_id IS NULL OR w.rules_sha IS DISTINCT FROM x.binding_json->>'rules_sha'
    OR w.gateway_ok IS DISTINCT FROM 1 OR w.state_validated IS DISTINCT FROM 1
    OR c.checkpoint_id IS NULL OR c.effect_id IS DISTINCT FROM x.effect_id
    OR c.effect_state IS DISTINCT FROM 'pending'
    OR c.content_digest IS DISTINCT FROM x.checkpoint_digest
    OR c.artifact_sha256 IS DISTINCT FROM x.artifact_sha256
    OR x.expires_at<=now_at OR t.lease_expires_at::timestamptz<=now_at
    OR a.lease_expires_at::timestamptz<=now_at
    OR w.last_heartbeat_at::timestamptz<now_at-make_interval(secs=>x.heartbeat_ttl_seconds)
  THEN RAISE EXCEPTION 'cas_fence_expired_or_changed' USING ERRCODE='P6101'; END IF;

  SELECT * INTO r FROM __AUTHORITY_SCHEMA__.cas_publication_receipts_v2
    WHERE task_id=x.task_id AND effect_id=x.effect_id;
  IF FOUND THEN
    IF r.attestation_id IS DISTINCT FROM x.attestation_id
      OR r.binding_sha256 IS DISTINCT FROM x.binding_sha256
      OR r.sha256_hex IS DISTINCT FROM x.artifact_sha256
    THEN RAISE EXCEPTION 'cas_replay_conflict' USING ERRCODE='P6101'; END IF;
    SELECT payload,retain_until INTO old_payload,old_retention
      FROM __AUTHORITY_SCHEMA__.cas_objects_v2 WHERE sha256_hex=r.sha256_hex;
    IF NOT FOUND OR old_payload IS DISTINCT FROM payload_bytes OR old_retention<=now_at
    THEN RAISE EXCEPTION 'cas_readback_invalid' USING ERRCODE='P6101'; END IF;
    RETURN FALSE;
  END IF;

  INSERT INTO __AUTHORITY_SCHEMA__.cas_objects_v2(sha256_hex,payload,size_bytes)
    VALUES(x.artifact_sha256,payload_bytes,octet_length(payload_bytes))
    ON CONFLICT(sha256_hex) DO NOTHING;
  SELECT payload,retain_until INTO old_payload,old_retention
    FROM __AUTHORITY_SCHEMA__.cas_objects_v2 WHERE sha256_hex=x.artifact_sha256;
  IF NOT FOUND OR old_payload IS DISTINCT FROM payload_bytes
    OR old_retention<now_at+interval '30 days'
  THEN RAISE EXCEPTION 'cas_retention_or_content_invalid' USING ERRCODE='P6101'; END IF;
  IF clock_timestamp()>=x.expires_at
  THEN RAISE EXCEPTION 'cas_lease_expired' USING ERRCODE='P6101'; END IF;

  INSERT INTO __AUTHORITY_SCHEMA__.cas_publication_receipts_v2(
    task_id,effect_id,attestation_id,binding_sha256,sha256_hex,repository,
    base_sha,source_sha,produced_sha)
  VALUES(x.task_id,x.effect_id,x.attestation_id,x.binding_sha256,x.artifact_sha256,
    x.binding_json->>'repository',x.binding_json->>'base_sha',
    x.binding_json->>'source_sha',x.binding_json->>'produced_sha');
  RETURN TRUE;
END
$fn$;

CREATE FUNCTION reconcile_cas_publication_v2(
  wanted_task TEXT,wanted_effect TEXT,wanted_digest TEXT,wanted_repository TEXT,
  wanted_base_sha TEXT,wanted_source_sha TEXT,wanted_produced_sha TEXT)
RETURNS TABLE(status TEXT,attestation_id UUID,sha256_hex TEXT,payload BYTEA,
  binding_sha256 TEXT,published_at TIMESTAMPTZ)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog SET statement_timeout='15s' AS $fn$
DECLARE
  rr __AUTHORITY_SCHEMA__.cas_publication_receipts_v2%ROWTYPE;
  aa __AUTHORITY_SCHEMA__.cas_attestations_v2%ROWTYPE;
  oo __AUTHORITY_SCHEMA__.cas_objects_v2%ROWTYPE;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM __AUTHORITY_SCHEMA__.cas_readers_v2
      WHERE role_name=session_user::name AND enabled)
  THEN RAISE EXCEPTION 'cas_reader_denied' USING ERRCODE='42501'; END IF;
  IF wanted_task IS NULL OR wanted_effect IS NULL OR wanted_repository IS NULL
    OR wanted_digest !~ '^[0-9a-f]{64}$' OR wanted_base_sha !~ '^[0-9a-f]{40}$'
    OR wanted_source_sha !~ '^[0-9a-f]{40}$' OR wanted_produced_sha !~ '^[0-9a-f]{40}$'
  THEN RAISE EXCEPTION 'cas_reconcile_input_invalid' USING ERRCODE='P6101'; END IF;

  PERFORM pg_advisory_xact_lock(__LOCK_KEY__::bigint);
  SELECT * INTO rr FROM __AUTHORITY_SCHEMA__.cas_publication_receipts_v2
    WHERE task_id=wanted_task AND effect_id=wanted_effect;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'not_applied'::text,NULL::uuid,NULL::text,NULL::bytea,
      NULL::text,NULL::timestamptz;
    RETURN;
  END IF;
  SELECT * INTO aa FROM __AUTHORITY_SCHEMA__.cas_attestations_v2
    WHERE cas_attestations_v2.attestation_id=rr.attestation_id;
  IF NOT FOUND OR rr.sha256_hex IS DISTINCT FROM wanted_digest
    OR rr.repository IS DISTINCT FROM wanted_repository
    OR rr.base_sha IS DISTINCT FROM wanted_base_sha
    OR rr.source_sha IS DISTINCT FROM wanted_source_sha
    OR rr.produced_sha IS DISTINCT FROM wanted_produced_sha
    OR aa.binding_sha256 IS DISTINCT FROM rr.binding_sha256
  THEN RAISE EXCEPTION 'cas_reconcile_scope_mismatch' USING ERRCODE='P6101'; END IF;
  SELECT * INTO oo FROM __AUTHORITY_SCHEMA__.cas_objects_v2
    WHERE cas_objects_v2.sha256_hex=rr.sha256_hex;
  IF NOT FOUND OR encode(public.digest(oo.payload,'sha256'),'hex')
      IS DISTINCT FROM rr.sha256_hex OR oo.size_bytes IS DISTINCT FROM octet_length(oo.payload)
    OR oo.retain_until<=clock_timestamp()
  THEN RAISE EXCEPTION 'cas_reconcile_unknown' USING ERRCODE='P6101'; END IF;
  RETURN QUERY SELECT 'confirmed'::text,rr.attestation_id,rr.sha256_hex,oo.payload,
    rr.binding_sha256,rr.published_at;
END
$fn$;

REVOKE ALL ON TABLE __AUTHORITY_SCHEMA__.cas_extension_meta_v2 FROM PUBLIC;
REVOKE ALL ON TABLE __AUTHORITY_SCHEMA__.cas_publishers_v2 FROM PUBLIC;
REVOKE ALL ON TABLE __AUTHORITY_SCHEMA__.cas_readers_v2 FROM PUBLIC;
REVOKE ALL ON TABLE __AUTHORITY_SCHEMA__.cas_objects_v2 FROM PUBLIC;
REVOKE ALL ON TABLE __AUTHORITY_SCHEMA__.cas_attestations_v2 FROM PUBLIC;
REVOKE ALL ON TABLE __AUTHORITY_SCHEMA__.cas_publication_receipts_v2 FROM PUBLIC;
REVOKE ALL ON FUNCTION __AUTHORITY_SCHEMA__.issue_cas_attestation_v2(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION __AUTHORITY_SCHEMA__.publish_fenced_cas_v2(UUID,BYTEA) FROM PUBLIC;
REVOKE ALL ON FUNCTION __AUTHORITY_SCHEMA__.reconcile_cas_publication_v2(TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT) FROM PUBLIC;
