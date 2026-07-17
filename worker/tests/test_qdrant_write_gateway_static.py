from __future__ import annotations

import ast
from pathlib import Path


QDRANT_MUTATION_METHODS = frozenset(
    {
        "batch_update_points",
        "clear_payload",
        "create_alias",
        "create_collection",
        "create_full_snapshot",
        "create_payload_index",
        "create_shard_key",
        "create_snapshot",
        "delete",
        "delete_alias",
        "delete_collection",
        "delete_full_snapshot",
        "delete_payload",
        "delete_payload_index",
        "delete_shard_key",
        "delete_snapshot",
        "delete_vectors",
        "lock_storage",
        "overwrite_payload",
        "recover_snapshot",
        "recreate_collection",
        "rename_alias",
        "set_payload",
        "update_collection",
        "update_collection_aliases",
        "update_vectors",
        "upload_collection",
        "upload_points",
        "upload_snapshot",
        "upsert",
    }
)
ALLOWED_QDRANT_MUTATION_CALLS = frozenset(
    {
        (
            "qdrant_write_gateway_runtime.py",
            "_QdrantProductMutationAdapter.upsert_points",
            "upsert",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "_QdrantProductMutationAdapter.delete_points",
            "delete",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "QdrantMutationMarkerStore._write_stored_payload_and_verify",
            "upsert",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "activate_qdrant_marker_collection",
            "create_collection",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "activate_qdrant_marker_collection",
            "create_payload_index",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "reconcile_qdrant_marker_metadata",
            "upsert",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "provision_qdrant_collection",
            "create_collection",
        ),
        (
            "qdrant_write_gateway_runtime.py",
            "provision_qdrant_collection",
            "create_payload_index",
        ),
        (
            "product_marker_activation_cli.py",
            "_QdrantActivationClientGuard.create_collection",
            "create_collection",
        ),
        (
            "product_marker_activation_cli.py",
            "_QdrantActivationClientGuard.create_payload_index",
            "create_payload_index",
        ),
        (
            "product_marker_activation_cli.py",
            "_QdrantActivationClientGuard.upsert",
            "upsert",
        ),
    }
)
OPERATOR_APIS = frozenset(
    {
        "activate_qdrant_marker_collection",
        "provision_qdrant_collection",
        "reconcile_qdrant_marker_metadata",
    }
)
ALLOWED_OPERATOR_CALLS = frozenset(
    {
        (
            "product_marker_activation_cli.py",
            "_activate_qdrant_pending",
            "activate_qdrant_marker_collection",
        ),
        (
            "product_marker_activation_cli.py",
            "_activate_qdrant_pending",
            "reconcile_qdrant_marker_metadata",
        ),
        (
            "product_marker_activation_cli.py",
            "_finalize_qdrant_coverage",
            "reconcile_qdrant_marker_metadata",
        ),
    }
)


class _CallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((".".join(self.scope), node))
        self.generic_visit(node)


def _imports_qdrant_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith(
            "qdrant_client"
        ):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("qdrant_client") for alias in node.names
        ):
            return True
    return False


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def test_qdrant_mutation_surface_has_exact_gateway_owned_ast_allowlist() -> None:
    package_root = Path(__file__).parents[1] / "lib" / "agent_knowledge"
    observed_allowed: set[tuple[str, str, str]] = set()
    violations: list[str] = []
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root).as_posix()
        if relative == "rag_ingress/qdrant_docling_testing.py":
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        qdrant_tainted = _imports_qdrant_client(tree) or "qdrant" in relative
        visitor = _CallVisitor()
        visitor.visit(tree)
        for scope, node in visitor.calls:
            name = _call_name(node)
            dynamic_name = ""
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and any(
                    token in ast.unparse(node.args[0]).casefold()
                    for token in ("client", "qdrant")
                )
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                dynamic_name = node.args[1].value
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "_method"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                dynamic_name = node.args[0].value
            mutation_name = name if name in QDRANT_MUTATION_METHODS else dynamic_name
            receiver_name = (
                ast.unparse(node.func.value).casefold()
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            receiver_tainted = receiver_name.rsplit(".", 1)[-1] in {
                "client",
                "_client",
                "qdrant_client",
            } or "qdrant" in receiver_name
            if (
                mutation_name not in QDRANT_MUTATION_METHODS
                or not (qdrant_tainted or receiver_tainted)
            ):
                continue
            callsite = (Path(relative).name, scope, mutation_name)
            if callsite in ALLOWED_QDRANT_MUTATION_CALLS:
                observed_allowed.add(callsite)
            else:
                violations.append(f"{relative}:{scope}:{mutation_name}")

        for literal in (
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ):
            lowered = literal.casefold()
            if "/collections/" in lowered and any(
                segment in lowered
                for segment in ("/points", "/aliases", "/snapshots", "/index")
            ):
                violations.append(f"{relative}:direct_qdrant_http_surface")

    assert violations == []
    assert observed_allowed == ALLOWED_QDRANT_MUTATION_CALLS


def test_operator_api_calls_have_one_exact_cli_allowlist() -> None:
    package_root = Path(__file__).parents[1] / "lib" / "agent_knowledge"
    observed: set[tuple[str, str, str]] = set()
    violations: list[str] = []
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _CallVisitor()
        visitor.visit(tree)
        for scope, node in visitor.calls:
            name = _call_name(node)
            if name not in OPERATOR_APIS:
                continue
            callsite = (Path(relative).name, scope, name)
            observed.add(callsite)
            if callsite not in ALLOWED_OPERATOR_CALLS:
                violations.append(f"{relative}:{scope}:{name}")
    assert violations == []
    assert observed == ALLOWED_OPERATOR_CALLS


def test_operator_activation_has_no_automation_or_deploy_invocation() -> None:
    repository = Path(__file__).parents[2]
    allowed_packaging_declarations = {
        "worker/pyproject.toml": {
            'product-marker-activation = "agent_knowledge.product_marker_activation_cli:main"'
        }
    }
    scanned_suffixes = {
        ".json",
        ".py",
        ".sh",
        ".groovy",
        ".yaml",
        ".yml",
        ".xml",
        ".toml",
    }
    scanned_names = {"Dockerfile", "Jenkinsfile", "Makefile"}
    needles = (
        "activate_qdrant_marker_collection(",
        "provision_qdrant_collection(",
        "reconcile_qdrant_marker_metadata(",
        "product-marker-activation",
        "agent_knowledge.product_marker_activation_cli",
    )
    violations: list[str] = []
    for path in repository.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or ".venv" in path.parts
            or "tests" in path.parts
            or path.name
            in {
                "product_marker_activation_cli.py",
                "qdrant_write_gateway_runtime.py",
            }
            or (path.suffix not in scanned_suffixes and path.name not in scanned_names)
        ):
            continue
        relative = path.relative_to(repository).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        allowed_declarations = allowed_packaging_declarations.get(relative, set())
        lines = text.splitlines()
        if any(lines.count(declaration) != 1 for declaration in allowed_declarations):
            violations.append(f"{relative}:packaging-declaration-mismatch")
        text = "\n".join(
            line for line in lines if line not in allowed_declarations
        )
        if any(needle in text for needle in needles):
            violations.append(relative)
    assert violations == []
