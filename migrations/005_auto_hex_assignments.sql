-- *!*! Persist whole-hex allocations so restart does not redistribute review work.
CREATE TABLE auto_hex_assignments (
    project_id text PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    signature text NOT NULL,
    options jsonb NOT NULL,
    allocations jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
