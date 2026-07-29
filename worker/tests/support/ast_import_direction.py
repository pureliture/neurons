"""Reusable AST import-direction guard for worker structural tests.

The guard resolves direct imports, package re-exports, facade aliases, and
lexical shadowing without importing the package under inspection.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ForbiddenExport:
    """A named package export whose concrete origins are forbidden."""

    name: str
    origin_modules: frozenset[str]


@dataclass(frozen=True)
class ImportDirectionRule:
    """Static dependency-direction constraint for one package-relative file."""

    relative_path: str
    forbidden_modules: frozenset[str] = frozenset()
    forbidden_exports: tuple[ForbiddenExport, ...] = ()


def violations_for(
    rule: ImportDirectionRule,
    *,
    package_root: Path,
    repo_root: Path,
) -> list[str]:
    """Return deterministic ``file:line`` diagnostics for ``rule`` violations."""

    path = package_root / rule.relative_path
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    current_module = _module_name_for(path, package_root=package_root)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_is_forbidden(rule, alias.name):
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
                _module_is_forbidden(rule, imported_module)
                or _module_is_forbidden(rule, imported_target)
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
                        statement=f"from {_display_from_module(node)} import {alias.name}",
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
    return any(_module_is_forbidden(rule, module) for module in origin_modules) or (
        _matches_forbidden_export(
            rule,
            export_name=export_name,
            origin_modules=origin_modules,
        )
    )


def _module_is_forbidden(rule: ImportDirectionRule, module: str) -> bool:
    return any(
        module == forbidden_module or module.startswith(f"{forbidden_module}.")
        for forbidden_module in rule.forbidden_modules
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

    def _visit_comprehension_scope(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        for generator in node.generators:
            self.visit(generator.iter)

    visit_ListComp = _visit_comprehension_scope
    visit_SetComp = _visit_comprehension_scope
    visit_DictComp = _visit_comprehension_scope
    visit_GeneratorExp = _visit_comprehension_scope


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
