# NBA Loaders: Operational Runbook

Reference for diagnosing and recovering from common operational issues.

---

## 1. Checking Loader Failures

When a loader exhausts all retries on an API call, it logs the failure to a JSON file in the working directory (wherever `main.py` was invoked from, typically `nba/`).

**File pattern**: `failed_attempts_<table_name>_<timestamp>.json`

```bash
# Find all failure logs
ls -lt nba/failed_attempts_*.json

# Inspect a failure log
python -c "
import json
with open('failed_attempts_raw_player_game_logs_20240501.json') as f:
    failures = json.load(f)
for i, f in enumerate(failures):
    print(f'--- Failure {i+1} ---')
    print('Function:', f.get('func'))
    print('Args:', f.get('args'))
    print('Error:', f.get('error'))
"
```

Each entry contains:
- `func`: the API function that failed
- `args` / `kwargs`: the arguments passed to it (e.g., player ID, season)
- `error`: the exception message after all retries were exhausted

**Retrying failed players manually** — identify the player IDs or seasons from the failure log, then call the loader directly:

```bash
cd nba
source venv/bin/activate

# Example: retry game logs for a specific player
python -c "
from loaders.game_logs import GameLogsLoader
loader = GameLogsLoader()
# Override to load only one player — inspect the loader's fetch_data()
# for how to pass player_id, then call the relevant API directly
loader.run()
"
```

For bulk retries, use `--resume` to skip already-loaded rows and `--limit-players` to isolate a small batch:

```bash
python main.py --skip-teams --skip-players --skip-player-info --skip-player-career \
  --skip-team-logs --skip-advanced-stats \
  --active-only --limit-players 10 --resume
```

---

## 2. Force-Reloading a Table

**Use case**: data quality issue, schema change, or a table needs a clean backfill.

The `--skip-*` flags let you run only the loaders you want. Every loader not skipped will run.

```bash
cd nba
source venv/bin/activate

# Reload ONLY raw_players (skip everything else)
python main.py \
  --skip-teams \
  --skip-player-info \
  --skip-player-career \
  --skip-game-logs \
  --skip-team-logs \
  --skip-advanced-stats

# Reload ONLY raw_teams
python main.py \
  --skip-players \
  --skip-player-info \
  --skip-player-career \
  --skip-game-logs \
  --skip-team-logs \
  --skip-advanced-stats
```

Available `--skip-*` flags:

| Flag | Loader skipped | Target table |
|------|---------------|--------------|
| `--skip-teams` | `load_teams` | `raw_teams` |
| `--skip-players` | `load_players` | `raw_players` |
| `--skip-player-info` | `load_player_info` | `raw_player_common_info` |
| `--skip-player-career` | `load_player_career` | `raw_player_career_stats` |
| `--skip-game-logs` | `load_game_logs` | `raw_player_game_logs` |
| `--skip-team-logs` | `load_team_game_logs` | `raw_team_game_logs` |
| `--skip-advanced-stats` | `load_player_advanced_season_stats` | `raw_player_advanced_season_stats` |

**Running a single loader directly** (fastest for targeted reloads):

```bash
cd nba
source venv/bin/activate

python -c "from loaders.players import load_players; load_players()"
python -c "from loaders.teams import load_teams; load_teams()"
python -c "from loaders.game_logs import load_game_logs; load_game_logs(start_season=2024, end_season=2025)"
python -c "from loaders.team_game_logs import load_team_game_logs; load_team_game_logs(start_season=2024, end_season=2025)"
python -c "from loaders.player_career import load_player_career; load_player_career()"
python -c "from loaders.player_info import load_player_info; load_player_info()"
```

Tables with `write_mode='truncate'` (`raw_teams`, `raw_players`, `raw_player_common_info`) DELETE all rows before inserting — a re-run is inherently a full reload. Tables with `write_mode='upsert'` merge on their upsert keys, so re-running is safe and idempotent.

---

## 3. Checking Data Freshness

