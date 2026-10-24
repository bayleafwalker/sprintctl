"""Deployment-only schema 28 for the separate operator-request lifecycle."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .pg_migrations import RemoteSchemaMigrationError


TABLES = ("operator_request", "operator_request_dependant", "operator_request_event")
INDEXES = (
    "operator_request_pkey",
    "operator_request_dependant_pkey",
    "operator_request_event_pkey",
)
RELATIONS = TABLES + INDEXES
FUNCTIONS = ("sprintctl_operator_request_event_append_only",)
TRIGGERS = (
    "sprintctl_operator_request_event_append_only",
    "sprintctl_operator_request_event_no_truncate",
)
_GUARD_BODY = "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514'; END;"
_EXPECTED_FUNCTIONS = {
    FUNCTIONS[0]: {"result_type": "trigger", "language": "plpgsql", "security_definer": False,
                   "body": _GUARD_BODY},
}
_EXPECTED_TRIGGERS = {
    "sprintctl_operator_request_event_append_only": {
        "table": "operator_request_event", "function": FUNCTIONS[0], "enabled": "O", "type": 27,
    },
    "sprintctl_operator_request_event_no_truncate": {
        "table": "operator_request_event", "function": FUNCTIONS[0], "enabled": "O", "type": 34,
    },
}
_COLUMNS = {
    "operator_request": (
        ("repo_id", "text", True), ("workspace_id", "text", True), ("request_id", "text", True),
        ("requester_principal_id", "text", True), ("revision", "integer", True),
        ("material_revision", "integer", True), ("state", "text", True), ("packet", "jsonb", True),
        ("packet_digest", "text", True), ("publish_digest", "text", True),
        ("created_at", "timestamp with time zone", True),
        ("updated_at", "timestamp with time zone", True),
    ),
    "operator_request_dependant": (
        ("repo_id", "text", True), ("workspace_id", "text", True), ("request_id", "text", True),
        ("work_item_id", "bigint", True), ("session_id", "text", True), ("runtime_id", "text", False),
        ("work_revision", "text", True), ("state", "text", True),
        ("attached_at", "timestamp with time zone", True), ("withdrawn_at", "timestamp with time zone", False),
    ),
    "operator_request_event": (
        ("repo_id", "text", True), ("workspace_id", "text", True), ("request_id", "text", True),
        ("revision", "integer", True), ("actor", "text", True), ("action", "text", True),
        ("details", "jsonb", True), ("recorded_at", "timestamp with time zone", True),
    ),
}
_CONSTRAINTS = {
    "operator_request_pkey": "PRIMARY KEY (repo_id, workspace_id, request_id)",
    "operator_request_id_format": "CHECK ((request_id ~ '^req_[0-9A-HJKMNP-TV-Z]{26}$'::text))",
    "operator_request_revision_positive": "CHECK ((revision > 0))",
    "operator_request_material_revision_positive": "CHECK ((material_revision > 0))",
    "operator_request_state": (
        "CHECK ((state = ANY (ARRAY['open'::text, 'reported-performed'::text, 'verifying'::text, "
        "'resolved'::text, 'declined'::text, 'withdrawn'::text, 'superseded'::text])))"
    ),
    "operator_request_dependant_pkey": (
        "PRIMARY KEY (repo_id, workspace_id, request_id, work_item_id, session_id)"
    ),
    "operator_request_dependant_state": (
        "CHECK ((state = ANY (ARRAY['active'::text, 'withdrawn'::text])))"
    ),
    "operator_request_dependant_withdrawal": (
        "CHECK (((state = 'withdrawn'::text) = (withdrawn_at IS NOT NULL)))"
    ),
    "operator_request_dependant_request_fk": (
        "FOREIGN KEY (repo_id, workspace_id, request_id) REFERENCES "
        "operator_request(repo_id, workspace_id, request_id) ON DELETE RESTRICT"
    ),
    "operator_request_event_pkey": "PRIMARY KEY (repo_id, workspace_id, request_id, revision)",
    "operator_request_event_revision_positive": "CHECK ((revision > 0))",
    "operator_request_event_request_fk": (
        "FOREIGN KEY (repo_id, workspace_id, request_id) REFERENCES "
        "operator_request(repo_id, workspace_id, request_id) ON DELETE RESTRICT"
    ),
}
_INDEX_DEFINITIONS = {
    "operator_request_pkey": "CREATE UNIQUE INDEX operator_request_pkey ON operator_request USING btree (repo_id, workspace_id, request_id)",
    "operator_request_dependant_pkey": "CREATE UNIQUE INDEX operator_request_dependant_pkey ON operator_request_dependant USING btree (repo_id, workspace_id, request_id, work_item_id, session_id)",
    "operator_request_event_pkey": "CREATE UNIQUE INDEX operator_request_event_pkey ON operator_request_event USING btree (repo_id, workspace_id, request_id, revision)",
}


def install(cur: Any) -> None:
    """Install the new schema only when every owned object name is absent."""
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (list(RELATIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema28 refuses a pre-existing operator-request relation or index")
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)",
        (list(FUNCTIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema28 refuses a pre-existing operator-request guard function")
    cur.execute(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND t.tgname=ANY(%s)",
        (list(TRIGGERS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema28 refuses a pre-existing operator-request guard trigger")
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    """Check the exact owned relation, column, constraint, and index inventory."""
    cur.execute(
        "SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_index i ON i.indexrelid=c.oid LEFT JOIN pg_class t ON t.oid=i.indrelid "
        "WHERE n.nspname=current_schema() AND c.relkind IN ('r','i') AND "
        "(c.relname=ANY(%s) OR (c.relkind='i' AND t.relname=ANY(%s))) ORDER BY c.relname",
        (list(TABLES), list(TABLES)),
    )
    relations = cur.fetchall()
    actual_relations = {(row["relname"], row["relkind"]) for row in relations}
    expected_relations = {(name, "r") for name in TABLES} | {(name, "i") for name in INDEXES}

    cur.execute(
        "SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod) AS data_type,"
        "a.attnotnull,a.attidentity,COALESCE(pg_get_expr(d.adbin,d.adrelid),'') AS default_expr "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s) AND c.relkind='r' "
        "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        (list(TABLES),),
    )
    columns = cur.fetchall()
    actual_columns = {
        table: tuple((row["attname"], row["data_type"], bool(row["attnotnull"]),
                      row["attidentity"], row["default_expr"])
                     for row in columns if row["relname"] == table)
        for table in TABLES
    }
    expected_columns = {
        table: tuple((name, data_type, not_null, "", "")
                     for name, data_type, not_null in _COLUMNS[table])
        for table in TABLES
    }

    cur.execute(
        "SELECT con.conname,pg_get_constraintdef(con.oid,false) AS definition,con.convalidated "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s) ORDER BY con.conname",
        (list(TABLES),),
    )
    constraint_rows = cur.fetchall()
    actual_constraints = {
        row["conname"]: (" ".join(row["definition"].split()), bool(row["convalidated"]))
        for row in constraint_rows
    }
    expected_constraints = {name: (" ".join(definition.split()), True)
                            for name, definition in _CONSTRAINTS.items()}

    cur.execute(
        "SELECT c.relname,pg_get_indexdef(c.oid) AS indexdef FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
        "AND c.relkind='i' AND c.oid IN (SELECT i.indexrelid FROM pg_index i "
        "JOIN pg_class t ON t.oid=i.indrelid WHERE t.relname=ANY(%s)) ORDER BY c.relname",
        (list(TABLES),),
    )
    actual_indexes = {row["relname"]: row["indexdef"] for row in cur.fetchall()}
    expected_indexes = _INDEX_DEFINITIONS
    actual_description = {
        "relations": sorted(actual_relations),
        "columns": actual_columns,
        "constraints": actual_constraints,
        "indexes": actual_indexes,
        "functions": {},
        "triggers": {},
    }
    expected_description = {
        "relations": sorted(expected_relations),
        "columns": expected_columns,
        "constraints": expected_constraints,
        "indexes": expected_indexes,
        "functions": _EXPECTED_FUNCTIONS,
        "triggers": _EXPECTED_TRIGGERS,
    }
    cur.execute(
        "SELECT t.tgname,c.relname AS table_name,p.proname AS function_name,t.tgenabled,t.tgtype "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s) AND NOT t.tgisinternal "
        "ORDER BY t.tgname",
        (list(TABLES),),
    )
    trigger_rows = cur.fetchall()
    actual_triggers = {
        row["tgname"]: {"table": row["table_name"], "function": row["function_name"],
                        "enabled": row["tgenabled"], "type": int(row["tgtype"])}
        for row in trigger_rows
    }
    cur.execute(
        "SELECT p.proname,pg_get_function_result(p.oid) AS result_type,l.lanname AS language,"
        "p.prosecdef AS security_definer,p.prosrc AS body FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s) ORDER BY p.proname",
        (list(FUNCTIONS),),
    )
    function_rows = cur.fetchall()
    actual_functions = {
        row["proname"]: {"result_type": row["result_type"], "language": row["language"],
                         "security_definer": bool(row["security_definer"]), "body": row["body"]}
        for row in function_rows
    }
    actual_description["triggers"] = actual_triggers
    actual_description["functions"] = actual_functions
    actual_digest = hashlib.sha256(json.dumps(actual_description, sort_keys=True, default=list).encode()).hexdigest()
    expected_digest = hashlib.sha256(json.dumps(expected_description, sort_keys=True, default=list).encode()).hexdigest()
    trigger_count = len(actual_triggers)
    available = (
        actual_relations == expected_relations
        and actual_columns == expected_columns
        and len(constraint_rows) == len(_CONSTRAINTS)
        and actual_constraints == expected_constraints
        and actual_indexes == expected_indexes
        and len(trigger_rows) == len(_EXPECTED_TRIGGERS)
        and actual_triggers == _EXPECTED_TRIGGERS
        and len(function_rows) == len(_EXPECTED_FUNCTIONS)
        and actual_functions == _EXPECTED_FUNCTIONS
    )
    return {
        "table_count": sum(kind == "r" for _, kind in actual_relations),
        "catalog_fingerprint": actual_digest,
        "expected_catalog_fingerprint": expected_digest,
        "trigger_count": trigger_count,
        "available": available,
        "function_count": len(actual_functions),
        "complete": not actual_relations and not actual_functions and not actual_triggers or available,
    }


DDL = r"""
CREATE TABLE operator_request (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    requester_principal_id text NOT NULL,
    revision integer NOT NULL,
    material_revision integer NOT NULL,
    state text NOT NULL,
    packet jsonb NOT NULL,
    packet_digest text NOT NULL,
    publish_digest text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT operator_request_pkey PRIMARY KEY (repo_id, workspace_id, request_id),
    CONSTRAINT operator_request_id_format CHECK (request_id ~ '^req_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT operator_request_revision_positive CHECK (revision > 0),
    CONSTRAINT operator_request_material_revision_positive CHECK (material_revision > 0),
    CONSTRAINT operator_request_state CHECK
        (state IN ('open','reported-performed','verifying','resolved','declined','withdrawn','superseded'))
);

CREATE TABLE operator_request_dependant (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    work_item_id bigint NOT NULL,
    session_id text NOT NULL,
    runtime_id text,
    work_revision text NOT NULL,
    state text NOT NULL,
    attached_at timestamptz NOT NULL,
    withdrawn_at timestamptz,
    CONSTRAINT operator_request_dependant_pkey PRIMARY KEY
        (repo_id, workspace_id, request_id, work_item_id, session_id),
    CONSTRAINT operator_request_dependant_state CHECK (state IN ('active','withdrawn')),
    CONSTRAINT operator_request_dependant_withdrawal CHECK
        ((state='withdrawn') = (withdrawn_at IS NOT NULL)),
    CONSTRAINT operator_request_dependant_request_fk FOREIGN KEY (repo_id,workspace_id,request_id)
        REFERENCES operator_request(repo_id,workspace_id,request_id) ON DELETE RESTRICT
);

CREATE TABLE operator_request_event (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    revision integer NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    details jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    CONSTRAINT operator_request_event_pkey PRIMARY KEY (repo_id,workspace_id,request_id,revision),
    CONSTRAINT operator_request_event_revision_positive CHECK (revision > 0),
    CONSTRAINT operator_request_event_request_fk FOREIGN KEY (repo_id,workspace_id,request_id)
        REFERENCES operator_request(repo_id,workspace_id,request_id) ON DELETE RESTRICT
);

CREATE FUNCTION sprintctl_operator_request_event_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514'; END;$$;
CREATE TRIGGER sprintctl_operator_request_event_append_only
    BEFORE UPDATE OR DELETE ON operator_request_event
    FOR EACH ROW EXECUTE FUNCTION sprintctl_operator_request_event_append_only();
CREATE TRIGGER sprintctl_operator_request_event_no_truncate
    BEFORE TRUNCATE ON operator_request_event
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_operator_request_event_append_only();
"""
