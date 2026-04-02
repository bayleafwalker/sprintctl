import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


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
    assert str(resolved).startswith(str(repo_root))
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


def test_phase4_docs_files_exist():
    expected_files = [
        "docs/customization.md",
        "docs/advanced/coordinator-mode.md",
        "docs/advanced/claim-discipline.md",
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
        "README.md", "Claim Discipline", "docs/advanced/claim-discipline.md"
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


def test_start_here_links_phase4_docs():
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Customization Guide", "../customization.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Coordinator Mode", "../advanced/coordinator-mode.md"
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/start-here.md", "Claim Discipline", "../advanced/claim-discipline.md"
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


def test_advanced_coordination_links_phase4_docs():
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/advanced-coordination.md",
        "Coordinator Mode",
        "../advanced/coordinator-mode.md",
    )
    _assert_markdown_link_declared_and_resolves(
        "docs/guides/advanced-coordination.md",
        "Claim Discipline",
        "../advanced/claim-discipline.md",
    )


def test_phase4_docs_local_markdown_links_resolve():
    phase4_docs = [
        "docs/customization.md",
        "docs/advanced/coordinator-mode.md",
        "docs/advanced/claim-discipline.md",
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