**Via dbt** (recommended — evaluates against declared thresholds):

```bash
cd dbt_nba/nba_analytics
dbt source freshness
```

`raw_player_game_logs` has declared thresholds (in `models/staging/sources.yml`):
- **warn** if `MAX(loaded_at)` is more than 36 hours ago
- **error** if `MAX(loaded_at)` is more than 72 hours ago

A `warn` means the daily load may have been missed or delayed. An `error` means data is stale enough to materially affect downstream models — investigate immediately.

**Via BigQuery** (for any table, without dbt):

```sql
-- Check freshness on all key raw tables
SELECT
  'raw_player_game_logs'        AS table_name,
  MAX(loaded_at)                AS last_loaded_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(loaded_at), HOUR) AS hours_since_load
FROM `nba-analytics-499420.nba_raw.raw_player_game_logs`

UNION ALL

SELECT
  'raw_team_game_logs',
  MAX(loaded_at),
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(loaded_at), HOUR)
FROM `nba-analytics-499420.nba_raw.raw_team_game_logs`

UNION ALL

SELECT
  'raw_players',
  MAX(loaded_at),
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(loaded_at), HOUR)
FROM `nba-analytics-499420.nba_raw.raw_players`

UNION ALL

SELECT
  'raw_player_career_stats',
  MAX(loaded_at),
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(loaded_at), HOUR)
FROM `nba-analytics-499420.nba_raw.raw_player_career_stats`

ORDER BY hours_since_load DESC;
```

---

## 4. Re-Running a dbt Model After a Backfill

After a loader backfill completes, re-run affected dbt models to propagate the new data downstream.

**Always start with `int_player_game_advanced`** — it is the single source of truth. Everything else derives from it.

```bash
cd dbt_nba/nba_analytics

# Re-run int_player_game_advanced and all downstream models (the + suffix)
dbt run --select int_player_game_advanced+

# This executes in dependency order:
#   1. int_player_game_advanced
#   2. int_player_season_stats
#   3. int_player_rolling_stats
#   4. int_player_situational_splits
#   5. mart_player_production_dashboard
#   6. mart_player_game_log
```

If only staging data changed (e.g., a schema fix in `stg_player_game_logs`):

```bash
# Re-run staging + everything downstream
dbt run --select stg_player_game_logs+
```

**When to use `--full-refresh`**: if a model uses incremental materialization (builds on existing rows rather than rebuilding the full table), `dbt run` only processes new records. To rebuild the entire table from scratch:

```bash
dbt run --select int_player_game_advanced --full-refresh
# Then run downstream to pick up the full rebuild
dbt run --select int_player_season_stats int_player_rolling_stats int_player_situational_splits mart_player_production_dashboard mart_player_game_log
```

Use `--full-refresh` when:
- A backfill added historical rows that an incremental model would skip
- The model logic changed in a way that affects existing rows
- You suspect the incremental state is corrupted

After running, validate with tests:

```bash
dbt test --select int_player_game_advanced+
```

---

## 5. BigQuery Table Health Checks

Run these queries in the BigQuery console or via `bq query --nouse_legacy_sql`.

**Row counts across all raw tables:**

```sql
SELECT 'raw_player_game_logs' AS table_name, COUNT(*) AS row_count
FROM `nba-analytics-499420.nba_raw.raw_player_game_logs`
UNION ALL
SELECT 'raw_team_game_logs', COUNT(*)
FROM `nba-analytics-499420.nba_raw.raw_team_game_logs`
UNION ALL
SELECT 'raw_players', COUNT(*)
FROM `nba-analytics-499420.nba_raw.raw_players`
UNION ALL
SELECT 'raw_teams', COUNT(*)
FROM `nba-analytics-499420.nba_raw.raw_teams`
UNION ALL
SELECT 'raw_player_career_stats', COUNT(*)
FROM `nba-analytics-499420.nba_raw.raw_player_career_stats`
UNION ALL
SELECT 'raw_player_common_info', COUNT(*)
FROM `nba-analytics-499420.nba_raw.raw_player_common_info`
ORDER BY table_name;
```

