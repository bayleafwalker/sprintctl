from __future__ import annotations

import json

import pytest

from sprintctl.material_inventory import (
    build_writer_inventory,
    manifest_digest,
    read_manifest,
    require_writer_inventory,
    source_digests,
    verify_manifest,
)


def test_writer_manifest_requires_exact_candidates_and_explicit_classification(tmp_path):
    source = tmp_path / 'writer.py'
    source.write_text('sql = "UPDATE work_item SET title = %s"\ncursor.execute(query)\n')
    candidates = {
        f'literal:{source}:1:work_item': 'work mutation API',
        f'dynamic:{source}:2': 'reviewed helper query; target resolved by caller',
    }
    inventory = build_writer_inventory([source], candidates)
    require_writer_inventory([source], inventory)

    source.write_text(source.read_text() + 'other = "DELETE FROM dep WHERE id=%s"\n')
    with pytest.raises(ValueError, match='writer candidate missing or extra classification'):
        require_writer_inventory([source], inventory)


def test_writer_inventory_rejects_empty_classification(tmp_path):
    source = tmp_path / 'writer.py'
    source.write_text('sql = "DELETE FROM dep WHERE id=%s"\n')
    with pytest.raises(ValueError, match='classification must be explicit'):
        build_writer_inventory([source], {f'literal:{source}:1:dep': '  '})


def test_manifest_digest_ignores_its_own_digest_but_covers_all_other_fields():
    manifest = {'manifest_version': 1, 'source_commit': 'a' * 40,
                'source_files': ['sprintctl/pg.py'], 'writer_inventory': {'version': 1}}
    manifest['sha256'] = manifest_digest(manifest)
    assert manifest_digest(manifest) == manifest['sha256']
    manifest['source_files'].append('sprintctl/db.py')
    assert manifest_digest(manifest) != manifest['sha256']


def test_manifest_reader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text('{"manifest_version":1,"manifest_version":1}')
    with pytest.raises(ValueError, match='duplicate manifest key'):
        read_manifest(path)


def test_manifest_digest_uses_deterministic_canonical_json():
    a = {'manifest_version': 1, 'source_commit': 'a' * 40, 'source_files': ['a', 'b']}
    b = {'source_files': ['a', 'b'], 'source_commit': 'a' * 40, 'manifest_version': 1}
    assert manifest_digest(a) == manifest_digest(b)


@pytest.mark.parametrize('mutation', ['source', 'digest'])
def test_manifest_verification_refuses_stale_or_tampered_input_before_catalog_access(mutation):
    manifest = {'manifest_version': 1, 'source_commit': 'a' * 40,
                'source_files': ['sprintctl/pg.py'], 'writer_inventory': {}, 'sha256': ''}
    manifest['sha256'] = manifest_digest(manifest)
    source_commit = 'a' * 40
    if mutation == 'source':
        source_commit = 'b' * 40
    else:
        manifest['source_commit'] = 'b' * 40
    with pytest.raises(ValueError, match='manifest invalid or stale'):
        verify_manifest(None, manifest, source_commit=source_commit,
                        source_paths=['sprintctl/pg.py'])


def test_manifest_verification_detects_same_line_sql_source_change(tmp_path):
    source = tmp_path / 'writer.py'
    source.write_text('sql = "UPDATE work_item SET title=1"\n')
    source_files = [str(source)]
    writer_inventory = build_writer_inventory(
        [source], {f'literal:{source}:1:work_item': 'work item writer'})
    manifest = {'manifest_version': 1, 'source_commit': 'a' * 40,
                'source_files': source_files, 'source_sha256': source_digests(source_files),
                'writer_inventory': writer_inventory, 'catalog': {}}
    manifest['sha256'] = manifest_digest(manifest)

    # Candidate location and target remain identical; only the SQL body changes.
    source.write_text('sql = "UPDATE work_item SET title=2"\n')
    assert build_writer_inventory(
        [source], {f'literal:{source}:1:work_item': 'work item writer'}) == writer_inventory
    with pytest.raises(ValueError, match='source file contents changed'):
        verify_manifest(None, manifest, source_commit='a' * 40, source_paths=[source])
