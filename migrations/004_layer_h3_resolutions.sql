-- *!*! Persist the configured H3 resolutions needed when users create features.
ALTER TABLE feature_layers
    ADD COLUMN h3_resolutions smallint[] NOT NULL
    DEFAULT ARRAY[5, 6, 7, 8, 9, 10]::smallint[],
    ADD CONSTRAINT feature_layers_h3_resolutions_check CHECK (
        cardinality(h3_resolutions) > 0
        AND 0 <= ALL(h3_resolutions)
        AND 15 >= ALL(h3_resolutions)
    );