**Duplicate upsert keys in `raw_player_game_logs`** (Player_ID + Game_ID + season_type must be unique):

```sql
SELECT
  Player_ID,
  Game_ID,
  season_type,
  COUNT(*) AS row_count
FROM `nba-analytics-499420.nba_raw.raw_player_game_logs`
GROUP BY Player_ID, Game_ID, season_type
HAVING COUNT(*) > 1
ORDER BY row_count DESC
LIMIT 20;
```

**Duplicate upsert keys in `raw_player_career_stats`** (PLAYER_ID + SEASON_ID):

```sql
SELECT
  PLAYER_ID,
  SEASON_ID,
  COUNT(*) AS row_count
FROM `nba-analytics-499420.nba_raw.raw_player_career_stats`
GROUP BY PLAYER_ID, SEASON_ID
HAVING COUNT(*) > 1
ORDER BY row_count DESC
LIMIT 20;
```

**NULL counts on critical columns in `raw_player_game_logs`:**

```sql
SELECT
  COUNTIF(Player_ID IS NULL)  AS null_player_id,
  COUNTIF(Game_ID IS NULL)    AS null_game_id,
  COUNTIF(SEASON_ID IS NULL)  AS null_season_id,
  COUNTIF(PTS IS NULL)        AS null_pts,
  COUNTIF(loaded_at IS NULL)  AS null_loaded_at,
  COUNT(*)                    AS total_rows
FROM `nba-analytics-499420.nba_raw.raw_player_game_logs`;
```

**Season coverage** (verify expected seasons are present):

```sql
SELECT
  SEASON_ID,
  season_type,
  COUNT(DISTINCT Player_ID) AS distinct_players,
  COUNT(*) AS game_log_rows
FROM `nba-analytics-499420.nba_raw.raw_player_game_logs`
GROUP BY SEASON_ID, season_type
ORDER BY SEASON_ID DESC, season_type;
```

---

## 6. Graceful Shutdown / Partial Data

When you interrupt a loader with Ctrl+C (SIGINT) or the process receives SIGTERM, the BaseLoader catches the signal and prints:

```
⚠️  Received SIGINT — saving partial progress before exiting...
   (Press Ctrl+C again to force quit immediately)

============================================================
Saving partial progress...
============================================================
Saving 4320 rows collected before shutdown...
  ✓ Inserted batch: 1000 rows
  ✓ Inserted batch: 1000 rows
  ✓ Inserted batch: 1000 rows
  ✓ Inserted batch: 320 rows
✓ Partial data saved
```

This means the rows fetched so far have been flushed to BigQuery. The loader did NOT complete — rows for the remaining players/seasons were not fetched.

**Verifying partial data was written:**

```sql
-- Check how many rows landed and what the latest loaded_at is
SELECT
  COUNT(*) AS rows_written,
  MAX(loaded_at) AS last_written_at,
  MIN(loaded_at) AS first_written_at
FROM `nba-analytics-499420.nba_raw.raw_player_game_logs`
WHERE DATE(loaded_at) = CURRENT_DATE();
```

**Completing the load after a partial run:**

For upsert-mode loaders (game logs, career stats), use `--resume` to skip players already loaded in this session. The loader compares player IDs already present in the table to skip them:

```bash
cd nba
source venv/bin/activate

# Resume game logs from where the interrupted run left off
python main.py \
  --skip-teams --skip-players --skip-player-info \
  --skip-player-career --skip-team-logs --skip-advanced-stats \
  --resume \
  --start-season 2023 --end-season 2025
```

For truncate-mode loaders (`raw_players`, `raw_teams`, `raw_player_common_info`), a partial run may have left an incomplete table because truncate DELETE+INSERT is not transactional across batches. Re-run the loader without `--resume` to do a full reload:

```bash
python -c "from loaders.players import load_players; load_players()"
```
