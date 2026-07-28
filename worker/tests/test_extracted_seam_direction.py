"""Structural import-direction guards for the extracted native-memory seams.

These AST checks preserve the dependency direction established by Cards 4--7.
They complement, rather than replace, behavior tests for the extracted code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "worker" / "lib" / "agent_knowledge"


@dataclass(frozen=True)
class ForbiddenExport:
    name: str
    origin_modules: frozenset[str]


@dataclass(frozen=True)
class ImportDirectionRule:
    relative_path: str
    forbidden_modules: frozenset[str] = frozenset()
    forbidden_exports: tuple[ForbiddenExport, ...] = ()


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


def _module_name_for(path: Path, *, package_root: Path) -> str:
    return ".".join(
        ("agent_knowledge", *path.relative_to(package_root).with_suffix("").parts)
    )


def _resolved_import_module(
    current_module: str,
    *,
    level: int,
    module: str | None,
) -> str:
    """Resolve an absolute or relative import to a canonical module path."""

    if level == 0:
        return module or ""

    package_parts = current_module.split(".")[:-1]
    base_end = len(package_parts) - level + 1
    if base_end <= 0:
        return module or ""
    base = ".".join(package_parts[:base_end])
    return ".".join(part for part in (base, module) if part)


def _resolved_from_module(current_module: str, node: ast.ImportFrom) -> str:
    return _resolved_import_module(
        current_module,
        level=node.level,
        module=node.module,
    )


def _display_from_module(node: ast.ImportFrom) -> str:
    return f"{'.' * node.level}{node.module or ''}"


def _package_init_path(module: str, *, package_root: Path) -> Path | None:
    parts = module.split(".")
    if not parts or parts[0] != "agent_knowledge":
        return None
    return package_root.joinpath(*parts[1:], "__init__.py")


def _reexport_origins(
    package_module: str,
    export_name: str,
    *,
    package_root: Path,
    seen: frozenset[tuple[str, str]] = frozenset(),
) -> frozenset[str]:
    """Return concrete modules that a package re-exports under ``export_name``.

    The worker has both ordinary ``from .module import Name`` exports and lazy
    ``_EXPORT_MODULES`` mappings. Resolve both statically so a package facade
    cannot bypass an extracted seam boundary.
    """

    key = (package_module, export_name)
    if key in seen:
        return frozenset()

    init_path = _package_init_path(package_module, package_root=package_root)
    if init_path is None or not init_path.is_file():
        return frozenset()

    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    current_module = f"{package_module}.__init__"
    origins: set[str] = set()
    next_seen = seen | {key}

    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            imported_module = _resolved_from_module(current_module, node)
            for alias in node.names:
                if alias.name == "*" or (alias.asname or alias.name) != export_name:
                    continue
                origins.add(imported_module)
                origins.update(
                    _reexport_origins(
                        imported_module,
                        alias.name,
                        package_root=package_root,
                        seen=next_seen,
                    )
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exposed_name = alias.asname or alias.name.split(".", 1)[0]
                if exposed_name == export_name:
                    origins.add(alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Dict):
                continue
            for mapping_key, mapping_value in zip(value.keys, value.values):
                if not (
                    isinstance(mapping_key, ast.Constant)
                    and mapping_key.value == export_name
                    and isinstance(mapping_value, ast.Constant)
                    and isinstance(mapping_value.value, str)
                ):
                    continue
                module_ref = mapping_value.value
                level = len(module_ref) - len(module_ref.lstrip("."))
                origins.add(
                    _resolved_import_module(
                        current_module,
                        level=level,
                        module=module_ref[level:] or None,
                    )
                )

    return frozenset(origins)


def _matches_forbidden_export(
    rule: ImportDirectionRule,
    *,
    export_name: str,
    origin_modules: frozenset[str],
) -> bool:
    return any(
        export.name == export_name
        and bool(export.origin_modules.intersection(origin_modules))
        for export in rule.forbidden_exports
    )


def _origin_modules_for_export(
    imported_module: str,
    export_name: str,
    *,
    package_root: Path,
) -> frozenset[str]:
    return frozenset(
        {
            imported_module,
            *_reexport_origins(
                imported_module,
                export_name,
                package_root=package_root,
            ),
        }
    )


def _origin_modules_are_forbidden(
    rule: ImportDirectionRule,
    *,
    export_name: str,
    origin_modules: frozenset[str],
) -> bool:
    return bool(rule.forbidden_modules.intersection(origin_modules)) or (
        _matches_forbidden_export(
            rule,
            export_name=export_name,
            origin_modules=origin_modules,
        )
    )


def _format_violation(
    path: Path,
    *,
    repo_root: Path,
    lineno: int,
    statement: str,
) -> str:
    return f"{path.relative_to(repo_root)}:{lineno}: {statement}"


def _local_module_exists(module: str, *, package_root: Path) -> bool:
    init_path = _package_init_path(module, package_root=package_root)
    if init_path is not None and init_path.is_file():
        return True

    parts = module.split(".")
    return bool(parts and parts[0] == "agent_knowledge") and package_root.joinpath(
        *parts[1:]
    ).with_suffix(".py").is_file()


def _import_alias_bindings(
    node: ast.Import | ast.ImportFrom,
    *,
    current_module: str,
    package_root: Path,
) -> dict[str, str]:
    """Map local package/module aliases to their canonical worker module paths."""

    aliases: dict[str, str] = {}
    if isinstance(node, ast.Import):
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            aliases[local_name] = alias.name if alias.asname else local_name
        return aliases

    imported_module = _resolved_from_module(current_module, node)
    for alias in node.names:
        if alias.name == "*":
            continue
        imported_target = ".".join(
            part for part in (imported_module, alias.name) if part
        )
        if _local_module_exists(imported_target, package_root=package_root):
            aliases[alias.asname or alias.name] = imported_target
    return aliases


def _attribute_path(node: ast.Attribute) -> tuple[str, ...]:
    parts = [node.attr]
    current = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ()
    parts.append(current.id)
    return tuple(reversed(parts))


def _facade_attribute_violation(
    rule: ImportDirectionRule,
    *,
    node: ast.Attribute,
    source: str,
    aliases: dict[str, str],
    package_root: Path,
    repo_root: Path,
    path: Path,
) -> str | None:
    attribute_path = _attribute_path(node)
    if not attribute_path or attribute_path[0] not in aliases:
        return None

    resolved_parts = (*aliases[attribute_path[0]].split("."), *attribute_path[1:])
    if len(resolved_parts) < 2:
        return None
    package_module = ".".join(resolved_parts[:-1])
    export_name = resolved_parts[-1]
    origin_modules = _origin_modules_for_export(
        package_module,
        export_name,
        package_root=package_root,
    )
    if not _origin_modules_are_forbidden(
        rule,
        export_name=export_name,
        origin_modules=origin_modules,
    ):
        return None

    statement = ast.get_source_segment(source, node) or ast.unparse(node)
    return _format_violation(
        path,
        repo_root=repo_root,
        lineno=node.lineno,
        statement=statement,
    )


class _FunctionLocalBindingVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(
            alias.asname or alias.name.split(".", 1)[0] for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(
            alias.asname or alias.name for alias in node.names if alias.name != "*"
        )

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _function_local_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    bindings = _FunctionLocalBindingVisitor()
    bindings.names.update(_argument_binding_names(node.args))
    for statement in node.body:
        bindings.visit(statement)
    return bindings.names - bindings.global_names - bindings.nonlocal_names


def _argument_binding_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _comprehension_target_names(node: ast.expr) -> set[str]:
    return {
        name.id
        for name in ast.walk(node)
        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
    }


class _FacadeAttributeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        rule: ImportDirectionRule,
        *,
        source: str,
        current_module: str,
        package_root: Path,
        repo_root: Path,
        path: Path,
    ) -> None:
        self._rule = rule
        self._source = source
        self._current_module = current_module
        self._package_root = package_root
        self._repo_root = repo_root
        self._path = path
        self._alias_scopes: list[dict[str, str]] = [{}]
        self.violations: list[str] = []

    @property
    def _aliases(self) -> dict[str, str]:
        return self._alias_scopes[-1]

    def visit_Import(self, node: ast.Import) -> None:
        self._aliases.update(
            _import_alias_bindings(
                node,
                current_module=self._current_module,
                package_root=self._package_root,
            )
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._aliases.update(
            _import_alias_bindings(
                node,
                current_module=self._current_module,
                package_root=self._package_root,
            )
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        violation = _facade_attribute_violation(
            self._rule,
            node=node,
            source=self._source,
            aliases=self._aliases,
            package_root=self._package_root,
            repo_root=self._repo_root,
            path=self._path,
        )
        if violation is not None:
            self.violations.append(violation)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_scope(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _visit_function_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

        local_bindings = _function_local_bindings(node)
        inherited_aliases = {
            name: module
            for name, module in self._aliases.items()
            if name not in local_bindings
        }
        self._alias_scopes.append(inherited_aliases)
        for statement in node.body:
            self.visit(statement)
        self._alias_scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

        inherited_aliases = {
            name: module
            for name, module in self._aliases.items()
            if name not in _argument_binding_names(node.args)
        }
        self._alias_scopes.append(inherited_aliases)
        self.visit(node.body)
        self._alias_scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, expressions=(node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, expressions=(node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, expressions=(node.key, node.value))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, expressions=(node.elt,))

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        *,
        expressions: tuple[ast.expr, ...],
    ) -> None:
        if not generators:
            for expression in expressions:
                self.visit(expression)
            return

        self.visit(generators[0].iter)
        self._alias_scopes.append(dict(self._aliases))
        for index, generator in enumerate(generators):
            if index > 0:
                self.visit(generator.iter)
            self.visit(generator.target)
            for name in _comprehension_target_names(generator.target):
                self._aliases.pop(name, None)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in expressions:
            self.visit(expression)
        self._alias_scopes.pop()


def _violations_for(
    rule: ImportDirectionRule,
    *,
    package_root: Path = PACKAGE_ROOT,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    path = package_root / rule.relative_path
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    current_module = _module_name_for(path, package_root=package_root)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in rule.forbidden_modules:
                    violations.append(
                        _format_violation(
                            path,
                            repo_root=repo_root,
                            lineno=node.lineno,
                            statement=f"import {alias.name}",
                        )
                    )
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        imported_module = _resolved_from_module(current_module, node)
        for alias in node.names:
            imported_target = ".".join(
                part for part in (imported_module, alias.name) if part
            )
            origin_modules = _origin_modules_for_export(
                imported_module,
                alias.name,
                package_root=package_root,
            )
            if (
                imported_module in rule.forbidden_modules
                or imported_target in rule.forbidden_modules
                or _origin_modules_are_forbidden(
                    rule,
                    export_name=alias.name,
                    origin_modules=origin_modules,
                )
            ):
                violations.append(
                    _format_violation(
                        path,
                        repo_root=repo_root,
                        lineno=node.lineno,
                        statement=(
                            f"from {_display_from_module(node)} import {alias.name}"
                        ),
                    )
                )

    facade_visitor = _FacadeAttributeVisitor(
        rule,
        source=source,
        current_module=current_module,
        package_root=package_root,
        repo_root=repo_root,
        path=path,
    )
    facade_visitor.visit(tree)
    violations.extend(facade_visitor.violations)

    return violations


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
