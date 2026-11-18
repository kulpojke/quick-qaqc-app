-- *!*! Per-building annotation lists. Hexes still decide what a reviewer sees
-- *!*! and edits; in a task with feature_lists, a reviewer may only label the
-- *!*! buildings on their own list. Existing tasks keep whole-hex labelling.
ALTER TABLE tasks
    ADD COLUMN feature_lists boolean NOT NULL DEFAULT false;

CREATE TABLE task_feature_assignments (
    task_id uuid NOT NULL,
    reviewer_id text NOT NULL,
    project_id text NOT NULL,
    layer_id text NOT NULL,
    feature_id text NOT NULL,
    -- allocated: shared out by --auto-assign-hex; drawn: the reviewer drew it;
    -- topped_up: added later because the building still needed reviewers.
    reason text NOT NULL CHECK (reason IN ('allocated', 'drawn', 'topped_up')),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, reviewer_id, feature_id),
    FOREIGN KEY (task_id, reviewer_id)
        REFERENCES task_reviewers(task_id, reviewer_id) ON DELETE CASCADE,
    FOREIGN KEY (task_id, project_id, layer_id)
        REFERENCES tasks(id, project_id, layer_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, layer_id, feature_id)
        REFERENCES features(project_id, layer_id, id) ON DELETE CASCADE
);

CREATE INDEX task_feature_assignments_feature_idx
    ON task_feature_assignments (task_id, feature_id);

-- *!*! NULL keeps the saved whole-hex allocation. Otherwise, per building
-- *!*! layer: the YAML reviewer order, caps, and H3 resolution that runtime
-- *!*! top-ups reuse between bootstrap runs.
ALTER TABLE auto_hex_assignments
    ADD COLUMN feature_lists jsonb;

-- *!*! The one rule for "may this reviewer label this feature in this task".
CREATE FUNCTION reviewer_may_label(
    check_task_id uuid,
    check_reviewer_id text,
    check_feature_id text
) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT task.feature_lists OR EXISTS (
        SELECT 1
        FROM task_feature_assignments AS listed
        WHERE listed.task_id = check_task_id
          AND listed.reviewer_id = check_reviewer_id
          AND listed.feature_id = check_feature_id
    )
    FROM tasks AS task
    WHERE task.id = check_task_id
$$;
