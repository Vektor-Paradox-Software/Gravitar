"""SPEC-17 S3 §6.3 column reader.

Reads column declarations from a ClassDef body:

- ``name = Column(TypeExpr, *, nullable=..., primary_key=...)``
- ``name: Mapped[T] = mapped_column(TypeExpr?, nullable=..., ...)``
- ``name: Mapped[T]`` (bare annotation — implicit mapped_column)

Also handles ``Annotated[T, mapped_column(primary_key=True)]`` alias
expansion so ``Mapped[intpk]`` with ``intpk = Annotated[int,
mapped_column(primary_key=True)]`` resolves correctly.

Nullability follows SPEC §6.3's single rule function (identical on
both the AST and live-instance sides); this file is the authoritative
implementation.

Everything type-related delegates to the R2 resolver so the AST and
import readers normalize identically. ``_type_from_annotation`` maps
Python-type annotations (``int``, ``str`` etc.) to SA types the
R2 resolver can match, following the SA 2.0 default annotation map.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from gravitar.schema.model_reader.consts import ConstFoldRewriter
from gravitar.schema.types import normalize_ast_type

#: SA 2.0 default `type_annotation_map`. Keys are the Python type
#: names (as they appear in a `Mapped[T]` annotation); values are
#: AST expressions constructed on demand that the R2 resolver
#: normalizes to the canonical type string. (SPEC-17 §4 verified
#: these against SA 2.1.3.)
_DEFAULT_ANNOTATION_MAP: dict[str, str] = {
    "int": "Integer",
    "str": "String",
    "float": "Float",
    "bool": "Boolean",
    "bytes": "LargeBinary",
    "datetime": "DateTime",
    "date": "Date",
    "time": "Time",
    "UUID": "Uuid",
    "Decimal": "Numeric",
    "timedelta": "Interval",
}


@dataclass(frozen=True)
class ModuleTypeContext:
    """SPEC-17 S5: per-module context the resolver needs to normalize
    Mapped[T] annotations beyond the SA 2.0 defaults.

    - ``annotation_map`` is a merge of the declarative base's own
      ``type_annotation_map`` and the shared SA defaults. Keys are
      Python-type names (e.g. ``"str"``), values are AST type
      expressions the R2 resolver normalizes.
    - ``enums`` maps a Python Enum class name (as declared in-module)
      to the sorted list of its member names, so ``Mapped[Color]``
      resolves to ``enum(BLUE,GREEN,RED)``.

    A column's ``resolved_type(ctx)`` call looks up its annotation
    against this context before falling to the default map / R2
    class-form path.
    """

    annotation_map: dict[str, ast.expr] = field(default_factory=dict)
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class AnnotationInfo:
    """Resolved information from a ``Mapped[T]`` annotation."""

    optional: bool  # True iff T is Optional[...] / T | None / Union[T, None]
    inner_expr: ast.expr | None  # the T itself, if parseable
    annotated_extras: list[ast.expr] = field(default_factory=list)  # Annotated[T, extras...]


@dataclass
class ColumnDecl:
    """SPEC-17 §6.1 ColumnDecl — one column read off a class body."""

    key: str  # attribute name as written on the class
    name: str  # database column name (defaults to key)
    type_expr: ast.expr | None  # AST expression the type came from
    annotation: AnnotationInfo | None
    nullable_explicit: bool | None  # kwarg; None iff not foldable or not given
    primary_key: bool | None
    fk_target: str | None  # "parent_table.column" if the column is FK-only
    line: int
    origin_class: str  # class the decl was found in (for mixin attribution)

    def resolved_type(self, ctx: ModuleTypeContext | None = None) -> str | None:
        """Normalized canonical type string via the R2 resolver, or
        None when no type expression could be found. Nullability /
        PK are *not* reflected here (they're separate columns).

        SPEC-17 S5: ``ctx`` carries the per-module annotation map
        and Enum table — resolution order is:
          1. Explicit type expression on the column call;
          2. ``Mapped[Literal[...]]`` inline enum;
          3. ``ctx.enums`` lookup for ``Mapped[PyEnumClass]``;
          4. ``ctx.annotation_map`` lookup (user override + SA defaults);
          5. SA 2.0 default annotation map (int → Integer, …).
        """
        if self.type_expr is not None:
            return normalize_ast_type(self.type_expr)
        if self.annotation is not None and self.annotation.inner_expr is not None:
            inner = self.annotation.inner_expr
            literal_enum = _enum_from_literal(inner)
            if literal_enum is not None:
                return normalize_ast_type(literal_enum)
            if ctx is not None:
                enum_expr = _enum_from_ctx(inner, ctx.enums)
                if enum_expr is not None:
                    return normalize_ast_type(enum_expr)
                override = _mapped_type_via_ctx(inner, ctx.annotation_map)
                if override is not None:
                    return normalize_ast_type(override)
            mapped = _type_from_annotation(inner)
            if mapped is not None:
                return normalize_ast_type(mapped)
        return None


# --- Nullability (SPEC §6.3) ---


def resolve_nullability(decl: ColumnDecl) -> bool | None:
    """SPEC §6.3 — the one function; both AST and live sides use
    it. Returns the resolved nullable, or None when the explicit
    kwarg was non-foldable (caller then emits a scoped note and
    treats the column as having unknown nullability)."""
    if decl.nullable_explicit is not None:
        return decl.nullable_explicit
    if decl.primary_key:
        return False
    if decl.annotation is not None:
        if decl.annotation.optional:
            return True
        if decl.annotation.inner_expr is not None:
            # SA 2.0 annotation present and non-Optional → NOT NULL.
            return False
    # Classic Column() default: NULL unless primary_key.
    return True


# --- Reader: entry point ---


def read_columns(
    class_node: ast.ClassDef, origin_class: str, aliases: dict[str, AnnotationInfo]
) -> list[ColumnDecl]:
    """Walk ``class_node.body`` once, extracting one ColumnDecl per
    attribute that looks like a column. ``aliases`` is the module's
    expanded ``Annotated[...]`` alias table (see
    ``collect_annotation_aliases``) so ``Mapped[intpk]`` can look
    up the real annotation shape."""
    decls: list[ColumnDecl] = []
    for stmt in class_node.body:
        decl = _column_from_stmt(stmt, origin_class, aliases)
        if decl is not None:
            decls.append(decl)
    return decls


def _column_from_stmt(
    stmt: ast.stmt, origin_class: str, aliases: dict[str, AnnotationInfo]
) -> ColumnDecl | None:
    """Dispatch over the shapes a column can take."""
    # ``name: Mapped[T] = mapped_column(...)`` or ``name: Mapped[T]``.
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return _column_from_annassign(stmt, origin_class, aliases)
    # ``name = Column(...)``
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        if isinstance(stmt.targets[0], ast.Name):
            return _column_from_classic_assign(stmt, origin_class)
    return None


def _column_from_classic_assign(stmt: ast.Assign, origin_class: str) -> ColumnDecl | None:
    """``name = Column(TypeExpr, *, nullable=..., primary_key=...)``."""
    target = stmt.targets[0]
    assert isinstance(target, ast.Name)  # noqa: S101
    key = target.id
    if key.startswith("__") and key.endswith("__"):
        return None
    rhs = stmt.value
    if not isinstance(rhs, ast.Call):
        return None
    callee = _callable_name(rhs.func)
    if callee != "Column":
        return None
    name, type_expr, primary_key, nullable_explicit, fk_target = _parse_column_call(rhs)
    return ColumnDecl(
        key=key,
        name=name or key,
        type_expr=type_expr,
        annotation=None,
        nullable_explicit=nullable_explicit,
        primary_key=primary_key,
        fk_target=fk_target,
        line=stmt.lineno,
        origin_class=origin_class,
    )


def _column_from_annassign(
    stmt: ast.AnnAssign, origin_class: str, aliases: dict[str, AnnotationInfo]
) -> ColumnDecl | None:
    """``name: Mapped[T]`` with optional ``= mapped_column(...)``."""
    assert isinstance(stmt.target, ast.Name)  # noqa: S101
    key = stmt.target.id
    if key.startswith("__") and key.endswith("__"):
        return None

    annotation_info = _annotation_info(stmt.annotation, aliases)
    if annotation_info is None:
        return None  # not a Mapped[...] annotation — skip entirely

    name = key
    type_expr: ast.expr | None = None
    primary_key: bool | None = None
    nullable_explicit: bool | None = None
    fk_target: str | None = None

    # Annotated extras may carry a mapped_column — expand them.
    for extra in annotation_info.annotated_extras:
        if isinstance(extra, ast.Call):
            e_name, e_type, e_pk, e_null, e_fk = _parse_column_call(extra)
            if e_type is not None and type_expr is None:
                type_expr = e_type
            if e_name:
                name = e_name
            if e_pk is not None:
                primary_key = e_pk
            if e_null is not None:
                nullable_explicit = e_null
            if e_fk is not None:
                fk_target = e_fk

    rhs = stmt.value
    if isinstance(rhs, ast.Call):
        callee = _callable_name(rhs.func)
        if callee == "mapped_column":
            n2, t2, pk2, null2, fk2 = _parse_column_call(rhs)
            # SPEC-17 §6.3: explicit kwargs win over the annotation.
            if n2:
                name = n2
            if t2 is not None:
                type_expr = t2
            if pk2 is not None:
                primary_key = pk2
            if null2 is not None:
                nullable_explicit = null2
            if fk2 is not None:
                fk_target = fk2

    return ColumnDecl(
        key=key,
        name=name,
        type_expr=type_expr,
        annotation=annotation_info,
        nullable_explicit=nullable_explicit,
        primary_key=primary_key,
        fk_target=fk_target,
        line=stmt.lineno,
        origin_class=origin_class,
    )


# --- Mapped[...] annotation parsing ---


def _annotation_info(node: ast.expr, aliases: dict[str, AnnotationInfo]) -> AnnotationInfo | None:
    """Unwrap ``Mapped[T]`` into inner + optional-ness. Returns None
    if ``node`` isn't a Mapped[...] annotation."""
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Name) and value.id == "Mapped":
            inner = node.slice
            return _resolve_inner_annotation(inner, aliases)
        if isinstance(value, ast.Attribute) and value.attr == "Mapped":
            inner = node.slice
            return _resolve_inner_annotation(inner, aliases)
    return None


