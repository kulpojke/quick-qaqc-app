

## Usage

The application requires three inputs:

1. a GeoJSON (in EPSG:4326) that  has:
    + polygon or multipolygon geometries
    + an id field
    + H3 columns h3_r5 through h3_r10


2. A csv of labels with:
    + an id field matching the GeoJson
    + a label field

3. A COG of imagery for the reviewer to use as comparison.

Chances are you do not have H3 indexes  attached to your polygons.  You can use `src/add_h3_indexes.py` to attach them.

For building a COG of imagery , `src/build_cog.py` has been provided