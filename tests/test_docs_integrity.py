import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
HEADING_WITH_LEVEL_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _slugify_anchor(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = slug.replace(" ", "-")
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        anchors.add(_slugify_anchor(match.group(1)))
    return anchors


def _assert_markdown_link_declared_and_resolves(source_path: str, label: str, link: str) -> None:
    source = REPO_ROOT / source_path
    source_text = source.read_text(encoding="utf-8")
    assert f"[{label}]({link})" in source_text

    target_path, _, fragment = link.partition("#")
    resolved = (source.parent / target_path).resolve()
    repo_root = REPO_ROOT.resolve()
    assert repo_root == resolved or repo_root in resolved.parents
    assert resolved.exists()
    assert resolved.is_file()

    if fragment:
        assert fragment in _markdown_anchors(resolved)


def _iter_local_markdown_links(path: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for label, target in MD_LINK_RE.findall(_read(path)):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if ".md" not in target:
            continue
        links.append((label, target))
    return links


def _iter_local_markdown_links_in_section(path: str, heading: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    in_section = False
    section_level = -1

    for line in _read(path).splitlines():
        heading_match = HEADING_WITH_LEVEL_RE.match(line.strip())
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2)
            if in_section and level <= section_level:
                break
            if not in_section and title == heading:
                in_section = True
                section_level = level
                continue

        if not in_section:
            continue

        for label, target in MD_LINK_RE.findall(line):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if ".md" not in target:
                continue
            links.append((label, target))

    assert in_section, f"Missing section heading '{heading}' in {path}"
    return links


def test_phase4_docs_files_exist():
    expected_files = [
        "docs/customization.md",
        "docs/advanced/coordinator-mode.md",
        "docs/advanced/reservation-discipline.md",
        "docs/advanced/takeup.md",
        "docs/examples/repo-template.md",
    ]
    missing = [path for path in expected_files if not (REPO_ROOT / path).exists()]
    assert missing == []


def test_phase3_docs_files_exist():
    expected_files = [
        "docs/guides/daily-loop.md",
        "docs/examples/alias-pack.md",
        "docs/examples/agent-prompt-snippets.md",
        "docs/examples/editor-and-terminal-integration.md",
    ]
    missing = [path for path in expected_files if not (REPO_ROOT / path).exists()]
    assert missing == []


def test_readme_links_phase4_docs():
    _assert_markdown_link_declared_and_resolves(
        "README.md", "Customization Guide", "docs/customization.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "README.md", "Coordinator Mode", "docs/advanced/coordinator-mode.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "README.md", "Reservation Discipline", "docs/advanced/reservation-discipline.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "README.md", "repo-template.md", "docs/examples/repo-template.md"
    )


def test_readme_links_phase3_docs():
    _assert_markdown_link_declared_and_resolves(
        "README.md", "Daily Loop", "docs/guides/daily-loop.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "README.md", "alias-pack.md", "docs/examples/alias-pack.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "README.md",
        "agent-prompt-snippets.md",
        "docs/examples/agent-prompt-snippets.md",
    )
    _assert_markdown_link_declared_and_resolves(
        "README.md",
        "editor-and-terminal-integration.md",
        "docs/examples/editor-and-terminal-integration.md",
    )


def test_capability_receipt_reference_is_linked_from_operator_surfaces():
    _assert_markdown_link_declared_and_resolves(
        "README.md",
        "Capability Receipts",
        "docs/reference/capability-receipts.md",
    )
    _assert_markdown_link_declared_and_resolves(
        "AGENTS.md",
        "Capability receipts at sprint close",
        "docs/reference/capability-receipts.md",
    )


def test_capability_receipt_reference_pins_private_draft_and_human_ratification():
    reference = _read("docs/reference/capability-receipts.md")
    normalized_reference = " ".join(reference.split())
    manifest = json.loads(_read("sprintctl.dispatch.json"))

    assert "capability-receipt" in manifest["skills"]["selected"]
    for fragment in (
        "capability-receipt/v1",
        "/projects/dev/_artifacts/<repo-id>/capability/receipts/<receipt-id>.json",
        "sprint-close-boundary",
        "boundary_revision: event:<id>",
        "capability-receipt-drafted",
        "project",
        "receipt_id",
        "receipt_path",
        "receipt_sha256",
        "Any unknown field is rejected",
        "agents must not ratify it",
        "sprintctl maintain sweep --auto-close",
        "does not count as a ratified capability boundary",
    ):
        assert fragment in normalized_reference

    # The close step is pinned by its flags, not its full command string:
    # --expected-revision is required for a direct sprint transition, so the
    # example must carry it, and a future flag must not silently break the
    # ordering check below into a ValueError.
    close_position = normalized_reference.index(
        "sprintctl sprint status --id <id> --status closed --actor <actor>"
    )
    assert "--expected-revision" in normalized_reference
    draft_position = normalized_reference.index(
        "For a supported delta, run the `capability-receipt` dispatch skill"
    )
    assert close_position < draft_position


def test_start_here_links_phase4_docs():
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Customization Guide", "../customization.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Coordinator Mode", "../advanced/coordinator-mode.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Reservation Discipline", "../advanced/reservation-discipline.md"
    )


def test_start_here_links_phase3_docs():
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Daily Loop", "daily-loop.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Alias Pack", "../examples/alias-pack.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md",
        "Agent Prompt Snippets",
        "../examples/agent-prompt-snippets.md",
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md",
        "Editor And Terminal Integration",
        "../examples/editor-and-terminal-integration.md",
    )


