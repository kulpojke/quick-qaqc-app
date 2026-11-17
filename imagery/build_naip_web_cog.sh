#!/usr/bin/env bash
# Build the NAIP 2018 (flown 2018-07-16/18, pre-fire) web COG for the Camp Fire
# app and record how it was made. Uses the tilestream env's GDAL 3.13.
#
# Input: our local NAIP 2018 quarter-quads (60 cm, NAD83 / UTM 10N, RGB + NIR).
# Output: a 3857 GoogleMapsCompatible JPEG COG cut to the WaldoAir v3 extent,
# uploaded to r2:cogs/naip-campfire/naip_2018_camp_web.tif.
set -euo pipefail

GDAL=~/miniforge3/envs/tilestream/bin
NAIP=/home/michael/Work/detection/tmp_symlinks/camp_fire_inputs/naip_2018/naip_2018.vrt
OUT=/media/disk1/naip_campfire_cog
mkdir -p "$OUT"

# 1. Label the NAD83 tiles as WGS84 (EPSG:32610). Without this, PROJ picks
#    the NAD83 -> WGS84 HARN grid shift for northern California, cannot load
#    the grid, and the warp comes out empty. NAD83 and WGS84 differ by about
#    1 m here; the rest of our data (lidar, buildings) treats them the same.
"$GDAL"/gdal_translate -q -of VRT -a_srs EPSG:32610 "$NAIP" "$OUT"/naip_2018_utm_wgs84.vrt

# 2. Cut to the WaldoAir v3 bounds (EPSG:3857), keep red/green/blue, and
#    reproject once onto the web map tile grid, so the app only cuts tiles.
"$GDAL"/gdal_translate -q -of COG -b 1 -b 2 -b 3 \
    -projwin -13542195.43 4838158.14 -13531188.5 4825928.22 -projwin_srs EPSG:3857 \
    "$OUT"/naip_2018_utm_wgs84.vrt "$OUT"/naip_2018_camp_web.tif \
    -co TILING_SCHEME=GoogleMapsCompatible -co COMPRESS=JPEG -co QUALITY=90 \
    -co OVERVIEW_QUALITY=90 -co RESAMPLING=BILINEAR -co BLOCKSIZE=256 \
    -co NUM_THREADS=ALL_CPUS

# 3. Upload next to, never over, existing objects.
# rclone copyto --immutable "$OUT"/naip_2018_camp_web.tif r2:cogs/naip-campfire/naip_2018_camp_web.tif
