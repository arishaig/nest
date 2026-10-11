#!/usr/bin/env python3
"""Prove a SQLite -> Postgres copy lost nothing.

Independent of pgloader: reads every table on both sides, normalises each
value by the Postgres column type, and compares the multiset of row digests
per table (order-independent, so collation differences can't matter).

Lossy matches allowed, each counted and reported per column:
- the 7th fractional digit of a timestamp: .NET writes 100ns ticks and
  Postgres stores microseconds, rounding exactly as pg_round_us() does;
- a SQLite double in a column the app's own Postgres schema declares `real`,
  compared at float32.
Anything else that differs fails, including a timestamp off by the rounding
itself.

Also checks: same table and column sets, schema-version table equal, and every
identity/serial sequence at or past max(id).

Usage: verify.py <clean.db> <postgres dsn> [--show-values] [--skip-tables a,b]
--skip-tables names app-owned tables that legitimately differ between the two
databases (Seerr's per-dialect TypeORM `migrations`); they are not compared.
Failure output names differing columns only; --show-values prints the values
too (they can include API keys, so only on a trusted terminal).
Exit 0 only if every check passes.
"""
import collections
import datetime as dt
import decimal
import hashlib
import re
import sqlite3
import struct
import sys

import psycopg

SAMPLE = 5
SHOW_VALUES = "--show-values" in sys.argv
if SHOW_VALUES:
    sys.argv.remove("--show-values")
SKIP_TABLES = set()
if "--skip-tables" in sys.argv:
    i = sys.argv.index("--skip-tables")
    SKIP_TABLES = {t for t in sys.argv[i + 1].split(",") if t}
    del sys.argv[i:i + 2]
TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


class Fail(Exception):
    pass


def parse_ts(s):
    """SQLite timestamp text -> (naive UTC datetime, whole seconds, fraction digits)."""
    m = TS_RE.match(s.strip())
    if not m:
        raise Fail(f"unparseable timestamp {s!r}")
    date, time, frac, tz = m.groups()
    frac = frac or ""
    if len(frac) > 7:
        raise Fail(f"timestamp finer than 100ns {s!r}")
    base = dt.datetime.fromisoformat(f"{date}T{time}")
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        base -= sign * dt.timedelta(hours=hh, minutes=mm)
    return base, frac


def pg_round_us(frac):
    """Microseconds exactly as Postgres derives them from fraction digits.

    Postgres parses the fraction with strtod() and stores rint(frac * 1e6),
    so the result follows binary floating point, not decimal rounding
    (.5054715 -> 505471, .0637965 -> 63797). Python's float and round()
    (half-to-even on the double) reproduce that bit for bit.
    """
    if not frac:
        return 0
    return round(float("0." + frac) * 1000000)


def pg_ts_to_naive_utc(v):
    if v.tzinfo is not None:
        v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return v


class Table:
    def __init__(self, name, cols):
        self.name = name
        self.cols = cols  # [(name, pg_type)]
        self.lossy = collections.Counter()


def digest(parts):
    return hashlib.sha256(repr(parts).encode()).digest()


def compare_table(lite, pg, t):
    names = [c for c, _ in t.cols]
    q_lite = "SELECT " + ", ".join(f'"{c}"' for c in names) + f' FROM "{t.name}"'
    q_pg = "SELECT " + ", ".join(f'"{c}"' for c in names) + f' FROM public."{t.name}"'
    s_rows, p_rows = collections.Counter(), collections.Counter()
    s_sample, p_sample = {}, {}
    pending = list(lite.execute(q_lite))
    with pg.cursor(name=f"v_{abs(hash(t.name))}") as cur:
        cur.itersize = 5000
        cur.execute(q_pg)
        p_all = list(cur)
    if len(pending) != len(p_all):
        raise Fail(f"{t.name}: row count SQLite={len(pending)} Postgres={len(p_all)}")
    # Rows are compared as multisets of digests, so order never matters.
    for row in pending:
        d = digest([norm_side(t, c, ty, v, "s") for (c, ty), v in zip(t.cols, row)])
        s_rows[d] += 1
        s_sample.setdefault(d, row)
    for row in p_all:
        parts = [norm_side(t, c, ty, v, "p") for (c, ty), v in zip(t.cols, row)]
        d = digest(parts)
        p_rows[d] += 1
        p_sample.setdefault(d, row)
    only_s = s_rows - p_rows
    only_p = p_rows - s_rows
    if only_s or only_p:
        msg = [f"{t.name}: {sum(only_s.values())} SQLite rows unmatched, "
               f"{sum(only_p.values())} Postgres rows unmatched"]
        # Pair unmatched rows by their first column (the Id for every *arr
        # table) and name the differing columns. Values stay hidden unless
        # --show-values: rows hold API keys and indexer URLs.
        p_by_key = {p_sample[d][0]: p_sample[d] for d in only_p}
        for d in list(only_s)[:SAMPLE]:
            srow = s_sample[d]
            prow = p_by_key.get(srow[0])
            if prow is None:
                msg.append(f"  {names[0]}={srow[0]!r}: no Postgres row with this key")
                continue
            diff = []
            for (c, ty), sv, pv in zip(t.cols, srow, prow):
                if norm_side(t, c, ty, sv, "s") != norm_side(t, c, ty, pv, "p"):
                    diff.append(f"{c} ({ty})" + (f": {sv!r} vs {pv!r}" if SHOW_VALUES else ""))
            msg.append(f"  {names[0]}={srow[0]!r}: differs in " + "; ".join(diff))
        raise Fail("\n".join(msg))
    return len(pending)


