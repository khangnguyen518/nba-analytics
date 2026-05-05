# sports_data: NBA Analytics Project

Two-repo pipeline for ingesting NBA data via APIs and transforming it into analytics-ready tables.

## Project Structure

```
sports_data/
├── nba/                    ← Python loaders (this repo)
│   ├── loaders/            ← Data ingestion code
│   ├── config.py           ← Environment & API settings
│   ├── db.py               ← BigQuery utilities
│   ├── main.py             ← Entry point
│   └── .claude/agents/     ← Shared across both repos (symlink in dbt_nba)
│
└── dbt_nba/
    └── nba_analytics/      ← dbt project
        ├── models/
        │   ├── staging/    ← Views: raw → cleaned
        │   ├── intermediate/ ← Tables: joins, calcs
        │   └── marts/      ← Tables: final analytics
        ├── tests/          ← dbt tests (inline in .yml)
        └── macros/         ← dbt macros
```

**Data flow**: APIs → Python loaders → `nba_raw` schema (BigQuery) → dbt staging → intermediate → marts

---

## BigQuery Setup

**Project**: `nba-analytics-499420`

**Schemas**:
- `nba_raw` — raw tables written by loaders. DO NOT TRANSFORM HERE. Tables start with `raw_` prefix.
- `staging` — dbt views (1:1 cleaning of raw tables). Prefix: `stg_`.
- `intermediate` — dbt tables (joins, window functions, deduplication). Prefix: `int_`.
- `marts` — dbt tables (final aggregates, dashboards). Prefix: `mart_`.

**Materialization rules** (from `dbt_project.yml`):
- Staging → `view` (lightweight, fast to refresh)
- Intermediate & Marts → `table` (can be slow to recompute, benefits from caching)

---

## Python Loaders (nba/)

### File Structure

```
loaders/
├── __init__.py          ← __all__ = [list of loader classes]
├── base.py              ← BaseLoader abstract class
├── players.py           ← PlayersLoader
├── teams.py             ← TeamsLoader
├── game_logs.py         ← GameLogsLoader
├── player_game_logs.py  ← PlayerGameLogsLoader
└── ...
```

### Loader Pattern

All loaders inherit from `BaseLoader` (in `loaders/base.py`). Minimal example:

```python
from loaders.base import BaseLoader

class MySourceLoader(BaseLoader):
    def __init__(self):
        super().__init__()
        self.table_name = "raw_my_source"  # Must start with raw_
        self.write_mode = "append"  # or "truncate"
        self.upsert_keys = ["id"]  # For MERGE logic (append mode)

    def get_create_table_ddl(self) -> str:
        """Define schema (type hints, nullability)."""
        return f"""
        CREATE TABLE IF NOT EXISTS `{self.dataset}.{self.table_name}` (
            id          INT64 NOT NULL,
            name        STRING,
            loaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
        )
        """

    def fetch_data(self) -> list:
        """Return list of dicts. BaseLoader handles BigQuery writes."""
        # Use self.api_call() for retries + rate limiting
        data = self.api_call(some_api_func, arg1, arg2)
        return data or []

def load_my_source():
    """Entry point for orchestration."""
    MySourceLoader().run()
```

### Key BaseLoader Methods

- `api_call(func, *args, **kwargs)` — Wraps API calls with retry logic, rate limiting (1s default), timeout (15s default), and jitter.
- `stamp_loaded_at(rows)` — Adds `loaded_at` timestamp to each row (UTC ISO format).
- `_clean_value(val)` — Converts NaN/inf to None for BigQuery.
- `run()` — Main orchestration: fetch → stamp → write → log.

### Configuration (config.py)

```python
BQ_PROJECT = "nba-analytics-499420"  # From env
BQ_DATASET = os.getenv("BQ_DATASET", "nba_raw")
BQ_KEYFILE = "bigquery-keyfile.json"

API_RATE_LIMIT = 1      # seconds between calls
API_TIMEOUT = 15        # request timeout
API_MAX_RETRIES = 5     # before giving up

BATCH_SIZE = 1000       # rows per BigQuery insert
VERBOSE = True          # print logs
```

