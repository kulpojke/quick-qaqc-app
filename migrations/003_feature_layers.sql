CREATE TABLE feature_layers (
    project_id text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id text NOT NULL CHECK (id <> ''),
    name text NOT NULL CHECK (name <> ''),
    source_crs text NOT NULL CHECK (source_crs <> ''),
    geometry_types text[] NOT NULL,
    feature_id_field text NOT NULL CHECK (feature_id_field <> ''),
    fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    h3_prefix text NOT NULL DEFAULT 'h3_r' CHECK (h3_prefix <> ''),
    editing jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, id),
    CHECK (
        cardinality(geometry_types) > 0
        AND geometry_types <@ ARRAY[
            'Point',
            'MultiPoint',
            'Polygon',
            'MultiPolygon'
        ]::text[]
    )
);

-- *!*! Existing single-source projects become one conventional feature layer.
INSERT INTO feature_layers (
    project_id,
    id,
    name,
    source_crs,
    geometry_types,
    feature_id_field,
    fields,
    h3_prefix
)
SELECT
    id,
    'features',
    'Features',
    'EPSG:4326',
    ARRAY['Polygon', 'MultiPolygon'],
    'id',
    '{"feature_id": "id"}'::jsonb,
    'h3_r'
FROM projects;

ALTER TABLE features
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features',
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN deleted_by text;

ALTER TABLE feature_h3
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features';

ALTER TABLE tasks
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features',
    ADD COLUMN managed boolean NOT NULL DEFAULT false;

ALTER TABLE annotations
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features';

ALTER TABLE annotation_history
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features';

ALTER TABLE feature_history
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features',
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN deleted_by text;

ALTER TABLE feature_imports
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features',
    ADD COLUMN bounds_wgs84 double precision[];

ALTER TABLE export_state
    ADD COLUMN layer_id text NOT NULL DEFAULT 'features';

ALTER TABLE feature_h3
    DROP CONSTRAINT feature_h3_project_id_feature_id_fkey;

ALTER TABLE annotations
    DROP CONSTRAINT annotations_project_id_feature_id_fkey;

ALTER TABLE features
    DROP CONSTRAINT features_pkey,
    ADD PRIMARY KEY (project_id, layer_id, id),
    ADD FOREIGN KEY (project_id, layer_id)
        REFERENCES feature_layers(project_id, id) ON DELETE CASCADE;

ALTER TABLE feature_h3
    DROP CONSTRAINT feature_h3_pkey,
    ADD PRIMARY KEY (project_id, layer_id, feature_id, resolution),
    ADD FOREIGN KEY (project_id, layer_id, feature_id)
        REFERENCES features(project_id, layer_id, id) ON DELETE CASCADE;

ALTER TABLE tasks
    ADD UNIQUE (id, project_id, layer_id),
    ADD FOREIGN KEY (project_id, layer_id)
        REFERENCES feature_layers(project_id, id) ON DELETE CASCADE;

ALTER TABLE annotations
    DROP CONSTRAINT annotations_task_id_project_id_fkey,
    DROP CONSTRAINT annotations_task_id_feature_id_reviewer_id_key,
    ADD UNIQUE (task_id, layer_id, feature_id, reviewer_id),
    ADD FOREIGN KEY (task_id, project_id, layer_id)
        REFERENCES tasks(id, project_id, layer_id) ON DELETE CASCADE,
    ADD FOREIGN KEY (project_id, layer_id, feature_id)
        REFERENCES features(project_id, layer_id, id) ON DELETE CASCADE;

ALTER TABLE feature_imports
    DROP CONSTRAINT feature_imports_pkey,
    ADD PRIMARY KEY (project_id, layer_id),
    ADD FOREIGN KEY (project_id, layer_id)
        REFERENCES feature_layers(project_id, id) ON DELETE CASCADE;

ALTER TABLE export_state
    DROP CONSTRAINT export_state_pkey,
    ADD PRIMARY KEY (project_id, layer_id),
    ADD FOREIGN KEY (project_id, layer_id)
        REFERENCES feature_layers(project_id, id) ON DELETE CASCADE;

DROP INDEX feature_h3_lookup_idx;
CREATE INDEX feature_h3_lookup_idx
    ON feature_h3 (project_id, layer_id, resolution, h3_index, feature_id);

DROP INDEX features_geometry_gix;
CREATE INDEX features_geometry_gix
    ON features USING gist (geometry)
    WHERE deleted_at IS NULL;

DROP INDEX feature_history_lookup_idx;
CREATE INDEX feature_history_lookup_idx
    ON feature_history (project_id, layer_id, feature_id, feature_version);

-- *!*! Replace the original polygon-only check with supported vector types.
DO $$
DECLARE
    constraint_row record;
BEGIN
    FOR constraint_row IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'features'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%geometrytype%'
    LOOP
        EXECUTE format(
            'ALTER TABLE features DROP CONSTRAINT %I',
            constraint_row.conname
        );
    END LOOP;
END
$$;

ALTER TABLE features
    ADD CONSTRAINT features_geometry_type_check
    CHECK (
        GeometryType(geometry) IN (
            'POINT',
            'MULTIPOINT',
            'POLYGON',
            'MULTIPOLYGON'
        )
    );

CREATE OR REPLACE FUNCTION archive_feature_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO feature_history (
        project_id,
        layer_id,
        feature_id,
        geometry,
        properties,
        feature_version,
        changed_by,
        created_at,
        deleted_at,
        deleted_by
    ) VALUES (
        OLD.project_id,
        OLD.layer_id,
        OLD.id,
        OLD.geometry,
        OLD.properties,
        OLD.version,
        NEW.updated_by,
        now(),
        OLD.deleted_at,
        OLD.deleted_by
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER archive_feature_before_update ON features;
CREATE TRIGGER archive_feature_before_update
BEFORE UPDATE OF geometry, properties, deleted_at ON features
FOR EACH ROW
WHEN (
    OLD.geometry IS DISTINCT FROM NEW.geometry
    OR OLD.properties IS DISTINCT FROM NEW.properties
    OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at
)
EXECUTE FUNCTION archive_feature_update();

CREATE OR REPLACE FUNCTION archive_annotation_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO annotation_history (
        annotation_id,
        task_id,
        project_id,
        layer_id,
        feature_id,
        reviewer_id,
        label,
        notes,
        feature_version_seen,
        annotation_version,
        created_at,
        replaced_at
    ) VALUES (
        OLD.id,
        OLD.task_id,
        OLD.project_id,
        OLD.layer_id,
        OLD.feature_id,
        OLD.reviewer_id,
        OLD.label,
        OLD.notes,
        OLD.feature_version_seen,
        OLD.version,
        OLD.created_at,
        now()
    );
    RETURN NEW;
END;
$$;
