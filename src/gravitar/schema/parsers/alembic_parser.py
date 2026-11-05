"""Alembic migration parsing (implemented behind the `MigrationParser` protocol).

Migration files are never imported (they may have side effects) — only parsed
as Python AST. Every `op.*` call inside `upgrade()` is resolved directly to a
DSL-agnostic `Operation` (plain values, not `ast.expr`) at parse time, so
`migrations.py`'s schema-state builder never has to know it came from AST.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from gravitar.schema import SourceLocation, Unresolved, make_unresolved
from gravitar.schema import types as types_module
from gravitar.schema.parsers import ColumnSpec, Operation, Revision
from gravitar.schema.types import normalize_ast_type, type_import_aliases_scope

DownRevision = str | list[str] | None


def _assignment_target_and_value(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """Return the single-name target and value from a module-level assignment,
    whether written as `revision = "..."` (Assign) or `revision: str = "..."`
    / `revision: Union[str, ...] = "..."` (AnnAssign, the Alembic ≥1.11
    default template — SPEC-16 amendment R9). AnnAssigns with no value
    (`revision: str`) and multi-target Assigns return None.
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        target = node.target
        if isinstance(target, ast.Name):
            return target.id, node.value
    return None


def _parse_down_revision(value: ast.expr) -> DownRevision | object:
    """Return the parsed down_revision, or the sentinel UNREADABLE if the RHS
    was present but not a shape we can decode.
    """
    if isinstance(value, ast.Constant) and (value.value is None or isinstance(value.value, str)):
        return value.value
    if isinstance(value, (ast.Tuple, ast.List)):
        parts = [
            elt.value
            for elt in value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        # Only return the list if every element was decodable; a partial
        # decode would silently drop parents.
        if len(parts) == len(value.elts):
            return parts
    return _UNREADABLE


_UNREADABLE: object = object()


def parse_revision_file(path: Path) -> Revision:
    """Extract revision, down_revision, and op.* calls from an Alembic migration.

    Only calls inside upgrade() are collected: the migration schema builder
    replays the forward path to build the final schema state, so downgrade()'s
    reverse operations must not be mixed in.

    SPEC-16 amendment R9: parses both `Assign` and `AnnAssign` for the
    revision identifiers (Alembic ≥1.11's default template uses `revision:
    str = "..."`). A file with `down_revision` but an unreadable `revision`
    yields a note, never a silent drop.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    revision: str | None = None
    down_revision: DownRevision = None
    revision_line: int | None = None
    down_revision_was_set = False
    down_revision_was_unreadable = False
    revision_was_unreadable = False
    unreadable_revision_line: int | None = None

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        parsed = _assignment_target_and_value(node)
        if parsed is None:
            continue
        name, value = parsed
        if name == "revision":
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                revision = value.value
                revision_line = node.lineno
            else:
                revision_was_unreadable = True
                unreadable_revision_line = node.lineno
        elif name == "down_revision":
            down_revision_was_set = True
            parsed_dr = _parse_down_revision(value)
            if parsed_dr is _UNREADABLE:
                down_revision_was_unreadable = True
            else:
                # mypy: the sentinel branch returned above, so this is a DownRevision
                down_revision = parsed_dr  # type: ignore[assignment]

    # SPEC-16 R2 (full): collect this file's import aliases so the type
    # resolver can canonicalize ``Str`` → ``String`` for the duration of
    # this file's walk. Scoped per-file because each migration defines
    # its own imports.
    _, type_aliases = _collect_import_aliases(tree)
    with type_import_aliases_scope(type_aliases):
        walk_ctx = _walk_upgrade(tree, source_file=str(path), revision=revision)
    operations = walk_ctx.operations

    # R9: a file with `down_revision` but unreadable `revision` yields a
    # note. Without the note, the revision would silently drop out of the
    # graph — exactly the "zero findings, no error" failure the readiness
    # doc called out (S1).
    notes: list[Unresolved] = []
    if revision is None and (down_revision_was_set or down_revision_was_unreadable):
        notes.append(
            make_unresolved(
                "unreadable_revision",
                "down_revision parsed but revision was unreadable or missing",
                SourceLocation(
                    file=str(path),
                    line=unreadable_revision_line,
                    revision_id=None,
                ),
            )
        )
    if revision_was_unreadable and revision is None:
        # Even without down_revision, an unreadable revision = <non-literal>
        # is itself a note — we won't emit it twice if the branch above
        # already covered this file.
        if not notes:
            notes.append(
                make_unresolved(
                    "unreadable_revision",
                    "revision identifier is not a string literal",
                    SourceLocation(
                        file=str(path),
                        line=unreadable_revision_line,
                        revision_id=None,
                    ),
                )
            )
    _ = revision_line  # reserved for a future "revision at <line>" message

    # SPEC-12 Amendment Scenario E: the walker's own notes (unrecognized_op)
    # and inert count must flow through so the per-revision accounting
    # ``nodes_seen == applied + inert + noted`` can be verified.
    notes.extend(walk_ctx.notes)
    return Revision(
        revision=revision,
        down_revision=down_revision,
        operations=operations,
        source_file=str(path),
        notes=notes,
        inert_count=walk_ctx.inert_count,
    )


def parse_versions_dir(versions_dir: Path) -> list[Revision]:
    """Parse every migration file in an Alembic versions/ directory.

    SPEC-16 amendment R11: a `SyntaxError` or any other parse failure for one
    file becomes a `parse_error` note on a placeholder Revision for that
    file; the whole run continues against the remaining files. Pre-R11 one
    unparseable file (e.g. Mealie's PEP 758 `except A, B:` under an older
    interpreter — readiness doc S10) aborted the whole run with exit 3.

    SPEC-16 amendment R10 (subset): walks nested subdirectories so a layout
    like ``versions/postgresql/0001.py`` + ``versions/sqlite/0001.py``
    (Prefect's dual-dialect layout, readiness doc S9) is discovered.
    Pre-R10 ``glob("*.py")`` only matched the top level, silently finding
    zero revisions and making every model table look like drift.

    Mealie-style "newer syntax than this interpreter" cases get a hint in
    the note's detail rather than being reported as corruption.
    """
    revisions: list[Revision] = []
    for path in sorted(versions_dir.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            revisions.append(parse_revision_file(path))
        except SyntaxError as e:
            revisions.append(_parse_error_revision(path, e))
        except Exception as e:  # noqa: BLE001 — per-file isolation is the whole point
            revisions.append(_parse_error_revision(path, e))
    return revisions


def _parse_error_revision(path: Path, exc: BaseException) -> Revision:
    """Build a placeholder Revision for a file that couldn't be parsed. The
    file isn't in the graph (revision=None, down_revision=None → not
    reachable as a base), but the note it carries makes the failure
    visible: coverage counts it as "skipped" and T1 is satisfied.
    """
    line = getattr(exc, "lineno", None)
    detail = _parse_error_detail(exc)
    note = make_unresolved(
        "parse_error",
        detail,
        SourceLocation(file=str(path), line=line, revision_id=None),
    )
    return Revision(
        revision=None,
        down_revision=None,
        operations=[],
        source_file=str(path),
        notes=[note],
    )


def _parse_error_detail(exc: BaseException) -> str:
    """Short detail for a parse_error note. Newer-syntax exceptions get a
    "parsed under older interpreter" hint rather than being reported as
    corruption (amendment R11).
    """
    msg = str(exc) or type(exc).__name__
    if isinstance(exc, SyntaxError) and _looks_like_newer_syntax(exc):
        return f"parsed under older interpreter — {msg} (install a newer Python)"
    # Truncate to 80 chars per SPEC-16 v1.0 §4's detail-length budget.
    return msg[:80]


_NEWER_SYNTAX_SIGNS = (
    "except*",  # PEP 654 (3.11)
    "match",  # PEP 634 (3.10)
    "pattern",
    "f-string: ",  # PEP 701 (3.12)
    "except A, B",  # PEP 758
)


def _looks_like_newer_syntax(exc: SyntaxError) -> bool:
    msg = (exc.msg or "").lower()
    text = (exc.text or "").lower()
    return any(s.lower() in msg or s.lower() in text for s in _NEWER_SYNTAX_SIGNS)


@dataclass
class _WalkContext:
    """SPEC-16 Amendment §Scenario E: the walker accumulates three things
    — operations to apply, notes for unresolved input, and a count of ops
    it correctly recognized as inert. Every top-level ``op.*`` and
    ``batch_op.*`` call falls into exactly one of those three buckets, so
    ``nodes_seen == len(operations) + inert_count + len(notes)``.

    SPEC-16 R4 (full):
    - ``conditional_depth`` increments when the walker descends into an
      ``if``/``else``/``except`` branch. Operations resolved while depth
      > 0 are marked ``conditional=True``: the comparator can still see
      them, but demotes findings on tables they touch to the review
      tier (R3 taint).
    - ``helpers`` holds the module-level ``def`` statements the pre-pass
      collected. When the walker meets a plain-function call that passes
      ``op`` and the callee is in this map, R4 inlines the body with the
      formal parameters bound to the call's actual args. ``helper_stack``
      is the recursion guard: a helper that transitively calls itself
      falls through to the external_call note instead of looping.

    SPEC-16 R2 (full):
    - ``op_aliases`` is the set of module-local names that resolve to
      Alembic's ``op`` object (e.g. ``from alembic import op as al_op``
      → ``{"op", "al_op"}``). A call on any of these receivers is
      walked as if it were a call on bare ``op``.
    """

    source_file: str
    revision: str | None
    operations: list[Operation] = field(default_factory=list)
    notes: list[Unresolved] = field(default_factory=list)
    inert_count: int = 0
    conditional_depth: int = 0
    helpers: dict[str, ast.FunctionDef] = field(default_factory=dict)
    helper_stack: list[str] = field(default_factory=list)
    op_aliases: frozenset[str] = frozenset({"op"})


def _collect_import_aliases(tree: ast.Module) -> tuple[frozenset[str], dict[str, str]]:
    """SPEC-16 R2 (full): walk module-level imports and build two maps.

    Returns ``(op_aliases, type_aliases)``.

    - ``op_aliases`` is every local name that refers to Alembic's ``op``.
      ``from alembic import op`` adds ``"op"``; ``as al_op`` adds
      ``"al_op"``; ``from alembic import op as al_op`` adds just
      ``"al_op"``.
    - ``type_aliases`` maps a local name to the SQLAlchemy type name it
      names: ``from sqlalchemy import String as Str, Integer`` →
      ``{"Str": "String", "Integer": "Integer"}``. The resolver uses
      this before dispatching bare names so an alias stops reading as
      ``unknown:Str``.

    Only module-level imports are scanned — imports inside function
    bodies are rare and R4-scope. ``import alembic`` (whole-module
    import without ``from``) is intentionally NOT treated as an op
    alias: ``alembic.op.create_table`` is a different access shape
    that the walker handles via its attribute chain, not the receiver
    name.
    """
    op_names: set[str] = {"op"}  # bare ``op`` from ``from alembic import op`` is the default
    type_aliases: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            module = stmt.module or ""
            if module == "alembic":
                for alias in stmt.names:
                    if alias.name == "op":
                        op_names.add(alias.asname or alias.name)
            elif module.startswith("sqlalchemy"):
                # ``from sqlalchemy import String`` or
                # ``from sqlalchemy.dialects.postgresql import JSONB``.
                # We only know what Gravitar's dispatch handles; others
                # fall through to ``unknown:`` at normalization time.
                for alias in stmt.names:
                    name = alias.asname or alias.name
                    type_aliases[name] = alias.name
    return frozenset(op_names), type_aliases


def _collect_module_constants(tree: ast.Module) -> dict[str, object]:
    """SPEC-16 R4 (full) constant folding. Pre-pass over module-level
    ``NAME = <literal>`` assignments.

    Only accepts literal values (``ast.literal_eval``-safe: strings,
    numbers, bools, None, tuples/lists of literals). A reassignment
    inside the body — rare at module scope — isn't tracked; the fold
    uses the LAST module-level value seen, matching how Python itself
    would resolve the name at upgrade() entry.
    """
    constants: dict[str, object] = {}
    for stmt in tree.body:
        parsed = _assignment_target_and_value(stmt)
        if parsed is None:
            continue
        name, value = parsed
        if name in {"revision", "down_revision", "branch_labels", "depends_on"}:
            # Alembic's own identifiers — handled by the identifier pass.
            continue
        try:
            constants[name] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            # Non-literal RHS — out of scope for const folding; the walker's
            # existing non-literal handling still sees the name as unresolved.
            continue
    return constants


def _collect_module_helpers(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """SPEC-16 R4 (full) helper inlining pre-pass. Collect module-level
    ``def <name>(op, ...): ...`` functions as inlining candidates.

    Only takes functions whose first positional parameter is literally
    named ``op`` — the "accepts the migration ``op`` object" shape.
    Keyword-only functions, async defs, and defs nested inside classes
    aren't candidates (they aren't the Airflow / Flask-Migrate helper
    idiom and inlining them would be unsound).
    """
    helpers: dict[str, ast.FunctionDef] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.FunctionDef):
            continue
        if stmt.name == "upgrade":
            continue
        args = stmt.args
        if not args.args:
            continue
        if args.args[0].arg != "op":
            continue
        helpers[stmt.name] = stmt
    return helpers


class _ConstFoldRewriter(ast.NodeTransformer):
    """SPEC-16 R4 (full) constant folding rewriter. Substitutes bare
    ``ast.Name(id=NAME)`` references with the corresponding literal
    value from the module's constant pool.

    Only literals representable as ``ast.Constant`` are inlined (str,
    int, bool, None). Tuples/lists stay as names — the loop-unroll pass
    recognizes the tuple/list shape at the iterable position; folding
    them earlier would make the loop_not_literal check see a Constant
    and lose the scope note. The loop pass handles that integration.
    """

    def __init__(self, constants: dict[str, object]) -> None:
        self._constants = constants

    def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802
        if not isinstance(node.ctx, ast.Load):
            return node
        if node.id not in self._constants:
            return node
        value = self._constants[node.id]
        if value is None or isinstance(value, (str, int, bool, float)):
            return ast.copy_location(ast.Constant(value=value), node)
        return node


def _walk_upgrade(tree: ast.Module, source_file: str, revision: str | None) -> _WalkContext:
    """SPEC-16 Amendment R1 (batch_alter_table) + SPEC-16 v1.0 §9.1.

    Walks the ``upgrade()`` function body statement-by-statement so that
    ``with op.batch_alter_table(T) as B:`` blocks can bind ``B`` → ``T``
    inside the body, and calls on ``B`` get translated to their equivalent
    ``op.*`` ops with ``table=T``. Pre-R1 this used a flat ``ast.walk``
    that only matched calls on the literal receiver ``op`` — every op
    inside a batch block was invisible, the dominant source of the pre-
    amendment false positives per readiness doc D1 (526 batch blocks
    across the pinned corpus).

    SPEC-12 Amendment §Scenario E (accounting identity): returns a
    ``_WalkContext`` so the caller can report notes for unrecognized ops
    and the count of inert ops. Pre-E, native ``op.*`` calls not in
    ``_OP_HANDLERS`` passed through silently — a T1 gap my earlier
    commits left open. Now anything the walker sees that isn't a handled
    op or on the known-inert list produces an ``unrecognized_op`` note.

    The non-``with`` form (``b = op.batch_alter_table(...)`` then
    ``b.add_column(...)``) is still out of scope — it currently produces
    an ``unrecognized_op`` note. R4's full statement walker is what would
    track that binding.
    """
    upgrade_fn = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )
    # SPEC-16 R4 (full) pre-passes: constants + helpers. Collect from
    # the full module (not just upgrade's body) because both the
    # ``TABLE_NAMES = (...)`` and ``def add_audit_columns(...)`` idioms
    # live at module scope.
    constants = _collect_module_constants(tree)
    helpers = _collect_module_helpers(tree)
    # SPEC-16 R2 (full) alias pre-pass. Op aliases let the walker
    # recognize ``al_op.create_table(...)`` from ``from alembic import
    # op as al_op``; type aliases let the resolver resolve
    # ``Str(50)`` back to String from ``from sqlalchemy import String
    # as Str``.
    op_aliases, type_aliases = _collect_import_aliases(tree)

    ctx = _WalkContext(
        source_file=source_file,
        revision=revision,
        helpers=helpers,
        op_aliases=op_aliases,
    )
    if upgrade_fn is None:
        return ctx

    # Apply the constant-fold rewrite to the upgrade body (and in-flight
    # inlined helper bodies — see `_inline_helper`). Rewriting the body
    # once up front means every downstream helper sees the folded names,
    # including the loop-unroll and type-resolution passes.
    body = upgrade_fn.body
    if constants:
        rewriter = _ConstFoldRewriter(constants)
        body = [rewriter.visit(stmt) for stmt in body]
        for stmt in body:
            ast.fix_missing_locations(stmt)
    _walk_statements(body, batch_table=None, ctx=ctx)
    return ctx


def _walk_statements(body: list[ast.stmt], batch_table: str | None, ctx: _WalkContext) -> None:
    """Recursive descent over a block of statements. ``batch_table`` is the
    bound table name while we're inside a batch_alter_table body, or None
    otherwise. We don't recurse into nested FunctionDefs or ClassDefs —
    those are helpers, not part of the upgrade's own control flow (R4
    will handle them separately)."""
    for stmt in body:
        if isinstance(stmt, ast.With):
            _walk_with(stmt, batch_table, ctx)
        elif isinstance(stmt, (ast.If, ast.Try)):
            # SPEC-16 R4 (full): descend into every branch so ops aren't
            # silently dropped, and increment ``conditional_depth`` so
            # ops resolved while descending are flagged ``conditional=True``.
            # The comparator can still apply them (T1: no silent skips),
            # but R3 taint uses the conditional flag to demote findings on
            # tables these ops touch — a dialect-switched op may or may
            # not run at upgrade time.
            ctx.conditional_depth += 1
            try:
                for child_body in _branch_bodies(stmt):
                    _walk_statements(child_body, batch_table, ctx)
            finally:
                ctx.conditional_depth -= 1
        elif isinstance(stmt, ast.For):
            _walk_for(stmt, batch_table, ctx)
        else:
            for expr in _call_exprs(stmt):
                _resolve_call_expr(expr, batch_table, ctx)


def _walk_for(stmt: ast.For, batch_table: str | None, ctx: _WalkContext) -> None:
    """SPEC-16 Amendment R4 (subset): loop handling with a non-literal note.

    If the iterable is a literal tuple/list of strings, unroll — rewrite
    the loop variable references in the body to each literal in turn,
    so e.g. ``for t in ("logs", "events"): op.create_table(t, ...)``
    results in two create_table operations.

    Otherwise, descend into the body as before (so op calls inside are
    still seen rather than silently dropped) AND emit a single
    ``loop_not_literal`` note scoped to any literal tables mentioned in
    the body — SPEC-16 v1.0 §4 explicitly says "one note for the whole
    loop", not one per op.
    """
    loop_var = stmt.target.id if isinstance(stmt.target, ast.Name) else None
    literal_items = _loop_literal_items(stmt.iter)

    if loop_var is not None and literal_items is not None:
        # Unroll: for each literal, rewrite the body and walk.
        for item in literal_items:
            rewritten_body = _rewrite_body(stmt.body, loop_var, item)
            _walk_statements(rewritten_body, batch_table, ctx)
        return

    # Non-literal — descend so inner ops are still processed, but emit
    # the loop-level note explaining why confidence should drop.
    body_before = len(ctx.operations)
    _walk_statements(stmt.body, batch_table, ctx)
    body_tables = _literal_tables_in_body(stmt.body, ctx.op_aliases)
    if ctx.operations[body_before:] or body_tables:
        # Only emit the note if the loop actually contains op calls —
        # a loop over a non-literal that happens to just log things
        # shouldn't lower analysis confidence.
        source = SourceLocation(file=ctx.source_file, line=stmt.lineno, revision_id=ctx.revision)
        ctx.notes.append(
            make_unresolved(
                "loop_not_literal",
                "for loop over a non-literal iterable; ops inside may not be fully resolved",
                source,
                tables=sorted(body_tables) if body_tables else None,
            )
        )


def _loop_literal_items(node: ast.expr) -> list[str] | None:
    """Return the list of literal string items if ``node`` is a Tuple or
    List of ast.Constant strings; None otherwise."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    items: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            items.append(elt.value)
        else:
            return None
    return items