def _resolve_inner_annotation(
    inner: ast.expr, aliases: dict[str, AnnotationInfo]
) -> AnnotationInfo:
    """Handle ``T`` / ``Optional[T]`` / ``T | None`` / ``Union[T, None]``
    / ``Annotated[T, extras...]`` / an alias.
    """
    optional = False
    annotated_extras: list[ast.expr] = []

    # Alias lookup (e.g. ``Mapped[intpk]`` where ``intpk = Annotated[...]``).
    if isinstance(inner, ast.Name) and inner.id in aliases:
        alias = aliases[inner.id]
        return AnnotationInfo(
            optional=alias.optional,
            inner_expr=alias.inner_expr,
            annotated_extras=list(alias.annotated_extras),
        )

    # Optional[T] / Union[...] / Annotated[...].
    if isinstance(inner, ast.Subscript) and isinstance(inner.value, ast.Name):
        outer_name = inner.value.id
        slice_node = inner.slice
        if outer_name == "Optional":
            optional = True
            inner = slice_node
        elif outer_name == "Union":
            inner, optional_union = _unwrap_union(slice_node)
            optional = optional or optional_union
        elif outer_name == "Annotated":
            resolved_inner, extras = _unwrap_annotated(slice_node)
            annotated_extras = list(extras)
            inner = resolved_inner

    # ``T | None`` — PEP 604.
    if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr):
        inner, flipped = _unwrap_pipe_none(inner)
        optional = optional or flipped

    return AnnotationInfo(optional=optional, inner_expr=inner, annotated_extras=annotated_extras)


