import json
from pathlib import Path
from types import SimpleNamespace

from sprintctl import db
from sprintctl import project
from sprintctl import cli as cli_module
from sprintctl.cli import cli


PROJECT_ID = "981b2073-d7af-4c28-bff3-3cf807495fba"


def _write_project(
    path: Path, members: list[tuple[str, bool]], *, home_repo: str
) -> Path:
    lines = [
        "schema_version = 1",
        f'project_id = "{PROJECT_ID}"',
        'display_name = "vuoro"',
        f'home_repo = "{home_repo}"',
        "",
    ]
    for repo_id, backlog in members:
        lines.extend(
            [
                "[[members]]",
                f'repo_id = "{repo_id}"',
                f"backlog = {str(backlog).lower()}",
                'render = "none"',
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _repo_db(tmp_path: Path, repo_id: str, title: str):
    conn = db.get_connection(tmp_path / f"{repo_id}.db")
    db.init_db(conn)
    sprint_id = db.create_sprint(
        conn,
        f"{repo_id} sprint",
        f"{repo_id} goal",
        "2026-07-01",
        "2026-07-31",
        "active",
    )
    track_id = db.get_or_create_track(conn, sprint_id, "project")
    item_id = db.create_work_item(conn, sprint_id, track_id, title)
    return conn, sprint_id, item_id


def _union_scopes(monkeypatch, binding, scopes):
    monkeypatch.setattr(
        cli_module,
        "_get_project_stores",
        lambda _obj, _project_path: (binding, scopes),
    )


def test_project_binding_resolves_direct_and_materialized_context(tmp_path):
    home = tmp_path / "agentops"
    home.mkdir()
    project_path = _write_project(
        home / "project.toml",
        [("agentops", True), ("sprintctl", True), ("ignored", False)],
        home_repo="agentops",
    )

    direct = project.load_project(project.resolve_project_path(home))
    assert direct.path == project_path
    assert [member.repo_id for member in direct.backlog_members] == [
        "agentops",
        "sprintctl",
    ]

    folder = tmp_path / "vuoro-scope"
    nested = folder / "members" / "sprintctl" / "src"
    nested.mkdir(parents=True)
    (folder / "project.context.json").write_text(
        json.dumps({"canonical_project": str(project_path)}), encoding="utf-8"
    )
    assert project.resolve_project_path(nested) == project_path


def test_project_binding_rejects_duplicate_members(tmp_path):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("agentops", True)],
        home_repo="agentops",
    )
    try:
        project.load_project(project_path)
    except project.ProjectConfigError as exc:
        assert "must be unique" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("duplicate project member was accepted")


def test_remote_project_stores_use_each_repo_discriminator(tmp_path, monkeypatch):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("sprintctl", True), ("kctl", False)],
        home_repo="agentops",
    )

    class Store:
        def __init__(self, conn, repo_id):
            self.conn = conn
            self.repo_id = repo_id

    module = SimpleNamespace(PgStore=Store)

    def fake_get_store(obj):
        obj["backend_config"] = SimpleNamespace(mode="remote", repo_id="agentops")
        return Store(object(), "agentops"), module

    monkeypatch.setattr(cli_module, "_get_store", fake_get_store)
    binding, scopes = cli_module._get_project_stores({}, project_path)

    assert binding.project_id == PROJECT_ID
    assert [repo_id for repo_id, _store, _module in scopes] == ["agentops", "sprintctl"]
    assert [store.repo_id for _repo_id, store, _module in scopes] == [
        "agentops",
        "sprintctl",
    ]
    assert scopes[0][1].conn is scopes[1][1].conn


def test_local_multi_repo_project_is_declined(runner, tmp_path):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("sprintctl", True)],
        home_repo="agentops",
    )

    result = runner.invoke(
        cli, ["item", "list", "--project", str(project_path), "--json"]
    )

    assert result.exit_code != 0
    assert (
        "multi-repository --project views require the remote backend" in result.output
    )


def test_usage_project_requires_context(runner, tmp_path):
    project_path = _write_project(
        tmp_path / "project.toml", [(tmp_path.name, True)], home_repo=tmp_path.name
    )

    result = runner.invoke(cli, ["usage", "--project", str(project_path)])

    assert result.exit_code != 0
    assert "--project requires --context" in result.output


