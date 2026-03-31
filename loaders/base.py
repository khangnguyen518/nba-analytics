import time
import random
import signal
import sys
import math
import inspect
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from google.cloud import bigquery
from google.oauth2 import service_account
from nba_api.stats.library import http as nba_http
from config import (
    BQ_PROJECT, BQ_DATASET, BQ_KEYFILE,
    API_RATE_LIMIT, API_MAX_RETRIES, API_TIMEOUT,
    BATCH_SIZE, VERBOSE
)

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


def get_bq_client():
    credentials = service_account.Credentials.from_service_account_file(BQ_KEYFILE)
    return bigquery.Client(credentials=credentials, project=BQ_PROJECT)


def stamp_loaded_at(rows: list) -> list:
    """Stamp loaded_at on every row that doesn't already have one."""
    now = datetime.now(timezone.utc).isoformat()
    return [{**row, 'loaded_at': row.get('loaded_at') or now} for row in rows]


class BaseLoader(ABC):

    def __init__(self):
        self.table_name          = None
        self.dataset             = BQ_DATASET
        self.write_mode          = 'append'
        self.upsert_keys         = []
        self._shutdown_requested = False
        self._partial_data       = []
        self._is_cleaning_up     = False
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT,  self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        signal_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"

        if self._is_cleaning_up:
            print("\n❌ Force shutdown during cleanup — some data may be lost")
            sys.exit(1)

        print(f"\n⚠️  Received {signal_name} — saving partial progress before exiting...")
        print("   (Press Ctrl+C again to force quit immediately)")
        self._shutdown_requested = True

        if hasattr(self, "_first_interrupt") and self._first_interrupt:
            print("\n❌ Force shutdown requested")
            sys.exit(1)

        self._first_interrupt = True

    def _cleanup(self):
        if not self._partial_data:
            return

        self._is_cleaning_up = True
        print(f"\n{'='*60}")
        print("Saving partial progress...")
        print(f"{'='*60}")

        try:
            all_rows = [row for batch in self._partial_data for row in batch]
            if all_rows:
                print(f"Saving {len(all_rows)} rows collected before shutdown...")
                self._write_to_bigquery(all_rows, force=True)
                print("✓ Partial data saved")
        except Exception as e:
            print(f"❌ Failed to save partial data: {e}")
        finally:
            self._is_cleaning_up = False
            self._partial_data = []

    def api_call(self, func, *args, **kwargs):
        if self._shutdown_requested:
            return None

        if "timeout" not in kwargs:
            try:
                sig = inspect.signature(func)
                if "timeout" in sig.parameters:
                    kwargs["timeout"] = API_TIMEOUT
            except (ValueError, TypeError):
                pass

        jitter = random.uniform(1, 3)
        time.sleep(API_RATE_LIMIT + jitter)

        for attempt in range(API_MAX_RETRIES):
            if self._shutdown_requested:
                return None

            try:
                return func(*args, **kwargs)

            except Exception as e:
                error_str = str(e).lower()

                if "resultset" in error_str or "'resultset'" in error_str:
                    if VERBOSE:
                        print("    ⚠️  No data available for this player")
                    return None

                if attempt == API_MAX_RETRIES - 1:
                    if VERBOSE:
                        print(f"❌ API call failed after {API_MAX_RETRIES} attempts: {e}")
                    if not hasattr(self, "failed_attempts"):
                        self.failed_attempts = []
                    self.failed_attempts.append({"func": func, "args": args, "kwargs": kwargs, "error": str(e)})
                    return None

                wait_time = 2 ** attempt
                if VERBOSE:
                    print(f"⚠️  Attempt {attempt + 1}/{API_MAX_RETRIES} failed: {e}")
                    print(f"   Retrying in {wait_time}s...")
                time.sleep(wait_time)

    def _clean_value(self, val):
        if val is None:
            return None
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        if isinstance(val, str) and val.lower() == "nan":
            return None
        return val

    def _write_to_bigquery(self, rows, force=False):
        if not rows:
            return

        rows      = stamp_loaded_at(rows)
        client    = get_bq_client()
        table_ref = f"{BQ_PROJECT}.{self.dataset}.{self.table_name}"

        for i in range(0, len(rows), BATCH_SIZE):
            if self._shutdown_requested and not force and not self._is_cleaning_up:
                print(f"⚠️  Shutdown requested — stopping insert")
                break

            batch = rows[i:i + BATCH_SIZE]

            if self.write_mode == 'truncate' and i == 0:
                write_disposition = bigquery.WriteDisposition.WRITE_TRUNCATE
            else:
                write_disposition = bigquery.WriteDisposition.WRITE_APPEND

            job_config = bigquery.LoadJobConfig(
                write_disposition=write_disposition,
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                autodetect=True,
            )

            job = client.load_table_from_json(batch, table_ref, job_config=job_config)
            job.result()

            if VERBOSE:
                print(f"  ✓ Inserted batch: {len(batch)} rows")

    def _upsert_to_bigquery(self, rows, force=False):
        if not rows:
            return

        rows       = stamp_loaded_at(rows)
        client     = get_bq_client()
        temp_table = f"{BQ_PROJECT}.{self.dataset}.{self.table_name}_temp"
        main_table = f"{BQ_PROJECT}.{self.dataset}.{self.table_name}"

        for i in range(0, len(rows), BATCH_SIZE):
            if self._shutdown_requested and not force and not self._is_cleaning_up:
                print(f"⚠️  Shutdown requested — stopping insert")
                break

            batch = rows[i:i + BATCH_SIZE]

            main_table_ref = client.get_table(main_table)

            write_disposition = (
                bigquery.WriteDisposition.WRITE_TRUNCATE
                if i == 0
                else bigquery.WriteDisposition.WRITE_APPEND
            )

            job_config = bigquery.LoadJobConfig(
                write_disposition=write_disposition,
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                schema=main_table_ref.schema,
            )

            job = client.load_table_from_json(batch, temp_table, job_config=job_config)
            job.result()

            if VERBOSE:
                print(f"  ✓ Staged batch: {len(batch)} rows")

        partition_keys = ", ".join(self.upsert_keys)

        dedup_sql = f"""
        CREATE OR REPLACE TABLE `{temp_table}` AS
        SELECT * EXCEPT (_row_num) FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY {partition_keys}
                ORDER BY loaded_at DESC NULLS LAST
            ) as _row_num
            FROM `{temp_table}`
        )
        WHERE _row_num = 1
        """
        client.query(dedup_sql).result()

        columns       = list(rows[0].keys())
        join_clause   = " AND ".join([f"t.{k} = s.{k}" for k in self.upsert_keys])
        update_cols   = [c for c in columns if c not in self.upsert_keys and c != 'loaded_at']
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

        if VERBOSE:
            print(f"  ✓ Upserted {len(rows)} rows into {self.table_name}")

    @abstractmethod
    def fetch_data(self) -> list:
        pass

    @abstractmethod
    def get_create_table_ddl(self) -> str:
        pass

    def create_table(self):
        client = get_bq_client()
        ddl    = self.get_create_table_ddl()
        client.query(ddl).result()
        if VERBOSE:
            print(f"✓ Table '{self.table_name}' is ready")

    def run(self):
        try:
            print(f"\n{'='*60}")
            print(f"Loading: {self.table_name}")
            print(f"{'='*60}")

            self.create_table()

            print("Fetching data from NBA API...")
            rows = self.fetch_data()

            if self._shutdown_requested:
                self._cleanup()
                return

            print(f"✓ Fetched {len(rows)} rows")
            print("Writing to BigQuery...")

            if self.write_mode == 'upsert':
                self._upsert_to_bigquery(rows)
            else:
                self._write_to_bigquery(rows)

            print(f"{'='*60}")
            print(f"✓ {self.table_name} complete!\n")

        except KeyboardInterrupt:
            print("\n⚠️  Interrupted — saving partial data...")
            self._cleanup()
            raise
        except Exception as e:
            print(f"\n❌ Error: {e}")
            self._cleanup()
            raise
        finally:
            if self._partial_data:
                self._cleanup()