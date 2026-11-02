"""Find mutating routes whose writes have no protection against a second delivery.

A client retries because the network lied. If the handler charges, inserts, or sends
before it can answer, the retry charges twice. Libraries that solve this exist per
framework; nothing tells you which of your routes is missing the guard.

This is static analysis over the AST, so it can only see what is written down. A guard
built by a decorator defined in another module, or an ORM method that commits for you,
is invisible. The README says so instead of implying the tool proves safety.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

MUTATING = ("POST", "PUT", "PATCH", "DELETE")

#: Methods that end in a write, anywhere in the codebase.
WRITE_METHODS = frozenset({
    "commit", "flush", "save", "insert", "insert_one", "insert_many", "update",
    "update_one", "upsert", "delete", "delete_one", "execute", "create", "add",
    "charge", "capture", "transfer", "refund", "enqueue", "delay", "send_message",
})

#: Outbound calls that move money or send something a retry would duplicate.
OUTBOUND = ("requests.post", "requests.put", "httpx.post", "httpx.put", "aiohttp.",
            "stripe.", "session.post", "client.post", "urlopen")

#: Any of these in the handler means somebody thought about the retry.
PROTECTION = ("idempotency", "idempotent", "idem_key", "idemkey", "request_id",
              "requestid", "dedupe", "dedup_key", "nonce", "client_reference",
              "get_or_create", "update_or_create", "on_conflict", "unique_together")

ROUTE_DECORATORS = frozenset({"post", "put", "patch", "delete", "route", "api_route"})


@dataclass(frozen=True)
class Route:
    """A handler and the mutating methods it answers."""

    line: int
    methods: Tuple[str, ...]
    name: str


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    severity: str
    route: str
    detail: str
    writes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "rule": self.rule,
                "severity": self.severity, "route": self.route,
                "detail": self.detail, "writes": self.writes}


@dataclass
class Report:
    files: int = 0
    routes: int = 0
    findings: List[Finding] = field(default_factory=list)


def _dotted(node: ast.AST) -> str:
    """The dotted name of a call target, or '' when it is not a plain name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _methods_from_decorator(node: ast.AST) -> Optional[Tuple[str, ...]]:
    """Return the mutating methods a decorator registers, or None if it is not a route."""
    if not isinstance(node, ast.Call):
        return None
    target = _dotted(node.func).rsplit(".", 1)[-1]
    if target not in ROUTE_DECORATORS:
        return None
    if target in ("route", "api_route"):
        for kw in node.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                names = [e.value.upper() for e in kw.value.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                picked = tuple(m for m in names if m in MUTATING)
                return picked or None
        return None
    method = target.upper()
    return (method,) if method in MUTATING else None


def _strings_and_names(func: ast.AST) -> str:
    """Every identifier and string literal in the handler, lowercased, for guard hunting."""
    chunks: List[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Name):
            chunks.append(node.id)
        elif isinstance(node, ast.Attribute):
            chunks.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            chunks.append(node.value)
        elif isinstance(node, ast.keyword) and node.arg:
            chunks.append(node.arg)
    return " ".join(chunks).lower()


def _writes(func: ast.AST) -> List[str]:
    """Dotted names of calls that look like writes or outbound sends."""
    found: List[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        short = name.rsplit(".", 1)[-1]
        low = name.lower()
        if short in WRITE_METHODS:
            found.append(name or short)
        elif any(marker in low for marker in OUTBOUND):
            found.append(name or low)
        elif short == "execute" and any(
            kw in low for kw in ("insert", "update", "delete")
        ):
            found.append(name)
    # dedupe, keep first-seen order
    seen: List[str] = []
    for name in found:
        if name not in seen:
            seen.append(name)
    return seen


def _docstring_node(func: ast.AST) -> Optional[ast.AST]:
    body = getattr(func, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


def _has_guard(func: ast.AST) -> bool:
    """Is there any sign that a second delivery was considered?

    The handler's own docstring is excluded: a docstring that says "TODO: idempotency"
    is evidence of the bug, not of the fix.
    """
    doc = _docstring_node(func)
    doc_text = doc.value if isinstance(doc, ast.Expr) else None
    skip = {id(doc), id(doc_text)}
    chunks: List[str] = []
    for node in ast.walk(func):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Name):
            chunks.append(node.id)
        elif isinstance(node, ast.Attribute):
            chunks.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            chunks.append(node.value)
        elif isinstance(node, ast.keyword) and node.arg:
            chunks.append(node.arg)
    text = " ".join(chunks).lower()
    return any(token in text for token in PROTECTION)


def _route_path(dec: ast.AST) -> str:
    if isinstance(dec, ast.Call) and dec.args:
        first = dec.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def analyze_source(source: str, path: str = "<src>") -> Tuple[int, List[Finding]]:
    """Return (mutating routes seen, findings) for one source file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, []
    routes = 0
    findings: List[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        methods: List[str] = []
        label = ""
        for dec in node.decorator_list:
            picked = _methods_from_decorator(dec)
            if picked:
                methods.extend(picked)
                label = label or _route_path(dec)
        if not methods:
            continue
        routes += 1
        writes = _writes(node)
        if not writes or _has_guard(node):
            continue
        outbound = any(any(m in w.lower() for m in OUTBOUND) for w in writes)
        severity = "high" if (outbound or "POST" in methods) else "medium"
        route = "%s %s" % ("|".join(sorted(set(methods))), label or node.name)
        findings.append(Finding(
            path=path, line=node.lineno, rule="missing-idempotency", severity=severity,
            route=route,
            detail="%s with no idempotency key, dedupe store or unique constraint"
                   % node.name,
            writes=writes,
        ))
    return routes, findings


def _looks_like_tests(path: Path) -> bool:
    parts = set(path.parts)
    return "tests" in parts or path.name.startswith("test_") or path.name == "conftest.py"


def analyze_files(paths: Iterable[Path], include_tests: bool = False) -> Report:
    report = Report()
    seen: set = set()
    for base in paths:
        files: Sequence[Path]
        if base.is_dir():
            files = sorted(p for p in base.rglob("*.py")
                           if not any(skip in p.parts for skip in
                                      (".git", "build", "dist", "node_modules", "__pycache__")))
        else:
            files = [base]
        for path in files:
            key = str(path.resolve())
            if key in seen or (not include_tests and _looks_like_tests(path)):
                continue
            seen.add(key)
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            routes, findings = analyze_source(source, str(path))
            report.files += 1
            report.routes += routes
            report.findings.extend(findings)
    report.findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}[f.severity],
                                        f.path, f.line))
    return report
