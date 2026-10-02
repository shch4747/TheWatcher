"""Import-boundary characterization (ADR-0003, ADR-0013, AGENTS.md).

Production code under `shared/` and the v1 agents may only import another
package through that package's `interface.py`. Frozen agents are excluded.
This is the lock that used to be convention-only.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PACKAGES_WITH_INTERFACE = (
    "shared.gateway",
    "shared.wiki",
    "shared.models",
    "shared.scheduler",
    "shared.observability",
    "shared.inbox",
    "shared.cms",
    "agents.wa_agent",
    "agents.project_agent",
)

FROZEN_PREFIXES = ("agents.innovation_agent", "agents.research_agent")
SKIP_DIRS = {"tests", "scripts", "skills", "docs", ".venv", "node_modules"}


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _owning_package(module: str) -> str | None:
    for package in PACKAGES_WITH_INTERFACE:
        if module == package or module.startswith(package + "."):
            return package
    return None


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in SKIP_DIRS:
            continue
        if rel.name == "main.py" or rel.parts[0] in {"shared", "agents"}:
            files.append(path)
    return files


def test_cross_package_imports_go_through_interface():
    violations: list[str] = []
    for path in _production_python_files():
        module = _module_name(path)
        if module.startswith(FROZEN_PREFIXES):
            continue
        owner = _owning_package(module)
        if owner is None:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for imported in _imported_modules(tree):
            imported_pkg = _owning_package(imported)
            if imported_pkg is None or imported_pkg == owner:
                continue
            allowed = imported_pkg + ".interface"
            if imported != allowed and not imported.startswith(allowed + "."):
                violations.append(f"{module} imports {imported} (want {allowed})")
    assert violations == []


def test_agents_do_not_import_shared_db():
    violations: list[str] = []
    for path in (ROOT / "agents" / "wa_agent").rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        for imported in _imported_modules(tree):
            if imported == "shared.db" or imported.startswith("shared.db."):
                violations.append(f"{module} imports {imported}")
    for path in (ROOT / "agents" / "project_agent").rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        for imported in _imported_modules(tree):
            if imported == "shared.db" or imported.startswith("shared.db."):
                violations.append(f"{module} imports {imported}")
    assert violations == []


def test_observability_does_not_import_gateway():
    violations: list[str] = []
    for path in (ROOT / "shared" / "observability").rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        for imported in _imported_modules(tree):
            if imported == "shared.gateway" or imported.startswith("shared.gateway."):
                violations.append(f"{module} imports {imported}")
    assert violations == []


def test_v1_agents_do_not_import_each_other():
    violations: list[str] = []
    for path in (ROOT / "agents" / "wa_agent").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for imported in _imported_modules(tree):
            if imported.startswith("agents.project_agent"):
                violations.append(f"{_module_name(path)} imports {imported}")
    for path in (ROOT / "agents" / "project_agent").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for imported in _imported_modules(tree):
            if imported.startswith("agents.wa_agent"):
                violations.append(f"{_module_name(path)} imports {imported}")
    assert violations == []


def test_main_imports_packages_through_interface():
    path = ROOT / "main.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    allowed_non_interface = {
        "shared.config",
        "shared.db",
        "shared.gateway.app",
    }
    violations: list[str] = []
    for imported in _imported_modules(tree):
        pkg = _owning_package(imported)
        if pkg is None:
            continue
        allowed = pkg + ".interface"
        if imported == allowed or imported.startswith(allowed + "."):
            continue
        if imported in allowed_non_interface:
            continue
        violations.append(f"main imports {imported} (want {allowed})")
    assert violations == []
