#!/usr/bin/env python3
"""
Generate a synthetic-but-realistic Apache httpd log dataset: a SECOND service alongside the
nginx one, for migrating the Elastic `apache` integration's dashboards to ClickStack.

Produces two files, which is all httpd writes:
  data/apache/access.log   Apache 'combined' LogFormat
  data/apache/error.log    Apache 2.4 error log, correlated to the 4xx/5xx above

Why a separate service rather than a re-skin of the nginx stream
----------------------------------------------------------------
The nginx dataset is a busy shop. This one is its documentation site: an order of magnitude
less traffic, static-file heavy, a different URL space, and a status mix dominated by 200/304
rather than API 4xx. Two services that differ let a migrated dashboard be *wrong* in a visible
way -- if an apache tile accidentally reads nginx rows, the numbers move. A 1:1 copy of the
nginx stream would have hidden exactly that class of bug.

What is deliberately shared with generate.py
--------------------------------------------
The diurnal curve, the client IP pool and the user-agent corpus are imported, not re-invented:

* same 24h window (`DAY`), so ONE shared SHIFT_ANCHOR_EPOCH lands both services on the same
  wall clock -- see RUNBOOK's "Keeping the two stacks on the same clock";
* same UA corpus, so the uap-core dictionary's coverage is identical on both services and a
  browser breakdown can be compared across them;
* same IP pool construction, so geo coverage is comparable too.

Apache-specific details that are NOT cosmetic
---------------------------------------------
* `combined` ends in `%b`, not nginx's `$body_bytes_sent`: a 304 logs a literal `-`, not `0`.
  The integration's grok has a `(?:%{NUMBER}|-)` branch for exactly this; emitting `-`
  exercises it.
* Range requests get 206, which the nginx stream never produces.
* The error log carries `[module:level]` and `[pid N:tid N]`, which is where the dashboard's
  `apache.error.module` and `log.level` fields come from. nginx's error format has no
  equivalent, so these could not have been derived from the existing data at all.

Deterministic: single seed, no wall-clock reads. Re-running gives byte-identical files.

Usage:  python3 generate-apache.py [--requests 250000] [--out ../data/apache]
"""

import argparse
import os
import random
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402  -- the nginx generator, reused as a library

SEED = 20260916
DAY = ng.DAY  # same 24h window as the nginx stream; see module docstring
SERVER_NAME = "docs.example.com"
DOCROOT = "/var/www/docs"
APACHE_BANNER = "Apache/2.4.62 (Unix) OpenSSL/3.0.13"

# mpm_event: a small pool of child processes, each with a worker thread pool. The dashboard
# never groups on pid, but `[pid N:tid N]` is mandatory in the grok pattern that yields
# apache.error.module, so the numbers have to be there and have to look plausible.
APACHE_NODES = ["docs-web-01", "docs-web-02"]

# --------------------------------------------------------------------------------------
# URL space: a documentation site
# --------------------------------------------------------------------------------------

TOPICS = [
    "getting-started", "install", "configuration", "cli", "api", "deployment",
    "security", "troubleshooting", "migration", "performance", "faq", "changelog",
]
TOPICS_CW = ng.cumweights([18, 14, 12, 9, 11, 8, 6, 7, 5, 4, 4, 2])

PAGES = [
    "index", "overview", "quickstart", "examples", "reference", "options",
    "upgrading", "limits", "errors", "glossary",
]
PAGES_CW = ng.cumweights([22, 14, 16, 12, 11, 8, 6, 4, 4, 3])

# A docs site is mostly static assets: every HTML hit drags in css/js/fonts/images.
ASSETS = [
    "/_static/css/theme.css", "/_static/css/pygments.css", "/_static/js/search.js",
    "/_static/js/theme.js", "/_static/img/logo.svg", "/_static/img/diagram-arch.png",
    "/_static/img/screenshot-ui.png", "/_static/fonts/inter-regular.woff2",
    "/_static/fonts/inter-bold.woff2", "/_static/fonts/jetbrains-mono.woff2",
    "/_static/searchindex.json",
]
ASSETS_CW = ng.cumweights([14, 7, 10, 9, 11, 6, 5, 12, 8, 6, 12])