def _unwrap_union(slice_node: ast.expr) -> tuple[ast.expr, bool]:
    """Return (first non-None element, saw-None-flag) from a
    ``Union[A, B, None]`` tuple slice."""
    optional = False
    first_non_none: ast.expr | None = None
    if isinstance(slice_node, ast.Tuple):
        for elt in slice_node.elts:
            if _is_none(elt):
                optional = True
            elif first_non_none is None:
                first_non_none = elt
    else:
        first_non_none = slice_node
    return (first_non_none or slice_node), optional


def _unwrap_pipe_none(node: ast.BinOp) -> tuple[ast.expr, bool]:
    """``T | None`` or ``None | T`` → (T, True)."""
    left, right = node.left, node.right
    if _is_none(right):
        return left, True
    if _is_none(left):
        return right, True
    return node, False


def _unwrap_annotated(slice_node: ast.expr) -> tuple[ast.expr, list[ast.expr]]:
    """``Annotated[T, extras...]`` → (T, [extras])."""
    if isinstance(slice_node, ast.Tuple) and slice_node.elts:
        return slice_node.elts[0], list(slice_node.elts[1:])
    return slice_node, []


def _is_none(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and node.value is None) or (
        isinstance(node, ast.Name) and node.id == "None"
    )


# --- Column(...) / mapped_column(...) argument parsing ---


