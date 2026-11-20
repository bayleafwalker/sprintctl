"""Storage for the source snapshot and destination identity map of a restore."""


def install(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS work_archive_restore (
            repo_id text PRIMARY KEY,
            workspace_id text NOT NULL,
            archive_digest text NOT NULL CHECK (archive_digest ~ '^[0-9a-f]{64}$'),
            restored_by text NOT NULL CHECK (btrim(restored_by) <> ''),
            restored_at timestamptz NOT NULL DEFAULT now(),
            source_archive jsonb NOT NULL CHECK (jsonb_typeof(source_archive) = 'object'),
            item_ids jsonb NOT NULL CHECK (jsonb_typeof(item_ids) = 'object')
        )
    """)