def test_daily_loop_links_phase3_examples():
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/daily-loop.md", "alias-pack.md", "../examples/alias-pack.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/daily-loop.md",
        "agent-prompt-snippets.md",
        "../examples/agent-prompt-snippets.md",
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/daily-loop.md",
        "editor-and-terminal-integration.md",
        "../examples/editor-and-terminal-integration.md",
    )


def test_advanced_coordination_links_phase4_docs():
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/advanced-coordination.md",
        "Coordinator Mode",
        "../advanced/coordinator-mode.md",
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/advanced-coordination.md",
        "Reservation Discipline",
        "../advanced/reservation-discipline.md",
    )


def test_phase4_docs_local_markdown_links_resolve():
    phase4_docs = [
        "docs/customization.md",
        "docs/advanced/coordinator-mode.md",
        "docs/advanced/reservation-discipline.md",
        "docs/advanced/takeup.md",
        "docs/examples/repo-template.md",
    ]
    for source in phase4_docs:
        for label, target in _iter_local_markdown_links(source):
            _assert_markdown_link_declared_and_resolves(source, label, target)


def test_phase3_docs_local_markdown_links_resolve():
    phase3_docs = [
        "docs/guides/daily-loop.md",
        "docs/examples/alias-pack.md",
        "docs/examples/agent-prompt-snippets.md",
        "docs/examples/editor-and-terminal-integration.md",
    ]
    for source in phase3_docs:
        for label, target in _iter_local_markdown_links(source):
            _assert_markdown_link_declared_and_resolves(source, label, target)


