-- Worker Pool PostgreSQL authority v1.
-- Lifecycle tables and constraints mirror WorkerPoolStore / WorkOrchestrator.
-- This is not a SQLite snapshot importer and does not enable fenced admission.
CREATE TABLE authority_meta (
  singleton BIGINT PRIMARY KEY CHECK(singleton=1),
  authority_id TEXT NOT NULL,
  schema_version BIGINT NOT NULL CHECK(schema_version=1),
  schema_sha256 TEXT NOT NULL
);

CREATE TABLE workers(
  worker_id TEXT PRIMARY KEY, host TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('builder','validator')),
  profile TEXT NOT NULL CHECK(profile IN ('NORMAL','ESTUDO')),
  capacity_score BIGINT NOT NULL DEFAULT 50,
  controller_version TEXT, rules_sha TEXT,
  gateway_ok BIGINT NOT NULL DEFAULT 0,
  state_validated BIGINT NOT NULL DEFAULT 0,
  worktree_root TEXT, registered_at TEXT NOT NULL,
  last_heartbeat_at TEXT NOT NULL, cpu_percent DOUBLE PRECISION,
  memory_percent DOUBLE PRECISION, correlation_id TEXT NOT NULL
);
CREATE TABLE repository_lanes(
  repository TEXT PRIMARY KEY,
  enabled BIGINT NOT NULL DEFAULT 1,
  max_in_flight BIGINT NOT NULL DEFAULT 1 CHECK(max_in_flight>=1),
  builder_claim_count BIGINT NOT NULL DEFAULT 0,
  validator_claim_count BIGINT NOT NULL DEFAULT 0,
  last_builder_claimed_at TEXT,
  last_validator_claimed_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE worker_repository_affinity(
  worker_id TEXT NOT NULL, repository TEXT NOT NULL,
  PRIMARY KEY(worker_id,repository),
  FOREIGN KEY(worker_id) REFERENCES workers(worker_id) ON DELETE CASCADE
);
CREATE TABLE tasks(
  task_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
  __pool_fifo BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
  issue_number BIGINT NOT NULL, request_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN
    ('queued','leased','running','validating','blocked','completed','failed')),
  priority BIGINT NOT NULL, branch TEXT NOT NULL,
  workspace_key TEXT NOT NULL, base_sha TEXT, produced_sha TEXT,
  builder_worker_id TEXT, validator_worker_id TEXT, leased_by TEXT,
  lease_token TEXT, lease_expires_at TEXT,
  lease_generation BIGINT NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  attempt_id TEXT,
  fencing_required BIGINT NOT NULL DEFAULT 0 CHECK(fencing_required IN (0,1)),
  attempt_count BIGINT NOT NULL DEFAULT 0,
  max_attempts BIGINT NOT NULL, blocked_reason TEXT, last_error TEXT,
  correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_material_progress_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE quarantine(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, task_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, correlation_id TEXT NOT NULL,
  attempt_count BIGINT NOT NULL, reason TEXT NOT NULL,
  quarantined_at TEXT NOT NULL
);
CREATE TABLE pool_control(
  control_key TEXT PRIMARY KEY CHECK(control_key='fenced_cutover'),
  enabled BIGINT NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
  enabled_at TEXT, enabled_by_correlation_id TEXT
);
INSERT INTO pool_control(control_key,enabled)
  VALUES('fenced_cutover',0);
CREATE TABLE task_attempts(
  attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK(lease_generation>=1),
  worker_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('builder','validator')),
  lease_started_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
  ended_at TEXT, end_reason TEXT,
  UNIQUE(task_id,lease_generation),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE task_checkpoints(
  task_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK(lease_generation>=1),
  attempt_id TEXT NOT NULL, sequence BIGINT NOT NULL CHECK(sequence>=1),
  stage TEXT NOT NULL, artifact_kind TEXT, artifact_ref TEXT,
  artifact_sha256 TEXT, next_action TEXT NOT NULL,
  effect_id TEXT, effect_state TEXT NOT NULL CHECK(effect_state IN
    ('none','pending','confirmed','not_applied','unknown')),
  effect_result_ref TEXT, content_digest TEXT NOT NULL,
  correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(task_id,checkpoint_id),
  UNIQUE(task_id,lease_generation,sequence),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(attempt_id) REFERENCES task_attempts(attempt_id)
);
CREATE TABLE effect_reconciliations(
  task_id TEXT NOT NULL, effect_id TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK(lease_generation>=1),
  outcome TEXT NOT NULL CHECK(outcome IN ('confirmed','not_applied')),
  readback_ref TEXT NOT NULL, content_digest TEXT NOT NULL,
  correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(task_id,effect_id,lease_generation),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX idx_task_checkpoints_generation
  ON task_checkpoints(task_id,lease_generation,sequence DESC);
CREATE INDEX idx_task_checkpoints_effect
  ON task_checkpoints(task_id,effect_id,sequence DESC);
CREATE INDEX idx_task_queue
  ON tasks(state,priority,created_at);
CREATE UNIQUE INDEX idx_worker_active
  ON tasks(leased_by)
  WHERE leased_by IS NOT NULL AND state IN ('leased','running','validating');
CREATE UNIQUE INDEX idx_workspace_active
  ON tasks(repository,workspace_key)
  WHERE state IN ('leased','running','validating');
CREATE INDEX idx_affinity_worker
  ON worker_repository_affinity(worker_id,repository);

    CREATE TABLE work_runs(
      work_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL UNIQUE,
      merge_policy TEXT NOT NULL CHECK(merge_policy IN ('governed')),
      evidence_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(evidence_state IN ('pending','verified')),
      merge_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(merge_state IN ('pending','ready','blocked','merged')),
      validation_run_id TEXT,
      validation_sha TEXT,
      evidence_reference TEXT,
      independent_readback BIGINT NOT NULL DEFAULT 0,
      positive_control BIGINT NOT NULL DEFAULT 0,
      negative_control BIGINT NOT NULL DEFAULT 0,
      expected_head_sha TEXT,
      merge_commit_sha TEXT,
      merge_reason TEXT,
      correlation_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      merged_at TEXT
    );
    CREATE INDEX idx_work_task ON work_runs(task_id);
    CREATE INDEX idx_work_merge_state ON work_runs(merge_state,updated_at);
    
CREATE FUNCTION guard_fenced_task_admission_fn() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
  IF NEW.fencing_required=1 AND NOT EXISTS (
    SELECT 1 FROM pool_control WHERE control_key='fenced_cutover' AND enabled=1
  ) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fenced_cutover_not_enabled';
  END IF;
  RETURN NEW;
END;
$guard$;
CREATE TRIGGER guard_fenced_task_admission
  BEFORE INSERT ON tasks FOR EACH ROW
  EXECUTE FUNCTION guard_fenced_task_admission_fn();

CREATE FUNCTION guard_fenced_claim_fn() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
  IF OLD.fencing_required=1 AND OLD.leased_by IS NULL
     AND NEW.leased_by IS NOT NULL AND (
       NEW.lease_generation<>OLD.lease_generation+1 OR
       NEW.attempt_id IS NULL OR
       NEW.attempt_id=COALESCE(OLD.attempt_id,'')
     ) THEN
    RAISE EXCEPTION USING ERRCODE='23514',
      MESSAGE='fenced_claim_requires_generation_and_attempt';
  END IF;
  RETURN NEW;
END;
$guard$;
CREATE TRIGGER guard_fenced_claim
  BEFORE UPDATE OF leased_by ON tasks FOR EACH ROW
  EXECUTE FUNCTION guard_fenced_claim_fn();