def _parse_column_call(
    call: ast.Call,
) -> tuple[str | None, ast.expr | None, bool | None, bool | None, str | None]:
    """Return (name, type_expr, primary_key, nullable, fk_target).

    Follows the same rule as the Alembic parser's ``_resolve_column_call``:
    the type is "the first positional ast arg after the name that
    looks like a type" — i.e. not a ForeignKey.
    """
    name: str | None = None
    type_expr: ast.expr | None = None
    primary_key: bool | None = None
    nullable: bool | None = None
    fk_target: str | None = None

    args = list(call.args)
    # ``Column("name", TypeExpr, ...)`` vs ``Column(TypeExpr, ...)``.
    if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
        name = args[0].value
        remaining = args[1:]
    else:
        remaining = args

    for arg in remaining:
        # ``ForeignKey("parent.id")`` isn't a type — capture target & skip.
        if isinstance(arg, ast.Call):
            callee = _callable_name(arg.func)
            if callee == "ForeignKey" and arg.args:
                fk_literal = _fold_literal(arg.args[0])
                if isinstance(fk_literal, str):
                    fk_target = fk_literal
                continue
            if callee in _NON_TYPE_CALLS:
                continue
        if type_expr is None:
            type_expr = arg
            break

    for kw in call.keywords:
        if kw.arg == "primary_key":
            val = _fold_literal(kw.value)
            if isinstance(val, bool):
                primary_key = val
        elif kw.arg == "nullable":
            val = _fold_literal(kw.value)
            if isinstance(val, bool):
                nullable = val
        elif kw.arg == "name":
            val = _fold_literal(kw.value)
            if isinstance(val, str):
                name = val
        elif kw.arg == "type_":
            type_expr = kw.value

    return name, type_expr, primary_key, nullable, fk_target


_NON_TYPE_CALLS: frozenset[str] = frozenset(
    {
        "ForeignKey",
        "Sequence",
        "Identity",
        "Computed",
        "Index",
        "CheckConstraint",
        "UniqueConstraint",
        "PrimaryKeyConstraint",
        "ForeignKeyConstraint",
    }
)


