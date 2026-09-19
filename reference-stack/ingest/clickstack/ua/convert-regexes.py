#!/usr/bin/env python3
"""Convert uap-core's regexes.yml into ClickHouse's regexp_tree dictionary format.

    python3 convert-regexes.py regexes.yml <output-dir>

Writes three files: ua-browser.yaml, ua-os.yaml, ua-device.yaml.

They must be SEPARATE dictionaries, not one. A regexp_tree lookup returns the attributes of
the first node that matches, so browser patterns and os patterns in a single tree would
shadow each other -- a user agent matching a browser rule would never reach the os rules.
uap-core treats the three lists as independent passes, and so must we.

Why this exists: ClickHouse's `regexp_tree` dictionary layout was added for exactly this job,
but it expects its own YAML shape -- a flat list of {regexp, <attributes>} -- while uap-core
ships three separate parser lists with `_replacement` fields and `$1`-style back-references.

The input should be the regexes.yml that YOUR Elasticsearch actually uses, pulled out of
modules/ingest-user-agent/ingest-user-agent-<version>.jar. Using the same corpus is what makes
the two platforms agree by construction instead of by coincidence -- uap-core is versioned and
different releases classify some agents differently.

uap-core semantics reproduced here:
  browser family = family_replacement with $1..$9 substituted, else capture group 1
  os family      = os_replacement      with $1..$9 substituted, else capture group 1
  device family  = device_replacement  with $1..$9 substituted, else capture group 1
ClickHouse uses \\1 rather than $1 for back-references, so those are rewritten.

Version components are emitted too, as separate `<attr>_v1`..`_v4` attributes:

  browser v1..v4 = v1_replacement / v2_replacement if present, else capture groups 2..5
  os      v1..v4 = os_v1_replacement .. os_v3_replacement if present, else groups 2..5

They are kept as four separate attributes rather than one pre-joined string because Elastic
assembles the version by appending components only while each is non-null -- it STOPS at the
first null rather than skipping it. Joining here would have to guess; ua.sql reproduces the
rule exactly on four columns. (Originally only the families were emitted, on the grounds that
the nginx dashboards group on family alone. The apache `[Logs Apache] Access and error logs`
dashboard breaks its browser and OS donuts down by name AND version, which is what made the
components necessary.)

A component is emitted only when the regex actually HAS that capture group -- an out-of-range
back-reference is not something to hand a dictionary at load time -- which also matches
Elastic's own `groupCount >= n` guard.
"""
import re
import sys

# Match `$1`..`$9`, which uap-core uses inside *_replacement strings.
DOLLAR_REF = re.compile(r"\$(\d)")


def count_groups(rx):
    """Number of capturing groups in a regex, ignoring `(?:`, `(?=`, `(?i)` and friends.

    Hand-rolled because `re.compile(rx).groups` would reject the handful of uap-core
    patterns that use constructs Python's `re` does not accept, and one unparseable
    pattern must not take the whole corpus down.
    """
    n = i = 0
    in_class = False
    while i < len(rx):
        c = rx[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            nxt = rx[i + 1:i + 2]
            if nxt != "?":
                n += 1
            elif rx[i + 1:i + 3] == "?P" and rx[i + 3:i + 4] != "=":
                n += 1          # (?P<name>...) still captures
        i += 1
    return n


def load_sections(path):
    """Minimal uap-core YAML reader.

    Deliberately not PyYAML: the file is 191 KB of regexes full of backslashes and quotes,
    the structure is rigidly `- regex: ...` blocks, and the repo's other generators are
    stdlib-only. A dependency here would have to be installed inside the ClickHouse
    container too.
    """
    sections, current, entry = {}, None, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\w+):\s*$", line)
        if m:                                     # user_agent_parsers: / os_parsers: / ...
            current = m.group(1)
            sections[current] = []
            entry = None
            continue
        if current is None:
            continue
        m = re.match(r"^\s*-\s+(\w+):\s*(.*)$", line)
        if m:                                     # first key of a new list entry
            entry = {}
            sections[current].append(entry)
            entry[m.group(1)] = unquote(m.group(2))
            continue
        m = re.match(r"^\s+(\w+):\s*(.*)$", line)
        if m and entry is not None:               # subsequent keys of the same entry
            entry[m.group(1)] = unquote(m.group(2))
    return sections


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        inner = v[1:-1]
        # YAML single-quoted strings escape a quote by doubling it
        return inner.replace("''", "'") if v[0] == "'" else inner
    return v


def yaml_quote(s):
    """Emit a YAML single-quoted scalar. Regexes are full of \\ and " -- single quotes need
    only the doubling rule, which keeps backslashes literal."""
    return "'" + s.replace("'", "''") + "'"


def emit(entries, replacement_key, attribute, version_keys=()):
    """One regexp_tree node per uap-core parser, in order. Order matters: regexp_tree
    resolves top-down and uap-core's lists are already priority-ordered.

    `version_keys` are the entry keys holding v1..v4 overrides, in order. Where an entry
    does not override a component, it falls back to capture group n+1 -- but only if the
    regex has that many groups.
    """
    out = []
    for e in entries:
        rx = e.get("regex")
        if not rx:
            continue
        value = e.get(replacement_key)
        value = DOLLAR_REF.sub(r"\\\1", value) if value else "\\1"
        node = ["- regexp: %s" % yaml_quote(rx),
                "  %s: %s" % (attribute, yaml_quote(value))]
        ngroups = count_groups(rx) if version_keys else 0
        for n, key in enumerate(version_keys, start=1):
            override = e.get(key)
            if override:
                v = DOLLAR_REF.sub(r"\\\1", override)
            elif ngroups >= n + 1:
                v = "\\%d" % (n + 1)
            else:
                continue
            node.append("  %s_v%d: %s" % (attribute, n, yaml_quote(v)))
        out.append("\n".join(node))
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, outdir = sys.argv[1], sys.argv[2].rstrip("/")
    s = load_sections(src)

    # uap-core declares v1/v2 overrides for user agents and os_v1..os_v3 for operating
    # systems; there is no v3/v4 override key for either, so those components always come
    # from capture groups. Listing four slots regardless keeps the fallback uniform.
    BROWSER_V = ("v1_replacement", "v2_replacement", "v3_replacement", "v4_replacement")
    OS_V = ("os_v1_replacement", "os_v2_replacement", "os_v3_replacement", "os_v4_replacement")

    for section, key, attr, fname, vkeys in (
        ("user_agent_parsers", "family_replacement", "browser", "ua-browser.yaml", BROWSER_V),
        ("os_parsers",         "os_replacement",     "os",      "ua-os.yaml",      OS_V),
        # device version is not a uap-core concept, and no dashboard asks for one
        ("device_parsers",     "device_replacement", "device",  "ua-device.yaml",  ()),
    ):
        nodes = emit(s.get(section, []), key, attr, vkeys)
        path = "%s/%s" % (outdir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Generated from uap-core regexes.yml by convert-regexes.py -- do not edit.\n")
            f.write("# Source corpus: Elasticsearch's own modules/ingest-user-agent jar, so the\n")
            f.write("# two platforms parse user agents with identical rules.\n")
            f.write("\n".join(nodes) + "\n")
        print("wrote %s: %d patterns" % (path, len(nodes)))


if __name__ == "__main__":
    main()