DOWNLOADS = [
    "/downloads/tool-3.2.2-linux-amd64.tar.gz", "/downloads/tool-3.2.2-darwin-arm64.tar.gz",
    "/downloads/tool-3.2.1-linux-amd64.tar.gz", "/downloads/tool-3.2.2.sha256",
    "/downloads/tool-3.2.2-windows-amd64.zip",
]
DOWNLOADS_CW = ng.cumweights([34, 22, 9, 17, 18])

# Paths that exist but are refused by config -- these are the 403 source and, unlike a 404,
# they come from mod_authz_core rather than core.
FORBIDDEN = ["/server-status", "/server-info", "/.git/config", "/.env", "/_private/"]
FORBIDDEN_CW = ng.cumweights([22, 12, 28, 26, 12])

# Directories with no DirectoryIndex -- autoindex refuses them, a distinctly apache error.
NOINDEX_DIRS = ["/guide/internal/", "/_static/fonts/", "/downloads/old/"]
NOINDEX_CW = ng.cumweights([40, 35, 25])

# Classic scanner noise. Shares shape with the nginx stream's scanners but not the paths,
# so the two services do not have identical 404 sets.
SCANNER_PATHS = [
    "/wp-login.php", "/wp-admin/setup-config.php", "/xmlrpc.php", "/phpmyadmin/index.php",
    "/administrator/index.php", "/cgi-bin/luci", "/vendor/phpunit/phpunit/phpunit.xml",
    "/.aws/credentials", "/config.json", "/actuator/env", "/solr/admin/info/system",
    "/index.php?s=/Index/\\x5Cthink\\x5Capp/invokefunction",
]
SCANNER_CW = ng.cumweights([16, 9, 11, 13, 7, 6, 5, 8, 7, 6, 6, 6])

# Pages that a stale external link still points at: the honest source of a docs 404.
DEAD_LINKS = [
    "/guide/old-install.html", "/api/v1/reference.html", "/tutorial/index.html",
    "/guide/configuration/legacy.html", "/favicon.ico",
]
DEAD_CW = ng.cumweights([24, 18, 16, 14, 28])

SEARCH_TERMS = [
    "install", "docker", "config+file", "api+key", "rate+limit", "timeout", "tls",
    "upgrade", "cli+flags", "env+vars", "permissions", "webhook", "retry", "proxy",
]
SEARCH_CW = ng.cumweights([12, 11, 9, 8, 7, 6, 6, 8, 7, 5, 4, 4, 3, 10])

REFERRERS = [
    "https://www.google.com/", "https://duckduckgo.com/", "https://news.ycombinator.com/",
    "https://github.com/example/tool", "https://stackoverflow.com/questions/12345678",
    "https://www.reddit.com/r/devops/", "-",
]
REFERRERS_CW = ng.cumweights([31, 6, 9, 17, 12, 4, 21])

# --------------------------------------------------------------------------------------
# response sizes, per content kind
# --------------------------------------------------------------------------------------

# (median bytes, sigma) -- lognormal, so a few big pages without a fat tail of absurd ones.
SIZES = {
    "html": (18400, 0.42),
    "css": (11200, 0.18),
    "js": (46800, 0.30),
    "svg": (3100, 0.35),
    "png": (128000, 0.55),
    "woff2": (31400, 0.12),
    "json": (214000, 0.40),   # searchindex.json is the big one
    "targz": (14600000, 0.45),
    "zip": (16100000, 0.40),
    "sha256": (96, 0.05),
    "txt": (740, 0.30),
    "xml": (24800, 0.45),
}

ASSET_KIND = {
    ".css": "css", ".js": "js", ".svg": "svg", ".png": "png", ".woff2": "woff2",
    ".json": "json", ".tar.gz": "targz", ".zip": "zip", ".sha256": "sha256",
    ".txt": "txt", ".xml": "xml",
}


def kind_of(uri):
    for ext, k in ASSET_KIND.items():
        if uri.endswith(ext):
            return k
    return "html"


def body_size(rng, uri):
    median, sigma = SIZES[kind_of(uri)]
    return int(ng.lognorm(rng, median, sigma, lo=64))


# --------------------------------------------------------------------------------------
# traffic mix
# --------------------------------------------------------------------------------------

