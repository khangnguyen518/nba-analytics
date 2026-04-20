import time
import random
from nba_api.stats.endpoints import playergamelog
from loaders.base import BaseLoader
from config import BQ_PROJECT, BQ_DATASET, BQ_KEYFILE, COOLDOWN_INTERVAL, COOLDOWN_TIME, VERBOSE, START_SEASON, END_SEASON
from google.cloud import bigquery
from google.oauth2 import service_account


class GameLogsLoader(BaseLoader):

    def __init__(self, active_only=False, historical_only=False, limit=None, resume=False,
                 start_season=None, end_season=None, current_season_only=False, season_type=None):
        super().__init__()
        self.table_name          = "raw_player_game_logs"
        self.active_only         = active_only
        self.historical_only     = historical_only
        self.limit               = limit
        self.resume              = resume
        self.start_season        = start_season if start_season is not None else START_SEASON
        self.end_season          = end_season   if end_season   is not None else END_SEASON
        self.current_season_only = current_season_only
        self.season_type         = season_type  # None = both, 'Regular Season', or 'Playoffs'
        self.write_mode          = 'upsert'
        self.upsert_keys         = ['Player_ID', 'Game_ID', 'season_type']

    def get_create_table_ddl(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS `{self.dataset}.{self.table_name}` (
            Player_ID       INT64,
            Game_ID         STRING,
            SEASON_ID       STRING,
            GAME_DATE       STRING,
            MATCHUP         STRING,
            WL              STRING,
            MIN             FLOAT64,
            FGM             INT64,
            FGA             INT64,
            FG_PCT          FLOAT64,
            FG3M            INT64,
            FG3A            INT64,
            FG3_PCT         FLOAT64,
            FTM             INT64,
            FTA             INT64,
            FT_PCT          FLOAT64,
            OREB            INT64,
            DREB            INT64,
            REB             INT64,
            AST             INT64,
            STL             INT64,
            BLK             INT64,
            TOV             INT64,
            PF              INT64,
            PTS             INT64,
            PLUS_MINUS      INT64,
            VIDEO_AVAILABLE INT64,
            season_type     STRING,
            loaded_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
        )
        """

    def _get_loaded_keys(self) -> set:
        if not self.resume:
            return set()
        try:
            credentials = service_account.Credentials.from_service_account_file(BQ_KEYFILE)
            client      = bigquery.Client(credentials=credentials, project=BQ_PROJECT)

            season_type_filter = (
                f"WHERE season_type = '{self.season_type}'"
                if self.season_type else ""
            )

            query = f"""
            SELECT DISTINCT Player_ID
            FROM `{BQ_PROJECT}.{BQ_DATASET}.{self.table_name}`
            {season_type_filter}
            """
            result = client.query(query).result()
            return {row.Player_ID for row in result}
        except Exception:
            return set()

    def _get_player_seasons(self) -> dict:
        credentials = service_account.Credentials.from_service_account_file(BQ_KEYFILE)
        client      = bigquery.Client(credentials=credentials, project=BQ_PROJECT)

        query = f"""
        SELECT p.id as player_id, c.SEASON_ID
        FROM `{BQ_PROJECT}.{BQ_DATASET}.raw_players` p
        JOIN `{BQ_PROJECT}.{BQ_DATASET}.raw_player_career_stats` c
          ON p.id = c.PLAYER_ID
        WHERE CAST(SUBSTR(c.SEASON_ID, 1, 4) AS INT64)
              BETWEEN {self.start_season} AND {self.end_season}
        {"AND CAST(SUBSTR(c.SEASON_ID, 1, 4) AS INT64) = " + str(self.end_season) if self.current_season_only else ""}
        {"AND p.is_active = TRUE" if self.active_only else "AND p.is_active = FALSE" if self.historical_only else ""}
        ORDER BY p.id, c.SEASON_ID
        """

        player_seasons: dict = {}
        for row in client.query(query).result():
            player_seasons.setdefault(row.player_id, []).append(row.SEASON_ID)

        if self.limit:
            player_seasons = dict(list(player_seasons.items())[:self.limit])

        return player_seasons

    def fetch_data(self) -> list:
        player_seasons = self._get_player_seasons()
        loaded_ids     = self._get_loaded_keys()
        results        = []
        total          = len(player_seasons)

        season_types = [self.season_type] if self.season_type else ['Regular Season', 'Playoffs']

        for i, (player_id, seasons) in enumerate(player_seasons.items()):
            if self._shutdown_requested:
                break

            if player_id in loaded_ids:
                if VERBOSE:
                    print(f"  Skipping {player_id} (already loaded)")
                continue

            if VERBOSE:
                print(f"[{i+1}/{total}] Player {player_id} — {len(seasons)} season(s)...")

            for season in seasons:
                for season_type in season_types:
                    response = self.api_call(
                        playergamelog.PlayerGameLog,
                        player_id=player_id,
                        season=season,
                        season_type_all_star=season_type
                    )

                    if response is None:
                        continue

                    try:
                        logs    = response.player_game_log.get_dict()
                        headers = logs['headers']
                        for row_data in logs['data']:
                            row = dict(zip(headers, row_data))
                            row['season_type'] = season_type
                            results.append({k: self._clean_value(v) for k, v in row.items()})
                    except Exception as e:
                        if VERBOSE:
                            print(f"  ⚠️  Could not parse player {player_id} {season_type}: {e}")

            if (i + 1) % COOLDOWN_INTERVAL == 0:
                if VERBOSE:
                    print(f"  Cooling down for {COOLDOWN_TIME}s...")
                time.sleep(COOLDOWN_TIME)

        return results


def load_game_logs(active_only=False, historical_only=False, limit=None, resume=False,
                   start_season=None, end_season=None, current_season_only=False, season_type=None):
    GameLogsLoader(
        active_only=active_only, historical_only=historical_only, limit=limit, resume=resume,
        start_season=start_season, end_season=end_season,
        current_season_only=current_season_only, season_type=season_type
    ).run()