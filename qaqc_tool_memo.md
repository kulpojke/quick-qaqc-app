# Damage Map QAQC Tool Memo

## Goal

Create a local QAQC tool for reviewing building damage model outputs. The tool should let technicians inspect building polygons on a map and record whether each model prediction is correct.

## Recommended Approach

Build a local app rather than a GitHub Pages-only app. A local app can write annotation files directly, which avoids adding a backend just to collect review results.

Good first implementation options:

- Streamlit
- Dash
- Panel

The app should read the existing processed building data:

```text
data/buildings_h3.geojson
```

and write human review output separately:

```text
data/qaqc/annotations_<reviewer>.csv
```

Do not make the annotation file the primary geometry/model data source. Keep model output and human QAQC separate, joined by Overture building `id`.

## Annotation Schema

Suggested fields:

```text
id
predicted_class
qa_status
qa_correct_class
qa_notes
reviewer
reviewed_at
```

Suggested values:

```text
qa_status: correct | not_correct | unsure
qa_correct_class: damaged | undamaged | unknown
```

## Workflow

1. Technician opens the local QAQC app.
2. App loads building polygons and model predictions.
3. Technician clicks a building polygon.
4. App shows model prediction and relevant metadata.
5. Technician records correct/not correct, optionally corrected class and notes.
6. App writes or updates that technician's annotation CSV.
7. Annotation CSVs are later merged across reviewers.

## Sync And Aggregation

Simplest multi-machine pattern:

```text
data/qaqc/annotations_alice.csv
data/qaqc/annotations_bob.csv
data/qaqc/annotations_chris.csv
```

Then add a merge script:

```text
src/merge_qaqc_annotations.py
```

The merge script should combine reviewer files, validate IDs, flag conflicts, and produce:

```text
data/qaqc/annotations_merged.csv
```

GitHub can be used for syncing these CSV files if the team is comfortable with commits and pull requests. If the workflow grows beyond that, consider a shared SQLite/DuckDB file, network storage, Supabase, or Postgres.

## Notes

The Overture building `id` appears stable and matches the source Overture building file, so it is a reasonable join key for annotations.

For a first pass, prioritize:

- map display similar to the current damage map
- click/select one building
- mark correct/not correct/unsure
- add optional corrected class and notes
- save annotations by reviewer
- export/merge CSVs
