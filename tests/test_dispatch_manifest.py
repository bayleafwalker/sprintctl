import json
from pathlib import Path
from uuid import UUID


def test_dispatch_manifest_uses_a_committed_uuid_repository_identity():
    manifest_path = Path(__file__).parents[1] / "sprintctl.dispatch.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    repository_id = manifest["repo_id"]

    assert repository_id == str(UUID(repository_id))
