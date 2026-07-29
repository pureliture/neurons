"""Structural import-direction guards for the extracted native-memory seams.

These AST checks preserve the dependency direction established by Cards 4--7.
They complement, rather than replace, behavior tests for the extracted code.
"""

from __future__ import annotations

from pathlib import Path

from support.ast_import_direction import (
    ForbiddenExport,
    ImportDirectionRule,
    violations_for,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "worker" / "lib" / "agent_knowledge"


_RULES = (
    ImportDirectionRule(
        relative_path="session_memory/regeneration_index_sync.py",
        forbidden_modules=frozenset(
            {
                "memory_regeneration",
                "agent_knowledge.session_memory.memory_regeneration",
            }
        ),
    ),
    ImportDirectionRule(
        relative_path="repository.py",
        forbidden_modules=frozenset(
            {
                "curation",
                "agent_knowledge.session_memory.curation",
            }
        ),
    ),
    ImportDirectionRule(
        relative_path="llm_brain_core/graph_scope.py",
        forbidden_modules=frozenset(
            {
                "graphiti_adapter",
                "graphiti_backend",
                "graphiti_core",
                "agent_knowledge.llm_brain_core.graphiti_adapter",
                "agent_knowledge.llm_brain_core.graphiti_backend",
                "agent_knowledge.llm_brain_core.graphiti_core",
            }
        ),
    ),
    ImportDirectionRule(
        relative_path="session_memory/autopilot_loop.py",
        forbidden_modules=frozenset(
            {
                "index_client",
                "mcp_server",
                "agent_knowledge.index_client",
                "agent_knowledge.mcp_server",
            }
        ),
        forbidden_exports=(
            ForbiddenExport(
                name="RetiredIndexBridgeMemoryCardProjectionClient",
                origin_modules=frozenset(
                    {
                        "index_projection",
                        "agent_knowledge.session_memory.index_projection",
                    }
                ),
            ),
        ),
    ),
)


def _violations_for(
    rule: ImportDirectionRule,
    *,
    package_root: Path = PACKAGE_ROOT,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    return violations_for(
        rule,
        package_root=package_root,
        repo_root=repo_root,
    )


def test_extracted_seams_keep_their_import_direction() -> None:
    """Keep extracted seams independent of their former runner and concrete edges."""

    violations = [
        violation
        for rule in _RULES
        for violation in _violations_for(rule)
    ]

    assert not violations, "Forbidden extracted-seam imports:\n" + "\n".join(violations)


def _write_fixture_source(package_root: Path, relative_path: str, source: str) -> None:
    path = package_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_guard_follows_absolute_and_relative_package_reexports(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"SessionMemoryRegenerationRunner": ".memory_regeneration"}\n',
    )
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "from agent_knowledge.session_memory import SessionMemoryRegenerationRunner\n",
    )
    _write_fixture_source(
        package_root,
        "llm_brain_core/__init__.py",
        "from .graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter\n",
    )
    _write_fixture_source(
        package_root,
        "llm_brain_core/graph_scope.py",
        "from . import GraphitiNeo4jGraphMemoryAdapter\n",
    )

    regeneration_violations = _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    )
    graph_violations = _violations_for(
        _RULES[2],
        package_root=package_root,
        repo_root=tmp_path,
    )

    assert regeneration_violations == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:1: "
        "from agent_knowledge.session_memory import SessionMemoryRegenerationRunner"
    ]
    assert graph_violations == [
        "worker/lib/agent_knowledge/llm_brain_core/graph_scope.py:1: "
        "from . import GraphitiNeo4jGraphMemoryAdapter"
    ]


def test_guard_reports_direct_absolute_and_relative_imports(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "import agent_knowledge.session_memory.memory_regeneration\n"
        "from . import memory_regeneration\n",
    )

    assert _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:1: "
        "import agent_knowledge.session_memory.memory_regeneration",
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:2: "
        "from . import memory_regeneration",
    ]


