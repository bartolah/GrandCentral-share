-- E269: the Worker's only table. No IPs, no email content.
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  token   TEXT NOT NULL,
  kind    TEXT NOT NULL,          -- open | click
  link    INTEGER,                -- which link, for a click
  src     TEXT,                   -- proxy-google | apple-prefetch | scanner | client | proxy-yahoo
  country TEXT,
  host    TEXT,                   -- t.<domain> it was served from
  ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_token ON events (token);
