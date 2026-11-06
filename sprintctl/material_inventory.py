"""Offline, versioned material-catalog drift inventory.

This is a review aid. It does not admit writers, fence restores, or prove that
all database and external writers were discovered. A manifest must be reviewed
and pinned independently; capturing and immediately verifying a live database
against itself is not qualification.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

COVERED = ('work_item', 'dep', 'event', 'work_decision', 'work_release',
           'ref', 'reservation', 'release_commit')


MANIFEST_VERSION = 1


def snapshot(cur, schema='public', role_names=None):
    if type(schema) is not str or not schema:
        raise ValueError('explicit schema required')
    if role_names is None:
        cur.execute("SELECT current_user AS role_name")
        role_names = [cur.fetchone()['role_name']]
    if (type(role_names) not in (tuple, list) or not role_names
            or any(type(name) is not str or not name for name in role_names)
            or len(set(role_names)) != len(role_names)):
        raise ValueError('explicit unique role names required')
    role_names = sorted(role_names)
    cur.execute("SELECT c.relname,c.relkind,c.relowner::regrole::text,c.relacl::text, "
                "c.relrowsecurity,c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname", (schema, list(COVERED)))
    relations = [dict(row) for row in cur.fetchall()]
    if {r['relname'] for r in relations} != set(COVERED):
        raise ValueError('covered relation missing')
    cur.execute("SELECT c.relname,t.tgname,t.tgenabled,t.tgisinternal,pg_get_triggerdef(t.oid) AS definition "
                "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname,t.tgname", (schema, list(COVERED)))
    triggers = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user")
    role = dict(cur.fetchone())
    cur.execute("SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type, "
                "a.attnotnull,a.attacl::text,pg_get_expr(d.adbin,d.adrelid) AS default_expr "
                "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "WHERE n.nspname=%s AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped "
                "ORDER BY c.relname,a.attnum", (schema, list(COVERED)))
    columns = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT c.relname,k.conname,k.contype,k.convalidated,k.condeferrable,k.condeferred, "
                "pg_get_constraintdef(k.oid) AS definition FROM pg_constraint k "
                "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND (c.relname=ANY(%s) OR k.confrelid IN "
                "(SELECT oid FROM pg_class WHERE relnamespace=n.oid AND relname=ANY(%s))) "
                "ORDER BY c.relname,k.conname", (schema, list(COVERED), list(COVERED)))
    constraints = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT c.relname,p.proname,p.oid::regprocedure::text AS signature, "
                "pg_get_functiondef(p.oid) AS definition,p.proowner::regrole::text AS owner, "
                "p.prosecdef,p.proconfig,p.proacl::text FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_proc p ON p.oid=t.tgfoid WHERE n.nspname=%s "
                "AND c.relname=ANY(%s) AND NOT t.tgisinternal ORDER BY c.relname,signature", (schema, list(COVERED)))
    functions = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT c.relname, has_table_privilege(current_user,c.oid,'SELECT') AS can_select, "
                "has_table_privilege(current_user,c.oid,'INSERT') AS can_insert, "
                "has_table_privilege(current_user,c.oid,'UPDATE') AS can_update, "
                "has_table_privilege(current_user,c.oid,'DELETE') AS can_delete, "
                "has_table_privilege(current_user,c.oid,'TRUNCATE') AS can_truncate, "
                "has_table_privilege(current_user,c.oid,'TRIGGER') AS can_trigger "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname", (schema, list(COVERED)))
    privileges = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT p.relname AS parent,c.relname AS child FROM pg_inherits i "
                "JOIN pg_class p ON p.oid=i.inhparent JOIN pg_class c ON c.oid=i.inhrelid "
                "JOIN pg_namespace n ON n.oid=p.relnamespace "
                "WHERE n.nspname=%s AND p.relname=ANY(%s) ORDER BY 1,2", (schema, list(COVERED)))
    inheritance = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT c.relname, i.relname AS index_name, pg_get_indexdef(i.oid) AS definition, "
                "ix.indisvalid,ix.indisready,ix.indisunique FROM pg_index ix "
                "JOIN pg_class c ON c.oid=ix.indrelid JOIN pg_class i ON i.oid=ix.indexrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s "
                "AND c.relname=ANY(%s) ORDER BY c.relname,i.relname", (schema, list(COVERED)))
    indexes = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls "
                "FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname", (role_names,))
    roles = [dict(row) for row in cur.fetchall()]
    if [entry['rolname'] for entry in roles] != role_names:
        raise ValueError('configured role missing')
    cur.execute("SELECT r.rolname AS member, parent.rolname AS inherited_role, "
                "pg_has_role(r.oid,parent.oid,'USAGE') AS has_usage "
                "FROM pg_roles r CROSS JOIN pg_roles parent WHERE r.rolname=ANY(%s) "
                "AND r.oid<>parent.oid AND pg_has_role(r.oid,parent.oid,'MEMBER') "
                "ORDER BY r.rolname,parent.rolname", (role_names,))
    memberships = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT r.rolname,c.relname,has_table_privilege(r.oid,c.oid,'SELECT') AS can_select, "
                "has_table_privilege(r.oid,c.oid,'INSERT') AS can_insert, "
                "has_table_privilege(r.oid,c.oid,'UPDATE') AS can_update, "
                "has_table_privilege(r.oid,c.oid,'DELETE') AS can_delete, "
                "has_table_privilege(r.oid,c.oid,'TRUNCATE') AS can_truncate, "
                "has_table_privilege(r.oid,c.oid,'TRIGGER') AS can_trigger "
                "FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE r.rolname=ANY(%s) AND n.nspname=%s AND c.relname=ANY(%s) "
                "ORDER BY r.rolname,c.relname", (role_names, schema, list(COVERED)))
    role_privileges = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT r.rolname,has_schema_privilege(r.oid,n.oid,'USAGE') AS can_usage, "
                "has_schema_privilege(r.oid,n.oid,'CREATE') AS can_create "
                "FROM pg_roles r CROSS JOIN pg_namespace n WHERE r.rolname=ANY(%s) "
                "AND n.nspname=%s ORDER BY r.rolname", (role_names, schema))
    schema_privileges = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT r.rolname,p.oid::regprocedure::text AS signature, "
                "has_function_privilege(r.oid,p.oid,'EXECUTE') AS can_execute "
                "FROM pg_roles r CROSS JOIN pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE r.rolname=ANY(%s) AND n.nspname=%s AND p.prokind IN ('f','p') "
                "AND (p.proname LIKE 'sprintctl%%' OR p.oid IN (SELECT t.tgfoid FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace tn ON tn.oid=c.relnamespace "
                "WHERE tn.nspname=%s AND c.relname=ANY(%s))) ORDER BY r.rolname,signature",
                (role_names, schema, schema, list(COVERED)))
    function_privileges = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT r.rolname,c.relname,a.attname, "
                "has_column_privilege(r.oid,c.oid,a.attnum,'SELECT') AS can_select, "
                "has_column_privilege(r.oid,c.oid,a.attnum,'INSERT') AS can_insert, "
                "has_column_privilege(r.oid,c.oid,a.attnum,'UPDATE') AS can_update, "
                "has_column_privilege(r.oid,c.oid,a.attnum,'REFERENCES') AS can_reference "
                "FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped "
                "WHERE r.rolname=ANY(%s) AND n.nspname=%s AND c.relname=ANY(%s) "
                "ORDER BY r.rolname,c.relname,a.attnum", (role_names, schema, list(COVERED)))
    column_privileges = [dict(row) for row in cur.fetchall()]
    return {'version': 3, 'role_names': role_names, 'roles': roles, 'role_memberships': memberships,
            'role_effective_privileges': role_privileges, 'indexes': indexes,
            'relations': relations, 'triggers': triggers, 'columns': columns,
            'schema': schema, 'constraints': constraints, 'trigger_functions': functions, 'current_role': role,
            'current_role_effective_table_privileges': privileges, 'inheritance': inheritance,
            'effective_schema_privileges': schema_privileges,
            'effective_function_privileges': function_privileges,
            'effective_column_privileges': column_privileges}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def manifest_digest(manifest):
    payload = dict(manifest)
    payload.pop('sha256', None)
    return hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()


def source_digests(source_files):
    """Hash every reviewed source file, including comments and whitespace."""
    if (type(source_files) is not list or not source_files
            or any(type(path) is not str or not path for path in source_files)
            or len(set(source_files)) != len(source_files)):
        raise ValueError('explicit unique source file list required')
    return {
        Path(path).as_posix(): hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in sorted(source_files)
    }


def build_manifest(cur, *, schema, role_names, source_commit, source_files, writer_inventory):
    """Capture an explicit candidate manifest for independent review/pinning."""
    if type(source_commit) is not str or len(source_commit) != 40:
        raise ValueError('full source commit required')
    if type(writer_inventory) is not dict or writer_inventory.get('version') != 1:
        raise ValueError('versioned writer inventory required')
    if (type(source_files) is not list or not source_files
            or any(type(path) is not str or not path for path in source_files)
            or len(set(source_files)) != len(source_files)):
        raise ValueError('explicit unique source file list required')
    manifest = {'manifest_version': MANIFEST_VERSION, 'source_commit': source_commit,
                'source_files': sorted(source_files),
                'source_sha256': source_digests(source_files),
                'catalog': snapshot(cur, schema=schema, role_names=role_names),
                'writer_inventory': writer_inventory}
    manifest['sha256'] = manifest_digest(manifest)
    return manifest


def require_manifest_match(cur, manifest, *, source_commit, writer_inventory):
    """Refuse malformed, mismatched or stale pinned manifests."""
    if (type(manifest) is not dict or manifest.get('manifest_version') != MANIFEST_VERSION
            or manifest.get('sha256') != manifest_digest(manifest)
            or manifest.get('source_commit') != source_commit
            or manifest.get('writer_inventory') != writer_inventory):
        raise ValueError('material inventory manifest invalid or stale')
    catalog = manifest.get('catalog')
    if type(catalog) is not dict:
        raise ValueError('material catalog manifest missing')
    live = snapshot(cur, schema=catalog.get('schema'), role_names=catalog.get('role_names'))
    if live != catalog:
        raise ValueError('catalog or role inventory mismatch')


def read_manifest(path):
    """Read a versioned manifest without accepting duplicate JSON keys."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate manifest key: {key}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def literal_writer_candidates(paths):
    """Find some literal SQL candidates; never use as proof of writer coverage.

    Quoted and schema-qualified simple identifiers are handled. SQL syntax is
    not parsed; aliases, indirect calls, COPY sources,
    functions, dynamic SQL and external clients remain unclassified.
    TRUNCATE target lists containing comments, inheritance stars, or later
    ONLY modifiers are rejected rather than partially inventoried.
    """
    import ast
    import re
    ident = r'(?:"(?:[^"]|"")*"|[a-z_][a-z_0-9]*)'
    target = rf'(?:{ident}\s*\.\s*)?(?P<name>{ident})'
    target_item = rf'(?:{ident}\s*\.\s*)?({ident})'
    pattern = re.compile(
        rf'\b(?:insert\s+into|update|delete\s+from|merge\s+into|copy)'
        rf'\s+(?:only\s+)?{target}', re.I)
    truncate = re.compile(
        rf'\btruncate(?:\s+table)?\s+(?:only\s+)?(?P<targets>{target_item}'
        rf'(?:\s*,\s*{target_item})*)', re.I)
    truncate_statement = re.compile(r'\btruncate\b(?P<body>[^;]*)(?:;|$)', re.I | re.S)
    unsupported_truncate_tail = re.compile(r'/\*|--|\*|,\s*only\b', re.I)

    def canonical_name(raw):
        # PostgreSQL folds unquoted identifiers to lowercase but preserves the
        # spelling (and case) of quoted identifiers.
        return raw[1:-1].replace('""', '"') if raw.startswith('"') else raw.lower()

    def add_target(path, lineno, raw_name, found):
        name = canonical_name(raw_name)
        if name in COVERED:
            found.add((str(path), lineno, name))
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is str:
                for statement in truncate_statement.finditer(node.value):
                    if unsupported_truncate_tail.search(statement.group('body')):
                        raise ValueError('unsupported TRUNCATE target-list syntax')
                for match in pattern.finditer(node.value):
                    add_target(path, node.lineno, match.group('name'), found)
                for match in truncate.finditer(node.value):
                    for target_match in re.finditer(target_item, match.group('targets'), re.I):
                        add_target(path, node.lineno, target_match.group(1), found)
    return frozenset(found)


