# Raw to curated: what changed, and which rule changed it

A row-level walkthrough of each of the four `raw_* → curated` pairs. Every figure below
was verified against the loaded database, and each section ends with the SQL to
reproduce it, so nothing here has to be taken on trust.

**Why the two layers exist.** The `raw_*` tables hold the delivered CSVs verbatim —
every column `TEXT`, nothing constrained — so a malformed value can never fail the
insert. The curated tables are typed and constrained. Keeping both means every
correction stays checkable against what actually arrived, and the data-quality page is a
projection of the rules rather than a separate artifact that can drift out of agreement
with them.

## Summary

| Pair | Rows | Cells changed | Columns added | Actions used |
|---|---|---|---|---|
| `raw_security_master` → `security` | 80 → 80 | 3 | — | DEFAULTED |
| `raw_marks_monthly` → `mark` | 890 → 890 | 3 | `clean_price_repaired` | REPAIRED, FLAGGED |
| `raw_holdings_monthly` → `holding` | 811 → **803** | 17 | `market_value_imputed`, `post_maturity` | DEDUPLICATED, REPAIRED, IMPUTED, FLAGGED |
| `raw_transactions` → `trade` | 75 → **72** | 0 | `id` | DEDUPLICATED, EXCLUDED |
| **Total** | **1856 → 1845** | **23** | | |

Two things worth drawing out. Only **23 cell values** were altered across 1,856 rows —
repairs are conservative by policy, and everything else is either a row-level decision or
a flag. And every row removal is arithmetically accounted for: 8 + 1 + 2 = 11, matching
1,856 → 1,845 exactly.

---

## Pair 1 — `raw_security_master` → `security`

**80 rows → 80 rows.** No rows added or removed.

### Schema

Raw adds provenance (`id`, `load_id`, `source_row_num`) and stores everything as `TEXT`.
Curated adds no new columns but types them — `DATE`, `NUMERIC(9,6)` — and applies
`NOT NULL` to `sector`, `rating` and `asset_class`. Those constraints are only
satisfiable *because* the rules below fill the gaps.

### Value changes

| Column | Rows changed |
|---|---|
| `sector` | **1** |
| `rating` | **2** |
| `description`, `issuer`, `coupon_pct`, `issue_date`, `maturity_date`, `asset_class` | 0 |

```
SM002  XMQXAE3CS  sector: (null) → 'Unclassified'   Fairfield Brands 5.467% 06/20/2025
SM003  63LG8HRJ3  rating: (null) → 'NR'             Talon Petroleum 6.055% 06/12/2025
SM003  N7Z68G2BD  rating: (null) → 'NR'             Quanta Systems 6.927% 08/03/2026
```

### Rules that fired

| Rule | Severity | Action | n |
|---|---|---|---|
| SM002 Missing sector on security master | WARNING | DEFAULTED | 1 |
| SM003 Missing rating on security master | WARNING | DEFAULTED | 2 |

### Why these choices

The rows were **not dropped**: those are real positions and must still count toward
portfolio market value. The placeholders are deliberately visible rather than plausible —
bucketing an unclassified bond into a real sector would corrupt the allocation view,
which is one of the required screens. Rating was **not inferred** from the issuer's other
bonds, because rating is instrument-level and seniority differences make that inference
unsafe.

### Verify

```sql
SELECT r.security_id, r.sector AS raw_sector, c.sector AS cur_sector,
       r.rating AS raw_rating, c.rating AS cur_rating
FROM raw_security_master r JOIN security c USING (security_id)
WHERE r.sector IS NULL OR r.rating IS NULL;
```

---

## Pair 2 — `raw_marks_monthly` → `mark`

**890 rows → 890 rows.** No rows added or removed.

### Schema

Curated **adds a column that does not exist in raw**: `clean_price_repaired`, a boolean
recording whether MK002 rewrote the price. It exists so the security drill-down can draw
a repaired observation differently from a delivered one, rather than presenting an
inferred value as though it had been supplied.

### Value changes

| Column | Rows changed |
|---|---|
| `clean_price` | **3** |
| `oas_bps` | 0 |

```
MK002  1AMUISU25  2025-04-30    1.010971 → 101.0971    ×100, flagged
MK002  H0HGFB0NV  2025-11-30    0.974963 →  97.4963    ×100, flagged
MK002  PSX36784P  2025-12-31    0.959560 →  95.9560    ×100, flagged
```

### Rules that fired

| Rule | Severity | Action | n |
|---|---|---|---|
| MK002 Price quoted as fraction of par instead of per 100 | ERROR | REPAIRED | 3 |
| MK007 Large month-over-month price move | INFO | **FLAGGED** | 10 |

### Why these choices

**MK002 repairs; MK007 does not.** That contrast is the design. A price of `0.974963` is
not a credible per-100 quote, and ×100 lands it back inside the plausible band, so the
correct value is recoverable from the data itself. Left uncorrected it understates the
position by ~99% and poisons both the sector average and the portfolio total for that
month.

