ion.effect_id=checkpoint.effect_id
                    AND reconciliation.lease_generation=checkpoint.lease_generation
                   WHERE checkpoint.task_id=?
                   ORDER BY checkpoint.lease_generation DESC,checkpoint.sequence DESC
                   LIMIT 1""",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_recovery_context(self, task_id: str) -> dict[str, Any]:
        latest_checkpoint = self.get_latest_checkpoint(task_id)
        with self._db() as db:
            effects = db.execute(
                """SELECT checkpoint.*,
                          reconciliation.outcome AS reconciliation_outcome,
                          reconciliation.readback_ref AS reconciliation_readback_ref,
                          reconciliation.created_at AS reconciled_at
                   FROM task_checkpoints checkpoint
                   LEFT JOIN effect_reconciliations reconciliation
                     ON reconciliation.task_id=checkpoint.task_id
                    AND reconciliation.effect_id=checkpoint.effect_id
                    AND reconciliation.lease_generation=checkpoint.lease_generation
                   WHERE checkpoint.task_id=?
                     AND checkpoint.effect_id IS NOT NULL
                     AND NOT EXISTS(
                       SELECT 1 FROM task_checkpoints later
                       WHERE later.task_id=checkpoint.task_id
                         AND later.effect_id=checkpoint.effect_id
                         AND (
                           later.lease_generation>checkpoint.lease_generation OR
                           (later.lease_generation=checkpoint.lease_generation
                            AND later.sequence>checkpoint.sequence)
                         )
                     )
                   ORDER BY checkpoint.effect_id""",
                (task_id,),
            ).fetchall()
        return {
            "latest_checkpoint": latest_checkpoint,
            "effects": [dict(row) for row in effects],
        }

    def reconcile_effect(
        self, *, task_id: str, effect_id: str, lease_generation: int,
        outcome: str, readback_ref: str, correlation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        if not SAFE_ID.fullmatch(effect_id):
            raise ValueError("effect_id inválido")
        if int(lease_generation) < 1:
            raise ValueError("lease_generation inválida")
        if outcome not in ("confirmed", "not_applied"):
            raise ValueError("outcome de reconciliação inválido")
        readback_ref = sanitize(readback_ref.strip())
        correlation_id = correlation_id.strip()
        if not readback_ref or not correlation_id:
            raise ValueError("reconciliação incompleta")
        semantic = {
            "effect_id": effect_id,
            "lease_generation": int(lease_generation),
            "outcome": outcome,
            "readback_ref": readback_ref,
        }
        digest = hashlib.sha256(
            json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self._tx() as db:
            task = db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task não encontrada: {task_id}")
            existing = db.execute(
                """SELECT * FROM effect_reconciliations
                   WHERE task_id=? AND effect_id=? AND lease_generation=?""",
                (task_id, effect_id, int(lease_generation)),
            ).fetchone()
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError("reconciliação divergente no replay")
                return self._d(existing), False
            if not bool(task["fencing_required"]):
                raise ConflictError("reconciliação exige task cercada")
            if task["state"] != "blocked" or task["blocked_reason"] != "effect_reconciliation_required":
                raise ConflictError("task não aguarda reconciliação de efeito")
            latest = db.execute(
                """SELECT * FROM task_checkpoints
                   WHERE task_id=? AND effect_id=?
                   ORDER BY lease_generation DESC,sequence DESC LIMIT 1""",
                (task_id, effect_id),
            ).fetchone()
            if latest is None or latest["effect_state"] not in ("pending", "unknown"):
                raise ConflictError("efeito não está pendente de reconciliação")
            if int(latest["lease_generation"]) != int(lease_generation):
                raise ConflictError("lease_generation da reconciliação divergente")
            stamp = iso(self.clock())
            db.execute(
                """INSERT INTO effect_reconciliations(
                     task_id,effect_id,lease_generation,outcome,readback_ref,
                     content_digest,correlation_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    task_id, effect_id, int(lease_generation), outcome, readback_ref,
                    digest, correlation_id, stamp,
                ),
            )
            created = db.execute(
                """SELECT * FROM effect_reconciliations
                   WHERE task_id=? AND effect_id=? AND lease_generation=?""",
                (task_id, effect_id, int(lease_generation)),
            ).fetchone()
        return self._d(created), True

    def requeue_blocked(self, task_id: str, *, correlation_id: str) -> dict[str, Any]:
        with self._tx() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise NotFoundError(f"task não encontrada: {task_id}")
            if row["state"] != "blocked":
                raise ConflictError("somente task bloqueada pode ser reenfileirada")
            if bool(row["fencing_required"]) and self._has_unresolved_effect(db, task_id):
                raise ConflictError("efeito requer reconciliação")
            state = "validating" if row["produced_sha"] else "queued"
            stamp = iso(self.clock())
            db.execute("""UPDATE tasks SET state=?,blocked_reason=NULL,
              correlation_id=?,updated_at=?,last_material_progress_at=? WHERE task_id=?""",
              (state, correlation_id, stamp, stamp, task_id))
        return self.get_task(task_id)

    def recover_expired_leases(self) -> int:
        with self._tx() as db:
            return self._recover(db)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._d(db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def list_quarantine(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute("""SELECT task_id,idempotency_key,
              correlation_id,attempt_count,reason,quarantined_at
              FROM quarantine ORDER BY id DESC""").fetchall()]

    def snapshot(self) -> dict[str, Any]:
        self.recover_expired_leases()
        self.recover_stalled_tasks()
        now = self.clock()
        with self._db() as db:
            workers = db.execute("SELECT * FROM workers ORDER BY host,worker_id").fetchall()
            tasks = db.execute("""SELECT * FROM tasks WHERE state<>'completed'
              ORDER BY priority,created_at,task_id""").fetchall()
            counts = {r["state"]: r["n"] for r in db.execute(
                "SELECT state,COUNT(*) n FROM tasks GROUP BY state").fetchall()}
            qn = db.execute("SELECT COUNT(*) n FROM quarantine").fetchone()["n"]
            repositories = db.execute(
                """SELECT r.repository,r.enabled,r.max_in_flight,
                          r.builder_claim_count,r.validator_claim_count,
                          r.last_builder_claimed_at,r.last_validator_claimed_at,r.updated_at,
                          SUM(CASE WHEN t.state='queued' THEN 1 ELSE 0 END) queued,
                          SUM(CASE WHEN t.state IN ('leased','running','validating') THEN 1 ELSE 0 END) active
                   FROM repository_lanes r
                   LEFT JOIN tasks t ON t.repository=r.repository
                   GROUP BY r.repository,r.enabled,r.max_in_flight,
                            r.builder_claim_count,r.validator_claim_count,
                            r.last_builder_claimed_at,r.last_validator_claimed_at,r.updated_at
                   ORDER BY r.repository"""
            ).fetchall()
            affinity_rows = db.execute(
                """SELECT worker_id,repository FROM worker_repository_affinity
                   ORDER BY worker_id,repository"""
            ).fetchall()
        affinity_map: dict[str, list[str]] = {}
        for item in affinity_rows:
            affinity_map.setdefault(str(item["worker_id"]), []).append(str(item["repository"]))
        active = {r["leased_by"]: dict(r) for r in tasks if r["leased_by"]}
        views = []
        for row in workers:
            w, age = dict(row), max(0.0, (now - parse_iso(row["last_heartbeat_at"])).total_seconds())
            online, task = age <= self.heartbeat_ttl_seconds, active.get(row["worker_id"])
            if not online:
                why = "offline_or_stale"
            elif row["profile"] == "ESTUDO":
                why = "profile_estudo"
            elif not row["gateway_ok"] or not row["state_validated"]:
                why = "governance_not_ready"
            elif task:
                why = None
            elif row["role"] == "builder" and not counts.get("queued", 0):
                why = "no_queued_work"
            elif row["role"] == "validator" and not counts.get("validating", 0):
                why = "no_validation_work"
            else:
                why = "available"
            w.update(gateway_ok=bool(w["gateway_ok"]), state_validated=bool(w["state_validated"]),
                     online=online, heartbeat_age_seconds=round(age, 3),
                     active_task=task["task_id"] if task else None,
                     active_sha=task["produced_sha"] if task else None,
                     repository_affinity=affinity_map.get(str(row["worker_id"]), []),
                     why_idle=why)
            views.append(w)
        safe = ("task_id","repository","issue_number","state","priority","branch","workspace_key",
                "base_sha","produced_sha","builder_worker_id","validator_worker_id","leased_by",
                "lease_expires_at","lease_generation","attempt_id","fencing_required",
                "attempt_count","max_attempts","blocked_reason","last_error",
                "correlation_id","updated_at","last_material_progress_at")
        task_views = []
        for row in tasks:
            item = {k: row[k] for k in safe}
            item["fencing_required"] = bool(item["fencing_required"])
            progress_age = max(
                0.0,
                (now - parse_iso(row["last_material_progress_at"])).total_seconds(),
            )
            item["progress_age_seconds"] = round(progress_age, 3)
            item["progress_stalled"] = (
                row["state"] in ACTIVE and progress_age >= self.progress_stall_seconds
            )
            task_views.append(item)
        return {
            "schema_version":"1.2.0", "generated_at":iso(now), "workers":views,
            "repositories":[
                {
                    **dict(r),
                    "enabled": bool(r["enabled"]),
                    "queued": int(r["queued"] or 0),
                    "active": int(r["active"] or 0),
                }
                for r in repositories
            ],
            "queue":{s:counts.get(s,0) for s in STATES}, "quarantine_count":qn,
            "progress_stall_seconds": self.progress_stall_seconds,
            "tasks": task_views,
        }
