# Adding a New Data Loader

Step-by-step guide for adding a new source to the NBA ingestion pipeline. Uses `raw_player_awards` as the working example throughout.

---

## 1. Create the Loader File

Create `nba/loaders/player_awards.py`. All loaders inherit from `BaseLoader` and must implement `fetch_data()` and `get_create_table_ddl()`.

```python
# nba/loaders/player_awards.py

from nba_api.stats.endpoints import playerawards  # replace with actual endpoint
from loaders.base import BaseLoader
from config import BQ_PROJECT, BQ_DATASET


class PlayerAwardsLoader(BaseLoader):

    def __init__(self, player_ids: list[int] | None = None):
        super().__init__()
        self.table_name  = "raw_player_awards"   # must start with raw_
        self.write_mode  = "upsert"              # upsert deduplicates on upsert_keys
        self.upsert_keys = ["PLAYER_ID", "DESCRIPTION", "SEASON"]
        self.player_ids  = player_ids or []

    def get_create_table_ddl(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS `{BQ_PROJECT}.{BQ_DATASET}.{self.table_name}` (
            PLAYER_ID     INT64   NOT NULL,
            DESCRIPTION   STRING  NOT NULL,
            SEASON        STRING,
            TEAM          STRING,
            loaded_at     TIMESTAMP
        )
        """

    def fetch_data(self) -> list:
        all_rows = []

        for player_id in self.player_ids:
            if self._shutdown_requested:
                break

            # api_call handles: 1s rate limit + 1-3s jitter, 5 retries,
            # exponential backoff (2^n seconds), timeout=15s
            result = self.api_call(
                playerawards.PlayerAwards,
                player_id=player_id
            )

            if result is None:
                continue

            # nba_api returns objects with .get_data_frames()
            df = result.get_data_frames()[0]

            for _, row in df.iterrows():
                clean_row = {
                    col: self._clean_value(val)   # converts NaN/inf → None
                    for col, val in row.items()
                }
                all_rows.append(clean_row)

        # stamp_loaded_at is called automatically by _write_to_bigquery /
        # _upsert_to_bigquery — no need to call it here unless you want
        # to inspect the timestamp before writing.
        return all_rows


def load_player_awards(player_ids: list[int] | None = None):
    """Entry point used by main.py and direct invocation."""
    PlayerAwardsLoader(player_ids=player_ids).run()
```

**Key BaseLoader behaviors to know:**

- `self.api_call(func, *args, **kwargs)` — wraps any callable with rate limiting (1s sleep + 1-3s random jitter before every call), a 15s timeout injected automatically if the function accepts it, and exponential backoff on failure (`2^attempt` seconds, up to 5 retries). Returns `None` if all retries fail.
- `self._clean_value(val)` — converts `float('nan')`, `float('inf')`, and the string `"nan"` to `None`. Always apply this before returning rows to avoid BigQuery insert errors.
- `self._shutdown_requested` — set to `True` on SIGINT/SIGTERM. Check it in long loops so the loader can exit cleanly.
- `stamp_loaded_at(rows)` — adds a UTC ISO timestamp to the `loaded_at` field. Called automatically inside `_write_to_bigquery` and `_upsert_to_bigquery`.

**How upsert works** (when `write_mode='upsert'`):

`run()` calls `_upsert_to_bigquery(rows)`, which:

1. Loads rows into a temp table (`raw_player_awards_temp`) in batches of 1000.
2. Deduplicates the temp table using `ROW_NUMBER() OVER (PARTITION BY <upsert_keys> ORDER BY loaded_at DESC)`, keeping the latest row per key.
3. Runs a MERGE statement against the main table:
   ```sql
   MERGE `nba-analytics-499420.nba_raw.raw_player_awards` t
   USING `nba-analytics-499420.nba_raw.raw_player_awards_temp` s
   ON t.PLAYER_ID = s.PLAYER_ID AND t.DESCRIPTION = s.DESCRIPTION AND t.SEASON = s.SEASON
   WHEN MATCHED THEN
       UPDATE SET t.TEAM = s.TEAM, t.loaded_at = s.loaded_at
   WHEN NOT MATCHED THEN
       INSERT (PLAYER_ID, DESCRIPTION, SEASON, TEAM, loaded_at)
       VALUES (s.PLAYER_ID, s.DESCRIPTION, s.SEASON, s.TEAM, s.loaded_at)
   ```
