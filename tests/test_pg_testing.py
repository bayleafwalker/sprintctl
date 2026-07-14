from __future__ import annotations

from dataclasses import replace

import pytest

from sprintctl.pg_testing import (
    DISPOSABLE_DATABASE_COMMENT,
    PostgresTestIdentity,
    UnsafePostgresTestTarget,
    new_test_repo_id,
    validate_disposable_identity,
)


@pytest.fixture
def disposable_identity() -> PostgresTestIdentity:
    return PostgresTestIdentity(
        database_name="sprintctl_test_ci",
        role_name="sprintctl_test_ci",
        database_owner="sprintctl_test_ci",
        database_comment=DISPOSABLE_DATABASE_COMMENT,
        role_superuser=False,
        role_create_db=False,
        role_create_role=False,
        role_replication=False,
        role_bypass_rls=False,
    )


def test_disposable_identity_accepts_dedicated_unprivileged_owner(disposable_identity):
    validate_disposable_identity(disposable_identity)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("database_name", "sprintctl", "database name"),
        ("role_name", "sprintctl", "role name"),
        ("database_owner", "postgres", "must own"),
        ("database_comment", None, "database comment"),
        ("role_superuser", True, "SUPERUSER"),
        ("role_create_db", True, "CREATEDB"),
        ("role_create_role", True, "CREATEROLE"),
        ("role_replication", True, "REPLICATION"),
        ("role_bypass_rls", True, "BYPASSRLS"),
    ],
)
def test_disposable_identity_rejects_unsafe_server_fact(
    disposable_identity, field, value, message
):
    with pytest.raises(UnsafePostgresTestTarget, match=message):
        validate_disposable_identity(replace(disposable_identity, **{field: value}))


def test_new_test_repo_id_is_guarded_and_normalized():
    repo_id = new_test_repo_id("Round Trip / Import")
    assert repo_id.startswith("itest-round-trip-import-")
    assert len(repo_id.rsplit("-", 1)[1]) == 12
