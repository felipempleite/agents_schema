import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_schema import cli, skills
from agents_schema.config import ConfigError
from agents_schema.skills import SkillFile, _load_skill_files, _parse_uses_frontmatter


class SkillsTests(unittest.TestCase):
    def test_load_skill_files_discovers_markdown_recursively_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / "revenue" / "arr.md"
            skill_path.parent.mkdir()
            content = (
                "---\n"
                "uses:\n"
                "  schemas:\n"
                "    - QUICKSTART_FINANCE\n"
                "  tables:\n"
                "    - QUICKSTART_FINANCE.ARR_SNAPSHOT\n"
                "---\n"
                "# ARR\n"
            )
            skill_path.write_text(content)

            skills = _load_skill_files(root)

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].key, "skill/revenue/arr")
        self.assertEqual(skills[0].content, content)
        self.assertEqual(
            skills[0].uses,
            (("schema", "QUICKSTART_FINANCE"), ("table", "QUICKSTART_FINANCE.ARR_SNAPSHOT")),
        )
        self.assertEqual(skills[0].warnings, ())

    def test_load_skill_files_rejects_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FileNotFoundError, "no \\*.md skill files"):
                _load_skill_files(Path(tmp))

    def test_parse_uses_frontmatter_accepts_missing_frontmatter(self):
        uses, warnings = _parse_uses_frontmatter("# Revenue\n")

        self.assertEqual(uses, ())
        self.assertEqual(warnings, ())

    def test_parse_uses_frontmatter_warns_for_malformed_uses(self):
        content = "---\nuses:\n  tables: FINANCE.ARR_SNAPSHOT\n---\n# Revenue\n"

        uses, warnings = _parse_uses_frontmatter(content)

        self.assertEqual(uses, ())
        self.assertEqual(warnings, ("uses.tables must be a list of strings",))

    def test_parse_uses_frontmatter_warns_for_unqualified_tables(self):
        content = "---\nuses:\n  tables:\n    - ARR_SNAPSHOT\n---\n# Revenue\n"

        uses, warnings = _parse_uses_frontmatter(content)

        self.assertEqual(uses, ())
        self.assertEqual(warnings, ("uses.tables entries must be schema-qualified",))

    def test_cli_dispatches_skills_with_provider(self):
        with (
            patch("agents_schema.cli.warehouse_type_from_env", return_value="snowflake"),
            patch("agents_schema.cli.skills.run") as run,
        ):
            result = cli.main(["skills", "--skills-dir", "skills", "--provider", "fivetran"])

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            {
                "warehouse": {"type": "snowflake"},
                "metadata_connection": {
                    "type": "skills",
                    "path": "skills",
                    "provider": "fivetran",
                },
            }
        )

    def test_cli_defaults_skills_provider_to_user(self):
        with (
            patch("agents_schema.cli.warehouse_type_from_env", return_value="snowflake"),
            patch("agents_schema.cli.skills.run") as run,
        ):
            result = cli.main(["skills", "--skills-dir", "skills"])

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            {
                "warehouse": {"type": "snowflake"},
                "metadata_connection": {
                    "type": "skills",
                    "path": "skills",
                    "provider": "user",
                },
            }
        )

    def test_cli_dispatches_snowflake_semantic_views(self):
        with (
            patch("agents_schema.cli.warehouse_type_from_env", return_value="snowflake"),
            patch("agents_schema.cli.snowflake_semantic.run") as run,
        ):
            result = cli.main(
                [
                    "snowflake-semantic",
                    "--semantic-view",
                    "ANALYTICS.FINANCE.REVENUE",
                    "--semantic-view",
                    "ANALYTICS.SALES.PIPELINE",
                ]
            )

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            {
                "warehouse": {"type": "snowflake"},
                "metadata_connection": {
                    "type": "snowflake-semantic",
                    "semantic_views": ["ANALYTICS.FINANCE.REVENUE", "ANALYTICS.SALES.PIPELINE"],
                },
            }
        )