def norm_side(t, col, ty, v, side):
    """Normalise one side to a canonical form both sides can reach."""
    if v is None:
        return None
    if ty.startswith("timestamp"):
        if side == "s":
            if isinstance(v, (int, float)):
                raise Fail(f"{t.name}.{col}: numeric timestamp {v!r} in SQLite")
            base, frac = parse_ts(v)
            us = pg_round_us(frac)
            if len(frac) > 6 and frac[6:].strip("0"):
                t.lossy[col] += 1
            epoch_s = (base - dt.datetime(1970, 1, 1)) // dt.timedelta(seconds=1)
            return ("ts", epoch_s * 1000000 + us)
        p = pg_ts_to_naive_utc(v)
        return ("ts", (p - dt.datetime(1970, 1, 1)) // dt.timedelta(microseconds=1))
    if ty == "boolean":
        if side == "s" and isinstance(v, str):
            m = {"true": 1, "false": 0, "1": 1, "0": 0, "t": 1, "f": 0}
            if v.lower() not in m:
                raise Fail(f"{t.name}.{col}: non-boolean {v!r}")
            v = m[v.lower()]
        return ("b", bool(int(v)))
    if ty == "date":
        return ("d", str(v)[:10] if side == "s" else v.isoformat())
    if ty in ("smallint", "integer", "bigint"):
        if side == "s" and isinstance(v, (str, bytes, float)):
            if isinstance(v, float) and v.is_integer():
                return ("i", int(v))
            raise Fail(f"{t.name}.{col}: non-integer {v!r} in integer column")
        return ("i", int(v))
    if ty == "real":
        # float4. Postgres sends the shortest text that round-trips at single
        # precision ("10.86"), so compare both sides as float32. A SQLite
        # double that float32 can't hold exactly is counted as lossy.
        f = struct.unpack("f", struct.pack("f", float(v)))[0]
        if side == "s" and f != float(v):
            t.lossy[col] += 1
        return ("f4", f)
    if ty == "double precision":
        return ("f", float(v))
    if ty == "numeric":
        return ("n", decimal.Decimal(str(v)).normalize())
    if ty == "bytea":
        if side == "s" and isinstance(v, str):
            v = v.encode()
        return ("x", bytes(v))
    if ty in ("text", "character varying", "character"):
        if isinstance(v, (bytes, memoryview)):
            raise Fail(f"{t.name}.{col}: binary value in text column")
        if side == "s" and not isinstance(v, str):
            # SQLite type affinity can store a number in a TEXT column; Postgres
            # holds its text form. Compare text, but only for exact renderings.
            v = str(v)
        return ("t", v)
    if ty in ("json", "jsonb", "uuid"):
        return ("t", str(v))
    raise Fail(f"{t.name}.{col}: unhandled Postgres type {ty}")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    lite = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
    lite.text_factory = str
    pg = psycopg.connect(sys.argv[2])
    failures, lossy_total, rows_total = [], 0, 0

    s_tables = {r[0] for r in lite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} - SKIP_TABLES
    p_tables = {r[0] for r in pg.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'")} - SKIP_TABLES
    if SKIP_TABLES:
        print(f"skipping app-owned tables: {', '.join(sorted(SKIP_TABLES))}")
    for name in sorted(p_tables - s_tables):
        n = pg.execute(f'SELECT count(*) FROM public."{name}"').fetchone()[0]
        if n:
            failures.append(f"{name}: only in Postgres, has {n} rows")
    for name in sorted(s_tables - p_tables):
        n = lite.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        failures.append(f"{name}: missing in Postgres ({n} SQLite rows)")

    for name in sorted(s_tables & p_tables):
        s_cols = [r[1] for r in lite.execute(f'PRAGMA table_info("{name}")')]
        p_cols = dict(pg.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (name,)).fetchall())
        if set(s_cols) != set(p_cols):
            failures.append(f"{name}: columns differ; SQLite-only "
                            f"{sorted(set(s_cols) - set(p_cols))}, Postgres-only "
                            f"{sorted(set(p_cols) - set(s_cols))}")
            continue
        t = Table(name, [(c, p_cols[c]) for c in s_cols])
        try:
            n = compare_table(lite, pg, t)
            rows_total += n
            status = f"ok   {name}: {n} rows"
            if t.lossy:
                lossy_total += sum(t.lossy.values())
                status += "  (precision lost: " + ", ".join(
                    f"{c}={k}" for c, k in sorted(t.lossy.items())) + ")"
            print(status)
        except Fail as e:
            failures.append(str(e))
            print(f"FAIL {name}")

    # Sequences: every owned sequence must be at or past max(column).
    seqs = pg.execute("""
        SELECT s.relname, t.relname, a.attname
        FROM pg_class s
        JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
        JOIN pg_namespace n ON n.oid = s.relnamespace
        WHERE s.relkind = 'S' AND n.nspname = 'public'""").fetchall()
    for seq, tbl, col in seqs:
        last, called = pg.execute(f'SELECT last_value, is_called FROM public."{seq}"').fetchone()
        mx = pg.execute(f'SELECT max("{col}") FROM public."{tbl}"').fetchone()[0]
        nxt = last + 1 if called else last
        if mx is not None and nxt <= mx:
            failures.append(f"sequence {seq}: next value {nxt} <= max({tbl}.{col}) {mx}")

    print(f"\n{len(s_tables & p_tables)} tables, {rows_total} rows compared, "
          f"{lossy_total} values lost precision (7th timestamp digit or float4), "
          f"{len(seqs)} sequences checked")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"- {f}")
        sys.exit(1)
    print("PASS: every row present and equal")


if __name__ == "__main__":
    main()
