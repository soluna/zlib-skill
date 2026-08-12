"""Repository-level checks for the public self-contained Skill."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
REQUIRED_PUBLIC_FILES = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "INSTALL.md",
    "LICENSE",
    "OPEN_SOURCE_GUIDE.md",
    "OPEN_SOURCE_READINESS.md",
    "README.md",
    "RELEASE_NOTES.md",
    "ROADMAP.md",
    "SECURITY.md",
    "SKILL.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    "agents/openai.yaml",
}
REQUIRED_SKILL_FILES = {
    "references/troubleshooting.md",
    "scripts/run.py",
    "scripts/requirements.in",
    "scripts/requirements.lock",
    "scripts/zlib_anna/__init__.py",
    "plugins/zlib-skill/.codex-plugin/plugin.json",
    "plugins/zlib-skill/skills/zlib-skill/SKILL.md",
    "plugins/zlib-skill/scripts/run.py",
    "plugins/zlib-skill/scripts/zlib_anna/engine.py",
    "plugins/zlib-skill/scripts/zlib_anna/network_safety.py",
    "plugins/zlib-skill/scripts/zlib_anna/zlibrary.py",
}


def test_required_public_and_skill_files_exist():
    required = REQUIRED_PUBLIC_FILES | REQUIRED_SKILL_FILES
    missing = sorted(path for path in required if not (ROOT / path).is_file())

    assert missing == []


def test_relative_markdown_links_resolve():
    markdown_files = list(ROOT.glob("*.md"))
    broken = []
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    for markdown_file in markdown_files:
        text = markdown_file.read_text(encoding="utf-8")
        for target in pattern.findall(text):
            target = target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (markdown_file.parent / target).exists():
                broken.append(f"{markdown_file.name}: {target}")

    assert broken == []


def test_skill_frontmatter_uses_new_name_and_supported_keys():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    metadata = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    keys = {line.split(":", 1)[0].strip() for line in frontmatter.splitlines() if ":" in line}

    assert keys == {"name", "description"}
    assert "name: zlib-skill" in frontmatter
    assert "$zlib-skill" in metadata
    assert "$zlib-anna-skill" not in metadata


def test_readme_leads_with_single_skill_install_request():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    first_screen = "\n".join(text.splitlines()[:60])

    assert "只要告诉 Agent 书名、作者，或者你记得的一点线索" in first_screen
    assert "快速安装" in first_screen
    assert "> 请帮我安装这个 Agent Skill：https://github.com/soluna/zlib-skill" in first_screen
    assert "仓库根目录就是" not in first_screen
    assert "{baseDir}" not in first_screen
    assert "pipx" not in first_screen
    assert "ZLIB_SKILL_" not in first_screen
    assert "schema" not in first_screen


def test_readme_shows_natural_requests_and_keeps_technical_details_elsewhere():
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "装好后这样用" in text
    assert "帮我找《" in text
    assert "下载第 2 本" in text
    assert "没有 Z-Library 账号" in text
    assert "## English" in text
    assert "Please install this Agent Skill: https://github.com/soluna/zlib-skill" in text
    assert "ZLIB_SKILL_RUNTIME_DIR" not in text
    assert "--require-hashes" not in text
    assert "schema 2" not in text


def test_install_guide_has_one_install_surface_and_isolated_verification():
    text = (ROOT / "INSTALL.md").read_text(encoding="utf-8")

    assert "--repo soluna/zlib-skill" in text
    assert "--path ." in text
    assert "--name zlib-skill" in text
    assert "scripts/run.py" in text
    assert "ZLIB_SKILL_CONFIG_DIR=" in text
    assert "/tmp/zlib-skill-install-check" in text
    assert "pipx install" not in text
    assert "must not claim" in text


def test_skill_invokes_only_the_bundled_runner():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    troubleshooting = (ROOT / "references" / "troubleshooting.md").read_text(encoding="utf-8")

    assert "python3 {baseDir}/scripts/run.py" in text
    assert "references/troubleshooting.md" in text
    assert "pipx" in troubleshooting  # Explicitly forbidden as a fallback.
    assert "pipx install" not in troubleshooting
    assert "zlib-cli " not in text


def test_skill_searches_both_sources_without_login_and_guides_login_only_for_download():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "Search Z-Library anonymously as well as Anna's Archive" in text
    assert "Do not ask the user to\nlog in just to get more results" in text
    assert "explicitly wants to download a Z-Library result" in text


def test_skill_presents_clear_choices_before_downloading():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "Show a short, readable list" in text
    assert "title, author, language, format" in text
    assert "Never turn a search into a download without the user's clear intent" in text
    assert 'download "<result_id>"' in text


def test_runtime_lock_pins_and_hashes_every_package():
    text = (ROOT / "scripts" / "requirements.lock").read_text(encoding="utf-8")
    package_starts = [
        match.start() for match in re.finditer(r"(?m)^[a-z0-9][a-z0-9-]*==[^\n]+", text)
    ]

    assert package_starts
    for index, start in enumerate(package_starts):
        end = package_starts[index + 1] if index + 1 < len(package_starts) else len(text)
        assert "--hash=sha256:" in text[start:end]


def test_runtime_and_development_dependencies_are_installed_separately():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    install_docs = [
        (ROOT / path).read_text(encoding="utf-8")
        for path in ("README.md", "CONTRIBUTING.md", "OPEN_SOURCE_GUIDE.md")
    ]

    assert "python -m pip install --require-hashes -r scripts/requirements.lock" in workflow
    assert "python -m pip install -r requirements-dev.txt" in workflow

    for text in [workflow, *install_docs]:
        assert "scripts/requirements.lock -r requirements-dev.txt" not in text


def test_repository_has_no_standalone_python_package_surface():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert not (ROOT / "MANIFEST.in").exists()
    assert not (ROOT / "requirements.txt").exists()
    assert list(ROOT.glob("*.py")) == []
    assert "[project]" not in pyproject
    assert "[project.scripts]" not in pyproject
    assert "[build-system]" not in pyproject


def test_active_install_docs_use_new_repository_url():
    for path in ("README.md", "INSTALL.md", "SKILL.md", "CONTRIBUTING.md", "SECURITY.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "github.com/soluna/zlib-cli" not in text
        assert "github.com/soluna/zlib-anna-skill" not in text