class _FakeWarehouse:
    """In-memory stand-in for a Destination, faithful enough to exercise reconcile_rows'
    upsert-then-scoped-delete semantics across two separate `run()` calls."""

    def __init__(self):
        self.tables = {}

    def _table(self, table):
        return self.tables.setdefault(table.name, {})

    def _primary_key(self, table, row):
        indexes = [i for i, column in enumerate(table.columns) if column.name in table.primary_key]
        return tuple(row[i] for i in indexes)

    def replace_table(self, table):
        self.tables[table.name] = {}

    def upsert_rows(self, table, rows):
        store = self._table(table)
        for row in rows:
            store[self._primary_key(table, row)] = row

    def insert_rows(self, table, rows):
        self.upsert_rows(table, rows)

    def delete_rows(self, table, key_columns, rows):
        store = self._table(table)
        key_indexes = [i for i, column in enumerate(table.columns) if column.name in key_columns]
        keys_to_delete = {tuple(row[i] for i in key_indexes) for row in rows}
        for pk, existing_row in list(store.items()):
            if tuple(existing_row[i] for i in key_indexes) in keys_to_delete:
                del store[pk]

    def reconcile_rows(self, table, rows, scope=None):
        rows = list(rows)
        self.upsert_rows(table, rows)
        store = self._table(table)
        keep = {self._primary_key(table, row) for row in rows}
        if scope is None:
            stale = [pk for pk in store if pk not in keep]
        else:
            column, value = scope
            column_index = next(i for i, c in enumerate(table.columns) if c.name == column)
            stale = [pk for pk, row in store.items() if row[column_index] == value and pk not in keep]
        for pk in stale:
            del store[pk]


class _DestinationContext:
    def __init__(self, dest):
        self.dest = dest

    def __enter__(self):
        return self.dest

    def __exit__(self, exc_type, exc, tb):
        return None


class SkillsRootReconcileTests(unittest.TestCase):
    def test_removing_a_skill_file_deletes_its_stale_root_row_on_next_run(self):
        warehouse = _FakeWarehouse()
        cfg = {
            "warehouse": {"type": "snowflake"},
            "metadata_connection": {"path": ".", "provider": "fivetran"},
        }
        revenue = SkillFile(key="skill/revenue", content="# Revenue\n", uses=())
        support = SkillFile(key="skill/support", content="# Support\n", uses=())

        def run_with(current_skills):
            with (
                patch("agents_schema.skills.open_destination", return_value=_DestinationContext(warehouse)),
                patch("agents_schema.skills._load_skill_files", return_value=current_skills),
                patch("builtins.print"),
                patch("agents_schema.skills.publish_builtin_skill"),
            ):
                skills.run(cfg)

        run_with([revenue, support])
        root = warehouse.tables["agents.root"]
        self.assertIn(("fivetran", "skill/revenue"), root)
        self.assertIn(("fivetran", "skill/support"), root)

        run_with([revenue])
        root = warehouse.tables["agents.root"]
        self.assertIn(("fivetran", "skill/revenue"), root)
        self.assertNotIn(("fivetran", "skill/support"), root)

    def test_removing_a_skill_file_does_not_touch_other_providers_root_rows(self):
        warehouse = _FakeWarehouse()
        warehouse.upsert_rows(
            skills.ROOT,
            [("dbt", "overview", "# dbt\n")],
        )
        cfg = {
            "warehouse": {"type": "snowflake"},
            "metadata_connection": {"path": ".", "provider": "fivetran"},
        }

        with (
            patch("agents_schema.skills.open_destination", return_value=_DestinationContext(warehouse)),
            patch("agents_schema.skills._load_skill_files", return_value=[]),
            patch("builtins.print"),
            patch("agents_schema.skills.publish_builtin_skill"),
        ):
            skills.run(cfg)

        self.assertIn(("dbt", "overview"), warehouse.tables["agents.root"])

    def test_provider_named_skills_is_rejected(self):
        cfg = {
            "warehouse": {"type": "snowflake"},
            "metadata_connection": {"path": ".", "provider": "skills"},
        }

        with self.assertRaisesRegex(ConfigError, "reserved"):
            skills.run(cfg)


if __name__ == "__main__":
    unittest.main()
