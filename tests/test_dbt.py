import json
import unittest

from agents_schema.dbt import DBT_COLUMN, _ingest

_MANIFEST = {
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "database": "ANALYTICS",
            "schema": "REVENUE",
            "config": {"materialized": "table"},
            "description": "One row per order.",
            "original_file_path": "models/orders.sql",
            "tags": ["revenue"],
            "columns": {
                "order_id": {
                    "data_type": "varchar",
                    "description": "Primary key.",
                    "config": {"meta": {"pii": False, "owner": "revenue-team"}},
                },
                "customer_email": {
                    "data_type": "varchar",
                    "description": "Billing email.",
                    "meta": {"pii": True},
                },
                "amount": {"data_type": "number", "description": "Order total."},
            },
            "depends_on": {"nodes": ["source.shop.raw.orders"]},
        }
    }
}


class FakeDestination:
    def __init__(self):
        self.calls = []

    def replace_table(self, table):
        self.calls.append(("replace", table.name))

    def insert_rows(self, table, rows):
        self.calls.append(("insert", table.name, list(rows)))

    def upsert_rows(self, table, rows):
        self.calls.append(("upsert", table.name, list(rows)))


class DbtColumnMetaTests(unittest.TestCase):
    def setUp(self):
        dest = FakeDestination()
        _ingest(dest, _MANIFEST)
        insert = next(c for c in dest.calls if c[0] == "insert" and c[1] == DBT_COLUMN.name)
        self.meta = {
            column_name: meta
            for _model_id, column_name, _data_type, _description, meta in insert[2]
        }

    def test_column_meta_serialized_from_config(self):
        self.assertEqual(
            json.loads(self.meta["order_id"]),
            {"pii": False, "owner": "revenue-team"},
        )

    def test_column_meta_serialized_from_top_level(self):
        self.assertEqual(json.loads(self.meta["customer_email"]), {"pii": True})

    def test_column_meta_defaults_to_empty_object(self):
        self.assertEqual(self.meta["amount"], "{}")


if __name__ == "__main__":
    unittest.main()
