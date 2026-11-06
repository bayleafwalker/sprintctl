#!/usr/bin/env python3
"""Verify a reviewed material catalog/source inventory without database writes."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from psycopg import connect
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprintctl.material_inventory import read_manifest, verify_postgres_manifest  # noqa: E402


def source_commit(root: Path) -> str:
    return subprocess.check_output(
        ['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True
    ).strip()


def require_clean_checkout(root: Path) -> None:
    changed = subprocess.check_output(
        ['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'], text=True
    )
    if changed:
        raise ValueError('source checkout has modified or untracked files')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='repository root')
    parser.add_argument('--manifest', type=Path, required=True, help='reviewed manifest JSON')
    parser.add_argument('--source-url-env', default='SPRINTCTL_MATERIAL_AUDIT_URL',
                        help='environment variable containing disposable PostgreSQL URL')
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        require_clean_checkout(root)
    except Exception as exc:
        parser.error(str(exc))
    dsn = os.environ.get(args.source_url_env)
    if not dsn:
        parser.error(f'{args.source_url_env} is required')
    manifest = read_manifest(args.manifest)
    files = [root / path for path in manifest.get('source_files', [])]
    if any(not path.is_file() or not path.resolve().is_relative_to(root) for path in files):
        parser.error('manifest source path missing or outside repository')
    try:
        with connect(dsn, row_factory=dict_row, options='-c default_transaction_read_only=on') as conn:
            verify_postgres_manifest(conn, manifest, source_commit=source_commit(root),
                                     source_paths=[path.relative_to(root) for path in files])
    except Exception as exc:
        print(f'material inventory refused: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print('material inventory matches reviewed manifest')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
