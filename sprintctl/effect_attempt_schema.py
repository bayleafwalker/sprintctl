"""Deployment-only additive storage for authenticated cooperative attempts.

The owner runtime never calls this installer. The coordinated schema-22
migrator calls it inside its existing migration transaction and lock.
"""
from __future__ import annotations

from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

RELATIONS = ("work_effect_attempt", "work_effect_attempt_event", "idx_work_effect_attempt_item",
             "effect_attempt_pkey", "effect_attempt_intent_operation_unique", "effect_attempt_open_key_unique",
             "effect_attempt_event_pkey", "effect_attempt_event_kind_unique")
FUNCTIONS = ("sprintctl_effect_attempt_guard", "sprintctl_effect_attempt_event_guard",
             "sprintctl_effect_attempt_consistency")


def install(cur: Any) -> None:
    """Refuse foreign objects, then create all storage and guards atomically."""
    cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)", (list(RELATIONS),))
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema22 refuses pre-existing attempt relation or index")
    cur.execute("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)", (list(FUNCTIONS),))
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema22 refuses pre-existing attempt guard function")
    cur.execute(DDL)


DDL = r"""
CREATE TABLE work_effect_attempt (
    repo_id text NOT NULL,
    attempt_id text NOT NULL CHECK (attempt_id ~ '^attempt_[0-9A-HJKMNP-TV-Z]{26}$'),
    intent_id text NOT NULL,
    intent_revision integer NOT NULL CHECK (intent_revision >= 1),
    canonical_intent_digest text NOT NULL CHECK (canonical_intent_digest ~ '^[0-9a-f]{64}$'),
    work_item_id bigint NOT NULL,
    expected_revision text NOT NULL CHECK (expected_revision ~
        '^item:[0-9a-fA-F-]{36}@description:v[0-9]+@sha256:[0-9a-f]{64}@revise:[0-9]+$'),
    release_digest text NOT NULL CHECK (release_digest ~ '^[0-9a-f]{64}$'),
    workspace_id text NOT NULL CHECK (length(workspace_id)>0),
    principal_id text NOT NULL CHECK (length(principal_id)>0),
    client_id text,
    grant_id text,
    provider_operation text NOT NULL CHECK (provider_operation IN ('push_branch','open_pull_request')),
    target jsonb NOT NULL CHECK (jsonb_typeof(target)='object'),
    target_digest text NOT NULL CHECK (target_digest ~ '^[0-9a-f]{64}$'),
    authorization_body jsonb NOT NULL CHECK (jsonb_typeof(authorization_body)='object'),
    authorization_digest text NOT NULL CHECK (authorization_digest ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('accepted','redeemed','sealed_unused')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    redeemed_at timestamptz,
    sealed_at timestamptz,
    CONSTRAINT effect_attempt_pkey PRIMARY KEY (repo_id,attempt_id),
    CONSTRAINT effect_attempt_intent_operation_unique UNIQUE (repo_id,intent_id,intent_revision,provider_operation),
    CONSTRAINT effect_attempt_open_key_unique UNIQUE (repo_id,workspace_id,principal_id,idempotency_key),
    FOREIGN KEY (repo_id,intent_id) REFERENCES work_effect_intent(repo_id,intent_id) ON DELETE RESTRICT,
    FOREIGN KEY (repo_id,work_item_id) REFERENCES work_item(repo_id,id) ON DELETE RESTRICT,
    FOREIGN KEY (repo_id,release_digest) REFERENCES work_release(repo_id,release_digest) ON DELETE RESTRICT,
    CHECK ((state='accepted' AND redeemed_at IS NULL AND sealed_at IS NULL)
        OR (state='redeemed' AND redeemed_at IS NOT NULL AND sealed_at IS NULL)
        OR (state='sealed_unused' AND redeemed_at IS NULL AND sealed_at IS NOT NULL))
);
CREATE INDEX idx_work_effect_attempt_item ON work_effect_attempt(repo_id,work_item_id,created_at);

CREATE TABLE work_effect_attempt_event (
    repo_id text NOT NULL,
    attempt_id text NOT NULL,
    event_seq integer NOT NULL CHECK (event_seq BETWEEN 0 AND 2),
    event_kind text NOT NULL CHECK (event_kind IN ('attempt_authorization_accepted',
        'invocation_authorization_redeemed','attempt_closed_without_redemption','application_report_received')),
    authorization_digest text NOT NULL CHECK (authorization_digest ~ '^[0-9a-f]{64}$'),
    previous_event_digest text CHECK (previous_event_digest ~ '^[0-9a-f]{64}$'),
    event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT effect_attempt_event_pkey PRIMARY KEY (repo_id,attempt_id,event_seq),
    CONSTRAINT effect_attempt_event_kind_unique UNIQUE (repo_id,attempt_id,event_kind),
    FOREIGN KEY (repo_id,attempt_id) REFERENCES work_effect_attempt(repo_id,attempt_id) ON DELETE RESTRICT,
    CHECK ((event_seq=0 AND event_kind='attempt_authorization_accepted' AND previous_event_digest IS NULL)
        OR (event_seq=1 AND event_kind IN ('invocation_authorization_redeemed','attempt_closed_without_redemption')
            AND previous_event_digest IS NOT NULL)
        OR (event_seq=2 AND event_kind='application_report_received' AND previous_event_digest IS NOT NULL))
);

CREATE FUNCTION sprintctl_effect_attempt_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP IN ('DELETE','TRUNCATE') THEN
        RAISE EXCEPTION 'effect attempts cannot be removed' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.state<>'accepted' OR NEW.redeemed_at IS NOT NULL OR NEW.sealed_at IS NOT NULL THEN
            RAISE EXCEPTION 'effect attempt must start accepted' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF (to_jsonb(NEW)-ARRAY['state','redeemed_at','sealed_at']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['state','redeemed_at','sealed_at']) THEN
        RAISE EXCEPTION 'effect attempt authorization is immutable' USING ERRCODE='23514';
    END IF;
    IF OLD.state<>'accepted' OR NOT (
        (NEW.state='redeemed' AND NEW.redeemed_at IS NOT NULL AND NEW.sealed_at IS NULL)
        OR (NEW.state='sealed_unused' AND NEW.redeemed_at IS NULL AND NEW.sealed_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'invalid effect attempt transition' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER sprintctl_effect_attempt_guard BEFORE INSERT OR UPDATE OR DELETE ON work_effect_attempt
    FOR EACH ROW EXECUTE FUNCTION sprintctl_effect_attempt_guard();
CREATE TRIGGER sprintctl_effect_attempt_no_truncate BEFORE TRUNCATE ON work_effect_attempt
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_effect_attempt_guard();

CREATE FUNCTION sprintctl_effect_attempt_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent work_effect_attempt%ROWTYPE; predecessor work_effect_attempt_event%ROWTYPE;
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION 'effect attempt events are append-only' USING ERRCODE='23514';
    END IF;
    SELECT * INTO parent FROM work_effect_attempt
        WHERE repo_id=NEW.repo_id AND attempt_id=NEW.attempt_id FOR UPDATE;
    IF NOT FOUND OR NEW.authorization_digest<>parent.authorization_digest THEN
        RAISE EXCEPTION 'effect event authorization mismatch' USING ERRCODE='23514';
    END IF;
    IF NEW.event_seq=0 THEN
        IF parent.state<>'accepted' THEN
            RAISE EXCEPTION 'effect acceptance event requires accepted state' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT * INTO predecessor FROM work_effect_attempt_event
            WHERE repo_id=NEW.repo_id AND attempt_id=NEW.attempt_id AND event_seq=NEW.event_seq-1;
        IF NOT FOUND OR NEW.previous_event_digest IS DISTINCT FROM predecessor.event_digest THEN
            RAISE EXCEPTION 'effect event predecessor mismatch' USING ERRCODE='23514';
        END IF;
        IF (NEW.event_kind='invocation_authorization_redeemed' AND parent.state<>'redeemed')
           OR (NEW.event_kind='attempt_closed_without_redemption' AND parent.state<>'sealed_unused')
           OR (NEW.event_kind='application_report_received' AND
               (parent.state<>'redeemed' OR parent.provider_operation<>'open_pull_request'
                OR predecessor.event_kind<>'invocation_authorization_redeemed')) THEN
            RAISE EXCEPTION 'effect event state mismatch' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER sprintctl_effect_attempt_event_guard BEFORE INSERT OR UPDATE OR DELETE ON work_effect_attempt_event
    FOR EACH ROW EXECUTE FUNCTION sprintctl_effect_attempt_event_guard();
CREATE TRIGGER sprintctl_effect_attempt_event_no_truncate BEFORE TRUNCATE ON work_effect_attempt_event
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_effect_attempt_event_guard();

CREATE FUNCTION sprintctl_effect_attempt_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent work_effect_attempt%ROWTYPE; kinds text[];
BEGIN
    SELECT * INTO parent FROM work_effect_attempt WHERE repo_id=NEW.repo_id AND attempt_id=NEW.attempt_id;
    SELECT array_agg(event_kind ORDER BY event_seq) INTO kinds FROM work_effect_attempt_event
        WHERE repo_id=NEW.repo_id AND attempt_id=NEW.attempt_id;
    IF (parent.state='accepted' AND kinds IS NOT DISTINCT FROM ARRAY['attempt_authorization_accepted'])
       OR (parent.state='sealed_unused' AND kinds IS NOT DISTINCT FROM
           ARRAY['attempt_authorization_accepted','attempt_closed_without_redemption'])
       OR (parent.state='redeemed' AND (kinds IS NOT DISTINCT FROM
           ARRAY['attempt_authorization_accepted','invocation_authorization_redeemed']
           OR kinds IS NOT DISTINCT FROM ARRAY['attempt_authorization_accepted',
              'invocation_authorization_redeemed','application_report_received'])) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'effect attempt state requires its complete event history' USING ERRCODE='23514';
END; $$;
CREATE CONSTRAINT TRIGGER sprintctl_effect_attempt_consistency AFTER INSERT OR UPDATE ON work_effect_attempt
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sprintctl_effect_attempt_consistency();
CREATE CONSTRAINT TRIGGER sprintctl_effect_attempt_event_consistency AFTER INSERT ON work_effect_attempt_event
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sprintctl_effect_attempt_consistency();
"""
