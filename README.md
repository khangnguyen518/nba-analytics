# NBA Analytics: Data Loaders

Python ingestion layer for the NBA Analytics project. Fetches data from the NBA API and writes it to BigQuery, feeding a downstream dbt transformation pipeline.

## Overview

This repository contains Python loaders that:
- Fetch data from the [NBA Stats API](https://stats.nba.com/)
- Handle retries, rate limiting, and graceful shutdown
- Write raw data to BigQuery (`nba_raw` schema)
- Track load timestamps and handle upserts for incremental data

The data is then transformed by [dbt](https://www.getdbt.com/) in the [`dbt_nba`](../dbt_nba) repository.

**Data flow**: NBA API → Python loaders → BigQuery `nba_raw` → dbt staging/intermediate/marts

## Quick Start

### Prerequisites

- Python 3.10+
- BigQuery credentials (service account JSON)
- Environment variables configured (see below)

### Setup

```bash
# Clone and navigate
git clone <repo-url>
cd nba/

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your BigQuery project, dataset, keyfile path
```

### Environment Variables

```bash
BQ_PROJECT=your-project-id
BQ_DATASET=nba_raw                    # or custom name
BQ_KEYFILE=/path/to/keyfile.json
VERBOSE=True                          # Optional
```

### Run a Loader

```bash
# Load all data
python main.py

# Or load specific tables
python -c "from loaders.players import load_players; load_players()"
python -c "from loaders.game_logs import load_game_logs; load_game_logs()"
```

## Project Structure

```
nba/
├── loaders/
│   ├── base.py                    # BaseLoader abstract class
│   ├── players.py                 # Player master data
│   ├── teams.py                   # Team reference data
│   ├── game_logs.py               # Game-by-game player box scores
│   ├── team_game_logs.py          # Game-by-game team box scores
│   └── ...                        # Other data sources
├── main.py                        # Orchestration entry point
├── config.py                      # Configuration (from env)
├── db.py                          # BigQuery utilities
├── requirements.txt               # Python dependencies
└── CLAUDE.md                      # Detailed conventions (for Claude Code)
```

## Loader Pattern

All loaders inherit from `BaseLoader` and implement:

```python
class MyLoader(BaseLoader):
    def __init__(self):
        super().__init__()
        self.table_name = "raw_my_table"
        self.write_mode = "append"  # or "truncate"
        self.upsert_keys = ["id"]   # For deduplication
    
    def get_create_table_ddl(self) -> str:
        """Define BigQuery table schema."""
        return f"""
        CREATE TABLE IF NOT EXISTS `{self.dataset}.{self.table_name}` (
            id INT64 NOT NULL,
            name STRING,
            loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
        )
        """
    
    def fetch_data(self) -> list:
        """Fetch and return list of dicts."""
        data = self.api_call(some_api_function, arg1)
        return data or []
```

## Data Sources

| Table | Source | Grain | Refresh | Rows |
|-------|--------|-------|---------|------|
| `raw_players` | NBA API | Player | Daily | ~3.5K |
| `raw_teams` | NBA API | Team | Daily | 30 |
| `raw_player_game_logs` | NBA Stats | Player-Game | Incremental | ~500K+ |
| `raw_team_game_logs` | NBA Stats | Team-Game | Incremental | ~100K+ |
| `raw_player_career_stats` | NBA Stats | Player-Season | Incremental | ~50K+ |
| `raw_player_common_info` | NBA Stats | Player | Daily | ~3.5K |

## Configuration

See `config.py`:

```python
API_RATE_LIMIT = 1          # seconds between API calls
API_TIMEOUT = 15            # request timeout
API_MAX_RETRIES = 5         # retry attempts before failure
BATCH_SIZE = 1000           # rows per BigQuery batch insert
VERBOSE = True              # detailed logging
```

## Error Handling

- **API failures**: Exponential backoff (2^attempt seconds, up to 5 retries).
- **Partial data**: On graceful shutdown (SIGINT), partial data is saved to BigQuery before exit.
- **Failed attempts**: Logged to `failed_attempts_*.json` for manual inspection.

## Testing

```bash
# Run a single loader with verbose output
VERBOSE=True python -c "from loaders.players import load_players; load_players()"

# Check BigQuery
bq query --nouse_legacy_sql "SELECT COUNT(*) FROM nba_raw.raw_players"
```

## Downstream Transformation

Data is transformed in the [dbt_nba](../dbt_nba) project:

- **Staging**: 1:1 cleaning, rename to snake_case
- **Intermediate**: Joins, aggregations, window functions
- **Marts**: Final analytics tables for dashboards

See [dbt_nba README](../dbt_nba/nba_analytics/README.md) and [CLAUDE.md](./CLAUDE.md) for full conventions.

## CI/CD

- Loaders can be triggered by Airflow, Cloud Scheduler, or manual invocation.
- Each loader is idempotent (safe to rerun; uses upsert keys for deduplication).

## Troubleshooting

**API rate limiting**: Increase `API_RATE_LIMIT` in `config.py`.

**BigQuery connection fails**: Check `BQ_KEYFILE` path and service account permissions.

**Partial data saved message**: Loader was interrupted; check `failed_attempts_*.json` for details.

## Contributing

See [CLAUDE.md](./CLAUDE.md) for detailed conventions on loader patterns, schema naming, testing, and workflows. This file is used by Claude Code sub-agents.
