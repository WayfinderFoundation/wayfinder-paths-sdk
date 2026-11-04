-- Trader Lab views over one release. Published in every manifest as
-- `views_sql`; `{release}` and `{t0}` are replaced with that release's
-- directory and the shared t0 directory (local paths in the SDK, the mounted
-- bucket in the query sandbox).
CREATE OR REPLACE VIEW universe AS SELECT * FROM read_parquet('{release}/universe/*.parquet');
CREATE OR REPLACE VIEW cohort_membership AS SELECT * FROM read_parquet('{release}/cohort_membership/*.parquet');
CREATE OR REPLACE VIEW persistence AS SELECT * FROM read_parquet('{release}/persistence/*.parquet');
CREATE OR REPLACE VIEW fills AS SELECT * FROM read_parquet('{release}/fills/*.parquet');
CREATE OR REPLACE VIEW round_trips AS SELECT * FROM read_parquet('{release}/round_trips/*.parquet');
CREATE OR REPLACE VIEW positions_hourly AS SELECT * FROM read_parquet('{release}/positions_hourly/*.parquet');
CREATE OR REPLACE VIEW markouts AS SELECT * FROM read_parquet('{release}/markouts/*.parquet');
CREATE OR REPLACE VIEW entry_states AS SELECT * FROM read_parquet('{release}/entry_states/*.parquet');
CREATE OR REPLACE VIEW leaderboard AS SELECT * FROM read_parquet('{release}/leaderboard/*.parquet');
CREATE OR REPLACE VIEW coin_flows AS SELECT * FROM read_parquet('{release}/coin_flows/*.parquet');
CREATE OR REPLACE VIEW entry_state_hist AS SELECT * FROM read_parquet('{release}/entry_state_hist/*.parquet');
CREATE OR REPLACE VIEW copyability AS SELECT * FROM read_parquet('{release}/copyability/*.parquet');
CREATE OR REPLACE VIEW account_day AS SELECT * FROM read_parquet('{t0}/account_day/*/part.parquet', hive_partitioning = true);
CREATE OR REPLACE VIEW bars_1m AS SELECT * FROM read_parquet('{t0}/bars_1m/*/part.parquet', hive_partitioning = true);
CREATE OR REPLACE VIEW funding AS SELECT * FROM read_parquet('{t0}/funding/*/part.parquet', hive_partitioning = true);
