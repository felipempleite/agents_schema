# ClickHouse Setup

## Credentials

Set the `WAREHOUSE_CREDENTIALS` GitHub Actions secret (or environment variable
for local runs) to:

```yaml
type: clickhouse
host: abc123.region.clickhouse.cloud   # or your self-hosted host
port: 8443                             # optional; defaults to the driver default (8443 secure / 8123 plain)
user: agents_schema_bot                # optional; defaults to "default"
password: your-password
secure: true                           # optional; defaults to true — set false for plain HTTP
```

The connection uses the ClickHouse HTTP interface via
[clickhouse-connect](https://github.com/ClickHouse/clickhouse-connect). The
least-privilege setup is for an admin to create the `agents` database once and
grant the sync user rights only inside it:

```sql
CREATE DATABASE IF NOT EXISTS agents;
GRANT CREATE TABLE, DROP TABLE, TRUNCATE, SELECT, INSERT, ALTER DELETE ON agents.* TO agents_schema_bot;
```

(The writer also issues `CREATE DATABASE IF NOT EXISTS agents`, which is a
no-op once the database exists; grant `CREATE DATABASE` to the sync user only
if you want it to bootstrap the database itself.)

Grant read access broadly so agents can consume the metadata:

```sql
GRANT SELECT ON agents.* TO your_analyst_role;
```

## How the AGENTS schema maps to ClickHouse

ClickHouse has a two-level `database.table` namespace, so the `AGENTS` schema
is a ClickHouse **database** named `agents`. ClickHouse identifiers are
case-sensitive and the writer creates the package's canonical lowercase names:
query `agents.root`, not `AGENTS.ROOT`.

Destination-specific mapping:

| Spec concept | ClickHouse |
|---|---|
| `AGENTS` schema | `agents` database |
| `varchar` / `text` columns | `String` (`Nullable(String)` when nullable) |
| `boolean` columns | `Bool` |
| `array` columns | `Array(String)`; non-string elements are stored as JSON text |
| `json` columns | native `JSON` on 25.3+, `String` holding JSON text on older servers |
| `PRIMARY KEY` | MergeTree `ORDER BY` key (ClickHouse does not enforce uniqueness) |
| Table replacement | atomic `CREATE OR REPLACE TABLE` |
| `ROOT` upserts | scoped lightweight `DELETE` of incoming keys + `INSERT` |

Because ClickHouse does not enforce primary keys, row uniqueness is guaranteed
by the publish path (full table replacement per source family; delete-then-insert
for `agents.root`), not by the engine. Treat the tables as generated metadata,
not hand-edited state.

## Deployment notes

- **ClickHouse Cloud:** works out of the box; plain DDL is replicated
  automatically.
- **Self-hosted single node:** works out of the box on the default `Atomic`
  database engine.
- **Self-hosted replicated clusters:** create the `agents` database with the
  `Replicated` database engine before the first run so DDL and data replicate
  across nodes:

  ```sql
  CREATE DATABASE agents ENGINE = Replicated('/clickhouse/databases/agents', '{shard}', '{replica}');
  ```

- **Minimum version:** 24.8+ recommended (recursive CTEs for the lineage
  queries in [SPEC.md](SPEC.md)); the native `JSON` column type is used on
  25.3+ and falls back to `String` on older servers.

## Local smoke test

```bash
docker run -d --name ch -p 8123:8123 -e CLICKHOUSE_PASSWORD=dev clickhouse/clickhouse-server
export WAREHOUSE_CREDENTIALS='{"type":"clickhouse","host":"localhost","port":8123,"password":"dev","secure":false}'
agents-schema dbt --project-dir path/to/dbt/project
docker exec ch clickhouse-client --password dev --query "SELECT provider, key FROM agents.root"
```