KINDS = ["reader_desktop", "reader_mobile", "search_bot", "ci_fetcher", "scanner"]
KINDS_CW = ng.cumweights([44, 27, 12, 10, 7])


def new_ctx(rng, kind, ips, ips_cw):
    """One visitor: a stable ip/ua pair and the node its requests land on."""
    if kind == "reader_desktop":
        ua = ng.pick(rng, ng.UA_DESKTOP, ng.UA_DESKTOP_CW)
    elif kind == "reader_mobile":
        ua = ng.pick(rng, ng.UA_MOBILE, ng.UA_MOBILE_CW)
    elif kind == "search_bot":
        ua = ng.pick(rng, ng.UA_GOODBOT, ng.UA_GOODBOT_CW)
    elif kind == "ci_fetcher":
        ua = ng.pick(rng, ng.UA_API, ng.UA_API_CW)
    else:
        ua = ng.pick(rng, ng.UA_SCANNER, ng.UA_SCANNER_CW)
    return {
        "kind": kind,
        "ip": ng.pick(rng, ips, ips_cw),
        "ua": ua,
        "node": rng.choice(APACHE_NODES),
        # A browser that has the site cached revalidates and collects 304s; a first-time
        # visitor does not. Deciding this per visitor rather than per request is what makes
        # the 304 share look like a real cache hit rate instead of uniform noise.
        "cached": rng.random() < 0.46,
        "seen": set(),
    }


def make_event(rng, ctx, method, uri, referer, status, end_time, body=None):
    e = ng.Event()
    e.end = end_time
    e.ip = ctx["ip"]
    e.user = "-"
    e.method = method
    e.uri = uri
    e.proto = "HTTP/1.1"
    e.status = status
    e.ua = ctx["ua"]
    e.referer = referer
    e.node = ctx["node"]
    e.profile = ctx["kind"]
    if status in (304, 204):
        e.body_bytes = None          # combined logs `-`, not 0
    elif status in (301, 302):
        e.body_bytes = rng.randint(220, 340)
    elif status == 403:
        e.body_bytes = 199
    elif status == 404:
        e.body_bytes = 196
    elif status in (500, 503):
        e.body_bytes = 185 if status == 503 else 528
    elif body is not None:
        e.body_bytes = body
    else:
        e.body_bytes = body_size(rng, uri)
    return e


# --------------------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------------------

def asset_burst(rng, ctx, page_uri, t, events):
    """The css/js/font/image requests a docs page drags in behind it."""
    n = rng.randint(3, 7)
    for i in range(n):
        a = ng.pick(rng, ASSETS, ASSETS_CW)
        t += rng.uniform(0.004, 0.09)
        # Revalidation only happens for something this visitor already fetched.
        if ctx["cached"] and a in ctx["seen"]:
            status = 304
        else:
            status = 200
            ctx["seen"].add(a)
        events.append(make_event(rng, ctx, "GET", a, "https://%s%s" % (SERVER_NAME, page_uri),
                                 status, t))
    return t


def reader_session(rng, ctx, t0, events):
    t = t0
    referer = ng.pick(rng, REFERRERS, REFERRERS_CW)
    depth = rng.randint(1, 6)
    for i in range(depth):
        r = rng.random()
        if i == 0 and r < 0.06:
            # arrived on a stale external link
            uri = ng.pick(rng, DEAD_LINKS, DEAD_CW)
            events.append(make_event(rng, ctx, "GET", uri, referer, 404, t))
            t += rng.uniform(0.3, 2.0)
            uri = "/"
            events.append(make_event(rng, ctx, "GET", uri, referer, 200, t))
        elif r < 0.10:
            term = ng.pick(rng, SEARCH_TERMS, SEARCH_CW)
            uri = "/search?q=%s" % term
            events.append(make_event(rng, ctx, "GET", uri, referer, 200, t,
                                     body=body_size(rng, "/x.html")))
        elif r < 0.16:
            topic = ng.pick(rng, TOPICS, TOPICS_CW)
            uri = "/guide/%s/" % topic
            events.append(make_event(rng, ctx, "GET", uri, referer, 200, t))
        else:
            topic = ng.pick(rng, TOPICS, TOPICS_CW)
            page = ng.pick(rng, PAGES, PAGES_CW)
            uri = "/guide/%s/%s.html" % (topic, page)
            status = 200
            if ctx["cached"] and uri in ctx["seen"]:
                status = 304
            ctx["seen"].add(uri)
            events.append(make_event(rng, ctx, "GET", uri, referer, status, t))

        t = asset_burst(rng, ctx, uri, t + rng.uniform(0.01, 0.12), events)
        referer = "https://%s%s" % (SERVER_NAME, uri)
        t += ng.lognorm(rng, 24.0, 0.9, lo=1.5, hi=600.0)   # reading time

        # occasional server-side failure on the search endpoint (it shells out to a cgi)
        if rng.random() < 0.0022:
            events.append(make_event(rng, ctx, "GET", "/search?q=%s" % ng.pick(
                rng, SEARCH_TERMS, SEARCH_CW), referer, 500, t))
            t += rng.uniform(0.5, 3.0)