def require_literal_writer_coverage(paths, approved):
    if type(approved) is not frozenset:
        raise ValueError('independently approved writer set required')
    unknown = literal_writer_candidates(paths) - approved
    if unknown:
        raise ValueError('unclassified literal writer')


def dynamic_sql_candidates(paths):
    """Conservatively list dynamic execute calls, including reads; manually classify all.

    This is a discovery aid: indirect calls, aliases and external clients require
    separate inventory, and a matching Python expression is not proof of a write.
    """
    import ast
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {'execute', 'executemany'} and node.args
                    and not (isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str)):
                found.add((str(path), node.lineno))
    return frozenset(found)


def require_dynamic_sql_classification(paths, classified):
    if type(classified) is not frozenset:
        raise ValueError('independently classified dynamic SQL set required')
    if dynamic_sql_candidates(paths) - classified:
        raise ValueError('unclassified dynamic SQL')


def build_writer_inventory(paths, classifications):
    """Build a deterministic, manually classified source-writer inventory.

    The parser finds candidates only. Review must separately account for
    indirect calls, stored procedures, migrations, and external clients.
    """
    if type(classifications) is not dict:
        raise ValueError('explicit writer classifications required')
    literals = literal_writer_candidates(paths)
    dynamics = dynamic_sql_candidates(paths)
    expected = {
        f'literal:{path}:{line}:{table}' for path, line, table in literals
    } | {
        f'dynamic:{path}:{line}' for path, line in dynamics
    }
    if set(classifications) != expected:
        raise ValueError('writer candidate missing or extra classification')
    entries = []
    for key in sorted(expected):
        classification = classifications[key]
        if type(classification) is not str or not classification.strip():
            raise ValueError('writer classification must be explicit')
        entries.append({'candidate': key, 'classification': classification.strip()})
    return {'version': 1, 'scope': 'Python literal SQL and dynamic execute discovery only',
            'entries': entries,
            'classifications': {entry['candidate']: entry['classification'] for entry in entries},
            'limitations': ['not a SQL parser', 'indirect calls need separate review',
                            'stored procedures and migrations need separate review',
                            'external clients are not discovered']}