def _callable_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _fold_literal(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


# --- Annotation-to-type mapping ---


def _type_from_annotation(inner: ast.expr) -> ast.expr | None:
    """Map ``Mapped[T]``'s T to an AST expression the R2 resolver
    accepts. ``int`` → ``ast.Name('Integer')``, etc. Returns None if
    T is a user-defined type / class the default map doesn't cover —
    S5's ``ctx`` lookups fire first for those."""
    name = _simple_name(inner)
    if name is None:
        return None
    sa_name = _DEFAULT_ANNOTATION_MAP.get(name)
    if sa_name is None:
        return None
    return ast.copy_location(ast.Name(id=sa_name, ctx=ast.Load()), inner)


# --- S5: Enum / Literal / annotation_map resolvers ---


def _enum_from_literal(inner: ast.expr) -> ast.expr | None:
    """SPEC-17 §4: ``Mapped[Literal["x", "y"]]`` → enum("x","y").
    Builds a synthetic ``Enum("x","y")`` call expression the R2
    resolver already knows how to normalize."""
    if not isinstance(inner, ast.Subscript):
        return None
    value = inner.value
    if not (
        (isinstance(value, ast.Name) and value.id == "Literal")
        or (isinstance(value, ast.Attribute) and value.attr == "Literal")
    ):
        return None
    slice_node = inner.slice
    elts: list[ast.expr] = []
    if isinstance(slice_node, ast.Tuple):
        elts = list(slice_node.elts)
    else:
        elts = [slice_node]
    args: list[ast.expr] = []
    for elt in elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            args.append(ast.copy_location(ast.Constant(value=elt.value), elt))
    if not args:
        return None
    return ast.copy_location(
        ast.Call(
            func=ast.Name(id="Enum", ctx=ast.Load()),
            args=args,
            keywords=[],
        ),
        inner,
    )


def _enum_from_ctx(inner: ast.expr, enums: dict[str, tuple[str, ...]]) -> ast.expr | None:
    """SPEC-17 §4: ``Mapped[PyEnum]`` resolves to enum(member names,
    not values). ``ctx.enums[PyEnum]`` carries those names, discovered
    by walking the module AST at build time."""
    name = _simple_name(inner)
    if name is None or name not in enums:
        return None
    members = enums[name]
    args: list[ast.expr] = [ast.copy_location(ast.Constant(value=m), inner) for m in members]
    return ast.copy_location(
        ast.Call(
            func=ast.Name(id="Enum", ctx=ast.Load()),
            args=args,
            keywords=[],
        ),
        inner,
    )


def _mapped_type_via_ctx(inner: ast.expr, annotation_map: dict[str, ast.expr]) -> ast.expr | None:
    """SPEC-17 §4: user-provided ``type_annotation_map`` on the
    declarative base overrides the default SA mapping. e.g.
    ``{str: String(50)}`` → ``Mapped[str]`` resolves to varchar(50)."""
    name = _simple_name(inner)
    if name is None:
        return None
    override = annotation_map.get(name)
    if override is None:
        return None
    return override


def collect_enum_members(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Scan module scope for ``class X(Enum): FOO = ...`` and record
    FOO as a member of X. SPEC-17 §4: Enum member NAMES, not values —
    SA 2.1 default behavior.

    Recognizes direct ``Enum`` / ``IntEnum`` / ``StrEnum`` base
    classes. Member-only assignments (``FOO = "bar"``) count;
    dunders and methods don't.
    """
    enums: dict[str, tuple[str, ...]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef):
            continue
        if not _is_enum_base_list(stmt.bases):
            continue
        members: list[str] = []
        for item in stmt.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                target_name = _single_target(item)
                if target_name and not target_name.startswith("_"):
                    members.append(target_name)
        if members:
            enums[stmt.name] = tuple(sorted(members))
    return enums


def _is_enum_base_list(bases: list[ast.expr]) -> bool:
    """A class whose base list reaches an ``Enum``/``IntEnum``/
    ``StrEnum`` identifier counts as a Python enum for our purposes.
    Doesn't resolve across imports — rare in model files and S9
    generative tests catch skew. SA 2.0 projects write ``from enum
    import Enum`` and reference the bare name."""
    enum_names = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
    for base in bases:
        if isinstance(base, ast.Name) and base.id in enum_names:
            return True
        if isinstance(base, ast.Attribute) and base.attr in enum_names:
            return True
    return False


def _single_target(stmt: ast.stmt) -> str | None:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        tgt = stmt.targets[0]
        if isinstance(tgt, ast.Name):
            return tgt.id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return None


def collect_annotation_map(cls: ast.ClassDef) -> dict[str, ast.expr]:
    """Read ``type_annotation_map = { str: String(50), ... }`` off a
    declarative base class body. Returns the parsed map; keys must
    be ``ast.Name`` references (Python type names like ``str``).
    """
    out: dict[str, ast.expr] = {}
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        tgt = stmt.targets[0]
        if not isinstance(tgt, ast.Name) or tgt.id != "type_annotation_map":
            continue
        if not isinstance(stmt.value, ast.Dict):
            continue
        for key, value in zip(stmt.value.keys, stmt.value.values, strict=False):
            if isinstance(key, ast.Name):
                out[key.id] = value
    return out


def build_module_type_context(tree: ast.Module, base_class_names: list[str]) -> ModuleTypeContext:
    """Build the per-module ``ModuleTypeContext`` by scanning the
    tree for Enum classes and declarative-base ``type_annotation_map``
    attributes. ``base_class_names`` names which classes to inspect
    for annotation_map; typically the ones the class graph marked
    as declarative bases."""
    enums = collect_enum_members(tree)
    annotation_map: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name in base_class_names:
            annotation_map.update(collect_annotation_map(stmt))
    return ModuleTypeContext(annotation_map=annotation_map, enums=enums)


def _simple_name(node: ast.expr) -> str | None:
    """Collapse an annotation reference to its simple name:
    ``int``, ``datetime.datetime`` → ``datetime``, etc."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# --- Annotated alias collection (module-level `intpk = Annotated[...]`) ---


def collect_annotation_aliases(tree: ast.Module) -> dict[str, AnnotationInfo]:
    """Walk module scope for ``NAME = Annotated[T, extras...]`` and
    produce an alias → AnnotationInfo map. SPEC-17 F-05: ``intpk =
    Annotated[int, mapped_column(primary_key=True)]`` so a column
    annotated ``Mapped[intpk]`` resolves correctly without the
    reader having to re-expand every usage."""
    aliases: dict[str, AnnotationInfo] = {}
    # First pass: capture any module-level NAME = <literal> consts
    # that an Annotated alias might reference (depth-1 const fold).
    const_map: dict[str, object] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            if isinstance(stmt.targets[0], ast.Name):
                try:
                    const_map[stmt.targets[0].id] = ast.literal_eval(stmt.value)
                except (ValueError, TypeError, SyntaxError):
                    pass

    # Second pass: find Annotated-alias assignments.
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        if not isinstance(stmt.targets[0], ast.Name):
            continue
        if not isinstance(stmt.value, ast.Subscript):
            continue
        if not (isinstance(stmt.value.value, ast.Name) and stmt.value.value.id == "Annotated"):
            continue
        inner_t, extras = _unwrap_annotated(stmt.value.slice)
        # If the alias used a const RHS for its inner type expression
        # (rare), fold here. Normally the inner is an ast.Name already.
        folded_inner = _fold_name_through_consts(inner_t, const_map)
        aliases[stmt.targets[0].id] = AnnotationInfo(
            optional=False, inner_expr=folded_inner, annotated_extras=list(extras)
        )
    return aliases


def _fold_name_through_consts(inner: ast.expr, const_map: dict[str, object]) -> ast.expr:
    """If `inner` is ``ast.Name(const_name)`` and the module has
    ``const_name = <str>`` at scope, replace with the string constant.
    Used by Annotated aliases referencing module-level names."""
    if isinstance(inner, ast.Name) and inner.id in const_map:
        val = const_map[inner.id]
        if isinstance(val, (str, int, bool, float)) or val is None:
            replacement: ast.expr = ast.copy_location(ast.Constant(value=val), inner)
            return replacement
    return inner


# --- Preserve ConstFoldRewriter re-export (used by S4 for computed names) ---


__all__ = [
    "AnnotationInfo",
    "ColumnDecl",
    "ConstFoldRewriter",
    "collect_annotation_aliases",
    "read_columns",
    "resolve_nullability",
]
