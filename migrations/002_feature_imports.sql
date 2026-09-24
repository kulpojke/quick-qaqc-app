CREATE TABLE feature_imports (
    project_id text PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source <> ''),
    feature_count bigint NOT NULL CHECK (feature_count >= 0),
    imported_at timestamptz NOT NULL DEFAULT now()
);
