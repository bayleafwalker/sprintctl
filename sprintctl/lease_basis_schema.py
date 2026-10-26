"""PostgreSQL schema 29 lease revision and Release binding.

The fields are nullable so a schema 28 lease is explicitly unbound: no
database migration can recover the item revision or Release a worker saw when
that lease was acquired. The settlement writer treats those rows as unknown
and refuses to apply their outcomes.
"""
from __future__ import annotations

from typing import Any

from .pg_migrations import RemoteSchemaMigrationError


_COLUMNS = {
    "basis_item_revision": ("text", False, ""),
    "basis_release_digest": ("text", False, ""),
}
_GUARD_FUNCTION = "sprintctl_work_decision_lease_basis_guard"
_GUARD_TRIGGER = "work_decision_lease_basis_guard"
_ACQUIRE_GUARD_FUNCTION = "sprintctl_work_lease_basis_insert_guard"
_ACQUIRE_GUARD_TRIGGER = "work_lease_basis_insert_guard"
_GUARD_BODY = """BEGIN
    IF NEW.kind = 'accept' AND NEW.actor = 'sprintctl:lease-settlement' THEN
        IF COALESCE(current_setting('sprintctl.lease_basis_checked', true), '') <> 'on' THEN
            RAISE EXCEPTION 'lease settlement requires a current schema 29 basis check'
                USING ERRCODE = '23514';
        END IF;
        PERFORM set_config('sprintctl.lease_basis_checked', '', true);
    END IF;
    RETURN NEW;
END;"""
_ACQUIRE_GUARD_BODY = """BEGIN
    IF NEW.basis_item_revision IS NULL OR btrim(NEW.basis_item_revision) = '' THEN
        RAISE EXCEPTION 'new work leases require an item revision basis'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;"""
_FUNCTION_BODIES = {
    _GUARD_FUNCTION: _GUARD_BODY,
    _ACQUIRE_GUARD_FUNCTION: _ACQUIRE_GUARD_BODY,
}
_TRIGGER_SHAPES = (
    (_ACQUIRE_GUARD_TRIGGER, "work_lease", _ACQUIRE_GUARD_FUNCTION, "O", 7),
    (_GUARD_TRIGGER, "work_decision", _GUARD_FUNCTION, "O", 7),
)


