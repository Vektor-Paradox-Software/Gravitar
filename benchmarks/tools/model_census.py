"""Read-only AST census of ORM model definition styles. Never imports target code."""

import ast
import collections
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
C = collections.Counter()
files = collections.Counter()
badsyn = 0
colnames = {"Column", "mapped_column"}


def fname(f):
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


for p in root.rglob("*.py"):
    parts = set(p.parts)
    if (
        ".git" in parts
        or "tests" in parts
        or "test" in parts
        or "migrations" in parts
        or "alembic" in parts
        or "node_modules" in parts
    ):
        continue
    try:
        t = p.read_text(encoding="utf-8")
    except Exception:
        continue
    if "Column" not in t and "mapped_column" not in t and "Mapped" not in t:
        continue
    try:
        tree = ast.parse(t)
    except SyntaxError:
        badsyn += 1
        continue
    used = False
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            tn = None
            nonliteral = False
            abstract = False
            has_table_args = False
            for b in n.body:
                tgt = None
                val = None
                if (
                    isinstance(b, ast.Assign)
                    and len(b.targets) == 1
                    and isinstance(b.targets[0], ast.Name)
                ):
                    tgt, val = b.targets[0].id, b.value
                elif isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name):
                    tgt, val = b.target.id, b.value
                if tgt == "__tablename__":
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        tn = val.value
                    else:
                        nonliteral = True
                if tgt == "__abstract__":
                    abstract = True
                if tgt == "__table_args__":
                    has_table_args = True
                if isinstance(b, ast.FunctionDef) and b.name == "__tablename__":
                    nonliteral = True
                # declared_attr-decorated tablename
                if isinstance(b, ast.FunctionDef) and any(
                    fname(d if not isinstance(d, ast.Call) else d.func) == "declared_attr"
                    for d in b.decorator_list
                ):
                    C["declared_attr_members"] += 1
                if tgt and val is not None and isinstance(val, ast.Call):
                    f = fname(val.func)
                    if f == "Column":
                        C["col_Column"] += 1
                        used = True
                    elif f == "mapped_column":
                        C["col_mapped_column"] += 1
                        used = True
                    elif f in ("relationship", "backref"):
                        C["relationship"] += 1
                    elif f in ("column_property", "synonym", "hybrid_property"):
                        C["col_" + f] += 1
                    # type arg shape for Column/mapped_column
                    if f in colnames:
                        if isinstance(b, ast.AnnAssign):
                            C["col_with_annotation"] += 1
                        a0 = val.args[0] if val.args else None
                        if a0 is not None:
                            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                                C["col_first_arg_string_name"] += 1
                            tn0 = (
                                a0
                                if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str))
                                else (val.args[1] if len(val.args) > 1 else None)
                            )
                            if tn0 is None and f == "mapped_column":
                                C["mapped_column_type_from_annotation"] += 1
                        elif f == "mapped_column":
                            C["mapped_column_type_from_annotation"] += 1
                        if any(k.arg == "name" for k in val.keywords):
                            C["col_explicit_name_kw"] += 1
                if (
                    isinstance(b, ast.AnnAssign)
                    and isinstance(b.annotation, ast.Subscript)
                    and fname(b.annotation.value) == "Mapped"
                ):
                    C["Mapped_annotation"] += 1
                    if val is None:
                        C["Mapped_no_mapped_column"] += 1
            if tn:
                C["class_literal_tablename"] += 1
                used = True
            if nonliteral:
                C["class_nonliteral_tablename"] += 1
            if abstract:
                C["class_abstract"] += 1
            if has_table_args:
                C["class_table_args"] += 1
            bases = [fname(x) for x in n.bases]
            if any(x in ("TypeDecorator", "UserDefinedType") for x in bases):
                C["custom_type_classes"] += 1
            if any(x and ("Mixin" in x) for x in bases):
                C["class_with_mixin_base"] += 1
        if isinstance(n, ast.Call) and fname(n.func) == "Table":
            C["core_Table_calls"] += 1
            used = True
        if isinstance(n, ast.Call) and fname(n.func) in ("MetaData",):
            C["MetaData_calls"] += 1
            if any(k.arg == "naming_convention" for k in n.keywords):
                C["metadata_naming_convention"] += 1
            if any(k.arg == "schema" for k in n.keywords):
                C["metadata_schema"] += 1
        if isinstance(n, ast.Call) and fname(n.func) in ("declarative_base",):
            C["declarative_base_calls"] += 1
        if isinstance(n, ast.Call) and fname(n.func) in ("setattr", "type") and len(n.args) == 3:
            C["dynamic_type3_or_setattr"] += 1
        if isinstance(n, ast.ClassDef) and any(fname(x) == "DeclarativeBase" for x in n.bases):
            C["DeclarativeBase_subclass"] += 1
        if isinstance(n, ast.ClassDef) and any(fname(x) in ("Model",) for x in n.bases):
            C["flask_db_Model_subclass"] += 1
        if isinstance(n, ast.Call) and fname(n.func) in ("event.listens_for", "listens_for"):
            C["event_listeners"] += 1
        if isinstance(n, ast.Call) and fname(n.func) == "with_variant":
            C["with_variant_calls"] += 1
    if used:
        files["model_files"] += 1
out = dict(counts=dict(sorted(C.items())), files=dict(files), unparseable=badsyn)
print(json.dumps(out))
