# Tests

The test suite uses Python's standard `unittest` runner. Most tests are
self-contained; files ending in `_integration.py` require a migrated PostGIS
database and are skipped when `TEST_DATABASE_URL` is absent.

## Files

| File | Coverage |
| --- | --- |
| `test_project_config.py` | YAML validation, path resolution, and PostGIS feature/review delegation |
| `test_fetch_overture_buildings.py` | Fused release discovery and selection, historical schema handling, COG bounds, clipping, H3 enrichment, and cache reuse |
| `test_frontend.py` | Static frontend separation, runtime config responses, MIME types, and browser-friendly startup URLs |
| `test_api.py` | API request-model validation, development authentication, response shaping, and migration checksums without a database |
| `test_api_integration.py` | Independent multi-reviewer annotations, annotation history, successful feature edits, and stale-version conflicts in PostGIS |
| `test_bootstrap.py` | Stable source identities and deterministic YAML-managed task IDs |
| `test_bootstrap_integration.py` | Transactional GeoParquet import, normalized H3 rows, assigned feature/review reads, database review writes, and preservation across repeated startup |

## Relationships

```text
test_project_config.py ----------> app.py + src/project_config.py
test_frontend.py ----------------> app.py + frontend/
test_fetch_overture_buildings.py -> src/fetch_overture_buildings.py
test_bootstrap.py ---------------> src/api/bootstrap.py
test_api.py ---------------------> src/api/{auth,main,migrate,models}.py

TEST_DATABASE_URL
    |
    +--> test_api_integration.py
    +--> test_bootstrap_integration.py
             |
             v
       migrated PostGIS
```

The two integration files create uniquely named projects, exercise real
transactions and constraints, and remove their current and history rows after
the tests.

## Running Tests

Run the full suite:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPYCACHEPREFIX=/tmp/damagemap-qaqc-pycache \
python -m unittest discover -s tests -v
```

With the Compose database running, include PostGIS integration coverage:

```bash
set -a
source .env
export DATABASE_URL="postgresql://${POSTGRES_USER}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}"
export TEST_DATABASE_URL="${DATABASE_URL}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
python -m src.api.migrate
python -m unittest discover -s tests -v
```

The integration tests assume the migrations have already been applied. They
must use a disposable development or test database, never a production
database.
