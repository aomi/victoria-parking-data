#!/usr/bin/env python3
"""Process local parking JSON snapshots into monthly CSVs with Polars."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import polars as pl


# Explicit fields keep missing values nullable and ignore unrelated JSON fields.
FIELDS = {
    "custID": ("customer_id", pl.Int64),
    "lotID": ("lot_id", pl.Int64),
    "type": ("type", pl.String),
    "occupied": ("occupied", pl.Int64),
    "empty": ("available", pl.Int64),
    "total": ("total", pl.Int64),
    "valid": ("valid", pl.Boolean),
    "percentFull": ("percent_full", pl.Float64),
    "full": ("is_full", pl.Boolean),
}
# Read scalar values as strings, then cast strictly in transform(). This handles
# integer/float JSON representations consistently while keeping missing fields null.
RECORD_SCHEMA = {name: pl.String for name in ("name", "address", "geo", *FIELDS)}
SNAPSHOT_SCHEMA = {**RECORD_SCHEMA, "children": pl.List(pl.Struct(RECORD_SCHEMA))}
SORT_COLUMNS = ["snapshot_at_utc", "parkade_id", "record_kind", "lot_id", "type", "source_file"]


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"{timestamp} {message}", flush=True)


def list_snapshots(raw_dir, month):
    months = defaultdict(list)
    vancouver = ZoneInfo("America/Vancouver")
    for path in raw_dir.rglob("*.json"):
        try:
            timestamp = datetime.fromisoformat(path.stem.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError(f"Cannot extract an ISO timestamp from: {path}") from exc
        local_month = timestamp.astimezone(vancouver).strftime("%Y-%m")
        if month is None or month == local_month:
            months[local_month].append((timestamp, path))
    for snapshots in months.values():
        snapshots.sort(key=lambda item: (item[0], str(item[1])))
    return months


def read_batch(snapshots):
    frames = []
    for timestamp, path in snapshots:
        try:
            frame = pl.read_json(path, schema=SNAPSHOT_SCHEMA).with_columns(
                pl.lit(timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")).alias("snapshot_at_utc"),
                pl.lit(str(path)).alias("source_file"),
            )
            frames.append(frame)
        except pl.exceptions.PolarsError as exc:
            raise ValueError(f"{path}: {exc}") from exc
    return pl.concat(frames)


def flatten(frame):
    # Parent metadata stays beside the record so sections can inherit location.
    parents = frame.select(
        "snapshot_at_utc", "source_file", "children",
        pl.col("lotID").alias("parkade_id"),
        pl.col("address").alias("parent_address"),
        pl.col("geo").alias("parent_geo"),
        pl.struct(list(RECORD_SCHEMA)).alias("record"),
    )
    parkades = parents.drop("children").with_columns(pl.lit("parkade").alias("record_kind"))
    sections = (
        parents.filter(pl.col("children").list.len() > 0)
        .drop("record")
        .explode("children")
        .rename({"children": "record"})
        .with_columns(pl.lit("section").alias("record_kind"))
        .select(parkades.columns)
    )
    records = pl.concat([parkades, sections]).unnest("record")
    missing = records.filter(
        pl.any_horizontal(pl.col("parkade_id", "lotID", "type").is_null())
    )
    if missing.height:
        raise ValueError(f"Missing required parkade ID, lot ID or type in: {missing['source_file'][0]}")
    return records.with_columns(
        *[pl.coalesce(
            pl.col(field).str.strip_chars().replace("", None),
            pl.col(f"parent_{field}"),
        ).alias(field) for field in ("address", "geo")]
    )


def boolean(field):
    return pl.col(field).str.to_lowercase().replace_strict(
        {"true": True, "false": False, "1": True, "0": False,
         "t": True, "f": False, "yes": True, "no": False},
        return_dtype=pl.Boolean,
    )


def transform(frame):
    frame = flatten(frame)
    coordinates = pl.col("geo").str.split(",")
    def coordinate(index):
        return coordinates.list.get(index, null_on_oob=True).str.strip_chars().replace("", None).cast(pl.Float64)

    return frame.select(
        "snapshot_at_utc", "record_kind",
        pl.col("parkade_id").cast(pl.Int64),
        "name", "address",
        coordinate(0).alias("latitude"),
        coordinate(1).alias("longitude"),
        *[(boolean(field) if dtype == pl.Boolean else pl.col(field).cast(dtype)).alias(name)
          for field, (name, dtype) in FIELDS.items()],
        "source_file",
    ).sort(SORT_COLUMNS, nulls_last=True).drop("source_file")


def batches(snapshots, size):
    start = 0
    while start < len(snapshots):
        end = min(start + size, len(snapshots))
        # Keep equal timestamps together so concatenated batches stay globally sorted.
        while end < len(snapshots) and snapshots[end][0] == snapshots[end - 1][0]:
            end += 1
        yield snapshots[start:end]
        start = end


def process(raw_dir, output_dir, month, batch_size):
    months = list_snapshots(raw_dir, month)
    if not months:
        log("No matching JSON snapshots found.")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for month, snapshots in sorted(months.items()):
        target = output_dir / f"parking-{month}.csv"
        partial = target.with_suffix(".csv.part")
        log(f"{month}: processing {len(snapshots):,} snapshots with Polars")
        count = completed = 0
        try:
            with partial.open("wb") as stream:
                for batch_number, batch in enumerate(batches(snapshots, batch_size)):
                    frame = transform(read_batch(batch))
                    frame.write_csv(stream, include_header=(batch_number == 0))
                    count += frame.height
                    completed += len(batch)
                    log(f"{month}: {completed:,}/{len(snapshots):,} snapshots, {count:,} rows")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        log(f"{month}: CSV complete ({count:,} rows) -> {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/monthly"))
    parser.add_argument("--month", help="Vancouver calendar month, YYYY-MM")
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args()
    if args.month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        parser.error("--month must be YYYY-MM")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    try:
        raw_dir = args.raw_dir.resolve()
        if not raw_dir.is_dir():
            raise ValueError(f"JSON directory does not exist: {raw_dir}")
        process(raw_dir, args.output_dir.resolve(), args.month, args.batch_size)
        return 0
    except (pl.exceptions.PolarsError, ValueError, OSError) as exc:
        print(f"Processing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
