

## Data requirements

The application requires three inputs:

1. a GeoJSON (in EPSG:4326) that  has:
    + polygon or multipolygon geometries
    + an id field
    + H3 columns h3_r5 through h3_r10


2. An annotation label list entered in the app, or an optional label field in the GeoJSON whose unique values can populate the annotation buttons.

3. A COG of imagery for the annotator to use as comparison.

## Helper scripts

Chances are you do not have H3 indexes  attached to your polygons.  You can use `src/add_h3_indexes.py` to attach them.

For building a COG of imagery , `src/build_cog.py` has been provided

The file `src/merge_qaqc_annotations.py` combines per-annotator CSVs into one wide CSV keyed by feature id, preserving each annotator’s latest annotation label, notes, and timestamp in annotator-specific columns.



## Usage

Build the conda environment:

```bash
conda env create -f environment.yml
conda activate damagemap-qaqc
```

If your polygons do not already have H3 columns, add them first:

```bash
python src/add_h3_indexes.py --input path/to/features.geojson --output path/to/features_h3.geojson
```

If needed, build a COG from a directory of TIFF imagery:

```bash
python src/build_cog.py path/to/imagery_dir
```

Run the app:

```bash
python app.py
```

Open `http://127.0.0.1:8501` in a web browser, set the feature GeoJSON, annotation labels, optional local COG path or COG URL, and annotator name. The label field is optional; when supplied, its unique values are used as annotation button options. If class probability is available, you can select that field from the GeoJSON and filter features by model confidence.

The imagery should appear as well as hexagonal grid cells. Grid cells only appear where features are present. The grid will change scale when you zoom. Zoom to the desired level and select a grid cell by clicking it. Use escape to exit a selected grid cell.

The cell will appear as a pink outline. A green circle will show the location of the first feature to annotate.

To begin annotating features, look at the image within the circle, decide what the class is, and pick it from the available buttons under "Annotation Label". When you save the annotation, it will jump to the next feature.  Repeat this process until all features in the hexgrid are complete.  When you are done, the app will exit the hexgrid and zoom back out.

Hexgrid colors will change based on completion.