def test_agent_protocol_mentions_takeup(runner, db_path):
    from sprintctl.cli import cli

    result = runner.invoke(cli, ["agent-protocol", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "takeup_model" in data
    assert data["takeup_model"]["event_types"] == [
        "sprint-taken-up",
        "sprint-released",
    ]


def test_phase3_examples_publish_core_sections():
    alias_pack = _read("docs/examples/alias-pack.md")
    assert "## Bash/Zsh functions" in alias_pack
    assert "## Reservation helpers (explicit handle retained)" in alias_pack
    assert "## Minimal alias-only mode" in alias_pack

    snippets = _read("docs/examples/agent-prompt-snippets.md")
    assert "## 1. Session startup snippet" in snippets
    assert "## 2. Reserve-and-execute snippet" in snippets
    assert "## 3. Coordinator + sub-agent snippet" in snippets
    assert "## 4. End-of-session snippet" in snippets

    integration = _read("docs/examples/editor-and-terminal-integration.md")
    assert "## tmux split layout" in integration
    assert "## VS Code tasks" in integration
    assert "## Neovim command wrappers" in integration


def test_readme_docs_map_links_resolve():
    links = _iter_local_markdown_links_in_section("README.md", "Docs Map")
    required = {
        ("Daily Loop", "docs/guides/daily-loop.md"),
        ("Capability Receipts", "docs/reference/capability-receipts.md"),
        ("alias-pack.md", "docs/examples/alias-pack.md"),
        ("agent-prompt-snippets.md", "docs/examples/agent-prompt-snippets.md"),
        ("editor-and-terminal-integration.md", "docs/examples/editor-and-terminal-integration.md"),
    }
    assert required.issubset(set(links))

    for label, target in links:
        _assert_markdown_link_declared_and_resolves("README.md", label, target)


def test_start_here_next_guides_links_resolve():
    links = _iter_local_markdown_links_in_section("docs/guides/start-here.md", "Next Guides")
    required = {
        ("Daily Loop", "daily-loop.md"),
        ("Alias Pack", "../examples/alias-pack.md"),
        ("Agent Prompt Snippets", "../examples/agent-prompt-snippets.md"),
        ("Editor And Terminal Integration", "../examples/editor-and-terminal-integration.md"),
    }
    assert required.issubset(set(links))

    for label, target in links:
        _assert_markdown_link_declared_and_resolves("docs/guides/start-here.md", label, target)


SKILL_COMMAND_RE = re.compile(r"\bsprintctl[ \t]+([a-z][a-z0-9-]*)(?:[ \t]+([a-z][a-z0-9-]*))?")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
FENCE_RE = re.compile(r"^\s*```")


def _code_spans(text: str) -> list[str]:
    """Only the parts of a document that are actually commands.

    Prose says things like "live sprintctl state" and "sprintctl permits overlap",
    which are not invocations. Checking those would make the test noisy enough to be
    switched off, which is worse than not having it.
    """
    spans: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            spans.append(line)
        else:
            spans.extend(INLINE_CODE_RE.findall(line))
    return spans


def _cli_surface() -> dict[str, set[str]]:
    """The command groups and subcommands the CLI actually constructs."""
    from click import Group

    from sprintctl.cli import cli

    surface: dict[str, set[str]] = {}
    for name, command in cli.commands.items():
        if isinstance(command, Group):
            surface[name] = set(command.commands)
        else:
            surface[name] = set()
    return surface


def test_skill_documents_only_cite_commands_that_exist() -> None:
    """A skill is an instruction to an agent, so a wrong command fails on first contact.

    Three canonical skills taught `sprintctl claim ...` for months after the claim
    model was deleted -- sqlite migration 19 archived the table, pg migration 9 drops
    it -- and nothing caught it, because doc integrity checked links rather than
    commands. Ordinary docs drift and get corrected by a reader; a skill is executed.
    """
    surface = _cli_surface()
    skills_root = REPO_ROOT / ".agents" / "skills"
    if not skills_root.is_dir():
        return

    unknown: list[str] = []
    for skill in sorted(skills_root.glob("*/SKILL.md")):
        spans = _code_spans(skill.read_text(encoding="utf-8"))
        for group, sub in [m for span in spans for m in SKILL_COMMAND_RE.findall(span)]:
            if group not in surface:
                unknown.append(f"{skill.relative_to(REPO_ROOT)}: sprintctl {group}")
                continue
            if sub and surface[group] and sub not in surface[group]:
                unknown.append(f"{skill.relative_to(REPO_ROOT)}: sprintctl {group} {sub}")

    assert not unknown, "skills cite commands that do not exist:\n" + "\n".join(sorted(set(unknown)))