def bot_session(rng, ctx, t0, events):
    """A crawler walks the tree steadily and does not fetch assets."""
    t = t0
    for _ in range(rng.randint(6, 40)):
        r = rng.random()
        if r < 0.05:
            uri = "/sitemap.xml"
        elif r < 0.09:
            uri = "/robots.txt"
        elif r < 0.14:
            uri = ng.pick(rng, DEAD_LINKS, DEAD_CW)
            events.append(make_event(rng, ctx, "GET", uri, "-", 404, t))
            t += ng.lognorm(rng, 2.6, 0.7, lo=0.2, hi=60.0)
            continue
        else:
            uri = "/guide/%s/%s.html" % (ng.pick(rng, TOPICS, TOPICS_CW),
                                         ng.pick(rng, PAGES, PAGES_CW))
        events.append(make_event(rng, ctx, "GET", uri, "-", 200, t))
        t += ng.lognorm(rng, 2.6, 0.7, lo=0.2, hi=60.0)


def ci_session(rng, ctx, t0, events):
    """CI pulling release artifacts: few requests, large bodies, some range requests."""
    t = t0
    for _ in range(rng.randint(1, 4)):
        uri = ng.pick(rng, DOWNLOADS, DOWNLOADS_CW)
        r = rng.random()
        if r < 0.12:
            # resumed download -- apache answers a Range request with 206
            status, body = 206, body_size(rng, uri) // rng.randint(2, 6)
        elif r < 0.145:
            status, body = 503, None      # backend refused while the mirror was syncing
        else:
            status, body = 200, None
        events.append(make_event(rng, ctx, "GET", uri, "-", status, t, body=body))
        t += ng.lognorm(rng, 9.0, 0.8, lo=0.4, hi=300.0)


def scanner_session(rng, ctx, t0, events):
    t = t0
    for _ in range(rng.randint(3, 22)):
        r = rng.random()
        if r < 0.24:
            uri = ng.pick(rng, FORBIDDEN, FORBIDDEN_CW)
            status = 403
        elif r < 0.30:
            uri = ng.pick(rng, NOINDEX_DIRS, NOINDEX_CW)
            status = 403                  # autoindex refuses it
        else:
            uri = ng.pick(rng, SCANNER_PATHS, SCANNER_CW)
            status = 404
        method = "POST" if uri.endswith(".php") and rng.random() < 0.34 else "GET"
        events.append(make_event(rng, ctx, method, uri, "-", status, t))
        t += ng.lognorm(rng, 1.1, 0.8, lo=0.05, hi=40.0)


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def render_access(e, dt):
    """Apache `combined`. Differs from nginx's in one visible way: %b logs `-`, not 0."""
    body = "-" if e.body_bytes is None else str(e.body_bytes)
    return '%s - %s [%s] "%s %s %s" %d %s "%s" "%s"' % (
        e.ip, e.user, ng.fmt_time_local(dt), e.method, ng.esc_default(e.uri), e.proto,
        e.status, body, ng.esc_default(e.referer), ng.esc_default(e.ua))


def fmt_error_time(dt):
    """`[Mon Aug 17 14:00:00.123456 2026]` -- grok's APACHE_TIME, microsecond precision.

    Built by hand rather than with %a/%b so the output cannot shift with the runner's locale.
    """
    return "%s %s %02d %02d:%02d:%02d.%06d %d" % (
        DAYS[dt.weekday()], ng.MONTHS[dt.month - 1], dt.day,
        dt.hour, dt.minute, dt.second, dt.microsecond, dt.year)


