"""Freeze and adopt the schema used by the working 0.2.1 release.

This revision never imports current app.models/Base.metadata. Its JSON schema
snapshot is immutable, including when future model classes are changed.
"""
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0001_legacy"
down_revision = None
branch_labels = None
depends_on = None


class LegacySchemaError(RuntimeError):
    pass


def snapshot():
    return json.loads((Path(__file__).parent.parent / "legacy_schema.json").read_text(encoding="utf-8"))["tables"]


def column_type(column):
    kind = column["type"]
    if kind == "string":
        return sa.String(column["length"])
    if kind == "text":
        return sa.Text()
    if kind == "datetime":
        return sa.DateTime(timezone=column["timezone"])
    return {"integer": sa.Integer, "boolean": sa.Boolean, "date": sa.Date}[kind]()


def frozen_metadata():
    metadata = sa.MetaData()
    for spec in snapshot():
        columns = []
        for c in spec["columns"]:
            fks = [sa.ForeignKey(fk["target"], ondelete=fk["ondelete"]) for fk in c["foreign_keys"]]
            columns.append(sa.Column(c["name"], column_type(c), *fks,
                                     nullable=c["nullable"], primary_key=c["primary_key"],
                                     autoincrement=c["autoincrement"], server_default=c["server_default"]))
        uniques = [sa.UniqueConstraint(*u["columns"], name=u["name"]) for u in spec["unique_constraints"]]
        table = sa.Table(spec["name"], metadata, *columns, *uniques)
        for index in spec["indices"]:
            sa.Index(index["name"], *(table.c[c] for c in index["columns"]), unique=index["unique"])
    return metadata


def _compatible_type(expected, actual, dialect):
    typ = expected["type"]
    if typ == "text":
        return isinstance(actual, sa.Text)
    if typ == "string":
        return isinstance(actual, sa.String) and (actual.length is None or actual.length >= expected["length"])
    if typ == "datetime":
        return isinstance(actual, sa.DateTime) and (dialect == "sqlite" or actual.timezone == expected["timezone"])
    if typ == "date":
        return isinstance(actual, sa.Date) and not isinstance(actual, sa.DateTime)
    if typ == "boolean":
        return isinstance(actual, sa.Boolean)
    return isinstance(actual, sa.Integer) and not isinstance(actual, sa.Boolean)


def validate_legacy(connection):
    """Reject incompatible or partial schemas before adopting them, without edits.

    The old bootstrap added wish_cards.level without NOT NULL. That one known
    difference is accepted only if all existing rows have a value. It does not
    require rewriting or rebuilding an existing table during adoption.
    """
    specs = snapshot()
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names()) - {"alembic_version"}
    expected_tables = {t["name"] for t in specs}
    if not existing:
        return False
    missing = expected_tables - existing
    if missing:
        raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: missing tables: " + ", ".join(sorted(missing)))
    for table in specs:
        name = table["name"]
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        for column in table["columns"]:
            label = f"{name}.{column['name']}"
            actual = columns.get(column["name"])
            if actual is None or not _compatible_type(column, actual["type"], connection.dialect.name):
                raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: column/type " + label)
            if column["nullable"] and not actual["nullable"]:
                raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: nullable column " + label)
            if not column["nullable"] and actual["nullable"]:
                if (name, column["name"]) != ("wish_cards", "level"):
                    raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: required column " + label)
                table_expr = sa.table(name, sa.column(column["name"]))
                if connection.execute(sa.select(sa.literal(1)).select_from(table_expr)
                                      .where(table_expr.c[column["name"]].is_(None)).limit(1)).first():
                    raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: null values in " + label)
        pk = inspector.get_pk_constraint(name).get("constrained_columns", [])
        if pk != [c["name"] for c in table["columns"] if c["primary_key"]]:
            raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: primary key " + name)
        actual_indexes = inspector.get_indexes(name)
        unique_sets = {frozenset(c["column_names"]) for c in inspector.get_unique_constraints(name)}
        unique_sets |= {frozenset(i["column_names"]) for i in actual_indexes if i["unique"]}
        required_uniques = [c["columns"] for c in table["unique_constraints"]]
        required_uniques += [i["columns"] for i in table["indices"] if i["unique"]]
        if any(frozenset(cols) not in unique_sets for cols in required_uniques):
            raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: unique constraints " + name)
        actual_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]),
             (fk.get("options", {}).get("ondelete") or "").upper())
            for fk in inspector.get_foreign_keys(name)
        }
        for column in table["columns"]:
            for fk in column["foreign_keys"]:
                target_table, target_column = fk["target"].split(".")
                expected = ((column["name"],), target_table, (target_column,), (fk["ondelete"] or "").upper())
                if expected not in actual_fks:
                    raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: foreign key " + name + "." + column["name"])
        known_columns = {c["name"] for c in table["columns"]}
        for extra in columns.keys() - known_columns:
            c = columns[extra]
            if not c["nullable"] and c.get("default") is None and not c.get("identity"):
                raise LegacySchemaError("LEGACY_SCHEMA_INCOMPATIBLE: required unknown column " + name + "." + extra)
    return True


def _encrypt_legacy_values(connection, metadata):
    # Explicit migration replaces the old automatic startup backfill. Validate
    # existing ciphertext with DATA_KEYS before committing any revision marker.
    from app.crypto import PREFIX, decrypt, encrypt

    for spec in snapshot():
        table = metadata.tables[spec["name"]]
        pk = list(table.primary_key.columns)
        for c in spec["columns"]:
            if not c.get("encrypted"):
                continue
            col = table.c[c["name"]]
            for row in connection.execute(sa.select(*pk, col.label("raw")).where(col.is_not(None))):
                raw = row._mapping["raw"]
                if raw.startswith(PREFIX):
                    decrypt(raw)
                    continue
                conditions = [key == row._mapping[key.name] for key in pk]
                connection.execute(table.update().where(*conditions).values({col.name: encrypt(raw)}))


def upgrade():
    connection = op.get_bind()
    existing = validate_legacy(connection)
    metadata = frozen_metadata()
    if not existing:
        metadata.create_all(connection)
    _encrypt_legacy_values(connection, metadata)


def downgrade():
    raise RuntimeError("Destructive schema downgrade is disabled; restore a verified backup explicitly")