def install(cur: Any) -> None:
    """Add basis columns only to the exact schema 18 lease store."""
    from . import pg

    foreign = pg._foreign_relations(
        cur,
        pg._SCHEMA_18_TABLES,
        pg._SCHEMA_18_INDEXES,
        pg._SCHEMA_18_INDEX_SHAPES,
    )
    if foreign:
        raise RemoteSchemaMigrationError(
            "schema 29 cannot bind leases: relation(s) "
            f"{', '.join(foreign)} do not have the exact schema 18 shape"
        )

    cur.execute(
        "SELECT name FROM ("
        "SELECT p.proname AS name FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s) "
        "UNION ALL "
        "SELECT t.tgname AS name FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND t.tgname=ANY(%s) AND NOT t.tgisinternal"
        ") existing LIMIT 1",
        (list(_FUNCTION_BODIES), [shape[0] for shape in _TRIGGER_SHAPES]),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError(
            "schema 29 refuses a pre-existing lease-basis guard function or trigger"
        )

    cur.execute(
        "SELECT a.attname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = 'work_lease' "
        "AND a.attnum > 0 AND NOT a.attisdropped AND a.attname = ANY(%s)",
        (list(_COLUMNS),),
    )
    existing = [row["attname"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]
    if existing:
        raise RemoteSchemaMigrationError(
            "schema 29 refuses pre-existing work_lease basis column(s): "
            + ", ".join(sorted(existing))
        )

    cur.execute(
        "ALTER TABLE work_lease "
        "ADD COLUMN basis_item_revision text, "
        "ADD COLUMN basis_release_digest text"
    )
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    """Read the exact new column contract without attempting schema repair."""
    cur.execute("SELECT to_regclass('work_lease') AS relation")
    relation = cur.fetchone()
    relation = relation["relation"] if isinstance(relation, dict) else relation[0]
    if relation is None:
        actual: dict[str, tuple[str, bool, str]] = {}
    else:
        cur.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS data_type, "
            "a.attnotnull, COALESCE(pg_get_expr(d.adbin, d.adrelid), '') AS default_expr "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE n.nspname = current_schema() AND c.relname = 'work_lease' "
            "AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped "
            "AND a.attname = ANY(%s) ORDER BY a.attname",
            (list(_COLUMNS),),
        )
        rows = cur.fetchall()
        actual = {
            (row["attname"] if isinstance(row, dict) else row[0]): (
                row["data_type"] if isinstance(row, dict) else row[1],
                bool(row["attnotnull"] if isinstance(row, dict) else row[2]),
                row["default_expr"] if isinstance(row, dict) else row[3],
            )
            for row in rows
        }

    expected = dict(_COLUMNS)
    cur.execute(
        "SELECT p.proname, p.pronargs, p.prorettype='trigger'::regtype AS returns_trigger, p.prosrc "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s) ORDER BY p.proname,p.oid",
        (list(_FUNCTION_BODIES),),
    )
    function_rows = cur.fetchall()
    functions = {}
    for row in function_rows:
        name = row["proname"] if isinstance(row, dict) else row[0]
        args = row["pronargs"] if isinstance(row, dict) else row[1]
        returns_trigger = row["returns_trigger"] if isinstance(row, dict) else row[2]
        body = row["prosrc"] if isinstance(row, dict) else row[3]
        functions.setdefault(name, []).append((int(args), bool(returns_trigger), body))
    cur.execute(
        "SELECT t.tgname, c.relname AS table_name, p.proname AS function_name, "
        "t.tgenabled, t.tgtype FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_proc p ON p.oid=t.tgfoid WHERE n.nspname=current_schema() "
        "AND t.tgname=ANY(%s) AND NOT t.tgisinternal ORDER BY t.tgname",
        ([shape[0] for shape in _TRIGGER_SHAPES],),
    )
    trigger_rows = cur.fetchall()
    triggers = [
        (
            row["tgname"] if isinstance(row, dict) else row[0],
            row["table_name"] if isinstance(row, dict) else row[1],
            row["function_name"] if isinstance(row, dict) else row[2],
            row["tgenabled"] if isinstance(row, dict) else row[3],
            int(row["tgtype"] if isinstance(row, dict) else row[4]),
        )
        for row in trigger_rows
    ]
    function_exact = (
        set(functions) == set(_FUNCTION_BODIES)
        and all(
            len(functions[name]) == 1
            and functions[name][0][0] == 0
            and functions[name][0][1]
            and "".join(functions[name][0][2].split()) == "".join(body.split())
            for name, body in _FUNCTION_BODIES.items()
        )
    )
    trigger_exact = triggers == sorted(_TRIGGER_SHAPES)
    available = actual == expected and function_exact and trigger_exact
    # No matching fields or guard objects is the known pre-29 shape. Any
    # partial install fails closed.
    complete = (not actual and not function_rows and not triggers) or available
    return {
        "schema_version": "sprintctl-work-lease-basis/v1",
        "available": available,
        "complete": complete,
        "basis_columns": sorted(actual),
        "expected_basis_columns": sorted(expected),
        "guard_function_available": function_exact,
        "acquire_guard_function_available": _ACQUIRE_GUARD_FUNCTION in functions,
        "guard_trigger_available": trigger_exact,
    }


DDL = rf"""
CREATE FUNCTION {_ACQUIRE_GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
{_ACQUIRE_GUARD_BODY}
$$;
CREATE TRIGGER {_ACQUIRE_GUARD_TRIGGER}
    BEFORE INSERT ON work_lease
    FOR EACH ROW EXECUTE FUNCTION {_ACQUIRE_GUARD_FUNCTION}();
CREATE FUNCTION {_GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
{_GUARD_BODY}
$$;
CREATE TRIGGER {_GUARD_TRIGGER}
    BEFORE INSERT ON work_decision
    FOR EACH ROW EXECUTE FUNCTION {_GUARD_FUNCTION}();
"""