4. Drops the temp table.

---

## 2. Register in `__init__.py`

Edit `nba/loaders/__init__.py` to export the new loader function:

```python
# Add this import
from loaders.player_awards import load_player_awards

# Add to __all__
__all__ = [
    "load_teams",
    "load_players",
    "load_player_info",
    "load_player_career",
    "load_game_logs",
    "load_team_game_logs",
    "load_player_advanced_season_stats",
    "load_player_awards",          # <-- add this line
]
```

---

## 3. Add to `main.py`

Three edits in `nba/main.py`:

**a) Import at the top:**

```python
from loaders import (
    load_teams,
    load_players,
    load_player_info,
    load_player_career,
    load_game_logs,
    load_team_game_logs,
    load_player_advanced_season_stats,
    load_player_awards,             # <-- add this
)
```

**b) Add the `--skip-*` argparse flag** inside `main()`, with the other `add_argument` calls:

```python
parser.add_argument("--skip-player-awards", action="store_true")
```

**c) Add the conditional call** inside `main()`, after the other loader calls:

```python
if not args.skip_player_awards:
    load_player_awards(player_ids=some_player_id_list)
```

---

## 4. Test Locally

**Run just the new loader:**

```bash
cd nba
source venv/bin/activate

python -c "from loaders.player_awards import load_player_awards; load_player_awards(player_ids=[2544, 201939])"
```

Watch the output for:
- `✓ Table 'raw_player_awards' is ready` — DDL executed without error
- `✓ Fetched N rows` — API returned data
- `✓ Upserted N rows into raw_player_awards` — BigQuery write succeeded

**Check the table in BigQuery:**

```bash
# Preview rows
bq query --nouse_legacy_sql \
  "SELECT * FROM \`nba-analytics-499420.nba_raw.raw_player_awards\` LIMIT 5"

# Row count
bq query --nouse_legacy_sql \
  "SELECT COUNT(*) AS row_count FROM \`nba-analytics-499420.nba_raw.raw_player_awards\`"

# Check for NULLs on required columns
bq query --nouse_legacy_sql "
SELECT
  COUNTIF(PLAYER_ID IS NULL)   AS null_player_id,
  COUNTIF(DESCRIPTION IS NULL) AS null_description,
  COUNTIF(loaded_at IS NULL)   AS null_loaded_at,
  COUNT(*)                     AS total_rows
FROM \`nba-analytics-499420.nba_raw.raw_player_awards\`
"

# Verify upsert keys are unique
bq query --nouse_legacy_sql "
SELECT PLAYER_ID, DESCRIPTION, SEASON, COUNT(*) AS cnt
FROM \`nba-analytics-499420.nba_raw.raw_player_awards\`
GROUP BY PLAYER_ID, DESCRIPTION, SEASON
HAVING COUNT(*) > 1
"
```

---

## 5. Add the Staging dbt Model

**a) Create `dbt_nba/nba_analytics/models/staging/stg_player_awards.sql`:**

```sql
{{ config(materialized='view') }}

with source as (
    select * from {{ source('nba_raw', 'raw_player_awards') }}
),

renamed as (
    select
        cast(player_id as int64)     as player_id,
        cast(description as string)  as award_description,
        cast(season as string)       as season_id,
        cast(team as string)         as team_name,
        cast(loaded_at as timestamp) as loaded_at
    from source
)

select * from renamed
```

Naming rules:
- Source reference uses the raw table name: `{{ source('nba_raw', 'raw_player_awards') }}`
- Rename columns to snake_case
- Cast every column to an explicit BigQuery type — do not rely on type inference
- Always pass `loaded_at` through

**b) Add the source table to `models/staging/sources.yml`** under the `nba_raw` source:

```yaml
      - name: raw_player_awards
        description: Player award history from the NBA API commonplayerinfo awards endpoint.
        columns:
          - name: PLAYER_ID
            description: Player identifier.
            tests:
              - not_null
          - name: DESCRIPTION
            description: Award name or description.
          - name: SEASON
            description: Season the award was won.
          - name: TEAM
            description: Team at the time of the award.
          - name: loaded_at
            description: Timestamp when the row was written to BigQuery.
```