def test_project_option_without_value_resolves_from_cwd(runner, tmp_path):
    _write_project(
        tmp_path / "project.toml", [(tmp_path.name, True)], home_repo=tmp_path.name
    )

    result = runner.invoke(cli, ["item", "list", "--project", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_served_project_views_do_not_read_client_binding_and_keep_sprint_json_shape(
    runner, monkeypatch
):
    """The server, not a client project.toml, is the project authority."""
    config = cli_module._backend.BackendConfig(
        mode="served",
        url=None,
        repo_root=None,
        repo_id="single",
        repo_source="test",
        marker=None,
        served_profile=cli_module._backend.ServedProfile(
            name="test",
            endpoint="https://vuoro.test/",
            credential_ref="file:test",
            expected_environment="test",
            source_path=Path("profile.json"),
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_served_config_or_none",
        lambda obj: obj.setdefault("backend_config", config) or config,
    )
    monkeypatch.setattr(
        cli_module._project,
        "load_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("served project command read client binding")
        ),
    )
    monkeypatch.setattr(
        cli_module._served,
        "project_sprints",
        lambda *_args, **_kwargs: {
            "contract_version": "project-1",
            "project": {"project_id": PROJECT_ID},
            "sprints": [{"id": 4, "name": "Backlog", "origin_repo": "agentops"}],
            "repositories": [{"origin_repo": "agentops", "status": "ok", "sprints": []}],
        },
    )
    sprints = runner.invoke(cli, ["sprint", "list", "--project", "missing.toml", "--json"])
    assert sprints.exit_code == 0, sprints.output
    assert json.loads(sprints.output) == [
        {"id": 4, "name": "Backlog", "origin_repo": "agentops"}
    ]

    project_context = {
        "contract_version": "project-1",
        "project": {"project_id": PROJECT_ID},
        "summary": {}, "sprints": [], "active_claims": [],
        "active_unclaimed_items": [], "conflicts": [], "ready_items": [],
        "blocked_items": [], "stale_items": [], "recent_decisions": [],
        "next_actions": [], "repositories": [],
    }
    monkeypatch.setattr(
        cli_module._served, "project_context", lambda *_args, **_kwargs: project_context
    )
    context = runner.invoke(cli, ["usage", "--context", "--project", "missing.toml", "--json"])
    assert context.exit_code == 0, context.output
    assert json.loads(context.output) == project_context


def test_next_work_unions_repo_scopes_with_origin(runner, tmp_path, monkeypatch):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("sprintctl", True)],
        home_repo="agentops",
    )
    binding = project.load_project(project_path)
    first, _first_sprint, first_item = _repo_db(tmp_path, "agentops", "Render project")
    second, _second_sprint, second_item = _repo_db(
        tmp_path, "sprintctl", "Union backlogs"
    )
    _union_scopes(
        monkeypatch,
        binding,
        [("agentops", first, db), ("sprintctl", second, db)],
    )

    result = runner.invoke(cli, ["next-work", "--project", str(project_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [(item["origin_repo"], item["id"]) for item in payload] == [
        ("agentops", first_item),
        ("sprintctl", second_item),
    ]
    assert {item["title"] for item in payload} == {"Render project", "Union backlogs"}


def test_project_scope_prefers_backlog_sprint_over_active_sprint(
    runner, tmp_path, monkeypatch
):
    project_path = _write_project(
        tmp_path / "project.toml", [("agentops", True)], home_repo="agentops"
    )
    binding = project.load_project(project_path)
    conn, _active_sprint, _active_item = _repo_db(tmp_path, "agentops", "Active work")
    backlog_id = db.create_sprint(
        conn,
        "Agentops backlog",
        "backlog",
        None,
        None,
        "planned",
        kind="backlog",
    )
    track_id = db.get_or_create_track(conn, backlog_id, "project")
    backlog_item = db.create_work_item(conn, backlog_id, track_id, "Backlog work")
    _union_scopes(monkeypatch, binding, [("agentops", conn, db)])

    result = runner.invoke(cli, ["next-work", "--project", str(project_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [(item["id"], item["title"]) for item in payload] == [
        (backlog_item, "Backlog work")
    ]


def test_next_work_explain_keeps_per_repo_contracts(runner, tmp_path, monkeypatch):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("sprintctl", True)],
        home_repo="agentops",
    )
    binding = project.load_project(project_path)
    first, _first_sprint, _first_item = _repo_db(tmp_path, "agentops", "Ready A")
    second, _second_sprint, _second_item = _repo_db(tmp_path, "sprintctl", "Ready B")
    _union_scopes(
        monkeypatch,
        binding,
        [("agentops", first, db), ("sprintctl", second, db)],
    )

    result = runner.invoke(
        cli,
        ["next-work", "--project", str(project_path), "--json", "--explain"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["contract_version"] == "project-1"
    assert payload["summary"] == {
        "repositories": 2,
        "repositories_with_sprints": 2,
        "ready": 2,
    }
    assert [item["origin_repo"] for item in payload["ready_items"]] == [
        "agentops",
        "sprintctl",
    ]
    for repository in payload["repositories"]:
        assert (
            repository["next_work"]["sprint"]["origin_repo"]
            == repository["origin_repo"]
        )
        assert all(
            item["origin_repo"] == repository["origin_repo"]
            for item in repository["next_work"]["ready_items"]
        )


def test_usage_context_unions_summaries_and_attributes_items(
    runner, tmp_path, monkeypatch
):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("sprintctl", True)],
        home_repo="agentops",
    )
    binding = project.load_project(project_path)
    first, _first_sprint, _first_item = _repo_db(tmp_path, "agentops", "Ready A")
    second, _second_sprint, _second_item = _repo_db(tmp_path, "sprintctl", "Ready B")
    _union_scopes(
        monkeypatch,
        binding,
        [("agentops", first, db), ("sprintctl", second, db)],
    )

    result = runner.invoke(
        cli,
        ["usage", "--context", "--project", str(project_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["contract_version"] == "project-1"
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["ready"] == 2
    assert [item["origin_repo"] for item in payload["ready_items"]] == [
        "agentops",
        "sprintctl",
    ]
    assert [sprint["origin_repo"] for sprint in payload["sprints"]] == [
        "agentops",
        "sprintctl",
    ]


def test_project_item_and_sprint_lists_include_origin(runner, tmp_path, monkeypatch):
    project_path = _write_project(
        tmp_path / "project.toml",
        [("agentops", True), ("sprintctl", True)],
        home_repo="agentops",
    )
    binding = project.load_project(project_path)
    first, _first_sprint, _first_item = _repo_db(tmp_path, "agentops", "Ready A")
    second, _second_sprint, _second_item = _repo_db(tmp_path, "sprintctl", "Ready B")
    _union_scopes(
        monkeypatch,
        binding,
        [("agentops", first, db), ("sprintctl", second, db)],
    )

    items = runner.invoke(
        cli, ["item", "list", "--project", str(project_path), "--json"]
    )
    sprints = runner.invoke(
        cli, ["sprint", "list", "--project", str(project_path), "--json"]
    )

    assert items.exit_code == 0, items.output
    assert sprints.exit_code == 0, sprints.output
    assert [item["origin_repo"] for item in json.loads(items.output)] == [
        "agentops",
        "sprintctl",
    ]
    assert [sprint["origin_repo"] for sprint in json.loads(sprints.output)] == [
        "agentops",
        "sprintctl",
    ]


def test_no_project_context_is_byte_identical_when_binding_appears(
    runner, conn, active_sprint, tmp_path
):
    track_id = db.get_or_create_track(conn, active_sprint["id"], "project")
    db.create_work_item(conn, active_sprint["id"], track_id, "Standalone work")

    before_next = runner.invoke(cli, ["next-work", "--json"])
    before_context = runner.invoke(cli, ["usage", "--context", "--json"])
    assert before_next.exit_code == 0, before_next.output
    assert before_context.exit_code == 0, before_context.output

    _write_project(
        tmp_path / "project.toml",
        [(tmp_path.name, True)],
        home_repo=tmp_path.name,
    )

    after_next = runner.invoke(cli, ["next-work", "--json"])
    after_context = runner.invoke(cli, ["usage", "--context", "--json"])

    assert after_next.exit_code == 0, after_next.output
    assert after_context.exit_code == 0, after_context.output
    assert after_next.output == before_next.output
    assert after_context.output == before_context.output