def test_guard_reports_forbidden_module_submodule_imports(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "llm_brain_core/graph_scope.py",
        "import graphiti_core.nodes\n"
        "from graphiti_core.nodes import EpisodicNode\n",
    )

    assert _violations_for(
        _RULES[2],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/llm_brain_core/graph_scope.py:1: "
        "import graphiti_core.nodes",
        "worker/lib/agent_knowledge/llm_brain_core/graph_scope.py:2: "
        "from graphiti_core.nodes import EpisodicNode",
    ]


def test_curation_service_requires_curation_module_provenance(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"CurationService": ".curation"}\n',
    )
    _write_fixture_source(
        package_root,
        "repository.py",
        "from agent_knowledge.session_memory import CurationService\n",
    )

    curation_rule = _RULES[1]
    assert _violations_for(
        curation_rule,
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/repository.py:1: "
        "from agent_knowledge.session_memory import CurationService"
    ]

    _write_fixture_source(
        package_root,
        "repository.py",
        "from unrelated_module import CurationService\n",
    )

    assert _violations_for(
        curation_rule,
        package_root=package_root,
        repo_root=tmp_path,
    ) == []


def test_guard_tracks_forbidden_package_facade_attributes(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        "_EXPORT_MODULES = {\n"
        '    "SessionMemoryRegenerationRunner": ".memory_regeneration",\n'
        '    "RetiredIndexBridgeMemoryCardProjectionClient": ".index_projection",\n'
        "}\n",
    )
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "import agent_knowledge.session_memory as sm\n"
        "sm.SessionMemoryRegenerationRunner\n",
    )
    _write_fixture_source(
        package_root,
        "session_memory/autopilot_loop.py",
        "import agent_knowledge.session_memory as sm\n"
        "sm.RetiredIndexBridgeMemoryCardProjectionClient\n",
    )
    _write_fixture_source(
        package_root,
        "llm_brain_core/__init__.py",
        "from .graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter\n",
    )
    _write_fixture_source(
        package_root,
        "llm_brain_core/graph_scope.py",
        "import agent_knowledge.llm_brain_core as core\n"
        "core.GraphitiNeo4jGraphMemoryAdapter\n",
    )

    assert _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:2: "
        "sm.SessionMemoryRegenerationRunner"
    ]
    assert _violations_for(
        _RULES[3],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/autopilot_loop.py:2: "
        "sm.RetiredIndexBridgeMemoryCardProjectionClient"
    ]
    assert _violations_for(
        _RULES[2],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/llm_brain_core/graph_scope.py:2: "
        "core.GraphitiNeo4jGraphMemoryAdapter"
    ]


def test_package_facade_import_without_forbidden_export_is_allowed(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"build_projection_job": ".index_projection"}\n',
    )
    _write_fixture_source(
        package_root,
        "session_memory/autopilot_loop.py",
        "import agent_knowledge.session_memory as sm\n"
        "sm.build_projection_job\n",
    )

    assert _violations_for(
        _RULES[3],
        package_root=package_root,
        repo_root=tmp_path,
    ) == []


def test_guard_tracks_from_import_package_and_module_aliases(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"SessionMemoryRegenerationRunner": ".memory_regeneration"}\n',
    )
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "from agent_knowledge import session_memory as sm\n"
        "sm.SessionMemoryRegenerationRunner\n",
    )
    _write_fixture_source(
        package_root,
        "session_memory/index_projection.py",
        "class RetiredIndexBridgeMemoryCardProjectionClient: ...\n",
    )
    _write_fixture_source(
        package_root,
        "session_memory/autopilot_loop.py",
        "from agent_knowledge.session_memory import index_projection as p\n"
        "p.RetiredIndexBridgeMemoryCardProjectionClient\n",
    )
    _write_fixture_source(
        package_root,
        "llm_brain_core/__init__.py",
        "from .graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter\n",
    )
    _write_fixture_source(
        package_root,
        "llm_brain_core/graph_scope.py",
        "from .. import llm_brain_core as core\n"
        "core.GraphitiNeo4jGraphMemoryAdapter\n",
    )

    assert _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:2: "
        "sm.SessionMemoryRegenerationRunner"
    ]
    assert _violations_for(
        _RULES[3],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/autopilot_loop.py:2: "
        "p.RetiredIndexBridgeMemoryCardProjectionClient"
    ]
    assert _violations_for(
        _RULES[2],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/llm_brain_core/graph_scope.py:2: "
        "core.GraphitiNeo4jGraphMemoryAdapter"
    ]