def render_error(dt, module, level, pid, tid, client, msg):
    head = "[%s] [%s:%s] [pid %d:tid %d]" % (fmt_error_time(dt), module, level, pid, tid)
    if client:
        head += " [client %s]" % client
    return "%s %s" % (head, msg)


# --------------------------------------------------------------------------------------
# error.log
# --------------------------------------------------------------------------------------

def worker(rng, meta, node):
    pid = rng.choice(meta[node]["pids"])
    return pid, rng.randint(139_600_000_000_000, 139_900_000_000_000) % 1000000 + 139000


def error_lines_for(rng, e, dt, meta):
    """The error-log entries a given request would have produced.

    Not every 4xx writes to the error log -- that is the point of keeping the two files
    correlated but not equal. A 404 on a missing file does; a 404 from a scanner POST to
    a .php that was never there does too, but a 304 or a 206 writes nothing at all.
    """
    out = []
    node = e.node
    pid, tid = worker(rng, meta, node)
    client = "%s:%d" % (e.ip, rng.randint(1024, 65535))

    if e.status == 404:
        out.append((e.end, render_error(
            dt, "core", "info", pid, tid, client,
            "AH00128: File does not exist: %s%s" % (DOCROOT, e.uri.split("?")[0]))))
    elif e.status == 403:
        path = e.uri.split("?")[0]
        if path in NOINDEX_DIRS:
            out.append((e.end, render_error(
                dt, "autoindex", "error", pid, tid, client,
                "AH01276: Cannot serve directory %s%s: No matching DirectoryIndex "
                "(index.html,index.php) found, and server-generated directory index "
                "forbidden by Options directive" % (DOCROOT, path))))
        else:
            out.append((e.end, render_error(
                dt, "authz_core", "error", pid, tid, client,
                "AH01630: client denied by server configuration: %s%s" % (DOCROOT, path))))
    elif e.status == 500:
        out.append((e.end, render_error(
            dt, "cgid", "error", pid, tid, client,
            "AH01215: (2)No such file or directory: exec of '%s/cgi-bin/search.cgi' failed: "
            "%s/cgi-bin/search.cgi" % (DOCROOT, DOCROOT))))
        out.append((e.end + 0.000012, render_error(
            dt, "cgid", "error", pid, tid, client,
            "End of script output before headers: search.cgi")))
    elif e.status == 503:
        out.append((e.end, render_error(
            dt, "proxy", "error", pid, tid, client,
            "AH00959: ap_proxy_connect_backend disabling worker for (10.0.9.14:8080) for 60s")))

    # Background noise unrelated to any single response: slow clients giving up mid-header.
    if rng.random() < 0.00035:
        out.append((e.end, render_error(
            dt, "reqtimeout", "info", pid, tid, client,
            "AH01382: Request header read timeout")))
    return out


