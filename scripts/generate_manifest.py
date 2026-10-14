#!/usr/bin/env python3
"""Prepare yearly ZIPs, monthly gzip CSVs, the manifest, and download links."""

import argparse
from collections import defaultdict
import csv
import gzip
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit
import zipfile

import polars as pl


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"{timestamp} {message}", flush=True)


PUBLIC_BASE_URL = "https://victoriaparking.oimo.ca"


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compress_month(path, target):
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with path.open("rb") as source, partial.open("wb") as destination:
            # No filename or current timestamp in the gzip header: reproducible output.
            with gzip.GzipFile(fileobj=destination, mode="wb", filename="", mtime=0, compresslevel=6) as compressed:
                shutil.copyfileobj(source, compressed, length=1024 * 1024)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def compress_year(year, paths, target):
    partial = target.with_suffix(target.suffix + ".part")
    csv_digest = hashlib.sha256()
    csv_bytes = 0
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            info = zipfile.ZipInfo(f"parking-{year}.csv", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with archive.open(info, "w", force_zip64=True) as destination:
                for index, path in enumerate(paths):
                    with path.open("rb") as source:
                        if index:
                            source.readline()  # Keep the header from the first month only.
                        last = b""
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            destination.write(chunk)
                            csv_digest.update(chunk)
                            csv_bytes += len(chunk)
                            last = chunk[-1:]
                        if last and last != b"\n":
                            destination.write(b"\n")
                            csv_digest.update(b"\n")
                            csv_bytes += 1
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return csv_bytes, csv_digest.hexdigest()


def build_manifest(files, destination, downloads_destination, artifacts_dir, base_url=PUBLIC_BASE_URL):
    monthly_dir = artifacts_dir / "monthly"
    yearly_dir = artifacts_dir / "yearly"
    monthly_dir.mkdir(parents=True, exist_ok=True)
    yearly_dir.mkdir(parents=True, exist_ok=True)
    columns = None
    entries = []
    for path in files:
        with path.open(newline="") as stream:
            header = next(csv.reader(stream))
        if columns is not None and header != columns:
            raise ValueError(f"CSV headers differ: {path}")
        columns = header
        if "snapshot_at_utc" not in header:
            raise ValueError(f"Missing snapshot_at_utc column: {path}")
        stats = pl.scan_csv(path, infer_schema=False).select(
            pl.len().alias("rows"),
            pl.col("snapshot_at_utc").n_unique().alias("snapshots"),
            pl.col("snapshot_at_utc").min().alias("first"),
            pl.col("snapshot_at_utc").max().alias("last"),
        ).collect(engine="streaming").row(0, named=True)
        compressed = monthly_dir / (path.name + ".gz")
        compress_month(path, compressed)
        key = f"csv/monthly/{compressed.name}"
        entries.append(dict(
            month=path.stem.removeprefix("parking-"), key=key,
            download_url=f"{base_url.rstrip('/')}/{key}",
            bytes=compressed.stat().st_size, sha256=checksum(compressed),
            csv_filename=path.name, csv_bytes=path.stat().st_size, csv_sha256=checksum(path),
            rows=stats["rows"], snapshots=stats["snapshots"],
            first_snapshot_utc=stats["first"], last_snapshot_utc=stats["last"],
        ))
        log(f"Prepared {path.name}: {stats['rows']:,} rows")
    years = defaultdict(list)
    for path, entry in zip(files, entries):
        years[entry["month"][:4]].append((path, entry))
    yearly = []
    for year, items in sorted(years.items()):
        target = yearly_dir / f"parking-{year}.zip"
        csv_bytes, csv_sha256 = compress_year(year, [path for path, _ in items], target)
        key = f"csv/yearly/{target.name}"
        first_times = [entry["first_snapshot_utc"] for _, entry in items if entry["first_snapshot_utc"] is not None]
        last_times = [entry["last_snapshot_utc"] for _, entry in items if entry["last_snapshot_utc"] is not None]
        yearly.append(dict(
            year=year, key=key, download_url=f"{base_url.rstrip('/')}/{key}",
            bytes=target.stat().st_size, sha256=checksum(target),
            csv_filename=f"parking-{year}.csv", csv_bytes=csv_bytes, csv_sha256=csv_sha256,
            months=[entry["month"] for _, entry in items],
            rows=sum(entry["rows"] for _, entry in items),
            snapshots=sum(entry["snapshots"] for _, entry in items),
            first_snapshot_utc=min(first_times) if first_times else None,
            last_snapshot_utc=max(last_times) if last_times else None,
        ))
        log(f"Prepared {target.name}: {target.stat().st_size / 1_000_000:.1f} MB")

    manifest = dict(
        generated_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        columns=columns, month_timezone="America/Vancouver",
        timestamp_source="Snapshot filename; sampled lastUpdatedUTC values are empty",
        files=entries, yearly_files=yearly,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        partial.write_text(json.dumps(manifest, indent=2) + "\n")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)

    downloads = "# Download Victoria parking data\n\n## Yearly downloads\n\n"
    downloads += "Each ZIP contains one combined CSV for the listed months. Extract it after downloading.\n\n"
    downloads += "\n".join(
        f"- [{entry['year']} ZIP]({entry['download_url']}) — {entry['bytes'] / 1_000_000:.1f} MB; "
        f"{entry['rows']:,} rows; {entry['months'][0]} through {entry['months'][-1]}"
        for entry in yearly
    )
    downloads += "\n\n## Monthly downloads\n\nSmaller files for individual months. Each gzip file extracts to one CSV.\n\n"
    downloads += "\n".join(
        f"- [{entry['month']} CSV.gz]({entry['download_url']}) — {entry['bytes'] / 1_000_000:.1f} MB"
        for entry in entries
    )
    downloads += "\n\nGenerated by `scripts/generate_manifest.py`.\n"
    downloads_destination.parent.mkdir(parents=True, exist_ok=True)
    downloads_destination.write_text(downloads)
    # Remove old generated packages so publishing cannot pick up retired months/years.
    expected = {Path(entry["key"]).name for entry in entries + yearly}
    for directory, pattern in ((monthly_dir, "parking-*.csv.gz"), (yearly_dir, "parking-*.zip")):
        for path in directory.glob(pattern):
            if path.name not in expected:
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, default=Path("data/monthly"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("data/public"))
    parser.add_argument("--output", type=Path, default=Path("manifest.json"))
    parser.add_argument("--downloads-output", type=Path, default=Path("downloads.md"))
    parser.add_argument("--base-url", default=PUBLIC_BASE_URL, help="Public download base URL")
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme != "https" or not url.hostname or url.query or url.fragment or url.username or url.password:
        parser.error("--base-url must be an HTTPS URL without credentials, query, or fragment")
    try:
        files = sorted(path for path in args.csv_dir.glob("parking-*.csv")
                       if re.fullmatch(r"parking-\d{4}-(0[1-9]|1[0-2])\.csv", path.name))
        if not files:
            raise ValueError(f"No monthly CSV files found in {args.csv_dir}")
        build_manifest(files, args.output, args.downloads_output, args.artifacts_dir, args.base_url)
        log(f"Generated {args.output} and {args.downloads_output}")
        return 0
    except (pl.exceptions.PolarsError, ValueError, OSError, StopIteration) as exc:
        print(f"Manifest generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