MK007 fired ten times and **changed nothing**. A genuine credit event has the same
signature as bad data — and the 2025 Energy selloff that Q3 exists to find would be
destroyed by an over-eager rule. So it reports at INFO and never rewrites.

Note the ordering dependency: MK002 runs **before** MK003 (the plausible-band check), so
a recoverable scale error is repaired rather than flagged as implausible. Reversed, all
three of these rows would have been reported as out-of-band prices instead of fixed.

### Verify

```sql
SELECT r.security_id, r.as_of_date, r.clean_price AS delivered,
       c.clean_price AS used, c.clean_price_repaired
FROM raw_marks_monthly r JOIN mark c USING (security_id, as_of_date)
WHERE c.clean_price_repaired = 1;
```

---

## Pair 3 — `raw_holdings_monthly` → `holding`

**811 rows → 803 rows.** The only pair that loses rows to deduplication, and the only one
where a repair changes the portfolio total.

### Schema

Curated **adds two columns absent from raw**: `market_value_imputed` and
`post_maturity`. Both are rule outputs, not source data. It also enforces
`UNIQUE (as_of_date, security_id)` — the constraint that proves deduplication actually
worked, rather than merely being attempted.

### Row accounting

```
raw rows                                     811
HL001  duplicate (as_of_date, security_id)     −8    one of each pair dropped
curated rows                                 803    ✓ 811 − 8 = 803
```

No rows were excluded — there are no orphan securities in holdings — and none were added.

### Value changes

| Column | Rows changed |
|---|---|
| `par_amount` | **1** |
| `market_value` | **24** — see below |
| `book_value` | 0 |

The 24 needs unpacking: **15** HL004 imputations + **1** HL002 sign flip = 16 genuine
changes. The other 8 are a join artefact — each duplicated key has two raw rows matching
one surviving curated row, so the *dropped* row also registers a difference when joined
on the business key.

### HL001 — duplicate snapshots

Eight `(security, month-end)` keys arrive twice, differing only in `database_date` and
`market_value`. The surviving row is chosen by **reconciliation against the independent
mark**, not by taking the latest load. For all eight pairs the *earlier* row's implied
price matches `marks_monthly` to four decimal places while the later one drifts by
around a point, so "latest wins" would have kept the corrupted value.

Example, January:

| `database_date` | market value | implied price | vs mark 103.8707 |
|---|---|---|---|
| 2025-02-03 | 12,049,001.20 | 103.8707 | **−0.0000** ← kept |
| 2025-02-13 | 12,140,969.61 | 104.6635 | +0.7928 |

Confirmation that this is right: HL006 (market value versus par × price) fires **zero**
times afterwards. Two rules corroborating, rather than one flagging the other's mistake.

### HL004 — imputed market values

15 rows had a null `market_value` with both par and a mark available, imputed as
`par × price / 100`. **$212,228,337.40** of market value added across the year — the
single largest effect of the entire cleaning layer. All 15 flagged
`market_value_imputed = 1`.

January alone accounts for $67,570,509.50 across four rows. Dropping those rows instead
would report January at $773.77M rather than $841.34M, and manufacture a fake +$65M jump
in February when the same securities do have values.

### HL002 — the sign flip

One row, and the asymmetry that identified it as an error rather than a short position:

| | `par_amount` | `book_value` | `market_value` |
|---|---|---|---|
| raw | **−3,900,000** | **+3,955,270.80** | **−3,842,108.40** |
| curated | +3,900,000 | +3,955,270.80 | +3,842,108.40 |

`VK4YN4WS3`, 2025-03-31. Book value stayed positive while par and market value went
negative. A genuine short position would carry a negative book value too — and this
security holds positive par in every other month.

### HL009 — post-maturity positions

Three rows **kept, not dropped**, flagged `post_maturity = 1`:

```
2025-10-31  XWRWGY7W4  par 4,500,000  MV 4,494,150
2025-11-30  XWRWGY7W4  par 4,500,000  MV 4,494,150
2025-12-31  XWRWGY7W4  par 4,500,000  MV 4,494,150
```

`XWRWGY7W4` (Fairfield Brands 6.475% **09/15/2025**) matures on 2025-09-15, redeems
correctly via a `MATURITY` trade for its full 15,200,000 par, and is correctly absent
from the September snapshot. It then reappears with a market value frozen at exactly
4,494,150 for three consecutive months, with no marks after August.

**$13,482,450 of fabricated value.** The rows are retained so the exclusion is auditable
and reversible; `load_positions(include_post_maturity=True)` re-includes them.

The test is a join, since `holding` has no maturity date of its own:

```
maturity_date is known  AND  as_of_date > maturity_date  AND  par_amount ≠ 0
```

