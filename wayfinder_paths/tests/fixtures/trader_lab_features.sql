-- Trader Lab entry-state features.
--
-- Input: a relation `points(coin VARCHAR, ts TIMESTAMPTZ)`, plus the `bars_1m`
-- and `funding` views. Output: one row per point with market state measured
-- only from data before the point's minute (no look-ahead). Published in every
-- release manifest as `features_sql`, so the server, the query sandbox and the
-- SDK compute identical features for any user's own entries. Returns one row
-- per distinct (coin, ts); join back to your points on both.
--
--   z{k}      k-minute log return / (per-minute vol * sqrt(k)), vol from the
--             trailing 24h of hourly returns
--   dist_hi   distance below the trailing-24h high, in 24h-vol units
--   dist_lo   distance above the trailing-24h low, in 24h-vol units
--   vol_ratio log(last hour's range / average hourly range over 24h)
--   vol_z     log(last hour's volume / average hourly volume over 24h)
--   btc_z60, btc_z240  the same returns for BTC
--   hour_sin, hour_cos, weekend  time of day / week (UTC)
--   funding   latest hourly funding rate for the coin
WITH
pts AS (
    SELECT coin, ts,
           date_trunc('minute', ts) - INTERVAL 1 MINUTE AS m0,
           date_trunc('minute', ts) - INTERVAL 2 MINUTE AS m1,
           date_trunc('minute', ts) - INTERVAL 6 MINUTE AS m5,
           date_trunc('minute', ts) - INTERVAL 16 MINUTE AS m15,
           date_trunc('minute', ts) - INTERVAL 61 MINUTE AS m60,
           date_trunc('minute', ts) - INTERVAL 241 MINUTE AS m240,
           date_trunc('minute', ts) - INTERVAL 1441 MINUTE AS m1440,
           date_trunc('hour', ts) - INTERVAL 1 HOUR AS h
    FROM (SELECT DISTINCT coin, ts FROM points)
),
hourly AS (
    SELECT coin, date_trunc('hour', ts) AS hour,
           arg_max(close, ts) AS close, max(high) AS high, min(low) AS low,
           sum(volume_usd) AS vol
    FROM bars_1m
    WHERE coin IN (SELECT DISTINCT coin FROM points UNION SELECT 'BTC')
    GROUP BY 1, 2
),
hstats AS (
    SELECT coin, hour,
           stddev_samp(r) OVER w AS sd_h,
           max(high) OVER w AS hi24,
           min(low) OVER w AS lo24,
           ln(nullif(high - low, 0) / nullif(avg(high - low) OVER w, 0)) AS vol_ratio,
           ln(nullif(vol, 0) / nullif(avg(vol) OVER w, 0)) AS vol_z
    FROM (
        SELECT *, ln(close / lag(close) OVER (PARTITION BY coin ORDER BY hour)) AS r
        FROM hourly
    )
    WINDOW w AS (PARTITION BY coin ORDER BY hour
                 RANGE BETWEEN INTERVAL 23 HOUR PRECEDING AND CURRENT ROW)
),
closes AS (
    SELECT p.coin, p.ts, p.h,
           c0.close AS c0, c1.close AS c1, c5.close AS c5, c15.close AS c15,
           c60.close AS c60, c240.close AS c240, c1440.close AS c1440
    FROM pts p
    ASOF LEFT JOIN bars_1m c0 ON p.coin = c0.coin AND p.m0 >= c0.ts
    ASOF LEFT JOIN bars_1m c1 ON p.coin = c1.coin AND p.m1 >= c1.ts
    ASOF LEFT JOIN bars_1m c5 ON p.coin = c5.coin AND p.m5 >= c5.ts
    ASOF LEFT JOIN bars_1m c15 ON p.coin = c15.coin AND p.m15 >= c15.ts
    ASOF LEFT JOIN bars_1m c60 ON p.coin = c60.coin AND p.m60 >= c60.ts
    ASOF LEFT JOIN bars_1m c240 ON p.coin = c240.coin AND p.m240 >= c240.ts
    ASOF LEFT JOIN bars_1m c1440 ON p.coin = c1440.coin AND p.m1440 >= c1440.ts
),
btc AS (
    SELECT p.coin, p.ts,
           b0.close AS b0, b60.close AS b60, b240.close AS b240, bs.sd_h AS bsd
    FROM pts p
    ASOF LEFT JOIN (SELECT * FROM bars_1m WHERE coin = 'BTC') b0 ON p.m0 >= b0.ts
    ASOF LEFT JOIN (SELECT * FROM bars_1m WHERE coin = 'BTC') b60 ON p.m60 >= b60.ts
    ASOF LEFT JOIN (SELECT * FROM bars_1m WHERE coin = 'BTC') b240 ON p.m240 >= b240.ts
    ASOF LEFT JOIN (SELECT * FROM hstats WHERE coin = 'BTC') bs ON p.h >= bs.hour
),
joined AS (
    SELECT c.*, s.sd_h / sqrt(60) AS sd_m, s.sd_h, s.hi24, s.lo24, s.vol_ratio, s.vol_z,
           f.funding_rate
    FROM closes c
    ASOF LEFT JOIN hstats s ON c.coin = s.coin AND c.h >= s.hour
    ASOF LEFT JOIN funding f ON c.coin = f.coin AND c.ts >= f.ts
)
SELECT j.coin, j.ts,
       ln(j.c0 / j.c1) / nullif(j.sd_m, 0) AS z1,
       ln(j.c0 / j.c5) / nullif(j.sd_m * sqrt(5), 0) AS z5,
       ln(j.c0 / j.c15) / nullif(j.sd_m * sqrt(15), 0) AS z15,
       ln(j.c0 / j.c60) / nullif(j.sd_m * sqrt(60), 0) AS z60,
       ln(j.c0 / j.c240) / nullif(j.sd_m * sqrt(240), 0) AS z240,
       ln(j.c0 / j.c1440) / nullif(j.sd_m * sqrt(1440), 0) AS z1440,
       ln(j.hi24 / j.c0) / nullif(j.sd_h * sqrt(24), 0) AS dist_hi,
       ln(j.c0 / j.lo24) / nullif(j.sd_h * sqrt(24), 0) AS dist_lo,
       j.vol_ratio,
       j.vol_z,
       ln(b.b0 / b.b60) / nullif(b.bsd / sqrt(60) * sqrt(60), 0) AS btc_z60,
       ln(b.b0 / b.b240) / nullif(b.bsd / sqrt(60) * sqrt(240), 0) AS btc_z240,
       sin(2 * pi() * hour(j.ts) / 24) AS hour_sin,
       cos(2 * pi() * hour(j.ts) / 24) AS hour_cos,
       dayofweek(j.ts) IN (0, 6) AS weekend,
       j.funding_rate AS funding
FROM joined j
JOIN btc b USING (coin, ts)