def lifecycle_lines(rng, meta):
    """Startup, log rotation reload, and the ssl warning httpd emits on every start."""
    out = []
    for i, node in enumerate(APACHE_NODES):
        m = meta[node]
        # boot, a little before the window opens for one node and just inside it for the other
        t = 0.4 + i * 1.7
        dt = DAY + timedelta(seconds=t)
        pid = m["master_pid"]
        out.append((t, render_error(dt, "ssl", "warn", pid, 0, None,
                                    "AH01909: %s:443:0 server certificate does NOT include "
                                    "an ID which matches the server name" % SERVER_NAME)))
        out.append((t + 0.03, render_error(dt, "mpm_event", "notice", pid, 0, None,
                                           "AH00489: %s configured -- resuming normal "
                                           "operations" % APACHE_BANNER)))
        out.append((t + 0.031, render_error(dt, "core", "notice", pid, 0, None,
                                            "AH00094: Command line: '/usr/sbin/httpd "
                                            "-D FOREGROUND'")))
        # nightly logrotate graceful restart, staggered across the two nodes
        t = m["reload_at"]
        dt = DAY + timedelta(seconds=t)
        out.append((t, render_error(dt, "mpm_event", "notice", pid, 0, None,
                                    "AH00493: SIGUSR1 received.  Doing graceful restart")))
        out.append((t + 0.42, render_error(dt, "ssl", "warn", pid, 0, None,
                                           "AH01909: %s:443:0 server certificate does NOT "
                                           "include an ID which matches the server name"
                                           % SERVER_NAME)))
        out.append((t + 0.45, render_error(dt, "mpm_event", "notice", pid, 0, None,
                                           "AH00489: %s configured -- resuming normal "
                                           "operations" % APACHE_BANNER)))
    return out


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=250000,
                    help="approximate access.log lines (default 250000)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "data", "apache"))
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    rng = random.Random(SEED)
    print("building client ip pool ...")
    ips, ips_cw = ng.build_ip_pool(rng, n=9000)   # a docs site sees fewer distinct clients

    meta = {}
    for i, node in enumerate(APACHE_NODES):
        meta[node] = {
            "master_pid": rng.randint(900, 1400),
            "pids": sorted(rng.sample(range(1500, 48000), 12)),
            "reload_at": 3 * 3600 + 12 * 60 + i * 331 + rng.uniform(0, 30),
        }

    events = []
    print("generating health checks ...")
    for probe_ip in ("10.0.1.31", "10.0.1.32"):
        ctx = new_ctx(rng, "ci_fetcher", ips, ips_cw)
        ctx.update(ip=probe_ip, ua=ng.pick(rng, ng.UA_MONITOR, ng.UA_MONITOR_CW), cached=False)
        for i in range(2880):            # every 30s
            ctx["node"] = APACHE_NODES[i % 2]
            events.append(make_event(rng, ctx, "GET", "/status.html", "-", 200,
                                     i * 30.0 + rng.uniform(0, 0.3), body=412))

    print("generating sessions ...")
    while len(events) < args.requests:
        kind = ng.pick(rng, KINDS, KINDS_CW)
        ctx = new_ctx(rng, kind, ips, ips_cw)
        t0 = ng.session_start(rng)
        if kind in ("reader_desktop", "reader_mobile"):
            reader_session(rng, ctx, t0, events)
        elif kind == "search_bot":
            bot_session(rng, ctx, t0, events)
        elif kind == "ci_fetcher":
            ci_session(rng, ctx, t0, events)
        else:
            scanner_session(rng, ctx, t0, events)

    events = [e for e in events if 0.0 <= e.end < 86400.0]
    events.sort(key=lambda e: e.end)
    print("  {:,} requests in window".format(len(events)))

    err = lifecycle_lines(rng, meta)

    print("writing access log ...")
    p_acc = os.path.join(outdir, "access.log")
    p_err = os.path.join(outdir, "error.log")
    buf = []
    with open(p_acc, "w", encoding="utf-8", newline="\n") as fa:
        for e in events:
            dt = DAY + timedelta(milliseconds=int(e.end * 1000))
            buf.append(render_access(e, dt))
            err.extend(error_lines_for(rng, e, dt, meta))
            if len(buf) >= 20000:
                fa.write("\n".join(buf) + "\n")
                buf.clear()
        if buf:
            fa.write("\n".join(buf) + "\n")

    print("writing error log ...")
    err.sort(key=lambda x: x[0])
    with open(p_err, "w", encoding="utf-8", newline="\n") as fe:
        fe.write("\n".join(line for _, line in err) + "\n")

    # ------------------------------------------------------------------ summary
    from collections import Counter
    st = Counter(e.status for e in events)
    fam = Counter(str(e.status)[0] + "xx" for e in events)
    print("\naccess.log  %8s lines  %8.1f MB" % (
        "{:,}".format(len(events)), os.path.getsize(p_acc) / 1e6))
    print("error.log   %8s lines  %8.1f MB" % (
        "{:,}".format(len(err)), os.path.getsize(p_err) / 1e6))
    print("\nstatus families:")
    for k in sorted(fam):
        print("  %s  %8s  %5.2f%%" % (k, "{:,}".format(fam[k]), 100.0 * fam[k] / len(events)))
    print("\ntop statuses:", ", ".join("%s=%s" % (c, "{:,}".format(n))
                                       for c, n in st.most_common(12)))
    print("distinct client ips:", len({e.ip for e in events}))


if __name__ == "__main__":
    main()
