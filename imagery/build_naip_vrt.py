'''Build a small VRT that mosaics one NAIP year over the Camp Fire area.

The tiles stay on Microsoft Planetary Computer's public Azure storage
(anonymous reads work, no SAS token needed). The VRT only lists their URLs, so
the app reads them remotely through GDAL's /vsicurl/, one tile request at a time.

Run once per year; the result is `naip_<year>_camp.vrt` next to this script:

    env PYTHONNOUSERSITE=1 ~/miniforge3/envs/geo/bin/python -s imagery/build_naip_vrt.py 2018
'''

import json
import os
import sys
import urllib.request
from collections import Counter
from pathlib import Path

import pyproj

os.environ.setdefault('PROJ_DATA', pyproj.datadir.get_data_dir())
os.environ['GDAL_DISABLE_READDIR_ON_OPEN'] = 'EMPTY_DIR'
from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

# The corrected WaldoAir mosaic's extent in lon/lat (west, south, east, north).
BBOX = [-121.6516, 39.7242, -121.5582, 39.8042]
STAC = 'https://planetarycomputer.microsoft.com/api/stac/v1/search'

year = sys.argv[1] if len(sys.argv) > 1 else '2018'
out = Path(__file__).with_name(f'naip_{year}_camp.vrt')

query = {'collections': ['naip'], 'bbox': BBOX, 'datetime': f'{year}-01-01/{year}-12-31'}
request = urllib.request.Request(
    STAC, json.dumps(query).encode(), {'Content-Type': 'application/json'},
)
items = json.load(urllib.request.urlopen(request))['features']
urls = sorted('/vsicurl/' + item['assets']['image']['href'] for item in items)
for url in urls:
    print(url)
# Flight dates, so we know how long before the fire (2018-11-08) it was flown.
print(Counter(item['properties']['datetime'][:10] for item in items))

# Bands 1-3 are red, green, blue; band 4 (near infrared) is ignored by the app.
gdal.BuildVRT(str(out), urls).Close()
print(f'{len(urls)} tiles -> {out}')
