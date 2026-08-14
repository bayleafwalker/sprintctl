"""Project-scoped aggregate service.

The service composes one WorkApplication per authorized repository.
"""

from __future__ import annotations

from .application_common import *
from .work_application import WorkApplication


@dataclass(frozen=True, slots=True)
class ProjectMemberApplication:
    origin_repo: str
    application: WorkApplication


def _tag_project_context(payload: Mapping[str, Any], origin_repo: str) -> dict[str, Any]:
    """Add the local project's origin tag to every project-visible record."""
    tagged = dict(payload)
    tagged["sprint"] = {**payload["sprint"], "origin_repo": origin_repo}
    for key in (
        "active_reservations",
        "active_unreserved_items",
        "conflicts",
        "ready_items",
        "blocked_items",
        "stale_items",
        "recent_decisions",
    ):
        tagged[key] = [{**value, "origin_repo": origin_repo} for value in payload[key]]
    tagged["next_action"] = {**payload["next_action"], "origin_repo": origin_repo}
    return tagged


@dataclass(slots=True)
class ProjectWorkApplication:
    """Deterministic multi-repository work reads and ordered batch dispatch."""

    project_id: str
    members: tuple[ProjectMemberApplication, ...]
    # Supplied by the Vuoro composition from its canonical, authorized project
    # binding.  It is deliberately not derived from a CLI ``project.toml``.
    # Existing project next-work/batch callers predate this metadata; the new
    # aggregates fail closed until composition supplies it.
    canonical_binding: Mapping[str, Any] | None = None

    def invoke(
        self, operation: str, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        if operation == "work.project.next-work":
            return self._next_work(arguments, context, explain=False)
        if operation == "work.project.next-work-explain":
            return self._next_work(arguments, context, explain=True)
        if operation == "work.project.items":
            return self._items(arguments, context)
        if operation == "work.project.context":
            return self._context(arguments, context)
        if operation == "work.project.sprints":
            return self._sprints(arguments, context)
        if operation == "work.project.batch":
            return self._batch(arguments, context)
        raise ApplicationRejection(
            "unknown-work-operation", f"unknown work operation: {operation}", 404
        )

    def _next_work(
        self, arguments: Mapping[str, Any], context: InvocationContext, *, explain: bool
    ) -> dict[str, Any]:
            binding = self._binding()
            self._require_member_authorization(context)
            repositories = []
            ready_items = []
            for member in self.members:
                try:
                    payload = self._in_member_snapshot(
                        member,
                        lambda application: application.next_work(
                            arguments.get("sprint_id"), prefer_backlog=True
                        ),
                    )
                except Exception as error:
                    repositories.append(
                        {**self._unavailable(member, error), "status": "unavailable"}
                    )
                    continue
                tagged = [
                    {**item, "origin_repo": member.origin_repo}
                    for item in payload["ready_items"]
                ]
                ready_items.extend(tagged)
                repositories.append(
                    {
                        "origin_repo": member.origin_repo,
                        "status": "ok",
                        "sprint": {
                            **payload["sprint"],
                            "origin_repo": member.origin_repo,
                        },
                        "ready_items": tagged,
                        "graph_ready": payload["graph_ready"],
                        "dispatch_admissible": payload["dispatch_admissible"],
                        "dispatch_reason": payload["dispatch_reason"],
                    }
                )
            result = {
                "contract_version": "project-1",
                "project_id": self.project_id,
                "project": dict(binding),
                "ready_items": ready_items,
                "repositories": repositories,
            }
            unavailable = [row for row in repositories if row["status"] == "unavailable"]
            if unavailable:
                result["graph_ready"] = False
                result["dispatch_admissible"] = "unknown"
                result["dispatch_reason"] = "member-unavailable"
            elif ready_items:
                result["graph_ready"] = True
                result["dispatch_admissible"] = "admissible"
                result["dispatch_reason"] = "ready-items-available"
            else:
                result["graph_ready"] = False
                result["dispatch_admissible"] = "inadmissible"
                result["dispatch_reason"] = "no-ready-items"
            if explain:
                result["explanation"] = {
                    "canonical_member_order": [member.origin_repo for member in self.members],
                    "authorization_checked_before_member_reads": True,
                    "unavailable_members": [row["origin_repo"] for row in unavailable],
                }
            return result

    def _binding(self) -> Mapping[str, Any]:
        binding = self.canonical_binding
        if binding is None:
            raise ApplicationRejection(
                "canonical-project-binding-required",
                "project aggregate is unavailable because no canonical server-side project binding is configured",
                503,
            )
        if binding.get("project_id") != self.project_id:
            raise ApplicationRejection(
                "canonical-project-binding-invalid",
                "canonical server-side project binding does not match the configured project",
                503,
            )
        if binding.get("backlog_repos") != [member.origin_repo for member in self.members]:
            raise ApplicationRejection(
                "canonical-project-binding-invalid",
                "canonical server-side project members do not match the configured aggregate members",
                503,
            )
        return binding

    def _require_member_authorization(self, context: InvocationContext) -> None:
        """Establish every aggregate read scope before any member is read."""
        authorizes_repo = getattr(context.identity, "authorizes_repo", None)
        if not callable(authorizes_repo):
            raise ApplicationRejection(
                "project-member-authorization-required",
                "project aggregate requires an identity with per-member repository authorization",
                403,
            )
        denied = [
            member.origin_repo for member in self.members
            if not authorizes_repo(member.origin_repo)
        ]
        if denied:
            raise ApplicationRejection(
                "project-member-unauthorized",
                "identity is not authorized for every project member: " + ", ".join(denied),
                403,
            )

    @staticmethod
    def _unavailable(member: ProjectMemberApplication, error: Exception) -> dict[str, Any]:
        if isinstance(error, ApplicationRejection):
            return {
                "origin_repo": member.origin_repo,
                "reason_code": error.code,
                "message": error.message,
            }
        return {
            "origin_repo": member.origin_repo,
            "reason_code": "member-read-unavailable",
            "message": "member repository aggregate is unavailable",
        }

    @staticmethod
    def _in_member_snapshot(member: ProjectMemberApplication, callback: Callable[[WorkApplication], dict[str, Any]]) -> dict[str, Any]:
        """Evaluate one member inside its own repeatable-read transaction.

        A project spans independently-versioned repositories, so there is no
        truthful global snapshot.  Each member result is nevertheless a
        complete point-in-time aggregate, matching the single-repository
        served context guarantee.
        """
        application = member.application
        snapshot = getattr(application.backend, "repeatable_read_snapshot", None)
        if callable(snapshot):
            with snapshot(application.store) as snapshot_store:
                return callback(replace(application, store=snapshot_store))
        return callback(application)

    def _context(
        self, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        binding = self._binding()
        self._require_member_authorization(context)
        now = datetime.now(timezone.utc)
        snapshots: list[dict[str, Any]] = []
        repositories: list[dict[str, Any]] = []
        for member in self.members:
            try:
                def read(application: WorkApplication) -> dict[str, Any]:
                    sprint = application._resolve_sprint(
                        arguments.get("sprint_id"), prefer_backlog=True
                    )
                    return context_contract.build_context_contract(
                        application.store, sprint, now, backend=application.backend
                    )

                snapshot = self._in_member_snapshot(member, read)
            except Exception as error:  # one member must not erase usable peers
                repositories.append({**self._unavailable(member, error), "status": "unavailable"})
                continue
            tagged = _tag_project_context(snapshot, member.origin_repo)
            snapshots.append(tagged)
            repositories.append(
                {"origin_repo": member.origin_repo, "status": "ok", "context": tagged}
            )
        if not snapshots:
            raise ApplicationRejection(
                "project-scope-unavailable",
                "project scope has no resolvable member sprint",
                503,
            )
        summary_keys = (
            "total", "done", "active", "pending", "blocked", "stale", "ready",
            "waiting_on_dependencies", "active_reservations", "active_unreserved",
        )
        return {
            "contract_version": "project-1",
            "project": dict(binding),
            "summary": {key: sum(snapshot["summary"][key] for snapshot in snapshots) for key in summary_keys},
            "sprints": [snapshot["sprint"] for snapshot in snapshots],
            "active_reservations": [value for snapshot in snapshots for value in snapshot["active_reservations"]],
            "active_unreserved_items": [value for snapshot in snapshots for value in snapshot["active_unreserved_items"]],
            "conflicts": [value for snapshot in snapshots for value in snapshot["conflicts"]],
            "ready_items": [value for snapshot in snapshots for value in snapshot["ready_items"]],
            "blocked_items": [value for snapshot in snapshots for value in snapshot["blocked_items"]],
            "stale_items": [value for snapshot in snapshots for value in snapshot["stale_items"]],
            "recent_decisions": [value for snapshot in snapshots for value in snapshot["recent_decisions"]],
            "next_actions": [snapshot["next_action"] for snapshot in snapshots],
            "repositories": repositories,
        }

    def _sprints(
        self, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        binding = self._binding()
        self._require_member_authorization(context)
        sprints: list[dict[str, Any]] = []
        repositories: list[dict[str, Any]] = []
        for member in self.members:
            try:
                payload = self._in_member_snapshot(
                    member,
                    lambda application: application._read_sprints(dict(arguments), object()),
                )
            except Exception as error:  # retain ordered partial results
                repositories.append({**self._unavailable(member, error), "status": "unavailable"})
                continue
            tagged = [{**sprint, "origin_repo": member.origin_repo} for sprint in payload["sprints"]]
            sprints.extend(tagged)
            repositories.append(
                {"origin_repo": member.origin_repo, "status": "ok", "sprints": tagged}
            )
        return {
            "contract_version": "project-1",
            "project": dict(binding),
            "sprints": sprints,
            "repositories": repositories,
        }

    def _items(
        self, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        binding = self._binding()
        self._require_member_authorization(context)
        items: list[dict[str, Any]] = []
        repositories: list[dict[str, Any]] = []
        for member in self.members:
            try:
                payload = self._in_member_snapshot(
                    member,
                    lambda application: application._read_items(dict(arguments), object()),
                )
            except Exception as error:
                repositories.append(
                    {**self._unavailable(member, error), "status": "unavailable"}
                )
                continue
            tagged = [
                {**item, "origin_repo": member.origin_repo}
                for item in payload["items"]
            ]
            items.extend(tagged)
            repositories.append(
                {"origin_repo": member.origin_repo, "status": "ok", "items": tagged}
            )
        return {
            "contract_version": "project-1",
            "project": dict(binding),
            "items": items,
            "repositories": repositories,
        }

    def _batch(
        self, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        raw_units = arguments.get("units")
        if not isinstance(raw_units, list) or not raw_units:
            raise ApplicationRejection(
                "invalid-project-batch", "units must be a non-empty array", 422
            )
        by_repo = {member.origin_repo: member.application for member in self.members}
        units: list[tuple[str, list[outbox.OutboxRecord]]] = []
        seen: set[str] = set()
        for raw in raw_units:
            unit = _required_mapping(raw, "unit")
            origin_repo = unit.get("origin_repo")
            if not isinstance(origin_repo, str) or origin_repo not in by_repo:
                raise ApplicationRejection(
                    "unknown-project-member",
                    f"unknown project member: {origin_repo!r}",
                    422,
                )
            if origin_repo in seen:
                raise ApplicationRejection(
                    "duplicate-project-member",
                    f"project batch repeats member {origin_repo!r}",
                    422,
                )
            seen.add(origin_repo)
            raw_records = unit.get("records")
            if not isinstance(raw_records, list) or not raw_records:
                raise ApplicationRejection(
                    "invalid-record-batch",
                    "unit records must be a non-empty array",
                    422,
                )
            records = [
                record_from_dict(_required_mapping(value, "record"))
                for value in raw_records
            ]
            units.append((origin_repo, records))
        declared_order = [member.origin_repo for member in self.members]
        supplied_order = [origin_repo for origin_repo, _records in units]
        expected_order = [
            origin_repo for origin_repo in declared_order if origin_repo in seen
        ]
        if supplied_order != expected_order:
            raise ApplicationRejection(
                "project-order-mismatch",
                "project batch units must follow declared member order",
                422,
            )
        if context.idempotency_key != project_batch_idempotency_key(units):
            raise ApplicationRejection(
                "idempotency-key-mismatch",
                "idempotency key must equal the canonical project-batch digest",
                422,
            )
        validated_units = []
        for origin_repo, records in units:
            application = by_repo[origin_repo]
            validated_records = [
                application._validate_record(record, context, SUPPORTED_BATCH_TYPES)
                for record in records
            ]
            validated_units.append((origin_repo, application, validated_records))

        results = []
        for origin_repo, application, records in validated_units:
            results.append(
                {
                    "origin_repo": origin_repo,
                    **application.apply_records(records, context),
                }
            )
        return {
            "contract_version": "project-batch-1",
            "project_id": self.project_id,
            "results": results,
        }
