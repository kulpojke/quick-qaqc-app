#!/usr/bin/env python
"""Merge per-reviewer QAQC annotation CSV files."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


ANNOTATION_FIELDS = [
    "id",
    "predicted_class",
    "qa_status",
    "qa_correct_class",
    "qa_notes",
    "reviewer",
    "reviewed_at",
]


def read_rows(paths: list[Path]) -> list[dict[str, str]]:
    rows = []
    for path in paths:
        with path.open(newline="") as file:
            rows.extend(csv.DictReader(file))
    return rows


def find_conflicts(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["id"]].append(row)

    conflicts = {}
    for building_id, building_rows in grouped.items():
        decisions = {
            (row.get("qa_status", ""), row.get("qa_correct_class", ""))
            for row in building_rows
        }
        if len(decisions) > 1:
            conflicts[building_id] = building_rows

    return conflicts


def merge_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    merged = {}
    for row in sorted(rows, key=lambda item: item.get("reviewed_at", "")):
        merged[row["id"]] = {field: row.get(field, "") for field in ANNOTATION_FIELDS}
    return list(merged.values())


def write_rows(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=ANNOTATION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge QAQC annotation CSV files.")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/qaqc"),
        help="Directory containing annotations_*.csv files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/qaqc/annotations_merged.csv"),
        help="Merged annotation CSV path.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    paths = sorted(
        path for path in args.input_dir.glob("annotations_*.csv")
        if path.name != args.output.name
    )
    rows = read_rows(paths)
    conflicts = find_conflicts(rows)
    merged = merge_rows(rows)
    write_rows(merged, args.output)

    print(f"Read {len(rows):,} annotations from {len(paths):,} files")
    print(f"Wrote {len(merged):,} merged annotations to {args.output}")
    print(f"Conflicting building IDs: {len(conflicts):,}")


if __name__ == "__main__":
    main()