**c) Add the model to `models/staging/schema.yml`:**

```yaml
  - name: stg_player_awards
    description: Cleaned player award history — one row per player per award per season.
    columns:
      - name: player_id
        description: player identifier
        tests:
          - not_null
      - name: award_description
        description: name or description of the award
        tests:
          - not_null
      - name: season_id
        description: season the award was won (e.g. '2023-24')
      - name: team_name
        description: team at the time of the award
      - name: loaded_at
        description: utc timestamp when the row was written to bigquery
```

---

## 6. Run and Test dbt

```bash
cd dbt_nba/nba_analytics

# Run just the new staging model
dbt run --select stg_player_awards

# Run tests defined in schema.yml
dbt test --select stg_player_awards

# Verify the source is fresh (checks loaded_at thresholds)
dbt source freshness

# If this model feeds downstream models, run them too
dbt run --select stg_player_awards+
```

Expected output for `dbt run`:
```
Running with dbt=1.x.x
Found 1 model, 0 tests, 0 snapshots, 0 analyses, ...

Concurrency: 4 threads ...

1 of 1 START sql view model nba_staging.stg_player_awards .......... [RUN]
1 of 1 OK created sql view model nba_staging.stg_player_awards ..... [CREATE VIEW in 1.23s]

Finished running 1 view model in 0 hours 0 minutes and 2.xx seconds.

Completed successfully.
```

---

## 7. Common Mistakes

**Table name doesn't start with `raw_`**

The `nba_raw` BigQuery schema convention requires all loader-written tables to use the `raw_` prefix. Staging models that reference them via `{{ source('nba_raw', 'raw_player_awards') }}` will fail if the table name doesn't match exactly.

**Forgot `loaded_at`**

`loaded_at` is stamped automatically by `stamp_loaded_at()` inside `_write_to_bigquery` and `_upsert_to_bigquery`, but the column must exist in the DDL. If `get_create_table_ddl()` doesn't declare `loaded_at TIMESTAMP`, BigQuery will reject the insert.

```python
# Wrong — missing loaded_at from DDL
CREATE TABLE IF NOT EXISTS ... (
    PLAYER_ID INT64,
    DESCRIPTION STRING
)

# Correct
CREATE TABLE IF NOT EXISTS ... (
    PLAYER_ID   INT64  NOT NULL,
    DESCRIPTION STRING NOT NULL,
    SEASON      STRING,
    TEAM        STRING,
    loaded_at   TIMESTAMP
)
```

**upsert_keys not matching DDL column casing**

The MERGE statement uses `self.upsert_keys` to build the `ON` clause literally. Column names in `upsert_keys` must exactly match the column names in the DDL (and in the data returned by `fetch_data()`). A mismatch causes a silent mismatch or BigQuery column-not-found error.

```python
# Wrong — key casing doesn't match DDL column name 'PLAYER_ID'
self.upsert_keys = ["player_id", "description"]

# Correct — matches DDL
self.upsert_keys = ["PLAYER_ID", "DESCRIPTION", "SEASON"]
```

**NaN values not cleaned before insert**

Python `float('nan')` is not valid JSON and causes BigQuery insert errors. Always apply `self._clean_value(val)` to every value when building row dicts:

```python
# Wrong — NaN from pandas will break the insert
for _, row in df.iterrows():
    all_rows.append(dict(row))

# Correct
for _, row in df.iterrows():
    all_rows.append({col: self._clean_value(val) for col, val in row.items()})
```

**Using `write_mode='append'` without `upsert_keys`**

`append` mode with no `upsert_keys` does a plain INSERT with no deduplication. Re-running the loader will create duplicate rows. If the data is uniquely keyed (almost always true), set `write_mode='upsert'` and declare the keys.

```python
# Duplicates on every re-run
self.write_mode  = "append"
self.upsert_keys = []

# Idempotent
self.write_mode  = "upsert"
self.upsert_keys = ["PLAYER_ID", "DESCRIPTION", "SEASON"]
```

**Referencing a staging model from another staging model**

Staging models must only reference `{{ source(...) }}` — never `{{ ref(...) }}` to another staging model. Cross-staging references indicate the logic belongs in the intermediate layer.