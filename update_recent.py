import argparse
import time
import random
from datetime import datetime, timedelta, timezone
from google.cloud import bigquery
from nba_api.stats.endpoints import leaguegamelog, playergamelog
from nba_api.stats.library import http as nba_http
from loaders.base import get_bq_client
from loaders import load_player_career, load_team_game_logs, load_player_advanced_season_stats
from config import BQ_PROJECT, BQ_DATASET, VERBOSE

nba_http.HEADERS = {
    'Host': 'stats.nba.com',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'x-nba-stats-origin': 'stats',
    'x-nba-stats-token': 'true',
    'Connection': 'keep-alive',
    'Referer': 'https://www.nba.com/',
    'Origin': 'https://www.nba.com',
}


def get_current_season() -> int:
    now = datetime.now()
    return now.year - 1 if now.month < 10 else now.year


def get_season_string(season_year: int) -> str:
    return f"{season_year}-{str(season_year + 1)[-2:]}"


def get_date_range() -> tuple[str, str]:
    now       = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime('%m/%d/%Y')
    today     = now.strftime('%m/%d/%Y')
    return yesterday, today


def get_season_types_for_month() -> list:
    """
    Determine which season types to fetch based on the current month:
    - Oct-Mar: Regular Season only
    - Apr:     Regular Season + Playoffs (overlap at start of playoffs)
    - May-Jun: Playoffs only
    """
    month = datetime.now().month
    if month == 4:
        return ['Regular Season', 'Playoffs']
    elif month in [5, 6]:
        return ['Playoffs']
    else:
        return ['Regular Season']


def fetch_players_who_played(date: str, season: str) -> list:
    """
    Use LeagueGameLog to get player IDs who played on a given date.
    Season types fetched depend on the current month:
    - Oct-Mar: Regular Season only
    - Apr:     Regular Season + Playoffs
    - May-Jun: Playoffs only
    """
    season_types = get_season_types_for_month()
    print(f"  Fetching players who played on {date} ({', '.join(season_types)})...")

    player_ids = set()

    for season_type in season_types:
        time.sleep(random.uniform(2, 4))

        try:
            r = leaguegamelog.LeagueGameLog(
                season=season,
                player_or_team_abbreviation='P',
                date_from_nullable=date,
                date_to_nullable=date,
                season_type_all_star=season_type
            )
            logs    = r.league_game_log.get_dict()
            headers = logs['headers']
            data    = logs['data']

            if data:
                player_id_idx = headers.index('PLAYER_ID')
                player_ids.update({row[player_id_idx] for row in data})
                print(f"  Found {len(data)} rows for {season_type}")
            else:
                print(f"  No games found for {season_type} on {date}")

        except Exception as e:
            print(f"  ❌ Could not fetch league game log ({season_type}): {e}")

    print(f"  Total unique players: {len(player_ids)}")
    return list(player_ids)


