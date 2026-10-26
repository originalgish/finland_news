-- Date each story by its articles' publication times. The worker used to stamp
-- first_seen and last_updated with the time of the run, so every story clustered in
-- one run shared a timestamp. The worker now uses published_at; this fixes older rows.

UPDATE clusters c
SET first_seen = m.first_published,
    last_updated = m.last_published
FROM (
    SELECT cluster_id, min(published_at) AS first_published, max(published_at) AS last_published
    FROM articles
    WHERE cluster_id IS NOT NULL
    GROUP BY cluster_id
) m
WHERE c.id = m.cluster_id;
