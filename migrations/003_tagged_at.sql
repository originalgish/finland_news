-- Pipeline marker for the rule-based tag stage (worker/tagging.py), like embedded_at
-- and enriched_at. Feed and keyword-rule tags are applied once, to new articles only,
-- so a tag removed in the web app stays removed. Existing rows start NULL and are
-- tagged on the next run.

ALTER TABLE articles ADD COLUMN tagged_at timestamptz;
CREATE INDEX ON articles (tagged_at) WHERE tagged_at IS NULL;