def require_writer_inventory(paths, expected):
    """Compare discovered candidates with the explicitly reviewed inventory."""
    if (type(expected) is not dict or expected.get('version') != 1
            or not isinstance(expected.get('classifications'), dict)):
        raise ValueError('writer inventory is malformed or unsupported')
    actual = build_writer_inventory(paths, expected['classifications'])
    for key in ('version', 'scope', 'entries', 'limitations'):
        if expected.get(key) != actual.get(key):
            raise ValueError('writer inventory mismatch')


def verify_manifest(cur, manifest, *, source_commit, source_paths):
    """Verify a reviewed manifest against a read-only source/catalog snapshot."""
    if (type(manifest) is not dict or manifest.get('manifest_version') != MANIFEST_VERSION
            or manifest.get('sha256') != manifest_digest(manifest)
            or manifest.get('source_commit') != source_commit):
        raise ValueError('material inventory manifest invalid or stale')
    expected_paths = manifest.get('source_files')
    if (type(expected_paths) is not list or not expected_paths
            or any(type(path) is not str or not path for path in expected_paths)
            or expected_paths != sorted(set(expected_paths))):
        raise ValueError('manifest source file list missing')
    actual_by_name = {Path(path).as_posix(): Path(path) for path in source_paths}
    if set(expected_paths) != set(actual_by_name):
        raise ValueError('source file inventory mismatch')
    expected_digests = manifest.get('source_sha256')
    if (type(expected_digests) is not dict
            or set(expected_digests) != set(expected_paths)):
        raise ValueError('source digest inventory missing or malformed')
    actual_digests = {
        name: hashlib.sha256(actual_by_name[name].read_bytes()).hexdigest()
        for name in expected_paths
    }
    if actual_digests != expected_digests:
        raise ValueError('source file contents changed')
    absolute_paths = [actual_by_name[path] for path in expected_paths]
    require_writer_inventory(absolute_paths, manifest.get('writer_inventory'))
    catalog = manifest.get('catalog')
    if type(catalog) is not dict:
        raise ValueError('material catalog manifest missing')
    live = snapshot(cur, schema=catalog.get('schema'), role_names=catalog.get('role_names'))
    if live != catalog:
        raise ValueError('catalog or role inventory mismatch')


