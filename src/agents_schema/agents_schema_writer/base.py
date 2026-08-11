from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable

from .schema import TableSchema


class AgentsSchemaWriter(ABC):
    @abstractmethod
    def ensure_table(self, table: TableSchema) -> None: ...

    @abstractmethod
    def replace_table(self, table: TableSchema) -> None: ...

    @abstractmethod
    def upsert_rows(self, table: TableSchema, rows: Iterable[tuple[Any, ...]]) -> None: ...

    @abstractmethod
    def insert_rows(self, table: TableSchema, rows: Iterable[tuple[Any, ...]]) -> None: ...

    @abstractmethod
    def delete_rows(
        self,
        table: TableSchema,
        key_columns: tuple[str, ...],
        rows: Iterable[tuple[Any, ...]],
    ) -> None: ...

    @abstractmethod
    def reconcile_rows(
        self,
        table: TableSchema,
        rows: Iterable[tuple[Any, ...]],
        scope: tuple[str, Any] | None = None,
    ) -> None:
        """Make the table (or a slice of it) match exactly what's passed in.

        Without `scope`, this reconciles the whole table: upsert rows, then
        delete anything absent from them. Only use this on a table one caller
        owns exclusively.

        With `scope` as a (column, value) pair, this instead deletes every
        existing row matching that column value, then inserts `rows` fresh —
        a full replace of just that slice, not a diff. This is how a caller
        can safely reconcile its own slice of a table shared across multiple
        providers (e.g. AGENTS.ROOT: one provider's rows are replaced, every
        other provider's rows are untouched since they don't match `scope`).
        """

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "AgentsSchemaWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