def test_non_package_from_import_alias_does_not_trigger_facade_guard(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "from unrelated_module import session_memory as sm\n"
        "sm.SessionMemoryRegenerationRunner\n",
    )

    assert _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    ) == []


def test_facade_alias_resolution_respects_function_lexical_scopes(tmp_path: Path) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"SessionMemoryRegenerationRunner": ".memory_regeneration"}\n',
    )
    rule = _RULES[0]

    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "import agent_knowledge.session_memory as sm\n"
        "\n"
        "def parameter_shadow(sm):\n"
        "    return sm.SessionMemoryRegenerationRunner\n"
        "\n"
        "def inherited_module_alias():\n"
        "    return sm.SessionMemoryRegenerationRunner\n",
    )
    assert _violations_for(
        rule,
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:7: "
        "sm.SessionMemoryRegenerationRunner"
    ]

    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "def local_alias():\n"
        "    import agent_knowledge.session_memory as sm\n"
        "    return sm.SessionMemoryRegenerationRunner\n",
    )
    assert _violations_for(
        rule,
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:3: "
        "sm.SessionMemoryRegenerationRunner"
    ]

    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "def alias_owner():\n"
        "    import agent_knowledge.session_memory as sm\n"
        "\n"
        "def unrelated_sibling():\n"
        "    return sm.SessionMemoryRegenerationRunner\n",
    )
    assert _violations_for(
        rule,
        package_root=package_root,
        repo_root=tmp_path,
    ) == []


def test_facade_alias_resolution_respects_lambda_and_comprehension_scopes(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"SessionMemoryRegenerationRunner": ".memory_regeneration"}\n',
    )
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "import agent_knowledge.session_memory as sm\n"
        "shadowed_lambda = lambda sm: sm.SessionMemoryRegenerationRunner\n"
        "inherited_lambda = lambda value: sm.SessionMemoryRegenerationRunner\n"
        "shadowed_list = [sm.SessionMemoryRegenerationRunner for sm in values]\n"
        "shadowed_set = {sm.SessionMemoryRegenerationRunner for sm in values}\n"
        "shadowed_dict = {sm.SessionMemoryRegenerationRunner: sm for sm in values}\n"
        "shadowed_generator = (sm.SessionMemoryRegenerationRunner for sm in values)\n"
        "inherited_list = [sm.SessionMemoryRegenerationRunner for value in values]\n",
    )

    assert _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:3: "
        "sm.SessionMemoryRegenerationRunner",
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:8: "
        "sm.SessionMemoryRegenerationRunner",
    ]


def test_function_comprehension_target_does_not_shadow_import_alias(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "worker" / "lib" / "agent_knowledge"
    _write_fixture_source(
        package_root,
        "session_memory/__init__.py",
        '_EXPORT_MODULES = {"SessionMemoryRegenerationRunner": ".memory_regeneration"}\n',
    )
    _write_fixture_source(
        package_root,
        "session_memory/regeneration_index_sync.py",
        "import agent_knowledge.session_memory as sm\n"
        "\n"
        "def inherits_module_alias(values):\n"
        "    seen = [value for sm in values]\n"
        "    return sm.SessionMemoryRegenerationRunner\n",
    )

    assert _violations_for(
        _RULES[0],
        package_root=package_root,
        repo_root=tmp_path,
    ) == [
        "worker/lib/agent_knowledge/session_memory/regeneration_index_sync.py:5: "
        "sm.SessionMemoryRegenerationRunner"
    ]