The third condition matters: a row reported after maturity with **zero** par is normal
housekeeping, not an anomaly. It is why HL009 fires 3 times and not 8, despite eight
securities maturing during 2025.

### Rules that fired

| Rule | Severity | Action | n |
|---|---|---|---|
| HL001 Duplicate holdings snapshot | ERROR | DEDUPLICATED | 8 |
| HL002 Negative par with positive book value | ERROR | REPAIRED | 1 |
| HL009 Position reported after the security matured | ERROR | FLAGGED | 3 |
| HL004 Missing market value imputed from par and price | WARNING | IMPUTED | 15 |
| HL010 Held position has no mark for the month | INFO | FLAGGED | 25 |

### Verify

```sql
-- row accounting
SELECT (SELECT COUNT(*) FROM raw_holdings_monthly) AS raw,
       (SELECT COUNT(*) FROM holding)              AS curated;

-- the imputations
SELECT COUNT(*), SUM(market_value) FROM holding WHERE market_value_imputed = 1;

-- post_maturity, recomputed from first principles
SELECT h.as_of_date, h.security_id, s.maturity_date, h.par_amount, h.post_maturity
FROM holding h JOIN security s USING (security_id)
WHERE s.maturity_date IS NOT NULL AND h.as_of_date > s.maturity_date
  AND h.par_amount <> 0;
```

---

## Pair 4 — `raw_transactions` → `trade`

**75 rows → 72 rows.** The only pair where rows are excluded rather than deduplicated,
and the only one where **no cell value changed at all**.

### Schema

Curated **adds a surrogate primary key** `id`. `trade_id` deliberately carries no
uniqueness constraint, because TX002 retains rows that share a `trade_id` with
*conflicting* details — an identifier collision is not a duplicate, and dropping one
would lose a real cash flow.

### Row accounting

```
raw rows                                      75
TX001  duplicate trade_id, identical details   −1    dropped
TX003  security absent from master             −2    excluded
curated rows                                  72    ✓ 75 − 1 − 2 = 72
```

### TX001 — the double-send

```
T1014  2025-03-15  BRN4YWM4W  BUY  2,400,000  98.3847
T1014  2025-03-15  BRN4YWM4W  BUY  2,400,000  98.3847
```

Byte-identical across `trade_date`, `security_id`, `trade_type`, `par_amount` and
`price`. Read as a double-send from the source feed and deduplicated. Counting both would
overstate activity by 2,400,000 par and corrupt the trading-versus-market decomposition.

Contrast TX002, which did not fire here: two rows sharing a `trade_id` with *different*
details are both kept and flagged, because neither can be dismissed.

### TX003 — orphan trades

```
T1074  2025-08-21  XK93PLR22  BUY  3,000,000  99.9315
T1073  2025-05-14  ZZ48Q1MM7  BUY  2,500,000  99.9230
```

**$5,500,000 par excluded.** Both securities are absent from the master *and* never
appear in holdings, so there is no position to attribute the trade to and no sector or
rating to allocate it into. This removes real cash flow, which is why it is disclosed on
the data-quality page and in the Q1 attribution residual rather than silently dropped.

### Value changes

**Zero, in every column** — `par_amount`, `price`, `trade_date`, `settlement_date`,
`trade_type` and `security_id` are all untouched on the 71 surviving rows. This pair is
purely about which rows survive, never about correcting one.

### Rules that fired

| Rule | Severity | Action | n |
|---|---|---|---|
| TX001 Duplicate trade_id with identical details | ERROR | DEDUPLICATED | 1 |
| TX003 Trade references unknown security | ERROR | EXCLUDED | 2 |

### Verify

```sql
SELECT trade_id, COUNT(*) FROM raw_transactions GROUP BY trade_id HAVING COUNT(*) > 1;

SELECT r.trade_id, r.security_id, r.par_amount
FROM raw_transactions r
WHERE r.security_id NOT IN (SELECT security_id FROM security);
```

---

## A known asymmetry in lifecycle checking

HL009 guards one end of a security's life. Nothing guards the other, and nothing checks
marks or trades against either end:

| Check | Rule | Rows here |
|---|---|---|
| Position after maturity | **HL009** | **3** |
| Position before issue | none | 0 |
| Mark before issue | none | 0 |
| Mark after maturity | none | 0 |
| Trade before issue | none | 0 |
| Trade after maturity | none | 0 |

Nothing was missed in this extract, but that is luck rather than coverage: the latest
`issue_date` in the master is 2024-12-05 and the earliest snapshot is 2025-01-31, a
margin of eight weeks. A security issued in February 2025 with a January holding would
pass silently.

`issue_date` is currently used by only two rules — SM004 (`maturity_date <= issue_date`)
and SM005 (unparseable dates) — both of which examine the master in isolation and never
compare it against another file. Closing the gap means four rules of the same shape as
HL009: a join to `security` and a date comparison.
