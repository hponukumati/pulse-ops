"""Schema construction, query limits and safe error formatting.

Limits (documented in docs/API.md): one operation per request, depth 8, 5 aliases,
20 fragments, cost 1,000 (computed with variable values before execution), page size
<= 100, introspection disabled, field suggestions disabled. Hidden schema is never
relied upon for authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import strawberry
from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    VariableNode,
    parse,
)
from redis.exceptions import RedisError
from strawberry.extensions import DisableIntrospection, MaxAliasesLimiter, QueryDepthLimiter
from strawberry.schema.config import StrawberryConfig

from pulseops.db import classify_db_error
from pulseops.errors import AppError, DependencyUnavailable
from pulseops.gql.operations import Mutation, Query, Subscription
from pulseops.logging import log_event
from pulseops.pagination import DEFAULT_PAGE, MAX_PAGE

MAX_DEPTH = 8
MAX_ALIASES = 5
MAX_FRAGMENTS = 20
MAX_COST = 1000
MAX_TOKENS = 3000

# Field weights; list/connection fields multiply their children by `first` (default 20).
FIELD_WEIGHTS = {"metrics": 50, "endpoints": 20, "health": 5, "diagnostics": 20, "transitions": 5, "ingestTokens": 5}
LIST_FIELDS = {"services", "routes", "alertRules", "incidents", "endpoints", "users", "serviceGrants", "auditEvents"}
FIXED_LISTS = {"transitions": 10, "ingestTokens": 10, "timeseries": 1, "topEndpoints": 1}


class QueryRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SafeSchema(strawberry.Schema):
    def process_errors(self, errors: list[GraphQLError], execution_context: Any = None) -> None:
        # Log only error codes/classes: messages may echo client-supplied values.
        for err in errors:
            orig = err.original_error
            if isinstance(orig, AppError):
                continue
            log_event("graphql.error", code="unexpected" if orig else "validation", error_class=type(orig).__name__ if orig else None)


schema = SafeSchema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    config=StrawberryConfig(disable_field_suggestions=True),
    extensions=[
        lambda: QueryDepthLimiter(max_depth=MAX_DEPTH),
        lambda: MaxAliasesLimiter(max_alias_count=MAX_ALIASES),
        DisableIntrospection,
    ],
)


@dataclass(frozen=True)
class PreparedOperation:
    document: DocumentNode
    operation: OperationDefinitionNode
    operation_type: OperationType
    cost: int


def _int_arg(node: FieldNode, name: str, variables: dict[str, Any]) -> int | None:
    for arg in node.arguments or ():
        if arg.name.value != name:
            continue
        if isinstance(arg.value, IntValueNode):
            return int(arg.value.value)
        if isinstance(arg.value, VariableNode):
            v = variables.get(arg.value.name.value)
            return v if isinstance(v, int) and not isinstance(v, bool) else None
    return None


def _cost(sel: SelectionSetNode | None, fragments: dict[str, FragmentDefinitionNode], variables: dict[str, Any], depth: int = 0) -> int:
    if sel is None:
        return 0
    if depth > 30:
        raise QueryRejected("QUERY_TOO_DEEP")
    total = 0
    for node in sel.selections:
        if isinstance(node, FieldNode):
            name = node.name.value
            base = FIELD_WEIGHTS.get(name, 1)
            child = _cost(node.selection_set, fragments, variables, depth + 1)
            if name in LIST_FIELDS:
                # Only the per-item part (edges) scales with the page size; totalCount/pageInfo do not.
                first = _int_arg(node, "first", variables)
                n = min(max(first if first is not None else DEFAULT_PAGE, 1), MAX_PAGE)
                per_item = _edges_cost(node.selection_set, fragments, variables, depth + 1)
                total += base + (child - per_item) + n * per_item
            else:
                total += base + FIXED_LISTS.get(name, 1) * child
        elif isinstance(node, InlineFragmentNode):
            total += _cost(node.selection_set, fragments, variables, depth + 1)
        elif isinstance(node, FragmentSpreadNode):
            frag = fragments.get(node.name.value)
            if frag is None:
                raise QueryRejected("GRAPHQL_VALIDATION_FAILED")
            total += _cost(frag.selection_set, fragments, variables, depth + 1)
        if total > MAX_COST * 10:
            break
    return total


def _edges_cost(sel: SelectionSetNode | None, fragments: dict[str, FragmentDefinitionNode], variables: dict[str, Any], depth: int) -> int:
    if sel is None:
        return 0
    total = 0
    for node in sel.selections:
        if isinstance(node, FieldNode) and node.name.value == "edges":
            total += _cost(SelectionSetNode(selections=(node,)), fragments, variables, depth)
        elif isinstance(node, InlineFragmentNode):
            total += _edges_cost(node.selection_set, fragments, variables, depth + 1)
        elif isinstance(node, FragmentSpreadNode) and (frag := fragments.get(node.name.value)) is not None:
            total += _edges_cost(frag.selection_set, fragments, variables, depth + 1)
    return total


def prepare(query: Any, operation_name: Any, variables: dict[str, Any]) -> PreparedOperation:
    """Parse and apply request-level limits before handing the document to Strawberry."""
    if not isinstance(query, str) or not query.strip():
        raise QueryRejected("BAD_REQUEST")
    if operation_name is not None and not isinstance(operation_name, str):
        raise QueryRejected("BAD_REQUEST")
    try:
        doc = parse(query, no_location=True, max_tokens=MAX_TOKENS)
    except GraphQLError as exc:
        raise QueryRejected("GRAPHQL_PARSE_FAILED") from exc
    ops = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
    frags = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}
    if len(ops) != 1:
        raise QueryRejected("SINGLE_OPERATION_REQUIRED")
    if len(frags) > MAX_FRAGMENTS:
        raise QueryRejected("TOO_MANY_FRAGMENTS")
    op = ops[0]
    if operation_name is not None and (op.name is None or op.name.value != operation_name):
        raise QueryRejected("BAD_REQUEST")
    cost = _cost(op.selection_set, frags, variables)
    if cost > MAX_COST:
        raise QueryRejected("QUERY_TOO_COMPLEX")
    return PreparedOperation(document=doc, operation=op, operation_type=op.operation, cost=cost)


def format_error(err: GraphQLError) -> dict[str, Any]:
    """Safe error shape: fixed message per code, optional safe details, path only."""
    orig = err.original_error
    if isinstance(orig, AppError):
        out: dict[str, Any] = {"message": orig.message, "extensions": {"code": orig.code}}
        if orig.details:
            out["extensions"]["details"] = orig.details
    elif orig is None:
        # Validation/coercion errors can quote literal or variable values: never echo them.
        msg = err.message
        code = "GRAPHQL_VALIDATION_FAILED"
        if "exceeds maximum operation depth" in msg:
            code = "QUERY_TOO_DEEP"
        elif "aliases" in msg.lower():
            code = "TOO_MANY_ALIASES"
        elif "introspection" in msg.lower():
            code = "INTROSPECTION_DISABLED"
        out = {"message": "Query validation failed", "extensions": {"code": code}}
    else:
        mapped = classify_db_error(orig) or (DependencyUnavailable() if isinstance(orig, (RedisError, OSError)) else None)
        out = (
            {"message": mapped.message, "extensions": {"code": mapped.code}}
            if mapped
            else {"message": "Internal error", "extensions": {"code": "INTERNAL"}}
        )
    if err.path:
        out["path"] = [p if isinstance(p, (str, int)) else str(p) for p in err.path]
    return out


def export_sdl() -> str:
    return schema.as_str() + "\n"
