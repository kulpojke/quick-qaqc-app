#!/usr/bin/env python
"""Merge per-annotator CSV files into one wide annotation table."""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


ANNOTATION_FIELDS = [
    "id",
    "predicted_class",
    "annotation_label",
    "qa_status",
    "qa_correct_class",
    "qa_notes",
    "reviewer",
    "reviewed_at",
]


def reviewer_key(value: str) -> str:
    key = re.sub(r"[^0-9A-Za-z]+", "_", value.strip().lower()).strip("_")
    return key or "unknown_reviewer"


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
    for feature_id, feature_rows in grouped.items():
        decisions = {
            (row.get("annotation_label") or row.get("qa_correct_class", ""))
            for row in feature_rows
        }
        if len(decisions) > 1:
            conflicts[feature_id] = feature_rows

    return conflicts


def reviewer_fields(reviewers: list[str]) -> list[str]:
    fields = ["id", "predicted_class", "reviewer_count"]
    for reviewer in reviewers:
        key = reviewer_key(reviewer)
        fields.extend(
            [
                f"{key}_annotation_label",
                f"{key}_qa_status",
                f"{key}_qa_correct_class",
                f"{key}_qa_notes",
                f"{key}_reviewed_at",
            ]
        )
    return fields


def merge_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    latest_by_reviewer = {}
    for row in sorted(rows, key=lambda item: item.get("reviewed_at", "")):
        reviewer = row.get("reviewer", "") or "unknown_reviewer"
        latest_by_reviewer[(row["id"], reviewer)] = row

    grouped = defaultdict(dict)
    for (feature_id, reviewer), row in latest_by_reviewer.items():
        grouped[feature_id][reviewer] = row

    merged = []
    for feature_id, reviewer_rows in sorted(grouped.items()):
        output = {
            "id": feature_id,
            "predicted_class": "",
            "reviewer_count": str(len(reviewer_rows)),
        }
        for reviewer, row in sorted(reviewer_rows.items()):
            if row.get("predicted_class"):
                output["predicted_class"] = row["predicted_class"]
            key = reviewer_key(reviewer)
            output[f"{key}_annotation_label"] = (
                row.get("annotation_label") or row.get("qa_correct_class", "")
            )
            output[f"{key}_qa_status"] = row.get("qa_status", "")
            output[f"{key}_qa_correct_class"] = row.get("qa_correct_class", "")
            output[f"{key}_qa_notes"] = row.get("qa_notes", "")
            output[f"{key}_reviewed_at"] = row.get("reviewed_at", "")
        merged.append(output)

    return merged


def write_rows(rows: list[dict[str, str]], output_path: Path, fieldnames: list[str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge annotation CSV files.")
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
    reviewers = sorted({row.get("reviewer", "") or "unknown_reviewer" for row in rows})
    write_rows(merged, args.output, reviewer_fields(reviewers))

    print(f"Read {len(rows):,} annotations from {len(paths):,} files")
    print(f"Wrote {len(merged):,} merged annotations to {args.output}")
    print(f"Conflicting feature IDs: {len(conflicts):,}")


if __name__ == "__main__":
    main()
