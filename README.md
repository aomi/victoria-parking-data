# Victoria parking data

This public repository contains historical parking availability data for Victoria, British Columbia.
The data comes from snapshots of the City of Victoria's parking website and is provided as monthly CSV files
for use in spreadsheets, analysis tools, or your own code.

How the raw data was transformed is explained in this document below.

## Download the data

**[Browse downloads](downloads.md)** for yearly ZIPs and individual monthly files.
Each yearly ZIP contains one combined CSV; monthly `.csv.gz` files each extract
to one CSV. The data contains individual observations, rather than averages.

The download list is generated automatically alongside the [file manifest](manifest.json),
which includes direct download URLs, file sizes, row counts, time ranges, and
SHA-256 checksums. The manifest records both compressed download sizes/checksums
and those of the extracted CSVs.

The first and last months may cover only part of a month. Collection gaps may
also occur; a monthly file is not a guarantee of uninterrupted coverage.

Each row represents one parking location or section at one observation time.
A single observation can therefore produce several rows. Summing `available`
across different times does not give the number of spaces available at one time. Filter by section/parkade as appropriate.

## Understand the columns

| Column | Meaning |
| --- | --- |
| `snapshot_at_utc` | When the snapshot was collected, expressed in UTC. |
| `record_kind` | `parkade` for a whole facility, or `section` for a section within it. |
| `parkade_id` | Identifier of the parent parking facility. |
| `name` | Name reported for this facility or section. |
| `address` | Reported address, using the parent address when a section has none. |
| `latitude`, `longitude` | Location coordinates, using the parent's when a section has none. |
| `customer_id` | Customer identifier from the source data. |
| `lot_id` | Identifier of this individual facility or section. |
| `type` | Record type reported by the source, such as `MAIN` or `GENERAL`. |
| `occupied` | Reported number of occupied spaces. |
| `available` | Reported number of free spaces, called `empty` in the original data. |
| `total` | Reported capacity. |
| `valid` | Validity flag supplied by the source. |
| `percent_full` | Reported percentage occupied; for example, `75` means 75%. |
| `is_full` | Full flag supplied by the source. |

Whole-facility totals and their sections overlap. Use `parkade` rows to compare
whole facilities, or `section` rows to explore their sections. Adding both kinds
together double counts capacity.

Blank cells represent missing values, rather than zero. Negative availability,
occupancy, or percentages are preserved as reported by the source. The conversion
does not correct these values or recalculate the reported percentages and flags.

Files are grouped by the calendar month in Victoria (`America/Vancouver`), while
the timestamp column uses UTC. For example, `2025-12-01T07:59:59.000000Z` is
November 30 at 11:59:59 p.m. in Victoria, so it belongs in the November file.
The `Z` at the end of a timestamp means UTC. Victoria is seven hours behind UTC
during daylight saving time and eight hours behind during standard time.

## How the data is transformed

The original snapshots are JSON files containing whole parking facilities and
their sections. The standalone [conversion script](scripts/process_parking.py):

1. Reads the timestamp from each JSON filename and assigns it to a Victoria
   calendar month. This is the collection time, not a separate update time for
   each facility.
2. Creates one `parkade` row for each facility and one `section` row for each of
   its immediate sections.
3. Keeps each record's own name. If a section's address or coordinates are missing
   or blank, it uses those of the parent facility.
4. Splits coordinates into latitude and longitude, renames fields into the CSV
   columns above, and preserves reported values and missing data.
5. Sorts observations by time and facility/section identifiers, then writes one
   CSV per month.

Invalid JSON, invalid timestamp filenames, incompatible field values, or missing
required identifiers or record types stop processing instead of silently dropping
records. Each monthly output replaces its previous version only after that month
has finished processing successfully.

## Reproduce and maintain the dataset

This section is for maintainers and anyone curious about running the data
pipeline. Run commands from the repository root.

### Setup

Install [uv](https://docs.astral.sh/uv/) and [rclone](https://rclone.org/install/),
then install the locked Python dependencies:

```sh
uv sync --locked
```

Python 3.12 is selected by default. The Python scripts use Polars and are
standalone, with no imports from other repository files.

For R2 transfers, provide these variables through your shell or secret manager:

- `R2_ACCOUNT_ID`: Cloudflare account ID.
- `R2_ACCESS_KEY_ID`: R2 S3 access key ID.
- `R2_SECRET_ACCESS_KEY`: R2 S3 secret access key.

Downloading original snapshots requires read/list access to `victoriaparking`.
Publishing requires read/write access to `victoria-parking-public`. The public
CSV downloads above do not require credentials. Transfer scripts configure rclone
through environment variables; credentials are not written to a config file.

### Download the original snapshots

The [rclone downloader](scripts/download_parking.sh) copies the original
JSON archive into `data/raw/`:

```sh
bash scripts/download_parking.sh
```

Rerun to resume an interrupted download. It preserves local files and skips
objects whose sizes match; this assumes timestamped snapshots are immutable.
Use `PARKING_RAW_DIR` to change the local destination or `PARKING_BUCKET` to change
the source bucket. Additional rclone flags can be passed through:

```sh
bash scripts/download_parking.sh --dry-run
```

### Convert JSON to monthly CSVs

```sh
uv run python scripts/process_parking.py
```

The processor reads `data/raw/` and writes `data/monthly/`. To change paths or
process one Victoria calendar month:

```sh
uv run python scripts/process_parking.py --raw-dir /path/to/json --output-dir /path/to/csv
uv run python scripts/process_parking.py --month 2025-12
```

Processing runs offline in batches of 128 files. Use `--batch-size 32` to reduce
batch size if memory is limited. Run only one conversion at a time against the
same output directory.

### Prepare compressed downloads and the manifest

The [preparation script](scripts/generate_manifest.py) creates yearly ZIPs with
one combined CSV per year, plus individual monthly gzip files in `data/public/`.
It calculates file sizes, SHA-256 checksums, row counts, observation counts, and
time ranges, then updates
`manifest.json` and the clickable `downloads.md` list in this repository.
It runs offline and needs no R2 credentials:

```sh
uv run python scripts/generate_manifest.py
```

Use `--csv-dir PATH`, `--artifacts-dir PATH`, and `--output PATH` to change the
source directory, compressed download directory, and
manifest destination, and `--downloads-output PATH` for the download list.
Use `--base-url URL` to override the production download domain,
`https://victoriaparking.oimo.ca`. Regenerate both after changing any monthly CSVs.

### Publish to R2

The [rclone publisher](scripts/publish_parking.sh) uploads the prepared
yearly ZIPs, monthly gzip files, and manifest:

```sh
bash scripts/publish_parking.sh
```

It writes yearly ZIPs to `csv/yearly/parking-YYYY.zip` and monthly gzip files to
`csv/monthly/parking-YYYY-MM.csv.gz` in `victoria-parking-public`. The manifest
is uploaded to `csv/manifest.json` after all compressed uploads succeed.
It replaces matching objects and leaves other objects in the bucket intact.
Publishing uses rclone only; it does not regenerate the manifest.

Use `PARKING_ARTIFACTS_DIR`, `PARKING_MANIFEST`, and `PARKING_PUBLIC_BUCKET` to
change the local compressed download directory, manifest path, and destination
bucket. Additional rclone
flags apply to all transfers, for example:

```sh
bash scripts/publish_parking.sh --dry-run
bash scripts/publish_parking.sh --transfers 8
```

Commit the updated `manifest.json` and `downloads.md` when adding or changing
months. Local snapshots and generated CSVs are ignored by Git.
