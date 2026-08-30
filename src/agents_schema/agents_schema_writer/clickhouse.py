from __future__ import annotations

import json
import re
from typing import Any, Iterable

from agents_schema.config import ConfigError

from .base import AgentsSchemaWriter
from .schema import AGENTS_SCHEMA, Column, TableSchema
from .utils import batched, primary_key_rows

INSERT_BATCH_SIZE = 1000
CLICKHOUSE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Lightweight DELETEs are applied as a mask immediately visible to subsequent
# SELECTs; setting lightweight_deletes_sync=2 additionally waits for the delete
# to execute on the current replica before returning.
_DELETE_SETTINGS = {"lightweight_deletes_sync": 2}


class ClickHouseAgentsSchemaWriter(AgentsSchemaWriter):
    """Writes agents.* tables to ClickHouse via a clickhouse-connect client.

    ClickHouse-specific mapping decisions:
    - The AGENTS schema maps to a ClickHouse *database* named ``agents``
      (ClickHouse has a two-level ``database.table`` namespace).
    - ClickHouse identifiers are case-sensitive; this writer quotes and creates
      the package's canonical lowercase names.
    - Declared primary keys become the MergeTree ``ORDER BY`` key. ClickHouse
      does not enforce uniqueness, so upserts are implemented as a scoped
      lightweight ``DELETE`` of the incoming keys followed by an ``INSERT``.
    - ``array`` columns map to ``Array(String)``. ``json`` columns map to the
      native ``JSON`` type on servers that support it (25.3+, where the type is
      production-ready), and fall back to ``String`` holding JSON text on
      older servers.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._json_type: str | None = None

    def ensure_table(self, table: TableSchema) -> None:
        self._command(f"CREATE DATABASE IF NOT EXISTS {self._identifier(AGENTS_SCHEMA)}")
        self._command(self._create_table_sql("CREATE TABLE IF NOT EXISTS", table))

    def replace_table(self, table: TableSchema) -> None:
        self._command(f"CREATE DATABASE IF NOT EXISTS {self._identifier(AGENTS_SCHEMA)}")
        self._command(self._create_table_sql("CREATE OR REPLACE TABLE", table))

    def upsert_rows(self, table: TableSchema, rows: Iterable[tuple[Any, ...]]) -> None:
        if not table.primary_key:
            raise ConfigError("upsert requires a table primary key")
        rows = list(rows)
        if not rows:
            return
        self.ensure_table(table)
        self._delete_matching_keys(table, table.primary_key, primary_key_rows(table, rows))
        self.insert_rows(table, rows)

    def insert_rows(self, table: TableSchema, rows: Iterable[tuple[Any, ...]]) -> None:
        rows = list(rows)
        if not rows:
            return
        columns = ", ".join(self._identifier(column.name) for column in table.columns)
        for batch in batched(rows, INSERT_BATCH_SIZE):
            values = ", ".join(self._row_literal(table, row) for row in batch)
            self._command(
                f"INSERT INTO {self._table_ref(table)} ({columns}) VALUES {values}"
            )

    def delete_rows(
        self,
        table: TableSchema,
        key_columns: tuple[str, ...],
        rows: Iterable[tuple[Any, ...]],
    ) -> None:
        if not key_columns:
            raise ConfigError("delete requires at least one key column")
        key_rows = list(rows)
        if not key_rows:
            return
        self.ensure_table(table)
        self._delete_matching_keys(table, key_columns, key_rows)

    def reconcile_rows(self, table: TableSchema, rows: Iterable[tuple[Any, ...]]) -> None:
        rows = list(rows)
        self.ensure_table(table)
        self.upsert_rows(table, rows)
        self._delete_absent_rows(table, primary_key_rows(table, rows))

    def close(self) -> None:
        self._client.close()

    def _command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self._client.command(sql, settings=settings)

    def _delete_matching_keys(
        self,
        table: TableSchema,
        key_columns: tuple[str, ...],
        key_rows: list[tuple[Any, ...]],
    ) -> None:
        for batch in batched(key_rows, INSERT_BATCH_SIZE):
            self._command(
                f"DELETE FROM {self._table_ref(table)} "
                f"WHERE {self._key_tuple_sql(key_columns)} IN ({self._key_values_sql(batch)})",
                settings=_DELETE_SETTINGS,
            )

    def _delete_absent_rows(self, table: TableSchema, key_rows: list[tuple[Any, ...]]) -> None:
        if not key_rows:
            self._command(f"TRUNCATE TABLE {self._table_ref(table)}")
            return
        self._command(
            f"DELETE FROM {self._table_ref(table)} "
            f"WHERE {self._key_tuple_sql(table.primary_key)} NOT IN ({self._key_values_sql(key_rows)})",
            settings=_DELETE_SETTINGS,
        )

    def _key_tuple_sql(self, key_columns: tuple[str, ...]) -> str:
        identifiers = [self._identifier(column) for column in key_columns]
        if len(identifiers) == 1:
            return identifiers[0]
        return "(" + ", ".join(identifiers) + ")"

    def _key_values_sql(self, key_rows: list[tuple[Any, ...]]) -> str:
        tuples = []
        for row in key_rows:
            literals = [_string_literal(value) for value in row]
            tuples.append(literals[0] if len(literals) == 1 else "(" + ", ".join(literals) + ")")
        return ", ".join(tuples)

    def _row_literal(self, table: TableSchema, row: tuple[Any, ...]) -> str:
        literals = []
        for index, (column, value) in enumerate(zip(table.columns, row, strict=True)):
            if index in table.array_indexes:
                literals.append(_array_literal(value))
            elif index in table.json_indexes:
                literals.append(_string_literal(json.dumps(value or {})))
            elif column.kind == "boolean":
                literals.append(_boolean_literal(value))
            else:
                literals.append(_string_literal(value))
        return "(" + ", ".join(literals) + ")"

    def _create_table_sql(self, prefix: str, table: TableSchema) -> str:
        definitions = ", ".join(
            f"{self._identifier(column.name)} {_clickhouse_type(column, self._resolve_json_type())}"
            for column in table.columns
        )
        order_by = (
            "(" + ", ".join(self._identifier(column) for column in table.primary_key) + ")"
            if table.primary_key
            else "tuple()"
        )
        return (
            f"{prefix} {self._table_ref(table)} ({definitions}) "
            f"ENGINE = MergeTree ORDER BY {order_by}"
        )

    def _table_ref(self, table: TableSchema) -> str:
        return f"{self._identifier(AGENTS_SCHEMA)}.{self._identifier(table.base_name)}"

    def _identifier(self, identifier: str) -> str:
        if not CLICKHOUSE_IDENTIFIER_RE.fullmatch(identifier):
            raise ConfigError(f"expected a simple ClickHouse identifier: {identifier}")
        return f"`{identifier}`"

    def _resolve_json_type(self) -> str:
        """Use the native JSON type on 25.3+, else String holding JSON text."""
        if self._json_type is None:
            self._json_type = "JSON" if _supports_json_type(self._client) else "String"
        return self._json_type


def _supports_json_type(client: Any) -> bool:
    version = getattr(client, "server_version", None)
    if not version:
        return True
    parts = str(version).split(".")
    try:
        major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return True
    return (major, minor) >= (25, 3)


def _clickhouse_type(column: Column, json_type: str) -> str:
    if column.kind == "array":
        return "Array(String)"
    if column.kind == "json":
        # Nullable(JSON) is not supported; missing values are written as {}.
        return json_type
    if column.kind == "boolean":
        return "Nullable(Bool)" if column.nullable else "Bool"
    if column.kind in {"text", "varchar"}:
        return "Nullable(String)" if column.nullable else "String"
    raise ValueError(f"unsupported column kind: {column.kind}")


def _string_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _array_literal(value: Any) -> str:
    items = value or []
    literals = []
    for item in items:
        # Non-string elements (e.g. OSI expression dicts) are stored as JSON text,
        # mirroring the JSON shape other destinations keep in VARIANT columns.
        text = item if isinstance(item, str) else json.dumps(item)
        literals.append(_string_literal(text))
    return "[" + ", ".join(literals) + "]"


def _boolean_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "true" if value else "false"