def fetch_game_logs_for_players(player_ids: list, season: str):
    """
    Fetch full-season game logs for a targeted list of players and upsert to BigQuery.
    Fetches both Regular Season and Playoffs for each player.
    """
    if not player_ids:
        print("  No players to fetch game logs for")
        return

    print(f"  Fetching game logs for {len(player_ids)} players...")

    client     = get_bq_client()
    main_table = f"{BQ_PROJECT}.{BQ_DATASET}.raw_player_game_logs"
    temp_table = f"{BQ_PROJECT}.{BQ_DATASET}.raw_player_game_logs_temp"

    all_rows = []
    now      = datetime.now(timezone.utc).isoformat()

    for i, player_id in enumerate(player_ids):
        if VERBOSE:
            print(f"  [{i+1}/{len(player_ids)}] Player {player_id}...")

        for season_type in ['Regular Season', 'Playoffs']:
            time.sleep(random.uniform(2, 4))

            try:
                r = playergamelog.PlayerGameLog(
                    player_id=player_id,
                    season=season,
                    season_type_all_star=season_type
                )
                logs    = r.player_game_log.get_dict()
                headers = logs['headers']

                for row_data in logs['data']:
                    row = dict(zip(headers, row_data))
                    row['Player_ID']   = player_id
                    row['season_type'] = season_type
                    row['loaded_at']   = now
                    all_rows.append(row)

            except Exception as e:
                if VERBOSE:
                    print(f"  ⚠️  Could not fetch player {player_id} ({season_type}): {e}")

    if not all_rows:
        print("  No game log rows fetched")
        return

    print(f"  Fetched {len(all_rows)} rows — writing to BigQuery...")

    main_table_ref = client.get_table(main_table)

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=main_table_ref.schema,
    )
    client.load_table_from_json(all_rows, temp_table, job_config=job_config).result()

    columns     = list(all_rows[0].keys())
    upsert_keys = ['Player_ID', 'Game_ID', 'season_type']

    dedup_sql = f"""
    CREATE OR REPLACE TABLE `{temp_table}` AS
    SELECT * EXCEPT (_row_num) FROM (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY {", ".join(upsert_keys)}
            ORDER BY loaded_at DESC NULLS LAST
        ) as _row_num
        FROM `{temp_table}`
    )
    WHERE _row_num = 1
    """
    client.query(dedup_sql).result()

    join_clause   = " AND ".join([f"t.{k} = s.{k}" for k in upsert_keys])
    update_cols   = [c for c in columns if c not in upsert_keys and c != 'loaded_at']
    update_clause = ", ".join([f"t.{c} = s.{c}" for c in update_cols])
    insert_cols   = ", ".join(columns)
    insert_vals   = ", ".join([f"s.{c}" for c in columns])

    merge_sql = f"""
    MERGE `{main_table}` t
    USING `{temp_table}` s
    ON {join_clause}
    WHEN MATCHED THEN
        UPDATE SET {update_clause}
    WHEN NOT MATCHED THEN
        INSERT ({insert_cols})
        VALUES ({insert_vals})
    """
    client.query(merge_sql).result()
    client.delete_table(temp_table, not_found_ok=True)

    print(f"  ✓ Upserted {len(all_rows)} rows into raw_player_game_logs")


def main():
    parser = argparse.ArgumentParser(description="Daily NBA data update")

    parser.add_argument("--skip-career",         action="store_true",
                        help="Skip career stats update")
    parser.add_argument("--skip-game-logs",      action="store_true",
                        help="Skip game logs update")
    parser.add_argument("--skip-advanced-stats", action="store_true",
                        help="Skip advanced season stats update")
    parser.add_argument("--date",                type=str, default=None,
                        help="Fetch game logs for a specific date only (MM/DD/YYYY)")

    args = parser.parse_args()

    current_season = get_current_season()
    season_string  = get_season_string(current_season)

    print("\n" + "="*60)
    print("          NBA DAILY UPDATE")
    print(f"          Season: {season_string}")
    print("="*60)

    if not args.skip_career:
        print("\nUpdating career stats for active players...")
        load_player_career(active_only=True, resume=False)

    if not args.skip_game_logs:
        if args.date:
            print(f"\nFetching game logs for {args.date}...")
            player_ids = fetch_players_who_played(args.date, season_string)
        else:
            print("\nFetching game logs for yesterday and today...")
            yesterday, today = get_date_range()
            ids_yesterday = fetch_players_who_played(yesterday, season_string)
            ids_today     = fetch_players_who_played(today, season_string)
            player_ids    = list(set(ids_yesterday + ids_today))
            print(f"  Total unique players across both days: {len(player_ids)}")

        fetch_game_logs_for_players(player_ids, season_string)

    print("\nFetching team game logs...")
    load_team_game_logs(start_season=current_season, end_season=current_season)

    if not args.skip_advanced_stats:
        print("\nUpdating advanced season stats...")
        load_player_advanced_season_stats(season=season_string)

    print("\n" + "="*60)
    print("✓ Daily update complete")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()