def _rewrite_body(body: list[ast.stmt], var: str, literal: str) -> list[ast.stmt]:
    """Return a shallow copy of ``body`` with every ``ast.Name(id=var)``
    expression substituted by ``ast.Constant(value=literal)``. Doesn't
    touch assignments to ``var`` inside the body — those are rare and
    out of scope for the loop-unroll subset."""

    class _Rewriter(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802
            if node.id == var and isinstance(node.ctx, ast.Load):
                return ast.copy_location(ast.Constant(value=literal), node)
            return node

    rewriter = _Rewriter()
    out: list[ast.stmt] = []
    for stmt in body:
        # Deep-copy via ast.parse+unparse would be heavy; NodeTransformer
        # mutates in place. Since we're walking the resulting body and
        # not reusing the original tree afterwards, in-place is fine for
        # a one-shot run of the parser.
        new_stmt = rewriter.visit(ast.parse(ast.unparse(stmt)).body[0])
        ast.fix_missing_locations(new_stmt)
        out.append(new_stmt)
    return out


def _inline_helper(
    call: ast.Call, helper_name: str, batch_table: str | None, ctx: _WalkContext
) -> bool:
    """SPEC-16 R4 (full): inline a module-level helper call. Returns True
    if the inline succeeded (and the walker has already recursed through
    the body), False if the caller should fall back to the
    ``external_call`` note path.

    Binding model: match positional args to the helper's formal params by
    position, keyword args by name. Bindings whose value is a literal
    (``ast.literal_eval``-safe) are inlined via the same NodeTransformer
    the constant-fold pass uses; non-literal args leave the parameter
    name untouched, so references inside the helper fall through to the
    walker's existing unresolved handling.

    Recursion guard: ``ctx.helper_stack`` tracks the call chain. If
    ``helper_name`` is already on the stack, the inline is refused and
    the caller falls back to external_call — a self-referential helper
    would otherwise walk forever.
    """
    if helper_name in ctx.helper_stack:
        return False
    helper = ctx.helpers.get(helper_name)
    if helper is None:
        return False

    formal_params = [arg.arg for arg in helper.args.args]
    # First param is ``op`` by convention — the caller passes its own ``op``
    # at the same position. Skip both when matching actuals to formals.
    bindings: dict[str, object] = {}
    pos_params = formal_params[1:]
    pos_actuals = call.args[1:] if call.args else []
    for formal, actual in zip(pos_params, pos_actuals, strict=False):
        try:
            bindings[formal] = ast.literal_eval(actual)
        except (ValueError, TypeError, SyntaxError):
            continue
    for kw in call.keywords:
        if kw.arg is None or kw.arg == "op":
            continue
        try:
            bindings[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, TypeError, SyntaxError):
            continue

    rewriter = _ConstFoldRewriter(bindings)
    inlined_body = [rewriter.visit(ast.parse(ast.unparse(stmt)).body[0]) for stmt in helper.body]
    for stmt in inlined_body:
        ast.fix_missing_locations(stmt)

    ctx.helper_stack.append(helper_name)
    try:
        _walk_statements(inlined_body, batch_table, ctx)
    finally:
        ctx.helper_stack.pop()
    return True


def _maybe_external_call(call: ast.Call, source: SourceLocation, ctx: _WalkContext) -> None:
    """R4 (subset) external_call detection. A plain function call that
    receives ``op`` as a positional or keyword argument is an external
    helper Gravitar can't follow — ``add_audit_columns(op, "widgets")``.
    The amendment scopes the resulting note to any literal table arg
    names present, else global."""
    passes_op = any(
        isinstance(arg, ast.Name) and arg.id in ctx.op_aliases for arg in call.args
    ) or any(
        isinstance(kw.value, ast.Name) and kw.value.id in ctx.op_aliases for kw in call.keywords
    )
    if not passes_op:
        return
    literal_tables = [
        arg.value
        for arg in call.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    callee = _call_name(call)
    ctx.notes.append(
        make_unresolved(
            "external_call",
            f"call to {callee!r} received op as an argument",
            source,
            tables=literal_tables or None,
        )
    )


def _literal_tables_in_body(
    body: list[ast.stmt], op_aliases: frozenset[str] = frozenset({"op"})
) -> set[str]:
    """Scan a block of statements for ``op.<x>(<literal_table>, ...)``
    and ``op.batch_alter_table(<literal_table>)`` call shapes; collect
    the literal first-arg strings. Used for scoping the single
    ``loop_not_literal`` note that covers a non-literal loop body.

    SPEC-16 R2 (full): accepts aliases (``al_op``) in addition to
    bare ``op``.
    """
    tables: set[str] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in op_aliases
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            tables.add(node.args[0].value)
    return tables


def _branch_bodies(stmt: ast.If | ast.Try) -> list[list[ast.stmt]]:
    if isinstance(stmt, ast.If):
        return [stmt.body, stmt.orelse]
    # try/except/else/finally — descend into all of them.
    bodies: list[list[ast.stmt]] = [stmt.body, stmt.orelse, stmt.finalbody]
    for handler in stmt.handlers:
        bodies.append(handler.body)
    return bodies


def _walk_with(stmt: ast.With, outer_batch: str | None, ctx: _WalkContext) -> None:
    """A ``with`` statement. The one shape R1 cares about is::

    with op.batch_alter_table("T", schema=...) as B:
        B.add_column(...)
        B.drop_column(...)
    """
    # Only one item: the common Alembic pattern. Multi-item withs are rare
    # and R4-scoped; descend into the body without binding anything.
    if len(stmt.items) != 1:
        _walk_statements(stmt.body, outer_batch, ctx)
        return
    item = stmt.items[0]
    batch_call = _batch_alter_table_call(item.context_expr, ctx.op_aliases)
    if batch_call is None:
        # Some other context manager (op.get_context().autocommit_block(), etc.)
        # — just descend.
        _walk_statements(stmt.body, outer_batch, ctx)
        return
    table = _string_arg(batch_call.args, 0)
    binding = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else None
    if table is None or binding is None:
        # Can't bind — descend without a batch_table, but preserve the outer
        # one if one is already active (nested batch blocks are rare but
        # possible).
        _walk_statements(stmt.body, outer_batch, ctx)
        return
    _walk_statements(stmt.body, batch_table=table, ctx=ctx)


def _batch_alter_table_call(
    expr: ast.expr, op_aliases: frozenset[str] = frozenset({"op"})
) -> ast.Call | None:
    """SPEC-16 R2 (full): batch_alter_table on an aliased op is still a
    batch block (``al_op.batch_alter_table(...)``)."""
    if not isinstance(expr, ast.Call):
        return None
    func = expr.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in op_aliases
        and func.attr == "batch_alter_table"
    ):
        return expr
    return None


def _call_exprs(stmt: ast.stmt) -> list[ast.Call]:
    """Expression statements that are calls — op.create_table(...) and
    friends. Chained attribute access (op.get_context().autocommit_block())
    isn't the shape here; those appear inside With items instead."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return [stmt.value]
    return []


def _resolve_call_expr(call: ast.Call, batch_table: str | None, ctx: _WalkContext) -> None:
    """Resolve a call expression and accumulate the outcome into ``ctx``.
    Three outcomes per the SPEC-12 Amendment Scenario E accounting
    identity:

    - **Operation** — the call corresponds to a handled op; append to
      ``ctx.operations``.
    - **Inert** — the call is on the known-inert list (index/constraint/
      bulk-insert/helper); bump ``ctx.inert_count``, no note.
    - **Unresolved** — the call is on ``op`` or a batch binding but isn't
      a handled op OR on the inert list; append an ``unrecognized_op``
      note to ``ctx.notes`` so T1 ("no silent skips") holds.

    A call whose receiver isn't ``op`` or a batch binding is NOT a
    migration op at all (incidental Python call); the walker ignores it.
    """
    func = call.func
    source = SourceLocation(file=ctx.source_file, line=call.lineno, revision_id=ctx.revision)
    # SPEC-16 Amendment R4 (subset): a plain Python call that receives
    # ``op`` as an argument is an external helper the walker can't
    # follow — e.g. ``add_audit_columns(op, "widgets")``. R4 (full) adds
    # in-file inlining: if the callee is a module-level ``def`` in this
    # same file whose first param is ``op``, inline its body instead of
    # emitting the external_call note. Falls back to external_call for
    # recursion or unknown helpers.
    if not isinstance(func, ast.Attribute):
        if isinstance(func, ast.Name) and func.id in ctx.helpers:
            if _inline_helper(call, func.id, batch_table, ctx):
                return
        _maybe_external_call(call, source, ctx)
        return
    if not isinstance(func.value, ast.Name):
        return
    receiver = func.value.id
    method = func.attr

    def _record_op(operation: Operation) -> None:
        """SPEC-16 R4 (full): tag an Operation ``conditional=True`` when
        the walker produced it inside an ``if``/``else``/``except``
        branch."""
        if ctx.conditional_depth > 0:
            operation.conditional = True
        ctx.operations.append(operation)

    # SPEC-16 R2 (full): any receiver name in ``op_aliases`` resolves to
    # ``op``. The default alias set is just ``{"op"}``; the pre-pass
    # expands it to include ``from alembic import op as X`` aliases.
    if receiver in ctx.op_aliases:
        if method in _NATIVE_HANDLED_OPS or method == "execute":
            _record_op(_resolve_call(call))
            return
        if method in _INERT_NATIVE_METHODS:
            ctx.inert_count += 1
            return
        # Not a known op — T1: emit a note rather than silently drop.
        ctx.notes.append(make_unresolved("unrecognized_op", f"op.{method}", source))
        return

    if batch_table is not None:
        if method in _INERT_BATCH_METHODS:
            ctx.inert_count += 1
            return
        if method in _BATCH_HANDLED_METHODS:
            _record_op(_resolve_batch_call(call, batch_table))
            return
        ctx.notes.append(
            make_unresolved(
                "unrecognized_op",
                f"batch_op.{method}",
                source,
                tables=[batch_table],
            )
        )
        return
    # Not op, not inside a batch block — some other object's method. Not
    # a migration op; the walker leaves it alone.


# Native ``op.*`` methods that resolve to an Operation handled by
# migrations.py's _OP_HANDLERS. ``execute`` is handled separately via
# the raw-SQL path (R12), so lives outside this set.
_NATIVE_HANDLED_OPS = frozenset(
    {
        "create_table",
        "add_column",
        "alter_column",
        "drop_column",
        "drop_table",
        "rename_table",
        "rename_column",
    }
)


# Native ``op.*`` methods Gravitar correctly recognizes as not affecting
# column existence/type/nullability (SPEC-16 v1.0 R3's known-inert list).
# Index/constraint operations: Gravitar's Schema has no index concept.
# Bulk-insert/helper methods: operate on data, not schema. Counted toward
# ``inert_count`` (Scenario E) so the accounting identity holds, but no
# note — they're fully understood, just not modeled.
_INERT_NATIVE_METHODS = frozenset(
    {
        "create_index",
        "drop_index",
        "rename_index",
        "create_foreign_key",
        "drop_constraint",
        "create_unique_constraint",
        "create_check_constraint",
        "create_primary_key",
        "create_foreign_key_constraint",
        "create_sequence",
        "drop_sequence",
        "bulk_insert",
        # Helpers: not migration ops at all, but ``f(...)``, ``inline_literal``,
        # ``get_context``, ``get_bind`` do show up as expression statements
        # in some migrations. Mark inert to be explicit.
        "f",
        "inline_literal",
        "get_context",
        "get_bind",
    }
)


# Known-inert methods on a batch binding — per SPEC-16 v1.0 R3's
# known-inert list. These don't change column existence/type/nullability,
# so there's nothing for Gravitar's schema model to apply, and intentionally
# not emitting a note either (they're correctly understood, just not
# represented in the Schema).
_INERT_BATCH_METHODS = frozenset(
    {
        "create_index",
        "drop_index",
        "create_foreign_key",
        "drop_constraint",
        "create_unique_constraint",
        "create_check_constraint",
        "create_primary_key",
    }
)

# Batch methods that resolve to an Operation (as opposed to inert or
# unrecognized). Add/alter/drop column are the three R1 cares about.
_BATCH_HANDLED_METHODS = frozenset({"add_column", "alter_column", "drop_column"})


def _resolve_batch_call(call: ast.Call, batch_table: str) -> Operation:
    """Translate a call on a batch binding to the equivalent op.* Operation.
    SPEC-16 v1.0 R1 maps three methods — add_column, alter_column,
    drop_column. The dispatcher (``_resolve_call_expr``) is responsible
    for filtering out non-handled methods (via ``_BATCH_HANDLED_METHODS``)
    before calling this.
    """
    func = call.func
    assert isinstance(func, ast.Attribute)
    method = func.attr
    args = list(call.args)
    line = call.lineno

    if method == "add_column":
        column_arg = args[0] if args else None
        spec = (
            _resolve_column_call(column_arg)
            if isinstance(column_arg, ast.Call) and _call_name(column_arg) == "Column"
            else None
        )
        return Operation(
            op="add_column",
            line=line,
            table=batch_table,
            columns=[spec] if spec is not None else [],
        )

    if method == "alter_column":
        column_name = _string_arg(args, 0)
        type_str: str | None = None
        nullable: bool | None = None
        new_column_name: str | None = None
        for kw in call.keywords:
            if kw.arg == "type_":
                # R2 fix for D2: accept both call form (sa.String(50)) and
                # class form (sa.BigInteger). Pre-R2 the ``isinstance(ast.Call)``
                # gate silently dropped class-form type_= and the column kept
                # its old type.
                type_str = normalize_ast_type(kw.value)
            elif kw.arg == "nullable":
                value = _literal(kw.value)
                if isinstance(value, bool):
                    nullable = value
            elif kw.arg == "new_column_name":
                value = _literal(kw.value)
                if isinstance(value, str):
                    new_column_name = value
        return Operation(
            op="alter_column",
            line=line,
            table=batch_table,
            column_name=column_name,
            type=type_str,
            nullable=nullable,
            new_column_name=new_column_name,
        )

    # drop_column is the only remaining handled method
    return Operation(
        op="drop_column", line=line, table=batch_table, column_name=_string_arg(args, 0)
    )


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _literal(node: ast.expr) -> object:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def _resolve_column_call(call: ast.Call) -> ColumnSpec | None:
    """SPEC-16 Amendment R2 (D10): the type is "the first positional ast arg
    after the name that *looks like a type*" — i.e. not a ForeignKey or
    Sequence or Identity (those are constraints/defaults, not types). Also
    accepts bare class-form (``sa.Integer``, no parens, an ``ast.Attribute``
    or ``ast.Name`` rather than an ``ast.Call``), and a ``type_=`` keyword
    override.

    Pre-R2, ``Column("uid", sa.ForeignKey("users.id"))`` resolved its type
    to ``unknown:ForeignKey`` because the walker took the first ``ast.Call``.
    """
    if not call.args:
        return None
    name = _literal(call.args[0])
    if not isinstance(name, str):
        return None

    type_str: str | None = None
    for arg in call.args[1:]:
        # Class form: ``sa.Integer`` (ast.Attribute) or bare ``Integer`` (ast.Name)
        if isinstance(arg, (ast.Name, ast.Attribute)) and not isinstance(arg, ast.Call):
            type_str = normalize_ast_type(arg)
            break
        if isinstance(arg, ast.Call):
            call_name = _call_name(arg)
            if call_name in types_module.NON_TYPE_CALL_NAMES:
                continue
            type_str = normalize_ast_type(arg)
            break

    primary_key = False
    nullable: bool | None = None
    for kw in call.keywords:
        if kw.arg == "primary_key":
            value = _literal(kw.value)
            if isinstance(value, bool):
                primary_key = value
        elif kw.arg == "nullable":
            value = _literal(kw.value)
            if isinstance(value, bool):
                nullable = value
        elif kw.arg == "type_" and type_str is None:
            # Explicit ``type_=`` kwarg — accepts Call or class form.
            type_str = normalize_ast_type(kw.value)
    if nullable is None:
        # SQLAlchemy's own Column() default: primary-key columns are NOT NULL
        # unless nullable is set explicitly.
        nullable = not primary_key

    return ColumnSpec(
        name=name, type=type_str or "unknown:missing", nullable=nullable, primary_key=primary_key
    )


def _string_arg(args: list[ast.expr], index: int) -> str | None:
    if len(args) <= index:
        return None
    value = _literal(args[index])
    return value if isinstance(value, str) else None


def _execute_arg_string(node: ast.expr) -> str | None:
    """Unwrap `sa.text("...")` / `text("...")` to its string argument (SPEC-16
    amendment R12). ``op.execute(sa.text("ALTER TABLE ..."))`` is the common
    idiom in post-SQLAlchemy-2.0 projects and used to parse to ``raw_sql=None``,
    making otherwise-recognizable DDL look dynamic.
    """
    # Plain string literal: `op.execute("...")`.
    literal = _literal(node)
    if isinstance(literal, str):
        return literal
    # `sa.text("...")` / `text("...")` — unwrap one layer.
    if isinstance(node, ast.Call) and _call_name(node) == "text" and node.args:
        inner = _literal(node.args[0])
        if isinstance(inner, str):
            return inner
    return None


def _resolve_call(call: ast.Call) -> Operation:
    func = call.func
    assert isinstance(func, ast.Attribute)
    op_name = func.attr
    args = list(call.args)
    line = call.lineno

    if op_name == "create_table":
        columns = [
            spec
            for arg in args[1:]
            if isinstance(arg, ast.Call) and _call_name(arg) == "Column"
            for spec in [_resolve_column_call(arg)]
            if spec is not None
        ]
        return Operation(op=op_name, line=line, table=_string_arg(args, 0), columns=columns)

    if op_name == "add_column":
        column_arg = args[1] if len(args) > 1 else None
        spec = (
            _resolve_column_call(column_arg)
            if isinstance(column_arg, ast.Call) and _call_name(column_arg) == "Column"
            else None
        )
        return Operation(
            op=op_name,
            line=line,
            table=_string_arg(args, 0),
            columns=[spec] if spec is not None else [],
        )

    if op_name == "alter_column":
        type_str: str | None = None
        nullable: bool | None = None
        new_column_name: str | None = None
        for kw in call.keywords:
            if kw.arg == "type_":
                # R2 fix for D2: accept both call form (sa.String(50)) and
                # class form (sa.BigInteger). Pre-R2 the ``isinstance(ast.Call)``
                # gate silently dropped class-form type_= and the column kept
                # its old type.
                type_str = normalize_ast_type(kw.value)
            elif kw.arg == "nullable":
                value = _literal(kw.value)
                if isinstance(value, bool):
                    nullable = value
            elif kw.arg == "new_column_name":
                value = _literal(kw.value)
                if isinstance(value, str):
                    new_column_name = value
        return Operation(
            op=op_name,
            line=line,
            table=_string_arg(args, 0),
            column_name=_string_arg(args, 1),
            type=type_str,
            nullable=nullable,
            new_column_name=new_column_name,
        )

    if op_name == "drop_column":
        return Operation(
            op=op_name, line=line, table=_string_arg(args, 0), column_name=_string_arg(args, 1)
        )

    if op_name == "drop_table":
        return Operation(op=op_name, line=line, table=_string_arg(args, 0))

    if op_name == "rename_table":
        return Operation(
            op=op_name, line=line, table=_string_arg(args, 0), new_table=_string_arg(args, 1)
        )

    if op_name == "rename_column":
        return Operation(
            op=op_name,
            line=line,
            table=_string_arg(args, 0),
            column_name=_string_arg(args, 1),
            new_column_name=_string_arg(args, 2),
        )

    if op_name == "execute":
        raw_sql = _execute_arg_string(args[0]) if args else None
        return Operation(op=op_name, line=line, raw_sql=raw_sql)

    # Unrecognized op.* call: carries no structured data. build_migration_schema's
    # _apply_operations skips it (no handler registered) and it isn't "execute",
    # so it's silently inert rather than an error — same as pre-Milestone-10.
    return Operation(op=op_name, line=line)


class AlembicParser:
    """`MigrationParser` implementation wrapping the functions above."""

    def parse(self, migration_path: str) -> Revision:
        return parse_revision_file(Path(migration_path))
