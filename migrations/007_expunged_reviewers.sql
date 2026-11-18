-- *!*! Reviewers whose labels no longer count. Nothing is deleted: their
-- *!*! annotations stay in the database, but counting, top-ups and exports
-- *!*! skip them. Deleting the row (src.api.expunge --restore) undoes it.
CREATE TABLE expunged_reviewers (
    project_id text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    reviewer_id text NOT NULL CHECK (reviewer_id <> ''),
    note text NOT NULL DEFAULT '',
    expunged_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, reviewer_id)
);
