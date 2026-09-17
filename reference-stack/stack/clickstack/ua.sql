-- Parse user agents with the SAME regex corpus Elasticsearch uses, via ClickHouse's
-- regexp_tree dictionary layout -- the counterpart to geoip.sql's ip_trie dictionary.
--
-- Elastic's `user_agent` ingest processor runs uap-core regexes at INDEX time and freezes
-- user_agent.name / .os.name into every document. ClickStack's collector does no UA parsing
-- at all, so the equivalent has to be recreated on the target. This does it as a dictionary
-- lookup in a MATERIALIZED column: recomputable with a mutation, repointable at a newer
-- corpus, and -- unlike the hand-written multiIf it replaces -- defined in exactly one place.
--
-- The corpus is lifted out of Elasticsearch's own ingest-user-agent jar (see ua.sh), not
-- downloaded from upstream. uap-core is versioned and different releases classify some
-- agents differently, so using the running stack's own copy is what makes the two platforms
-- agree by construction rather than by coincidence.

CREATE DATABASE IF NOT EXISTS ua;

-- ---------------------------------------------------------------------------------------
-- Drop the columns FIRST. A MATERIALIZED column whose expression calls dictGet registers a
-- hard dependency on that dictionary, so on a re-run `DROP DICTIONARY` fails with
--   Code 630 ... because some tables depend on it: default.otel_logs (HAVE_DEPENDENT_OBJECTS)
-- Removing the dependents before the dictionaries is what makes this file re-runnable.
-- ---------------------------------------------------------------------------------------

ALTER TABLE default.otel_logs
    DROP COLUMN IF EXISTS ua_browser,
    DROP COLUMN IF EXISTS ua_browser_version,
    DROP COLUMN IF EXISTS ua_os,
    DROP COLUMN IF EXISTS ua_os_version,
    DROP COLUMN IF EXISTS ua_device;

-- ---------------------------------------------------------------------------------------
-- Three dictionaries, not one. A regexp_tree lookup returns the attributes of the FIRST
-- node that matches, so browser and os patterns in a single tree would shadow each other.
-- uap-core treats its three parser lists as independent passes; so do we.
-- ---------------------------------------------------------------------------------------

-- The _v1.._v4 attributes are the version COMPONENTS, not a version string: uap-core yields
-- them separately and Elastic joins them (see the ua_*_version columns below). They default
-- to '' rather than 'Other' -- an absent component is absent, not unknown.
DROP DICTIONARY IF EXISTS ua.browser;
CREATE DICTIONARY ua.browser
(
    regexp     String,
    browser    String DEFAULT 'Other',
    browser_v1 String DEFAULT '',
    browser_v2 String DEFAULT '',
    browser_v3 String DEFAULT '',
    browser_v4 String DEFAULT ''
)
PRIMARY KEY regexp
SOURCE(YAMLRegExpTree(PATH '/var/lib/clickhouse/user_files/ua/ua-browser.yaml'))
LAYOUT(regexp_tree)
LIFETIME(0);

DROP DICTIONARY IF EXISTS ua.os;
CREATE DICTIONARY ua.os
(
    regexp String,
    os     String DEFAULT 'Other',
    os_v1  String DEFAULT '',
    os_v2  String DEFAULT '',
    os_v3  String DEFAULT '',
    os_v4  String DEFAULT ''
)
PRIMARY KEY regexp
SOURCE(YAMLRegExpTree(PATH '/var/lib/clickhouse/user_files/ua/ua-os.yaml'))
LAYOUT(regexp_tree)
LIFETIME(0);

DROP DICTIONARY IF EXISTS ua.device;
CREATE DICTIONARY ua.device
(
    regexp String,
    device String DEFAULT 'Other'
)
PRIMARY KEY regexp
SOURCE(YAMLRegExpTree(PATH '/var/lib/clickhouse/user_files/ua/ua-device.yaml'))
LAYOUT(regexp_tree)
LIFETIME(0);

-- ---------------------------------------------------------------------------------------
-- Materialized columns. They were dropped at the top of this file rather than here, so that
-- the dictionaries above could be replaced; re-adding them updates the definitions instead
-- of silently keeping stale ones.
--
-- Note what these deliberately do NOT reproduce: Elastic emits no user_agent.os.name at all
-- for bots and HTTP clients -- the field is simply absent, so those documents appear in no
-- OS bucket. A ClickHouse column has to hold something, so unmatched agents land in 'Other'.
-- Same partition of the data, one extra visible slice. Filter `ua_os != 'Other'` to compare
-- like for like with Kibana's donut.
-- ---------------------------------------------------------------------------------------

-- The version columns reproduce Elastic's join rule, which is subtler than it looks:
-- UserAgentProcessor appends v1, then v2, then v3, then v4, each only if the previous one
-- was non-null. It therefore STOPS at the first missing component rather than skipping it,
-- so a rule yielding v1='18', v2=null, v3='2' gives "18" in Elastic -- not "18.2".
--
-- `extract(..., '^[^.]*(?:\\.[^.]+)*')` is exactly that rule on the dot-joined components:
-- it takes the leading run and stops at the first empty one. Concatenating and trimming
-- trailing dots would agree on the common cases and disagree on precisely that one.
ALTER TABLE default.otel_logs
    ADD COLUMN ua_browser LowCardinality(String)
        MATERIALIZED dictGetOrDefault('ua.browser', 'browser',
                     LogAttributes['http_user_agent'], 'Other'),
    ADD COLUMN ua_browser_version String
        MATERIALIZED extract(arrayStringConcat([
                         dictGetOrDefault('ua.browser', 'browser_v1', LogAttributes['http_user_agent'], ''),
                         dictGetOrDefault('ua.browser', 'browser_v2', LogAttributes['http_user_agent'], ''),
                         dictGetOrDefault('ua.browser', 'browser_v3', LogAttributes['http_user_agent'], ''),
                         dictGetOrDefault('ua.browser', 'browser_v4', LogAttributes['http_user_agent'], '')
                     ], '.'), '^[^.]*(?:\\.[^.]+)*'),
    ADD COLUMN ua_os LowCardinality(String)
        MATERIALIZED dictGetOrDefault('ua.os', 'os',
                     LogAttributes['http_user_agent'], 'Other'),
    ADD COLUMN ua_os_version String
        MATERIALIZED extract(arrayStringConcat([
                         dictGetOrDefault('ua.os', 'os_v1', LogAttributes['http_user_agent'], ''),
                         dictGetOrDefault('ua.os', 'os_v2', LogAttributes['http_user_agent'], ''),
                         dictGetOrDefault('ua.os', 'os_v3', LogAttributes['http_user_agent'], ''),
                         dictGetOrDefault('ua.os', 'os_v4', LogAttributes['http_user_agent'], '')
                     ], '.'), '^[^.]*(?:\\.[^.]+)*'),
    ADD COLUMN ua_device LowCardinality(String)
        MATERIALIZED dictGetOrDefault('ua.device', 'device',
                     LogAttributes['http_user_agent'], 'Other');

-- Backfill the rows already there. MATERIALIZED only computes on insert, so without this
-- the existing rows read back empty. Asynchronous; watch system.mutations.
ALTER TABLE default.otel_logs
    MATERIALIZE COLUMN ua_browser,
    MATERIALIZE COLUMN ua_browser_version,
    MATERIALIZE COLUMN ua_os,
    MATERIALIZE COLUMN ua_os_version,
    MATERIALIZE COLUMN ua_device;
