CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE projects (
    id text PRIMARY KEY CHECK (id <> ''),
    name text NOT NULL CHECK (name <> ''),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE features (
    project_id text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id text NOT NULL CHECK (id <> ''),
    geometry geometry(Geometry, 4326) NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, id),
    CHECK (ST_IsValid(geometry)),
    CHECK (NOT ST_IsEmpty(geometry)),
    CHECK (GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON'))
);

CREATE INDEX features_geometry_gix ON features USING gist (geometry);

CREATE TABLE feature_h3 (
    project_id text NOT NULL,
    feature_id text NOT NULL,
    resolution smallint NOT NULL CHECK (resolution BETWEEN 0 AND 15),
    h3_index text NOT NULL CHECK (h3_index <> ''),
    PRIMARY KEY (project_id, feature_id, resolution),
    FOREIGN KEY (project_id, feature_id)
        REFERENCES features(project_id, id) ON DELETE CASCADE
);

CREATE INDEX feature_h3_lookup_idx
    ON feature_h3 (project_id, resolution, h3_index, feature_id);

CREATE TABLE tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (name <> ''),
    mode text NOT NULL CHECK (mode IN ('annotation', 'qaqc', 'editing')),
    labels text[] NOT NULL DEFAULT ARRAY[]::text[],
    blind boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, project_id)
);

CREATE TABLE task_reviewers (
    task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    reviewer_id text NOT NULL CHECK (reviewer_id <> ''),
    all_features boolean NOT NULL DEFAULT false,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, reviewer_id)
);

CREATE TABLE task_h3_assignments (
    task_id uuid NOT NULL,
    reviewer_id text NOT NULL,
    resolution smallint NOT NULL CHECK (resolution BETWEEN 0 AND 15),
    h3_index text NOT NULL CHECK (h3_index <> ''),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, reviewer_id, h3_index),
    FOREIGN KEY (task_id, reviewer_id)
        REFERENCES task_reviewers(task_id, reviewer_id) ON DELETE CASCADE
);

CREATE INDEX task_h3_assignment_lookup_idx
    ON task_h3_assignments (task_id, reviewer_id, resolution, h3_index);

CREATE TABLE annotations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id uuid NOT NULL,
    project_id text NOT NULL,
    feature_id text NOT NULL,
    reviewer_id text NOT NULL CHECK (reviewer_id <> ''),
    label text NOT NULL CHECK (label <> ''),
    notes text NOT NULL DEFAULT '',
    feature_version_seen bigint NOT NULL CHECK (feature_version_seen > 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, feature_id, reviewer_id),
    FOREIGN KEY (task_id, project_id)
        REFERENCES tasks(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, feature_id)
        REFERENCES features(project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (task_id, reviewer_id)
        REFERENCES task_reviewers(task_id, reviewer_id) ON DELETE CASCADE
);

CREATE INDEX annotations_feature_idx
    ON annotations (project_id, feature_id, task_id);

CREATE TABLE annotation_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    annotation_id uuid NOT NULL,
    task_id uuid NOT NULL,
    project_id text NOT NULL,
    feature_id text NOT NULL,
    reviewer_id text NOT NULL,
    label text NOT NULL,
    notes text NOT NULL,
    feature_version_seen bigint NOT NULL,
    annotation_version bigint NOT NULL,
    created_at timestamptz NOT NULL,
    replaced_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE feature_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL,
    feature_id text NOT NULL,
    geometry geometry(Geometry, 4326) NOT NULL,
    properties jsonb NOT NULL,
    feature_version bigint NOT NULL,
    changed_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX feature_history_lookup_idx
    ON feature_history (project_id, feature_id, feature_version);

CREATE TABLE export_state (
    project_id text PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    exported_revision bigint NOT NULL DEFAULT 0 CHECK (exported_revision >= 0),
    object_key text,
    exported_at timestamptz
);

CREATE OR REPLACE FUNCTION archive_feature_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO feature_history (
        project_id,
        feature_id,
        geometry,
        properties,
        feature_version,
        changed_by,
        created_at
    ) VALUES (
        OLD.project_id,
        OLD.id,
        OLD.geometry,
        OLD.properties,
        OLD.version,
        NEW.updated_by,
        now()
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER archive_feature_before_update
BEFORE UPDATE OF geometry, properties ON features
FOR EACH ROW
WHEN (
    OLD.geometry IS DISTINCT FROM NEW.geometry
    OR OLD.properties IS DISTINCT FROM NEW.properties
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

CREATE TRIGGER archive_annotation_before_update
BEFORE UPDATE OF label, notes, feature_version_seen ON annotations
FOR EACH ROW
WHEN (
    OLD.label IS DISTINCT FROM NEW.label
    OR OLD.notes IS DISTINCT FROM NEW.notes
    OR OLD.feature_version_seen IS DISTINCT FROM NEW.feature_version_seen
)
EXECUTE FUNCTION archive_annotation_update();