def verify_postgres_manifest(conn, manifest, *, source_commit, source_paths):
    """Run the audit in a read-only transaction on PostgreSQL 16 only."""
    with conn.transaction():
        cur = conn.cursor()
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute('SHOW server_version_num')
        version = int(cur.fetchone()['server_version_num'])
        if not 160000 <= version < 170000:
            raise ValueError('material inventory requires PostgreSQL 16')
        cur.execute("SELECT current_database() AS database_name, current_user AS role_name, "
                    "r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls, "
                    "d.datdba = r.oid AS owns_database, shobj_description(d.oid, 'pg_database') AS comment "
                    "FROM pg_database d CROSS JOIN pg_roles r "
                    "WHERE d.datname=current_database() AND r.rolname=current_user")
        identity = cur.fetchone()
        if (identity is None or not identity['database_name'].startswith('sprintctl_test')
                or not identity['role_name'].startswith('sprintctl_test')
                or identity['comment'] != 'sprintctl:disposable-integration-test'
                or not identity['owns_database']
                or any(identity[attr] for attr in ('rolsuper', 'rolcreatedb', 'rolcreaterole',
                                                    'rolreplication', 'rolbypassrls'))):
            raise ValueError('material inventory requires the repository disposable test database')
        verify_manifest(cur, manifest, source_commit=source_commit, source_paths=source_paths)
