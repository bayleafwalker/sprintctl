"""Repository-scoped work authority service.

The public compatibility module remains sprintctl.application; this module
owns the single-repository service implementation.
"""

from __future__ import annotations

from .application_common import *


@dataclass(slots=True)
class WorkApplication:
    """One repository-scoped work authority application."""

    repo_id: str
    store: Any
    backend: Any
    ingest_records: RecordIngestor
    arbitrate_command: CommandArbiter
    list_records: RecordReader
    list_decisions: DecisionReader
    credential_resolver: CredentialResolver | None = None
    repo_root: Path | None = None
    _connection_recovery_lock: RLock = field(default_factory=RLock, repr=False)
    _postgres_runtime_available: bool = field(default=True, repr=False)

    @classmethod
    def postgres(
        cls,
        store: Any,
        *,
        credential_resolver: CredentialResolver | None = None,
        repo_root: Path | None = None,
    ) -> WorkApplication:
        """Compose the served application from sprintctl's PostgreSQL authority.

        ``store.repo_id`` seeds the instance returned here, but every served
        invocation re-scopes to the calling identity's ``repo_id`` (see
        :meth:`invoke` and :meth:`_scoped_for`); one running application can
        serve every repository tenant a bound identity is authorized for.
        """

        from . import pg  # Lazy: standalone SQLite needs no psycopg.

        return cls(
            repo_id=store.repo_id,
            store=store,
            backend=pg,
            **cls._store_bound_callables(store),
            credential_resolver=credential_resolver,
            repo_root=repo_root,
        )

    @staticmethod
    def _store_bound_callables(store: Any) -> dict[str, Any]:
        from . import authority, pg  # Lazy: standalone SQLite needs no psycopg.

        return {
            "ingest_records": lambda records: pg.ingest_records(store, records),
            "arbitrate_command": lambda record, credentials, authenticated_actor=None: authority.arbitrate_command(
                store,
                record,
                credentials=credentials,
                authenticated_actor=authenticated_actor,
            ),
            "list_records": lambda after, limit: pg.list_ingested_records(
                store, after_offset=after, limit=limit
            ),
            "list_decisions": lambda after, limit: authority.list_authority_decisions(
                store, after_offset=after, limit=limit
            ),
        }

    def _scoped_for(self, repo_id: str) -> WorkApplication:
        """Return a copy of this application bound to ``repo_id`` for one call.

        The underlying connection (``store.conn``) is shared, unchanged from
        today's single-tenant behavior; only the repository scope is
        request-local. When ``store`` is a real :class:`~sprintctl.pg.PgStore`
        (the only backend production composition uses), this rebuilds
        ``store``, ``ingest_records``, ``arbitrate_command``, ``list_records``,
        and ``list_decisions`` so every backend call this copy makes resolves
        against ``repo_id``. Test doubles that pass a bare connection or no
        store at all (``WorkApplication`` also backs local-SQLite and
        unit-test call sites that have no concept of a repo-scoped store) are
        left exactly as constructed; only the ``repo_id`` field is updated for
        them.
        """

        from dataclasses import fields, is_dataclass, replace

        store = self.store
        if is_dataclass(store) and any(field.name == "repo_id" for field in fields(store)):
            scoped_store = replace(store, repo_id=repo_id)
            return replace(
                self,
                repo_id=repo_id,
                store=scoped_store,
                **self._store_bound_callables(scoped_store),
            )
        return replace(self, repo_id=repo_id)

    @staticmethod
    def _is_postgres_admin_shutdown(error: BaseException) -> bool:
        """Return whether psycopg reported PostgreSQL's AdminShutdown SQLSTATE.

        Avoid importing psycopg into the standalone SQLite application path.
        Psycopg exposes the SQLSTATE on both the concrete error and compatible
        test/dialect exceptions, which is the stable recovery classification.
        """
        return getattr(error, "sqlstate", None) == _POSTGRES_ADMIN_SHUTDOWN_SQLSTATE

    @staticmethod
    def _can_retry_after_admin_shutdown(
        operation: str, context: InvocationContext
    ) -> bool:
        if operation.startswith("work.read.") or operation in _ADMIN_SHUTDOWN_READ_OPERATIONS:
            return True
        return (
            operation in _ADMIN_SHUTDOWN_IDEMPOTENT_OPERATIONS
            and getattr(context, "idempotency_requirement", None) == "required"
            and bool(getattr(context, "idempotency_key", None))
        )

    def _replace_admin_shutdown_connection(self, failed_connection: Any) -> bool:
        """Replace the shared runtime connection once, without exposing its DSN.

        A request-scoped ``PgStore`` is a dataclass copy that shares the root
        application's connection.  Updating the root store means the retry and
        later invocations both use the same fresh connection.  If a concurrent
        request already replaced it, the caller can retry without opening
        another connection.
        """
        factory = getattr(self.store, "connection_factory", None)
        if not callable(factory):
            self._mark_postgres_runtime_unavailable(failed_connection)
            return False
        with self._connection_recovery_lock:
            if getattr(self.store, "conn", None) is not failed_connection:
                self._postgres_runtime_available = getattr(self.store, "conn", None) is not None
                return self._postgres_runtime_available
            try:
                replacement = factory()
            except Exception:
                self._mark_postgres_runtime_unavailable(failed_connection)
                return False
            if replacement is None:
                self._mark_postgres_runtime_unavailable(failed_connection)
                return False
            previous = self.store.conn
            self.store.conn = replacement
            self._postgres_runtime_available = True
            try:
                previous.close()
            except Exception:
                pass
            return True

    def _mark_postgres_runtime_unavailable(self, failed_connection: Any) -> None:
        """Quarantine a terminated connection without replaying a command.

        A non-idempotent command has an unknown outcome after an administrative
        shutdown, so it must return rather than reconnect-and-replay.  Closing
        and clearing the shared connection prevents a later request from
        issuing a new command through a known-dead socket.  A later eligible
        read (or durable-idempotent command) can acquire a fresh connection
        before its handler begins; an unsafe mutation cannot.
        """
        with self._connection_recovery_lock:
            if getattr(self.store, "conn", None) is not failed_connection:
                return
            self.store.conn = None
            self._postgres_runtime_available = False
            if failed_connection is None:
                return
            try:
                failed_connection.close()
            except Exception:
                pass

    def served_runtime_ready(self) -> bool:
        """Whether the essential served PostgreSQL runtime is usable.

        Service composition can use this boolean for its readiness probe.  It
        becomes false whenever the shared runtime connection is quarantined;
        it becomes true only after a replacement was established successfully.
        Local SQLite and test-only applications retain their initial true
        state because they never enter PostgreSQL shutdown recovery.
        """
        with self._connection_recovery_lock:
            return self._postgres_runtime_available

    def _ensure_postgres_runtime_available(
        self, operation: str, context: InvocationContext
    ) -> bool:
        """Acquire a replacement before an eligible handler sees ``conn=None``."""
        if self.served_runtime_ready():
            return True
        if not self._can_retry_after_admin_shutdown(operation, context):
            return False
        return self._replace_admin_shutdown_connection(None)

    def _admin_shutdown_unavailable(self) -> ApplicationRejection:
        return ApplicationRejection(
            "postgres-runtime-unavailable",
            "served PostgreSQL runtime is unavailable after administrative shutdown; retry an eligible read or the exact idempotent command after readiness recovers",
            503,
        )

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        context: InvocationContext,
        *,
        _admin_shutdown_retry: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ApplicationRejection(
                "invalid-arguments", "operation arguments must be an object", 422
            )
        # The server has already authorized context.repo_id against the
        # caller's identity before invoke() runs (vuoro_service.app._dispatch
        # + Identity.authorizes_repo) -- this only needs a value to scope to.
        # A context with no repo_id at all (every existing protocol-v1-only
        # test double, and any caller built before the envelope field
        # existed) falls back to the application's own construction-time
        # repo_id, preserving today's single-tenant behavior exactly.
        requested_repo_id = getattr(context, "repo_id", None) or self.repo_id
        if not requested_repo_id:
            raise ApplicationRejection(
                "repo-id-required",
                "identity is not bound to a repository",
                403,
            )
        if not self._ensure_postgres_runtime_available(operation, context):
            raise self._admin_shutdown_unavailable()
        target = self._scoped_for(requested_repo_id)
        handlers = {
            "work.identity.current": target._identity_current,
            "work.read.sprints": target._read_sprints,
            "work.read.item": target._read_item,
            "work.read.items": target._read_items,
            "work.read.claims": target._read_claims,
            "work.read.claim": target._read_claim,
            "work.read.context": target._read_context,
            "work.read.context-candidates": target._read_context_candidates,
            "work.read.handoff": target._read_handoff,
            "work.read.next-work": target._read_next_work,
            "work.read.next-work-explain": target._read_next_work_explain,
            "work.read.records": target._read_records,
            "work.read.decisions": target._read_decisions,
            "work.read.events": target._read_events,
            "work.read.sprint": target._read_sprint,
            "work.read.sprint-detail": target._read_sprint_detail,
            "work.maintain.check": target._maintain_check,
            "work.read.maintenance-capability": target._maintenance_get,
            "work.maintenance.prepare": target._maintenance_prepare,
            "work.maintenance.transition": target._maintenance_transition,
            "work.maintenance.recovery-record": target._maintenance_recovery_append,
            "work.maintenance.resource.prepare": target._maintenance_resource_prepare,
            "work.maintenance.resource.get": target._maintenance_resource_get,
            "work.maintenance.resource.changes": target._maintenance_resource_changes,
            "work.sprint.create": target._sprint_create,
            "work.event.add": target._event_add,
            "work.handoff.record": target._handoff_record,
            "work.item.create": target._item_create,
            "work.item.edit": target._item_edit,
            "work.item.ref.add": target._item_ref_add,
            "work.item.ref.remove": target._item_ref_remove,
            "work.item.dep.add": target._item_dep_add,
            "work.item.dep.remove": target._item_dep_remove,
            "work.claim.start": target._claim_start,
            "work.claim.context": target._claim_context,
            "work.claim.arbitrate": target._claim_arbitrate,
            "work.lifecycle.arbitrate": target._lifecycle_arbitrate,
            "work.evidence.ingest": target._evidence_ingest,
            "work.item.note": target._item_note,
            "work.batch.apply": target._batch_apply,
        }
        try:
            handler = handlers[operation]
        except KeyError as exc:
            raise ApplicationRejection(
                "unknown-work-operation", f"unknown work operation: {operation}", 404
            ) from exc
        try:
            return handler(dict(arguments), context)
        except ApplicationRejection:
            raise
        except StaleCapabilityRevision as exc:
            raise ApplicationRejection(
                "maintenance-revision-conflict", str(exc), 409
            ) from exc
        except MaintenanceCapabilityError as exc:
            raise ApplicationRejection(
                "maintenance-capability-rejected", str(exc), 422
            ) from exc
        except ValueError as exc:
            raise ApplicationRejection("validation-failed", str(exc), 422) from exc
        except Exception as exc:
            if not self._is_postgres_admin_shutdown(exc):
                raise
            if _admin_shutdown_retry or not self._can_retry_after_admin_shutdown(
                operation, context
            ):
                self._mark_postgres_runtime_unavailable(
                    getattr(target.store, "conn", None)
                )
                raise self._admin_shutdown_unavailable() from exc
            if not self._replace_admin_shutdown_connection(
                getattr(target.store, "conn", None)
            ):
                raise self._admin_shutdown_unavailable() from exc
            return self.invoke(
                operation,
                arguments,
                context,
                _admin_shutdown_retry=True,
            )

    def _identity_current(
        self, _arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Return the authenticated work actor without exposing credentials."""
        return {"repo_id": self.repo_id, "actor": context.identity.actor}

    def _read_sprints(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        active_only = bool(arguments.get("active_only", False))
        rows = (
            self.backend.list_active_sprints(self.store)
            if active_only
            else self.backend.list_sprints(self.store)
        )
        if not active_only:
            kinds = {"active_sprint"}
            if arguments.get("include_backlog", False):
                kinds.add("backlog")
            if arguments.get("include_archive", False):
                kinds.add("archive")
            rows = [row for row in rows if row.get("kind", "active_sprint") in kinds]
        return {"repo_id": self.repo_id, "sprints": rows}

    def _read_item(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        current = self.backend.get_work_item_with_edit_revision(self.store, item_id)
        if current is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )
        item, edit_revision = current
        return {
            "repo_id": self.repo_id,
            "item": {**item, "edit_revision": edit_revision},
            "events": [
                event
                for event in self.backend.list_events(self.store, item["sprint_id"])
                if event.get("work_item_id") == item_id
            ],
            "active_claims": self.backend.list_claims(
                self.store, item_id, active_only=True
            ),
            "refs": self.backend.list_refs(self.store, item_id),
            "deps": {
                "blocked_by": self.backend.list_deps_blocking(self.store, item_id),
                "blocks": self.backend.list_deps_blocked_by(self.store, item_id),
            },
        }

    def _read_items(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        sprint_id = _optional_positive_int(arguments.get("sprint_id"), "sprint_id")
        track_name = _optional_text(arguments.get("track_name"), "track_name")
        status = _optional_text(arguments.get("status"), "status")
        if status is not None and status not in {"pending", "active", "done", "blocked"}:
            raise ApplicationRejection("invalid-arguments", "status must be pending, active, done, or blocked", 422)
        return {"repo_id": self.repo_id, "items": self.backend.list_work_items(
            self.store, sprint_id=sprint_id, track_name=track_name, status=status
        )}

    def _read_claims(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _optional_positive_int(arguments.get("item_id"), "item_id")
        sprint_id = _optional_positive_int(arguments.get("sprint_id"), "sprint_id")
        if item_id is not None and sprint_id is not None:
            raise ApplicationRejection("invalid-arguments", "provide at most one of item_id or sprint_id", 422)
        instance_id = _optional_text(arguments.get("instance_id"), "instance_id")
        runtime_session_id = _optional_text(arguments.get("runtime_session_id"), "runtime_session_id")
        hostname = _optional_text(arguments.get("hostname"), "hostname")
        pid = _optional_positive_int(arguments.get("pid"), "pid")
        if hostname is None and pid is not None:
            raise ApplicationRejection("invalid-arguments", "pid requires hostname", 422)
        active_only = bool(arguments.get("active_only", True))
        identity_query = instance_id or runtime_session_id or hostname
        if identity_query:
            # Domain backend owns canonical (AND-composed) identity matching
            # and intentionally searches the entire repository for resume.
            claims = self.backend.find_claim_by_identity(
                self.store, instance_id=instance_id, runtime_session_id=runtime_session_id,
                hostname=hostname, pid=pid, active_only=active_only,
            )
            if item_id is not None:
                claims = [claim for claim in claims if claim["work_item_id"] == item_id]
            if sprint_id is not None:
                if self.backend.get_sprint(self.store, sprint_id) is None:
                    raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
                item_ids = {item["id"] for item in self.backend.list_work_items(self.store, sprint_id=sprint_id)}
                claims = [claim for claim in claims if claim["work_item_id"] in item_ids]
        elif item_id is not None:
            claims = self.backend.list_claims(self.store, item_id, active_only=active_only)
        elif sprint_id is not None:
            if self.backend.get_sprint(self.store, sprint_id) is None:
                raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
            claims = self.backend.list_claims_by_sprint(self.store, sprint_id, active_only=active_only)
        else:
            sprint = self._resolve_sprint(None)
            claims = self.backend.list_claims_by_sprint(self.store, sprint["id"], active_only=active_only)
        return {"repo_id": self.repo_id, "claims": claims}

    def _read_claim(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Return one claim's inspectable state, never its bearer proof."""
        claim_id = _positive_int(arguments.get("claim_id"), "claim_id")
        claim = self.backend.get_claim(self.store, claim_id, include_secret=False)
        if claim is None:
            raise ApplicationRejection("claim-not-found", f"Claim #{claim_id} not found", 404)
        # Backends must honour include_secret=False; keep this defensive
        # boundary so a serialization regression cannot publish a token.
        claim.pop("claim_token", None)
        return {"repo_id": self.repo_id, "claim": claim}

    def _read_context(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Return ContextContract v1 from one repeatable-read server snapshot.

        This intentionally returns the contract itself, with no transport
        envelope fields: ``usage --context --json`` has a frozen top-level
        shape.  PostgreSQL is the production served backend; the transaction
        makes the several domain reads that feed the aggregate observe one
        point in time instead of exposing a client-composed partial result.
        """
        now = datetime.now(timezone.utc)
        snapshot = getattr(self.backend, "repeatable_read_snapshot", None)
        if callable(snapshot):
            # Never alter the service's shared connection: a prior invocation
            # may have started its implicit non-autocommit transaction.
            with snapshot(self.store) as snapshot_store:
                snapshot_app = replace(self, store=snapshot_store)
                return context_contract.build_context_contract(
                    snapshot_store,
                    snapshot_app._resolve_sprint(arguments.get("sprint_id")),
                    now,
                    backend=self.backend,
                )
        return context_contract.build_context_contract(
            self.store, self._resolve_sprint(arguments.get("sprint_id")), now,
            backend=self.backend,
        )

    def _read_context_candidates(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        """Build the bounded, read-only Tier-1 dispatch packet at the authority."""
        sprint = self._resolve_sprint(arguments.get("sprint_id"))
        explicit_item_id = _optional_positive_int(arguments.get("item_id"), "item_id")
        raw_paths = arguments.get("target_paths", [])
        if not isinstance(raw_paths, list) or any(
            not isinstance(path, str) or not path for path in raw_paths
        ):
            raise ApplicationRejection(
                "invalid-arguments", "target_paths must be an array of non-empty strings", 422
            )
        query = _optional_text(arguments.get("query"), "query")
        limit = _positive_int(
            arguments.get("limit", context_candidates.DEFAULT_CANDIDATE_LIMIT), "limit"
        )
        ready_items = self.backend.get_ready_items(self.store, sprint["id"])
        refs_by_item = self.backend.list_refs_for_items(
            self.store, [item["id"] for item in ready_items]
        )
        explicit_item = (
            self.backend.get_work_item(self.store, explicit_item_id)
            if explicit_item_id is not None
            else None
        )
        payload = context_candidates.build_context_candidates(
            ready_items=ready_items,
            refs_by_item=refs_by_item,
            explicit_item_id=explicit_item_id,
            explicit_item=explicit_item,
            target_paths=raw_paths,
            query=query,
            limit=limit,
            watermark=None,
        )
        payload["sprint"] = {"id": sprint["id"], "name": sprint["name"]}
        payload["projection"] = {
            "enabled": False,
            "source": "backend",
            "fallback_reason": "served-authority",
            "watermark_offset": None,
            "watermark_age_seconds": None,
            "schema_version": None,
        }
        return payload

    def _maintain_check(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Return the owning maintenance diagnostic from one server snapshot."""
        now = datetime.now(timezone.utc)

        def build(store: Any) -> dict[str, Any]:
            snapshot_app = replace(self, store=store)
            report = maintain.check(
                store,
                snapshot_app._resolve_sprint(arguments.get("sprint_id"))["id"],
                now,
                _m=self.backend,
            )
            pending_threshold = report["pending_threshold"]
            return {
                "repo_id": self.repo_id,
                "sprint": report["sprint"],
                "risk": report["risk"],
                "stale_items": report["stale_items"],
                "track_health": report["track_health"],
                "findings": report["findings"],
                "threshold_hours": report["threshold"].total_seconds() / 3600,
                "pending_threshold_hours": (
                    pending_threshold.total_seconds() / 3600
                    if pending_threshold is not None
                    else None
                ),
            }

        snapshot = getattr(self.backend, "repeatable_read_snapshot", None)
        if callable(snapshot):
            with snapshot(self.store) as snapshot_store:
                return build(snapshot_store)
        return build(self.store)

    def _maintenance_store(self) -> Any:
        """Bind the owner lifecycle to this invocation's repository scope."""
        if hasattr(self.store, "repo_id") and hasattr(self.store, "conn"):
            return PostgresMaintenanceCapabilityStore(self.store)
        return SQLiteMaintenanceCapabilityStore(self.store)

    def _maintenance_resource_store(self) -> MaintenanceResourceStore:
        return MaintenanceResourceStore(self._maintenance_store())

    def maintenance_resource_schema_available(self) -> bool:
        """Gate catalog publication on the installed owner-storage release."""
        if hasattr(self.store, "repo_id"):
            return int(getattr(self.store, "remote_schema_version", 0) or 0) >= 7
        if self.store is None or not hasattr(self.store, "execute"):
            return False
        row = self.store.execute("SELECT version FROM schema_version").fetchone()
        return bool(row and int(row[0]) >= 17 and MaintenanceResourceStore.schema_exists(self._maintenance_store()))

    @staticmethod
    def _maintenance_request_identity(
        context: InvocationContext, request_id: Any
    ) -> str:
        if not isinstance(request_id, str) or not request_id:
            raise ApplicationRejection(
                "invalid-arguments", "request_id must be a non-empty string", 422
            )
        if context.idempotency_key != request_id:
            raise ApplicationRejection(
                "idempotency-mismatch",
                "idempotency_key must exactly equal the maintenance request_id",
                409,
            )
        return request_id

    @staticmethod
    def _maintenance_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _maintenance_get(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        capability_id = _optional_text(arguments.get("capability_id"), "capability_id")
        if capability_id is None:
            raise ApplicationRejection(
                "invalid-arguments", "capability_id is required", 422
            )
        row = self._maintenance_store().get(capability_id)
        if row is None:
            raise ApplicationRejection(
                "maintenance-capability-not-found",
                "unknown maintenance capability",
                404,
            )
        public_fields = (
            "capability_id", "envelope_id", "envelope_digest", "plan_ref",
            "operator_identity", "not_before", "expires_at", "state",
            "revision", "next_sequence", "created_at", "updated_at",
        )
        capability = {
            field: (
                value.isoformat().replace("+00:00", "Z")
                if isinstance((value := row.get(field)), datetime)
                else value
            )
            for field in public_fields
        }
        return {"repo_id": self.repo_id, "capability": capability}

    def _maintenance_prepare(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        request_id = self._maintenance_request_identity(context, context.request_id)
        envelope = arguments.get("envelope")
        if not isinstance(envelope, Mapping):
            raise ApplicationRejection(
                "invalid-arguments", "envelope must be an object", 422
            )
        operator = envelope.get("operator")
        if not isinstance(operator, Mapping) or operator.get("identity") != context.identity.actor:
            raise ApplicationRejection(
                "maintenance-actor-mismatch",
                "authenticated actor must equal the frozen envelope operator",
                403,
            )
        result = self._maintenance_store().prepare(
            capability_id=arguments.get("capability_id"),
            request_id=request_id,
            envelope=envelope,
            actor=context.identity.actor,
            at=self._maintenance_now(),
        )
        return {"repo_id": self.repo_id, **result}

    def _maintenance_transition(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        request_id = self._maintenance_request_identity(context, context.request_id)
        result = self._maintenance_store().transition(
            capability_id=arguments.get("capability_id"),
            request_id=request_id,
            action=arguments.get("action"),
            expected_revision=arguments.get("expected_revision"),
            actor=context.identity.actor,
            at=self._maintenance_now(),
            step_id=arguments.get("step_id"),
            command_id=arguments.get("command_id"),
            command_ref=arguments.get("command_ref"),
            effect_ref=arguments.get("effect_ref"),
            reconciliation=arguments.get("reconciliation"),
        )
        return {"repo_id": self.repo_id, **result}

    def _maintenance_resource_prepare(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        request_id = self._maintenance_request_identity(context, context.request_id)
        envelope = arguments.get("envelope")
        if not isinstance(envelope, Mapping):
            raise ApplicationRejection("invalid-arguments", "envelope must be an object", 422)
        operator = envelope.get("operator")
        if not isinstance(operator, Mapping) or operator.get("identity") != context.identity.actor:
            raise ApplicationRejection("maintenance-actor-mismatch", "authenticated actor must equal the frozen envelope operator", 403)
        result = self._maintenance_store().prepare(
            capability_id=arguments.get("capability_id"), request_id=request_id,
            envelope=envelope, actor=context.identity.actor,
            at=self._maintenance_now(), resource=True,
        )
        return {"repo_id": self.repo_id, **result}

    def maintenance_resource_reference(self, result: dict[str, Any]) -> dict[str, Any]:
        """Owner decoder registered at Vuoro's service composition boundary."""
        return self._maintenance_resource_store().reference_envelope(result["capability_id"])

    def maintenance_resource_visible(self, resource_ref: Any, *, authorized: bool) -> bool:
        """Owner half of Vuoro's frozen non-disclosing visibility guard."""
        return self._maintenance_resource_store().visible(resource_ref, authorized=authorized)

    def _maintenance_resource_get(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        try:
            return self._maintenance_resource_store().snapshot(arguments.get("resource_ref"))
        except ResourceNotFound as error:
            raise ApplicationRejection("resource_not_found", "resource not found", 404) from error

    def _maintenance_resource_changes(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        try:
            return self._maintenance_resource_store().changes(
                arguments.get("resource_ref"), arguments.get("cursor"), arguments.get("wait_seconds", 0)
            )
        except ResourceNotFound as error:
            raise ApplicationRejection("resource_not_found", "resource not found", 404) from error
        except CursorExpired as error:
            raise ApplicationRejection("cursor_expired", "fetch a fresh snapshot", 409) from error
        except ValueError as error:
            raise ApplicationRejection("invalid_wait", str(error), 400) from error

    def _maintenance_recovery_append(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        record_id = self._maintenance_request_identity(context, context.request_id)
        result = self._maintenance_store().append_recovery_record(
            capability_id=arguments.get("capability_id"),
            record_id=record_id,
            kind=arguments.get("kind"),
            payload_ref=arguments.get("payload_ref"),
            actor=context.identity.actor,
            at=self._maintenance_now(),
        )
        return {"repo_id": self.repo_id, **result}

    def _read_handoff(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        events_limit = _positive_int(arguments.get("events_limit"), "events_limit")
        if events_limit > 500:
            raise ApplicationRejection("invalid-arguments", "events_limit must be at most 500", 422)
        git_context = arguments.get("git_context")
        if git_context is not None and not isinstance(git_context, dict):
            raise ApplicationRejection("invalid-arguments", "git_context must be an object or null", 422)
        sprint = self._resolve_sprint(arguments.get("sprint_id"))
        return handoff.build_handoff_bundle(self.store, sprint, events_limit, backend=self.backend, version=__import__("sprintctl").__version__, git_context=git_context)

    def _handoff_record(self, arguments: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        if self.backend.get_sprint(self.store, sprint_id) is None:
            raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
        bundle = arguments.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("bundle_type") != "handoff" or bundle.get("bundle_version") != "1":
            raise ApplicationRejection("invalid-arguments", "bundle must be a HandoffBundle v1", 422)
        if bundle.get("sprint", {}).get("id") != sprint_id:
            raise ApplicationRejection("invalid-arguments", "bundle sprint must match sprint_id", 422)
        event_id = handoff.record_handoff_generated(self.store, sprint_id, bundle, backend=self.backend, actor=context.identity.actor)
        return {"event_id": event_id, "sprint_id": sprint_id, "actor": context.identity.actor}

    def _item_ref_add(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        ref_type = _optional_text(arguments.get("ref_type"), "ref_type")
        url = _optional_text(arguments.get("url"), "url")
        label = arguments.get("label", "")
        if not ref_type or not url or not isinstance(label, str):
            raise ApplicationRejection("invalid-arguments", "item_id, ref_type, url, and string label are required", 422)
        try:
            ref_id = self.backend.add_ref(self.store, item_id, ref_type, url, label)
        except ValueError as exc:
            raise ApplicationRejection("ref-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "ref_id": ref_id}

    def _item_ref_remove(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        ref_id = _positive_int(arguments.get("ref_id"), "ref_id")
        try:
            self.backend.remove_ref(self.store, ref_id, item_id)
        except ValueError as exc:
            raise ApplicationRejection("ref-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "ref_id": ref_id}

    def _item_dep_add(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        blocked_item_id = _positive_int(arguments.get("blocked_item_id"), "blocked_item_id")
        try:
            dep_id = self.backend.add_dep(self.store, item_id, blocked_item_id)
        except ValueError as exc:
            raise ApplicationRejection("dependency-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "dep_id": dep_id}

    def _item_dep_remove(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        dep_id = _positive_int(arguments.get("dep_id"), "dep_id")
        try:
            self.backend.remove_dep(self.store, dep_id, item_id)
        except ValueError as exc:
            raise ApplicationRejection("dependency-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "dep_id": dep_id}

    def _read_events(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        sprint = self.backend.get_sprint(self.store, sprint_id)
        if sprint is None:
            raise ApplicationRejection(
                "sprint-not-found", f"Sprint #{sprint_id} not found", 404
            )
        work_item_id = _optional_positive_int(
            arguments.get("work_item_id"), "work_item_id"
        )
        events = self.backend.list_events(self.store, sprint_id)
        if work_item_id is not None:
            events = [
                event for event in events if event.get("work_item_id") == work_item_id
            ]
        after, limit = _pagination(arguments)
        if after:
            events = events[after:]
        if limit is not None:
            events = events[:limit]
        return {"repo_id": self.repo_id, "events": events}

    def _read_sprint(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        return {"repo_id": self.repo_id, "sprint": self._resolve_sprint(arguments.get("sprint_id"))}

    def _read_sprint_detail(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        """Build the complete detail view within one server-side snapshot."""
        now = datetime.now(timezone.utc)
        snapshot = getattr(self.backend, "repeatable_read_snapshot", None)
        if callable(snapshot):
            # A request may follow an unrelated read on the shared service
            # connection.  Use a sibling read-only repeatable snapshot, just
            # like ``work.read.context``, rather than reconfiguring it.
            with snapshot(self.store) as snapshot_store:
                snapshot_app = replace(self, store=snapshot_store)
                sprint = snapshot_app._resolve_sprint(arguments.get("sprint_id"))
                return {
                    "repo_id": self.repo_id,
                    "sprint": sprint_detail.build_sprint_show_detail(
                        snapshot_store, sprint, backend=self.backend, now=now
                    ),
                }
        sprint = self._resolve_sprint(arguments.get("sprint_id"))
        return {
            "repo_id": self.repo_id,
            "sprint": sprint_detail.build_sprint_show_detail(
                self.store, sprint, backend=self.backend, now=now
            ),
        }

    def _sprint_create(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Create a sprint inside the authenticated repository scope."""
        name = _optional_text(arguments.get("name"), "name")
        goal = arguments.get("goal", "")
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")
        status = arguments.get("status", "planned")
        kind = arguments.get("kind", "active_sprint")
        if not name or not isinstance(goal, str):
            raise ApplicationRejection("invalid-arguments", "name and string goal are required", 422)
        if start_date is not None and not isinstance(start_date, str):
            raise ApplicationRejection("invalid-arguments", "start_date must be a string or null", 422)
        if end_date is not None and not isinstance(end_date, str):
            raise ApplicationRejection("invalid-arguments", "end_date must be a string or null", 422)
        if status not in {"planned", "active", "closed"}:
            raise ApplicationRejection("invalid-arguments", "status must be planned, active, or closed", 422)
        if kind not in {"active_sprint", "backlog", "archive"}:
            raise ApplicationRejection("invalid-arguments", "kind must be active_sprint, backlog, or archive", 422)
        try:
            sprint_id = self.backend.create_sprint(
                self.store, name, goal, start_date, end_date, status, kind=kind
            )
        except ValueError as exc:
            raise ApplicationRejection("sprint-create-rejected", str(exc), 422) from exc
        sprint = self.backend.get_sprint(self.store, sprint_id)
        if sprint is None:  # pragma: no cover - backend postcondition
            raise ApplicationRejection("sprint-create-failed", "created sprint could not be read back", 500)
        return {"repo_id": self.repo_id, "sprint": sprint}

    def _event_add(self, arguments: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
        """Synchronously create a generic event as the authenticated actor."""
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        event_type = _optional_text(arguments.get("event_type"), "event_type")
        if not event_type:
            raise ApplicationRejection("invalid-arguments", "event_type is required", 422)
        if self.backend.get_sprint(self.store, sprint_id) is None:
            raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
        work_item_id = _optional_positive_int(arguments.get("work_item_id"), "work_item_id")
        if work_item_id is not None and self.backend.get_work_item(self.store, work_item_id) is None:
            raise ApplicationRejection("item-not-found", f"Work item #{work_item_id} not found", 404)
        source_type = arguments.get("source_type", "actor")
        if source_type not in {"actor", "daemon", "system"}:
            raise ApplicationRejection("invalid-arguments", "source_type must be actor, daemon, or system", 422)
        payload = arguments.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise ApplicationRejection("invalid-arguments", "payload must be an object or null", 422)
        try:
            event_id = self.backend.create_event(
                self.store, sprint_id, actor=context.identity.actor, event_type=event_type,
                source_type=source_type, work_item_id=work_item_id, payload=payload,
                expected_project=self.repo_id,
            )
        except ValueError as exc:
            raise ApplicationRejection("event-rejected", str(exc)) from exc
        return {"event_id": event_id, "sprint_id": sprint_id, "item_id": work_item_id,
                "type": event_type, "actor": context.identity.actor, "source": source_type}

    def _item_create(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Create an item and resolve its track in the server-side repository scope."""
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        track_name = _optional_text(arguments.get("track_name"), "track_name")
        title = _optional_text(arguments.get("title"), "title")
        if not track_name or not title:
            raise ApplicationRejection("invalid-arguments", "track_name and title are required", 422)
        if self.backend.get_sprint(self.store, sprint_id) is None:
            raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
        description = arguments.get("description")
        if description is not None:
            try:
                db.validate_work_item_description(description)
            except ValueError as exc:
                raise ApplicationRejection("invalid-arguments", str(exc), 422) from exc
        assignee = arguments.get("assignee")
        if assignee is not None and not isinstance(assignee, str):
            raise ApplicationRejection("invalid-arguments", "assignee must be a string or null", 422)
        priority = arguments.get("priority")
        try:
            db.validate_priority(priority)
            track_id = self.backend.get_or_create_track(self.store, sprint_id, track_name)
            item_id = self.backend.create_work_item(self.store, sprint_id, track_id, title,
                description=description or "", assignee=assignee, priority=priority)
        except ValueError as exc:
            raise ApplicationRejection("item-create-rejected", str(exc)) from exc
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:  # pragma: no cover - backend postcondition
            raise ApplicationRejection("item-create-failed", "created item could not be read back", 500)
        return {"item": item, "track_name": track_name}

    def _item_edit(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """CAS-edit an item and append an audit event as the authenticated actor."""
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        description = arguments.get("description")
        try:
            db.validate_work_item_description(description)
        except ValueError as exc:
            raise ApplicationRejection("invalid-arguments", str(exc), 422) from exc
        expected_revision = _optional_text(
            arguments.get("expected_revision"), "expected_revision"
        )
        if not expected_revision:
            raise ApplicationRejection(
                "invalid-arguments", "expected_revision is required", 422
            )
        try:
            db.validate_item_edit_revision(expected_revision)
        except ValueError as exc:
            raise ApplicationRejection("invalid-arguments", str(exc), 422) from exc
        if self.backend.get_work_item(self.store, item_id) is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )
        try:
            result = self.backend.update_work_item_description(
                self.store,
                item_id,
                description,
                expected_revision=expected_revision,
                actor=context.identity.actor,
            )
        except db.EditConflict as exc:
            raise ApplicationRejection("item-edit-conflict", str(exc), 409) from exc
        except ValueError as exc:
            raise ApplicationRejection("item-edit-rejected", str(exc), 422) from exc
        return {
            "repo_id": self.repo_id,
            "item_id": item_id,
            "actor": context.identity.actor,
            **result,
        }

    def _resolve_sprint(
        self, requested: Any, *, prefer_backlog: bool = False
    ) -> dict[str, Any]:
        if requested is not None:
            sprint_id = _positive_int(requested, "sprint_id")
            sprint = self.backend.get_sprint(self.store, sprint_id)
            if sprint is None:
                raise ApplicationRejection(
                    "sprint-not-found", f"Sprint #{sprint_id} not found", 404
                )
            return sprint
        if prefer_backlog:
            backlog = [
                row
                for row in self.backend.list_sprints(self.store)
                if row.get("kind") == "backlog" and row.get("status") != "closed"
            ]
            if len(backlog) == 1:
                return backlog[0]
            if len(backlog) > 1:
                raise ApplicationRejection(
                    "ambiguous-sprint", "multiple open backlog sprints are available"
                )
        active = self.backend.get_active_sprint(self.store)
        if active is None:
            raise ApplicationRejection(
                "sprint-not-found", "no active sprint found", 404
            )
        return active

    def _read_next_work(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        return self.next_work(arguments.get("sprint_id"))

    def _read_next_work_explain(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        """Return the complete, server-assembled next-work explain contract.

        This is intentionally one authority operation.  A served CLI must not
        reproduce this aggregate by opening a local store or by making a
        sequence of independently-versioned read calls.
        """
        sprint = self._resolve_sprint(arguments.get("sprint_id"))
        return _next_work_explain_contract(
            self.backend, self.store, sprint, repo_id=self.repo_id,
            now=datetime.now(timezone.utc),
        )

    def next_work(
        self, sprint_id: Any = None, *, prefer_backlog: bool = False
    ) -> dict[str, Any]:
        sprint = self._resolve_sprint(sprint_id, prefer_backlog=prefer_backlog)
        ready = self.backend.get_ready_items(self.store, sprint["id"])
        return {
            "repo_id": self.repo_id,
            "sprint": sprint,
            "ready_items": ready,
        }

    def _read_records(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        after, limit = _pagination(arguments)
        records = self.list_records(after, limit)
        # A ledger page may legitimately omit historic rows while the served
        # authority still retains sequence-admission cursors.  Expose those
        # cursors as read-only recovery evidence; never infer or mutate them
        # from a client-side outbox.
        stream_high_water: dict[str, int] = {}
        try:
            from . import pg

            if hasattr(self.store, "conn") and hasattr(self.store, "repo_id"):
                stream_high_water = pg.list_ingest_stream_high_water(self.store)
        except (AttributeError, TypeError):
            # Local/test application compositions have no PostgreSQL ingest
            # stream table.  Their existing records-only contract remains
            # valid with an empty cursor map.
            pass
        return {
            "repo_id": self.repo_id,
            "records": [
                {
                    "ingest_offset": int(value.ingest_offset),
                    "record": record_to_dict(value.record),
                }
                for value in records
            ],
            "stream_high_water": stream_high_water,
        }

    def _read_decisions(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        after, limit = _pagination(arguments)
        return {
            "repo_id": self.repo_id,
            "decisions": [
                _json_value(value) for value in self.list_decisions(after, limit)
            ],
        }

    def _claim_arbitrate(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        return self._arbitrate_one(arguments, context, CLAIM_COMMAND_TYPES)

    def _claim_start(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Create an execute claim and activate its item as one served flow.

        This mirrors the legacy ``claim start`` orchestration while remaining
        independent of Click.  The flow is deliberately not retry-safe: the
        catalog forbids an idempotency key, and durable callers should use an
        immutable ``claim.acquire`` command through ``work.claim.arbitrate``.
        """

        item_id = _positive_int(arguments.get("item_id"), "item_id")
        ttl_seconds = _positive_int(arguments.get("ttl_seconds", 300), "ttl_seconds")
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )

        actor = context.identity.actor
        runtime_session_id = (
            _optional_text(arguments.get("runtime_session_id"), "runtime_session_id")
            or os.environ.get("SPRINTCTL_RUNTIME_SESSION_ID")
            or os.environ.get("CODEX_THREAD_ID")
        )
        instance_id = (
            _optional_text(arguments.get("instance_id"), "instance_id")
            or os.environ.get("SPRINTCTL_INSTANCE_ID")
            or str(uuid4())
        )
        hostname = (
            _optional_text(arguments.get("hostname"), "hostname")
            or socket.gethostname()
        )
        pid = _optional_positive_int(arguments.get("pid"), "pid") or os.getpid()
        previous_status = item["status"]

        try:
            claim_id = self.backend.create_claim(
                self.store,
                work_item_id=item_id,
                agent=actor,
                claim_type="execute",
                exclusive=True,
                ttl_seconds=ttl_seconds,
                branch=_optional_text(arguments.get("branch"), "branch"),
                worktree_path=_optional_text(
                    arguments.get("worktree_path"), "worktree_path"
                ),
                commit_sha=_optional_text(arguments.get("commit_sha"), "commit_sha"),
                pr_ref=_optional_text(arguments.get("pr_ref"), "pr_ref"),
                runtime_session_id=runtime_session_id,
                instance_id=instance_id,
                hostname=hostname,
                pid=pid,
            )
        except ValueError as exc:
            raise ApplicationRejection("claim-start-rejected", str(exc)) from exc

        claim = self.backend.get_claim(self.store, claim_id, include_secret=True)
        if claim is None or not claim.get("claim_token"):
            raise ApplicationRejection(
                "claim-start-result-invalid",
                "created claim is unavailable or has no ownership proof",
                500,
            )

        transitioned = False
        if previous_status != "active":
            try:
                self.backend.set_work_item_status(
                    self.store,
                    item_id,
                    "active",
                    actor=actor,
                    claim_id=claim_id,
                    claim_token=claim["claim_token"],
                )
                transitioned = True
            except Exception as transition_error:
                try:
                    self.backend.release_claim(
                        self.store, claim_id, claim["claim_token"], actor=actor
                    )
                except Exception as release_error:
                    raise ApplicationRejection(
                        "claim-start-rollback-failed",
                        "claim was created, activation failed, and automatic release failed",
                        500,
                    ) from release_error
                raise ApplicationRejection(
                    "claim-start-transition-failed",
                    "claim was released after the item could not be moved to active",
                ) from transition_error

        updated_item = self.backend.get_work_item(self.store, item_id)
        if updated_item is None:
            raise ApplicationRejection(
                "claim-start-result-invalid",
                "claimed item is unavailable after claim start",
                500,
            )
        return {
            "operation": "claim_start",
            "claim_id": claim_id,
            "claim_token": claim["claim_token"],
            "claim": claim,
            "item_id": item_id,
            "item_status_before": previous_status,
            "item_status_after": updated_item["status"],
            "status_transition_applied": transitioned,
            "refs": self.backend.list_refs(self.store, item_id),
        }

    def _claim_context(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Non-secret authority context a served client needs to construct a
        canonical claim command without database access (``work:claim``
        read).

        Returns exactly the "Approved authority-context contract" fields:
        the resolved authenticated actor, Sprintctl's ``repo_id`` plus the
        authority repository UUID, the current non-secret claim snapshot
        (including ``work_item_id``), and the canonical current
        ``claim_revision``.  Never a claim token, a proof digest, another
        identity's bearer credential, or a database DSN.  A missing or
        inaccessible claim is rejected before any producer/outbox record
        could be created -- this handler is read-only.
        """

        from . import authority  # Lazy: standalone SQLite needs no psycopg.

        claim_id = _positive_int(arguments.get("claim_id"), "claim_id")
        claim = self.backend.get_claim(self.store, claim_id, include_secret=False)
        if claim is None:
            raise ApplicationRejection(
                "claim-not-found", f"Claim #{claim_id} not found", 404
            )
        return {
            "repo_id": self.repo_id,
            "authority_repo_uuid": getattr(self.store, "authority_repo_uuid", None),
            "actor": context.identity.actor,
            "claim": claim,
            "claim_revision": authority.claim_revision(claim),
        }

    def _lifecycle_arbitrate(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        return self._arbitrate_one(arguments, context, LIFECYCLE_COMMAND_TYPES)

    def _arbitrate_one(
        self,
        arguments: dict[str, Any],
        context: InvocationContext,
        allowed_types: frozenset[str],
    ) -> dict[str, Any]:
        record = record_from_dict(_required_mapping(arguments.get("record"), "record"))
        record = self._validate_record(record, context, allowed_types)
        if context.basis_revision != record.basis_revision:
            raise ApplicationRejection(
                "basis-revision-mismatch",
                "invocation basis revision must equal the command basis revision",
                422,
            )
        if context.idempotency_key != record.event_id:
            raise ApplicationRejection(
                "idempotency-key-mismatch",
                "idempotency key must equal the immutable command event_id",
                422,
            )
        credentials = self._credentials(context, record)
        return _json_value(
            self.arbitrate_command(record, credentials, context.identity.actor)
        )

    def _evidence_ingest(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        records = self._records(arguments, context, OBSERVATION_TYPES)
        self._require_batch_key(records, context)
        results = self.ingest_records(records)
        return {
            "repo_id": self.repo_id,
            "results": [_ingest_result(value) for value in results],
        }

    def _item_note(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Record a structured note event on a work item (``item note``).

        Unlike ``work.evidence.ingest``, this is a direct, synchronous write
        (mirrors the local CLI's ``create_event`` call) rather than a durable
        outbox-producer record -- ``item note`` has no local outbox/retry
        semantics either, so this does not invent any for the served path.
        The recording actor is always the authenticated identity, never a
        client-supplied argument, matching ``work.claim.start``.
        """

        item_id = _positive_int(arguments.get("item_id"), "item_id")
        note_type = _optional_text(arguments.get("note_type"), "note_type")
        summary = _optional_text(arguments.get("summary"), "summary")
        if not note_type or not summary:
            raise ApplicationRejection(
                "invalid-arguments", "note_type and summary are required", 422
            )
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )
        payload: dict[str, Any] = {"summary": summary}
        detail = _optional_text(arguments.get("detail"), "detail")
        if detail:
            payload["detail"] = detail
        tags = arguments.get("tags")
        if tags:
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) and tag for tag in tags
            ):
                raise ApplicationRejection(
                    "invalid-arguments", "tags must be an array of non-empty strings", 422
                )
            payload["tags"] = list(tags)
        evidence_item_id = _optional_positive_int(
            arguments.get("evidence_item_id"), "evidence_item_id"
        )
        if evidence_item_id is not None:
            payload["evidence_item_id"] = evidence_item_id
        evidence_event_id = _optional_positive_int(
            arguments.get("evidence_event_id"), "evidence_event_id"
        )
        if evidence_event_id is not None:
            payload["evidence_event_id"] = evidence_event_id
        for field in ("git_branch", "git_sha", "git_worktree"):
            value = _optional_text(arguments.get(field), field)
            if value:
                payload[field] = value
        try:
            event_id = self.backend.create_event(
                self.store,
                item["sprint_id"],
                actor=context.identity.actor,
                event_type=note_type,
                source_type="actor",
                work_item_id=item_id,
                payload=payload,
            )
        except ValueError as exc:
            raise ApplicationRejection("note-rejected", str(exc)) from exc
        return {
            "event_id": event_id,
            "item_id": item_id,
            "note_type": note_type,
            "summary": summary,
        }

    def _batch_apply(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        # A command whose producer actor does not match this invocation must
        # reach authority arbitration so the authority can consume its origin
        # sequence with a durable rejection.  Ordinary one-command operations
        # remain fail-closed before the backend.
        records = self._records(
            arguments,
            context,
            SUPPORTED_BATCH_TYPES,
            allow_authority_actor_mismatch=True,
        )
        self._require_batch_key(records, context)
        return self.apply_records(records, context)

    def apply_records(
        self, records: Sequence[outbox.OutboxRecord], context: InvocationContext
    ) -> dict[str, Any]:
        """Apply records in producer order; identical retries reuse durable results."""

        results: list[dict[str, Any]] = []
        observations: list[outbox.OutboxRecord] = []

        def flush_observations() -> None:
            if not observations:
                return
            results.extend(
                _ingest_result(value) for value in self.ingest_records(observations)
            )
            observations.clear()

        for record in records:
            if record.record_class == contracts.RecordClass.OBSERVATION.value:
                observations.append(record)
                continue
            flush_observations()
            decision = self.arbitrate_command(
                record,
                self._credentials(context, record),
                context.identity.actor,
            )
            results.append(
                {
                    "kind": "decision",
                    "event_id": record.event_id,
                    **_json_value(decision),
                }
            )
        flush_observations()
        return {"repo_id": self.repo_id, "results": results}

    def _records(
        self,
        arguments: dict[str, Any],
        context: InvocationContext,
        allowed_types: frozenset[str],
        *,
        allow_authority_actor_mismatch: bool = False,
    ) -> list[outbox.OutboxRecord]:
        raw = arguments.get("records")
        if not isinstance(raw, list) or not raw:
            raise ApplicationRejection(
                "invalid-record-batch", "records must be a non-empty array", 422
            )
        records = [
            record_from_dict(_required_mapping(value, "record")) for value in raw
        ]
        return [
            self._validate_record(
                record,
                context,
                allowed_types,
                allow_authority_actor_mismatch=allow_authority_actor_mismatch,
            )
            for record in records
        ]

    def _validate_record(
        self,
        record: outbox.OutboxRecord,
        context: InvocationContext,
        allowed_types: frozenset[str],
        *,
        allow_authority_actor_mismatch: bool = False,
    ) -> outbox.OutboxRecord:
        if record.event_type not in allowed_types:
            raise ApplicationRejection(
                "record-type-not-allowed",
                f"record type {record.event_type!r} is not allowed by this operation",
                422,
            )
        expected_class = contracts.record_class_for_type(record.event_type).value
        if record.record_class != expected_class:
            raise ApplicationRejection(
                "record-class-mismatch",
                f"record type {record.event_type!r} must use class {expected_class!r}",
                422,
            )
        encoded_payload = json.dumps(
            record.payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if (
            hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest()
            != record.payload_sha256
        ):
            raise ApplicationRejection(
                "payload-digest-mismatch",
                "record payload digest does not match its canonical payload",
                422,
            )
        permit_actor_mismatch = (
            allow_authority_actor_mismatch
            and record.record_class == contracts.RecordClass.AUTHORITY_COMMAND.value
        )
        if record.actor != context.identity.actor and not permit_actor_mismatch:
            raise ApplicationRejection(
                "actor-mismatch",
                "record actor must match the authenticated identity",
                403,
            )
        if record.record_class == contracts.RecordClass.AUTHORITY_COMMAND.value:
            try:
                envelope = contracts.record_from_dict(record.payload)
            except (TypeError, ValueError) as exc:
                raise ApplicationRejection(
                    "invalid-command-envelope",
                    "record payload is not a valid authority-command envelope",
                    422,
                ) from exc
            if not isinstance(envelope, contracts.AuthorityCommand):
                raise ApplicationRejection(
                    "invalid-command-envelope",
                    "record payload must be an authority-command envelope",
                    422,
                )
            if envelope.to_dict() != record.payload:
                raise ApplicationRejection(
                    "noncanonical-command-envelope",
                    "authority-command envelope must use its canonical form",
                    422,
                )
            if envelope.actor != context.identity.actor and not permit_actor_mismatch:
                raise ApplicationRejection(
                    "actor-mismatch",
                    "outer record, command actor, and authenticated identity must match",
                    403,
                )
            if (
                envelope.record_type == "claim.acquire"
                and envelope.payload["agent"] != context.identity.actor
                and not permit_actor_mismatch
            ):
                raise ApplicationRejection(
                    "claim-agent-mismatch",
                    "claim agent must match the authenticated identity",
                    403,
                )
            if (
                envelope.event_id != record.event_id
                or envelope.record_type != record.event_type
                or envelope.basis_revision != record.basis_revision
                or envelope.correlation_id != record.correlation_id
                or envelope.causation_id != record.causation_id
                or envelope.authored_at != record.occurred_at
            ):
                raise ApplicationRejection(
                    "noncanonical-command-envelope",
                    "authority-command envelope differs from its outer record",
                    422,
                )
        if (
            record.record_class == contracts.RecordClass.AUTHORITY_COMMAND.value
            and context.basis_revision is not None
            and context.basis_revision != record.basis_revision
        ):
            raise ApplicationRejection(
                "basis-revision-mismatch",
                "invocation basis revision must equal each command basis revision",
                422,
            )
        return record

    def _require_batch_key(
        self, records: Sequence[outbox.OutboxRecord], context: InvocationContext
    ) -> None:
        if context.idempotency_key != batch_idempotency_key(records):
            raise ApplicationRejection(
                "idempotency-key-mismatch",
                "idempotency key must equal the canonical batch digest",
                422,
            )

    def _credentials(
        self, context: InvocationContext, record: outbox.OutboxRecord
    ) -> Mapping[str, str]:
        if self.credential_resolver is None:
            return {}
        resolved = self.credential_resolver(context, record)
        return dict(resolved or {})