### Write Modes

- **`append`**: INSERT rows. For upserts, set `upsert_keys = ["id"]` and BaseLoader uses MERGE (dedup on those keys).
- **`truncate`**: DELETE then INSERT (for small, reference tables like teams/players).

### Schema Naming Conventions

- Table name: `raw_<source_name>` (e.g., `raw_players`, `raw_game_logs`)
- Column names: UPPERCASE if from API as-is, snake_case if cleaned by the loader. **Staging layer converts to snake_case.**
- Always include `loaded_at TIMESTAMP` (set by `stamp_loaded_at`).

### Error Handling

- Failed API calls logged to `failed_attempts_*.json` (see `.gitignore`).
- Graceful shutdown on SIGINT/SIGTERM: saves partial data in-flight before exiting.
- Retry logic with exponential backoff: 2^attempt seconds, up to 5 tries.

---

## dbt Models (dbt_nba/nba_analytics/)

### Layer Structure

**Staging** (`models/staging/stg_*.sql`, materialized as `view`)
- 1:1 transformation of raw tables.
- Rename columns to snake_case and recast types.
- Light cleaning: handle NULLs, parse dates.
- Pattern:
  ```sql
  {{ config(materialized='view') }}
  
  with source as (
      select * from {{ source('nba_raw', 'raw_players') }}
  ),
  
  renamed as (
      select
          cast(id as int64) as player_id,
          cast(full_name as string) as player_name,
          cast(loaded_at as timestamp) as loaded_at
      from source
  )
  
  select * from renamed
  ```

**Intermediate** (`models/intermediate/int_*.sql`, materialized as `table`)
- Join, denormalize, compute metrics.
- Complex transforms (window functions, rolling stats, dedupe).
- Always include a grain column (e.g., `player_id`, `game_id`).
- Example: `int_player_rolling_stats` (computes 10-game averages).

**Marts** (`models/marts/mart_*.sql`, materialized as `table`)
- Final aggregates for dashboards/BI.
- Named by consumer: `mart_<dashboard_name>` or `mart_<consumer_use_case>`.
- Example: `mart_player_production_dashboard` (joins player stats with team info).

### Naming Conventions

| Layer | Prefix | Materialization | Example |
|-------|--------|---|---|
| Staging | `stg_` | view | `stg_player_game_logs` |
| Intermediate | `int_` | table | `int_player_season_stats` |
| Marts | `mart_` | table | `mart_player_production_dashboard` |

**Source names** (in `sources.yml`): `{{ source('nba_raw', 'raw_players') }}`

### dbt Project Config

Located in `dbt_nba/nba_analytics/dbt_project.yml`:
- Project name: `nba_analytics`
- Profile: `nba_analytics` (defined in `~/.dbt/profiles.yml`)
- Model paths: `models/`
- Materialization defaults by layer (see above)

### Testing Patterns

Tests are defined inline in `models/staging/schema.yml` (dbt semantics):

```yaml
columns:
  - name: player_id
    tests:
      - unique
      - not_null
  - name: player_name
    tests:
      - not_null
```

**Run tests**:
```bash
cd dbt_nba/nba_analytics
dbt test
```

**Key test sources** (`sources.yml`):
- All raw tables listed with column descriptions.
- Source `nba_raw` points to `nba-analytics-499420.nba_raw`.
- Freshness checks on `raw_player_game_logs` (warn >36h, error >72h).

---

## Source Data: nba_raw Tables

All defined in `dbt_nba/nba_analytics/models/staging/sources.yml`. Key ones:

| Table | Loader | Grain | Key Column | Refresh |
|-------|--------|-------|-----------|---------|
| `raw_players` | PlayersLoader | One row per player | `id` | Truncate (daily) |
| `raw_teams` | TeamsLoader | One row per team | `id` | Truncate (daily) |
| `raw_player_game_logs` | PlayerGameLogsLoader | One row per player-game | `Player_ID`, `Game_ID` | Incremental (hourly) |
| `raw_team_game_logs` | TeamGameLogsLoader | One row per team-game | `Game_ID`, `Team_ID` | Incremental (hourly) |
| `raw_player_career_stats` | PlayerCareerStatsLoader | One row per player-season | `PLAYER_ID`, `SEASON_ID` | Incremental (daily) |
| `raw_player_common_info` | PlayerInfoLoader | One row per player | `PERSON_ID` | Truncate (daily) |

---

## Development Workflows

### Adding a New Loader

1. Create `loaders/my_source.py` inheriting from `BaseLoader`.
2. Implement `fetch_data()` and `get_create_table_ddl()`.
3. Add entry to `loaders/__init__.py`.
4. Add to `main.py` or orchestration tool (Airflow, etc.).
5. Test: `python -c "from loaders.my_source import load_my_source; load_my_source()"`
6. Check BigQuery: `SELECT * FROM nba_raw.raw_my_source LIMIT 10;`

### Adding a New dbt Model

1. Create `models/staging/stg_my_table.sql` (if transforming a raw table).
2. Add source definition to `models/staging/sources.yml`.
3. Add tests (unique, not_null) to the source columns.
4. Run `dbt run --select stg_my_table`.
5. Run `dbt test --select stg_my_table`.

### Running dbt Commands

```bash
cd dbt_nba/nba_analytics

# Parse and compile models
dbt parse
dbt compile

# Run all models
dbt run

# Run specific model
dbt run --select stg_players

# Run tests
dbt test

# Generate docs
dbt docs generate
```

---

## Common Patterns

### Late-Arriving Facts

Raw tables may receive historical data after initial load (e.g., a game box score from 2 weeks ago).

- **Loaders**: Use `upsert_keys = ["id"]` to handle MERGE (dedup + update).
- **dbt**: Use `loaded_at` to track freshness; alert on late-arriving data.

### Incremental Loaders

For high-volume tables like `raw_player_game_logs`:

1. Loaders track a cursor (e.g., last `loaded_at` timestamp).
2. Fetch only new rows: `fetch_games_since(last_cursor)`.
3. Write in append mode with upsert keys to handle reruns.

### Slowly Changing Dimensions (SCDs)

Players change teams; handle with:

- **Type 1** (overwrite): `raw_player_common_info` (current team only).
- **Type 2** (history): Not yet implemented; intermediate layer can add `valid_from/valid_to` if needed.

---

## Project-Level References

- **BigQuery Dataset**: `nba_raw` (raw) → `staging` → `intermediate` → `marts`
- **dbt Profiles**: `~/.dbt/profiles.yml` (contact maintainer for setup)
- **Python Env**: `nba/venv/` (use `source venv/bin/activate`)
- **Secrets**: `.env` file (BQ keyfile, API credentials) — in `.gitignore`

---

## For Claude Code Agents

These conventions are inherited by all 5 sub-agents (in `.claude/agents/`). Agents should:

1. **Before writing code**: Read existing loaders or models in the same layer.
2. **Name everything consistently**: `raw_`, `stg_`, `int_`, `mart_` prefixes.
3. **Always include `loaded_at`** in raw tables (via `stamp_loaded_at`).
4. **Test incrementally**: Loaders can be run locally; dbt models can be `dbt run --select`.
5. **Document data contracts** in `sources.yml` (column names, types, tests).

Use `dbt-design-consultant` to decide grain/grain conflicts. Use `data-loader-developer` to implement. Use `test-creator` to scaffold pytest/dbt tests. Use `documentation-writer` for runbooks. Use `dbt-requirements-clarifier` to scope new models.
