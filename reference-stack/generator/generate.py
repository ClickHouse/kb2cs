#!/usr/bin/env python3
"""
Generate a synthetic-but-realistic nginx log dataset for ClickStack observability
training (Elasticsearch -> ClickStack migration).

Produces three views of THE SAME request stream:
  data/access.log        strict nginx 'combined' format
  data/access.json.log   nginx 'log_format json escape=json' with upstream/timing/TLS fields
  data/error.log         nginx error log, correlated to the 5xx/499/limit_req events above

Design notes
------------
* Realistic baseline only: diurnal traffic, ordinary error budget, bots and scanners at
  background levels. No planted incident, no hidden narrative.
* Deterministic: single seed, no wall-clock reads. Re-running gives byte-identical files.
* Events are emitted at request *completion* time (as nginx does), then sorted, so the
  files are time-ordered the way a real log is.

Usage:  python3 generate.py [--requests 500000] [--out ../data]
"""

import argparse
import json
import math
import os
import random
from bisect import bisect
from datetime import datetime, timedelta, timezone

SEED = 20260817
DAY = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
SERVER_NAME = "shop.example.com"
NGINX_NODES = ["web-edge-01", "web-edge-02", "web-edge-03"]

# $connection is a per-worker-process counter, so on a real host it starts near 1 and the
# three nodes' numbers would overlap. We ship ONE merged error.log, so each node gets its
# own 10M block: that keeps `connection` <-> `*<connection>` a unique join key across files.
CONN_BASE = {node: (i + 1) * 10_000_000 for i, node in enumerate(NGINX_NODES)}

# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def cumweights(weights):
    total, acc, out = float(sum(weights)), 0.0, []
    for w in weights:
        acc += w
        out.append(acc / total)
    return out


def pick(rng, items, cw):
    return items[bisect(cw, rng.random())]


def lognorm(rng, median, sigma, lo=0.0, hi=None):
    v = median * math.exp(rng.gauss(0.0, sigma))
    if hi is not None:
        v = min(v, hi)
    return max(v, lo)


# --------------------------------------------------------------------------------------
# traffic shape: hourly multipliers, smoothly interpolated
# --------------------------------------------------------------------------------------

HOURLY = [
    0.34, 0.24, 0.19, 0.17, 0.20, 0.31, 0.52, 0.78, 1.02, 1.21, 1.34, 1.44,
    1.50, 1.44, 1.39, 1.34, 1.29, 1.31, 1.42, 1.57, 1.61, 1.41, 1.01, 0.61,
]


def _minute_weights():
    """Per-minute weights over the 24h window, cosine-interpolated between hours."""
    w = []
    for m in range(1440):
        h, frac = divmod(m / 60.0, 1.0)
        h = int(h)
        a, b = HOURLY[h], HOURLY[(h + 1) % 24]
        blend = (1 - math.cos(math.pi * frac)) / 2.0
        w.append(a + (b - a) * blend)
    return w


MINUTE_CW = cumweights(_minute_weights())


def session_start(rng):
    minute = bisect(MINUTE_CW, rng.random())
    return minute * 60.0 + rng.random() * 60.0


# --------------------------------------------------------------------------------------
# client IPs
# --------------------------------------------------------------------------------------

_RESERVED_V4 = [
    (0, 0), (10, 10), (100, 100), (127, 127), (169, 169), (172, 172),
    (192, 192), (198, 198), (203, 203), (224, 255),
]


def _rand_public_v4(rng):
    while True:
        a = rng.randint(1, 223)
        if any(lo <= a <= hi for lo, hi in _RESERVED_V4):
            continue
        return "%d.%d.%d.%d" % (a, rng.randint(0, 255), rng.randint(0, 255), rng.randint(1, 254))


def _rand_public_v6(rng):
    head = rng.choice(["2001:4860", "2a03:2880", "2600:1700", "2804:14c", "2401:4900", "2a02:6b8"])
    tail = ":".join("%x" % rng.randint(0, 0xFFFF) for _ in range(4))
    return f"{head}:{tail}"


def build_ip_pool(rng, n=22000):
    """Zipf-ish popularity: a few chatty clients, a long tail of one-offs."""
    ips = [_rand_public_v6(rng) if rng.random() < 0.04 else _rand_public_v4(rng) for _ in range(n)]
    weights = [1.0 / ((i + 1) ** 0.62) for i in range(n)]
    return ips, cumweights(weights)


# --------------------------------------------------------------------------------------
# user agents
# --------------------------------------------------------------------------------------

UA_DESKTOP = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
]
UA_DESKTOP_CW = cumweights([26, 9, 17, 11, 12, 6, 19])

UA_MOBILE = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1",
]
UA_MOBILE_CW = cumweights([34, 12, 24, 21, 9])

UA_GOODBOT = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)",
    "Mozilla/5.0 (compatible; Applebot/0.1; +http://www.apple.com/go/applebot)",
]
UA_GOODBOT_CW = cumweights([40, 22, 16, 12, 10])

UA_API = [
    "python-requests/2.32.3",
    "Go-http-client/2.0",
    "axios/1.7.4",
    "okhttp/4.12.0",
    "ShopMobile/4.12.1 (iOS 18.6; iPhone16,2)",
    "ShopMobile/4.11.0 (Android 15; Pixel 9)",
    "curl/8.7.1",
]
UA_API_CW = cumweights([14, 12, 21, 16, 20, 13, 4])

UA_SCANNER = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36",
    "Mozilla/5.0 zgrab/0.x",
    "python-urllib3/1.26.18",
    "Go-http-client/1.1",
    "masscan/1.3 (https://github.com/robertdavidgraham/masscan)",
]
UA_SCANNER_CW = cumweights([34, 18, 22, 18, 8])

UA_MONITOR = ["kube-probe/1.30", "Prometheus/2.54.1", "ELB-HealthChecker/2.0"]
UA_MONITOR_CW = cumweights([50, 30, 20])

# --------------------------------------------------------------------------------------
# content catalog
# --------------------------------------------------------------------------------------

CATEGORIES = [
    "kitchen", "outdoors", "electronics", "home-office", "coffee", "garden",
    "lighting", "storage", "bath", "pet-supplies", "audio", "tools",
]
CATEGORIES_CW = cumweights([14, 11, 16, 9, 12, 7, 6, 5, 5, 6, 6, 3])

SEARCH_TERMS = [
    "coffee grinder", "cast iron pan", "desk lamp", "usb c hub", "raised garden bed",
    "espresso machine", "noise cancelling headphones", "standing desk", "dutch oven",
    "wireless keyboard", "air fryer", "cordless drill", "shelf brackets", "dog bed",
    "kettle", "monitor arm", "watering can", "bath mat", "bookshelf speaker",
    "chef knife", "yoga mat", "laptop stand", "french press", "storage bins",
    "mug", "cutting board", "led strip", "hose reel", "toolbox", "blender",
]
SEARCH_CW = cumweights([9, 7, 8, 11, 4, 6, 10, 8, 5, 9, 12, 5, 3, 4, 6, 7, 3, 3, 4, 6, 5, 7, 6, 4, 8, 5, 4, 2, 3, 6])

STATIC_ASSETS = [
    ("/static/css/app.8f3c21d9.css", 48_000),
    ("/static/css/vendor.5b2a77c1.css", 132_000),
    ("/static/js/runtime.2c9f01ab.js", 12_400),
    ("/static/js/app.7d41e0f2.js", 286_000),
    ("/static/js/vendor.9a10cc5e.js", 512_000),
    ("/static/js/checkout.4e77b210.js", 96_000),
    ("/static/fonts/inter-var.woff2", 78_000),
    ("/static/fonts/inter-latin.woff2", 41_000),
    ("/static/img/logo.svg", 3_100),
    ("/static/img/sprite.11c4de90.svg", 22_000),
    ("/static/img/hero-lg.webp", 214_000),
    ("/static/img/hero-sm.webp", 61_000),
]
STATIC_CW = cumweights([12, 8, 11, 12, 10, 4, 9, 6, 11, 7, 5, 5])

SCANNER_PATHS = [
    "/wp-login.php", "/wp-admin/setup-config.php", "/.env", "/.git/config",
    "/phpmyadmin/index.php", "/admin/config.php", "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php",
    "/.aws/credentials", "/config.json", "/actuator/env", "/solr/admin/info/system",
    "/cgi-bin/luci/;stok=/locale", "/xmlrpc.php", "/backup.sql", "/.svn/entries",
    "/api/v1/../../etc/passwd", "/index.php?s=/index/\\think\\app/invokefunction",
    '/search?q=%22><script>alert(1)</script>',
    "/login?next=%2Fadmin%2F%00", "/server-status",
    # these three deliberately exercise log-escaping: a literal double quote, a literal
    # backslash, and non-ASCII bytes. nginx renders them as \x22 / \x5C / \xC3\xA9 in the
    # combined log, and as proper JSON escapes in the JSON log.
    '/product?id=1" OR "1"="1',
    "/download?file=..\\..\\windows\\win.ini",
    "/search?q=café+crème",
]
SCANNER_CW = cumweights([16, 6, 14, 11, 7, 5, 4, 4, 5, 3, 2, 2, 5, 3, 2, 2, 2, 2, 2, 3, 2, 2, 2])

UPSTREAM_WEB = ["10.0.2.11:8080", "10.0.2.12:8080", "10.0.2.13:8080", "10.0.2.14:8080"]
UPSTREAM_API = ["10.0.3.11:9000", "10.0.3.12:9000", "10.0.3.13:9000", "10.0.3.14:9000",
                "10.0.3.15:9000", "10.0.3.16:9000"]

# --------------------------------------------------------------------------------------
# endpoint profiles
# --------------------------------------------------------------------------------------
# status weights are (code, weight) pairs; latency is a lognormal median/sigma in seconds.

PROFILES = {
    "home": dict(pool="web", median=0.035, sigma=0.55, size=14_000, ssigma=0.25,
                 statuses=[(200, 9550), (304, 200), (301, 120), (499, 30), (500, 40),
                           (502, 20), (503, 10), (504, 5)]),
    "search": dict(pool="web", median=0.118, sigma=0.78, size=9_400, ssigma=0.35,
                   statuses=[(200, 9400), (499, 80), (500, 60), (502, 30), (503, 15),
                             (504, 20), (400, 10), (404, 5)]),
    "product": dict(pool="web", median=0.058, sigma=0.62, size=22_000, ssigma=0.3,
                    statuses=[(200, 9300), (404, 380), (304, 120), (499, 40), (500, 60),
                              (502, 30), (503, 12), (504, 8)]),
    "category": dict(pool="web", median=0.081, sigma=0.66, size=18_500, ssigma=0.3,
                     statuses=[(200, 9420), (404, 210), (304, 100), (499, 45), (500, 55),
                               (502, 25), (503, 10), (504, 10)]),
    "cart": dict(pool="api", median=0.046, sigma=0.6, size=4_200, ssigma=0.4,
                 statuses=[(200, 9500), (401, 120), (499, 60), (500, 70), (502, 30),
                           (503, 12), (504, 8)]),
    "checkout": dict(pool="api", median=0.243, sigma=0.72, size=11_000, ssigma=0.35,
                     statuses=[(200, 8980), (302, 300), (400, 90), (401, 100), (499, 110),
                               (500, 190), (502, 90), (503, 35), (504, 65)]),
    "checkout_confirm": dict(pool="api", median=0.412, sigma=0.68, size=6_800, ssigma=0.3,
                             statuses=[(302, 8850), (200, 500), (400, 120), (409, 70),
                                       (499, 130), (500, 180), (502, 70), (503, 30), (504, 90)]),
    "login_get": dict(pool="web", median=0.031, sigma=0.5, size=7_600, ssigma=0.2,
                      statuses=[(200, 9820), (304, 80), (499, 30), (500, 40), (502, 20), (503, 10)]),
    "login_post": dict(pool="api", median=0.164, sigma=0.6, size=900, ssigma=0.5,
                       statuses=[(302, 6100), (401, 3500), (429, 130), (499, 40), (500, 150),
                                 (502, 60), (503, 20)]),
    "api": dict(pool="api", median=0.038, sigma=0.7, size=3_100, ssigma=0.55,
                statuses=[(200, 9330), (304, 200), (401, 130), (404, 90), (429, 40),
                          (499, 60), (500, 90), (502, 40), (503, 12), (504, 8)]),
    "static": dict(pool=None, median=0.0029, sigma=0.55, size=0, ssigma=0.0,
                   statuses=[(200, 7750), (304, 2140), (404, 90), (206, 20)]),
    "health": dict(pool=None, median=0.0004, sigma=0.3, size=17, ssigma=0.0, fixed_size=17,
                   statuses=[(200, 9990), (503, 10)]),
    "meta": dict(pool=None, median=0.0021, sigma=0.4, size=1_300, ssigma=0.5,
                 statuses=[(200, 9600), (404, 400)]),
    "scanner": dict(pool=None, median=0.0035, sigma=0.7, size=0, ssigma=0.0,
                    statuses=[(404, 7900), (403, 1600), (301, 300), (400, 120), (444, 80)]),
}

for _p in PROFILES.values():
    codes = [c for c, _ in _p["statuses"]]
    _p["codes"] = codes
    _p["codes_cw"] = cumweights([w for _, w in _p["statuses"]])

# nginx default error page sizes (bytes of body actually sent)
NGINX_ERROR_PAGE = {400: 157, 403: 153, 404: 153, 409: 0, 429: 169, 444: 0,
                    500: 157, 502: 157, 503: 190, 504: 160}

# --------------------------------------------------------------------------------------
# event record
# --------------------------------------------------------------------------------------


class Event:
    __slots__ = (
        "end", "ip", "user", "method", "uri", "proto", "status", "body_bytes", "bytes_sent",
        "req_len", "req_time", "upstream", "up_status", "up_rt", "up_ct", "up_ht", "referer",
        "ua", "xff", "req_id", "scheme", "tls_proto", "tls_cipher", "gzip_ratio", "conn",
        "conn_reqs", "node", "profile", "raw",
    )


def hexid(rng):
    return "%032x" % rng.getrandbits(128)


# --------------------------------------------------------------------------------------
# request construction
# --------------------------------------------------------------------------------------


def rotate_conn(ctx):
    """Start a fresh TCP connection for this client: new $connection, counter back to 0."""
    ctx["counter"][ctx["node"]] += 1
    ctx["conn"] = ctx["counter"][ctx["node"]]
    ctx["req_seq"] = 0


KEEPALIVE_TIMEOUT = 75.0        # nginx default; idle longer than this and the client reconnects
PROXY_BUFFERS_BYTES = 8 * 4096  # stock `proxy_buffers 8 4k`: bigger responses spool to disk


def make_request(rng, ctx, profile, method, uri, referer, end_time):
    p = PROFILES[profile]
    e = Event()
    e.profile = profile
    e.ip = ctx["ip"]
    e.user = "-"
    e.method = method
    e.uri = uri
    e.proto = ctx["proto"]
    e.referer = referer or "-"
    e.ua = ctx["ua"]
    e.xff = ctx["xff"]
    e.req_id = hexid(rng)
    e.scheme = "https" if ctx["tls"] else "http"
    e.tls_proto = ctx["tls_proto"]
    e.tls_cipher = ctx["tls_cipher"]
    e.node = ctx["node"]
    e.conn = ctx["conn"]
    ctx["req_seq"] += 1
    e.conn_reqs = ctx["req_seq"]
    e.status = pick(rng, p["codes"], p["codes_cw"])

    # --- latency -----------------------------------------------------------------------
    if e.status == 504:
        e.req_time = round(60.0 + rng.random() * 0.05, 3)          # proxy_read_timeout 60s
    elif e.status == 502:
        e.req_time = round(lognorm(rng, 0.004, 0.8, 0.001, 3.0), 3)  # connection refused, fast
    elif e.status == 503:
        e.req_time = round(lognorm(rng, 0.002, 0.6, 0.0, 0.5), 3)
    elif e.status == 499:
        e.req_time = round(lognorm(rng, p["median"] * 9, 0.9, 0.05, 75.0), 3)
    elif e.status == 304:
        e.req_time = round(lognorm(rng, min(p["median"], 0.006), 0.5, 0.0), 3)
    elif e.status in (403, 404, 444, 400):
        e.req_time = round(lognorm(rng, 0.0022, 0.6, 0.0), 3)
    elif profile == "static":
        # $request_time includes writing the body to the client, so it scales with asset
        # size -- a 512 KB bundle is not served as fast as a 3 KB icon. ~50 MB/s effective.
        base = 0.0011 + (ctx.get("asset_size") or 20_000) / 5.0e7
        e.req_time = round(lognorm(rng, base, 0.62, 0.0005, 30.0), 3)
    else:
        e.req_time = round(lognorm(rng, p["median"], p["sigma"], 0.001, 90.0), 3)

    # --- upstream ----------------------------------------------------------------------
    served_by_upstream = p["pool"] is not None and e.status not in (304, 404, 403, 444)
    if served_by_upstream:
        pool = UPSTREAM_WEB if p["pool"] == "web" else UPSTREAM_API
        e.upstream = rng.choice(pool)
        if e.status == 502:
            # nginx retries the next upstream on a refused connection
            second = rng.choice([u for u in pool if u != e.upstream])
            e.upstream = f"{e.upstream}, {second}"
            e.up_status = "502, 502"
            e.up_rt = f"{e.req_time / 2:.3f}, {e.req_time / 2:.3f}"
            e.up_ct = "-, -"
            e.up_ht = "-, -"
        elif e.status == 503:
            e.upstream = "-"
            e.up_status, e.up_rt, e.up_ct, e.up_ht = "-", "-", "-", "-"
        elif e.status == 504:
            e.up_status = "504"
            e.up_rt = f"{e.req_time:.3f}"
            e.up_ct = f"{min(0.004, e.req_time):.3f}"
            e.up_ht = "-"
        elif e.status == 499:
            e.up_status = "-"
            e.up_rt = f"{e.req_time:.3f}"
            e.up_ct = f"{lognorm(rng, 0.0012, 0.5):.3f}"
            e.up_ht = "-"
        else:
            ct = min(lognorm(rng, 0.0011, 0.45), e.req_time)
            urt = max(e.req_time - lognorm(rng, 0.0016, 0.5), 0.001)
            e.up_status = str(e.status)
            e.up_rt = f"{urt:.3f}"
            e.up_ct = f"{ct:.3f}"
            e.up_ht = f"{max(urt - lognorm(rng, 0.0009, 0.6), ct):.3f}"
    else:
        e.upstream = "-"
        e.up_status, e.up_rt, e.up_ct, e.up_ht = "-", "-", "-", "-"

    # --- response size -----------------------------------------------------------------
    # `raw` is the uncompressed body; body_bytes_sent is what actually went on the wire,
    # so when gzip applies we divide by the ratio. That keeps $gzip_ratio verifiable.
    if e.status in (304, 444):
        raw = 0
    elif e.status in NGINX_ERROR_PAGE and not (e.status == 500 and served_by_upstream):
        raw = NGINX_ERROR_PAGE[e.status]
    elif e.status == 500:
        raw = int(lognorm(rng, 940, 0.45, 120))                     # app-rendered error page
    elif e.status in (301, 302):
        raw = rng.choice([0, 138, 145, 169])
    elif e.status == 401:
        raw = int(lognorm(rng, 320, 0.4, 40))
    elif profile == "static":
        base = ctx.get("asset_size") or 20_000
        raw = int(base * rng.uniform(0.97, 1.03))
        if e.status == 206:
            raw = int(raw * rng.uniform(0.05, 0.5))
    elif p.get("fixed_size") is not None:
        raw = p["fixed_size"]
    else:
        raw = int(lognorm(rng, p["size"], p["ssigma"] or 0.3, 0))

    compressible = profile in ("home", "search", "product", "category", "cart", "checkout",
                              "checkout_confirm", "login_get", "api", "meta") or (
        profile == "static" and (".css" in uri or ".js" in uri or ".svg" in uri))
    if compressible and raw > 1024 and ctx["gzip"] and e.status not in (304, 444):
        ratio = rng.uniform(2.1, 6.4)
        e.gzip_ratio = "%.2f" % ratio
        e.body_bytes = int(raw / float(e.gzip_ratio))
    else:
        e.gzip_ratio = "-"
        e.body_bytes = raw

    # Keep the pre-gzip size: nginx buffers what the UPSTREAM sent, before compressing it,
    # so this (not body_bytes) is what decides whether the response hits a temp file.
    e.raw = raw

    e.req_len = int(lognorm(rng, 780 if ctx["kind"] != "api_client" else 430, 0.28, 120))
    if method in ("POST", "PUT", "PATCH"):
        e.req_len += int(lognorm(rng, 640, 0.7, 20))
    e.bytes_sent = e.body_bytes + int(lognorm(rng, 260, 0.18, 90))

    e.end = end_time + e.req_time
    return e


# --------------------------------------------------------------------------------------
# session generators
# --------------------------------------------------------------------------------------

PAGE_CHAIN = {
    "home": (["search", "product", "category", "END"], cumweights([35, 30, 15, 20])),
    "search": (["product", "search", "category", "END"], cumweights([54, 20, 6, 20])),
    "product": (["cart", "product", "search", "category", "END"], cumweights([17, 24, 16, 5, 38])),
    "category": (["product", "search", "category", "END"], cumweights([52, 13, 8, 27])),
    "cart": (["checkout", "product", "search", "END"], cumweights([44, 20, 6, 30])),
    "checkout": (["checkout_confirm", "cart", "END"], cumweights([56, 10, 34])),
    "checkout_confirm": (["END"], cumweights([100])),
    "login_get": (["login_post", "END"], cumweights([88, 12])),
    "login_post": (["home", "cart", "checkout", "END"], cumweights([42, 24, 16, 18])),
}
ENTRY = (["home", "search", "product", "category", "login_get"], cumweights([38, 12, 30, 12, 8]))


def page_uri(rng, page, ctx):
    if page == "home":
        return "GET", "/", "home"
    if page == "search":
        term = pick(rng, SEARCH_TERMS, SEARCH_CW).replace(" ", "+")
        q = f"/search?q={term}"
        if rng.random() < 0.22:
            q += f"&page={rng.randint(2, 9)}"
        if rng.random() < 0.15:
            q += f"&sort={rng.choice(['price_asc', 'price_desc', 'rating', 'new'])}"
        return "GET", q, "search"
    if page == "product":
        pid = ctx["rng_product"]()
        slug = pick(rng, CATEGORIES, CATEGORIES_CW)
        return "GET", f"/product/{pid}-{slug}-item", "product"
    if page == "category":
        c = pick(rng, CATEGORIES, CATEGORIES_CW)
        u = f"/category/{c}"
        if rng.random() < 0.3:
            u += f"?page={rng.randint(2, 12)}"
        return "GET", u, "category"
    if page == "cart":
        return "GET", "/cart", "cart"
    if page == "checkout":
        return ("POST" if rng.random() < 0.55 else "GET"), "/checkout", "checkout"
    if page == "checkout_confirm":
        return "POST", "/checkout/confirm", "checkout_confirm"
    if page == "login_get":
        return "GET", "/login", "login_get"
    if page == "login_post":
        return "POST", "/login", "login_post"
    raise ValueError(page)


def new_ctx(rng, kind, ips, ips_cw):
    ip = pick(rng, ips, ips_cw)
    if kind == "human_desktop":
        ua = pick(rng, UA_DESKTOP, UA_DESKTOP_CW)
    elif kind == "human_mobile":
        ua = pick(rng, UA_MOBILE, UA_MOBILE_CW)
    elif kind == "api_client":
        ua = pick(rng, UA_API, UA_API_CW)
    elif kind == "good_bot":
        ua = pick(rng, UA_GOODBOT, UA_GOODBOT_CW)
    else:
        ua = pick(rng, UA_SCANNER, UA_SCANNER_CW)

    tls = kind != "scanner" or rng.random() < 0.7
    proto = "HTTP/1.1"
    if kind in ("human_desktop", "human_mobile") and rng.random() < 0.62:
        proto = "HTTP/2.0"
    elif kind == "scanner" and rng.random() < 0.25:
        proto = "HTTP/1.0"

    return {
        "ip": ip,
        "ua": ua,
        "kind": kind,
        "tls": tls,
        "proto": proto,
        "tls_proto": ("TLSv1.3" if rng.random() < 0.86 else "TLSv1.2") if tls else "-",
        "tls_cipher": ("TLS_AES_128_GCM_SHA256" if rng.random() < 0.7
                       else "ECDHE-RSA-AES256-GCM-SHA384") if tls else "-",
        "xff": pick(rng, ips, ips_cw) if rng.random() < 0.09 else "-",
        "gzip": kind not in ("scanner",) and rng.random() < 0.93,
        "node": None,
        "conn": None,
        "req_seq": 0,
        "counter": None,
        "rng_product": lambda: 1000 + int(abs(rng.gauss(0, 1)) * 340) % 2400,
    }


def human_session(rng, ctx, t0, events):
    """Page views + the static assets each page pulls in."""
    t = t0
    page = pick(rng, *ENTRY)
    referer = "-"
    if rng.random() < 0.34:
        referer = rng.choice([
            "https://www.google.com/", "https://www.google.com/", "https://www.bing.com/",
            "https://duckduckgo.com/", "https://t.co/", "https://www.reddit.com/",
            "https://news.ycombinator.com/", "https://www.facebook.com/",
        ])
    first_page = True
    depth = 0
    while page != "END" and depth < 14:
        method, uri, profile = page_uri(rng, page, ctx)
        e = make_request(rng, ctx, profile, method, uri, referer, t)
        events.append(e)
        page_url = f"https://{SERVER_NAME}{uri}"

        # static assets pulled by the page (fewer on later views: warm cache).
        # HTTP/2 multiplexes these onto the page's own connection; HTTP/1.1 clients open
        # parallel sockets, so we rotate part-way through.
        if profile not in ("login_post", "checkout_confirm") and e.status in (200, 304):
            n = rng.randint(7, 13) if first_page else rng.randint(0, 4)
            for i in range(n):
                if ctx["proto"] == "HTTP/1.1" and i and i % 4 == 0:
                    rotate_conn(ctx)
                asset, size = pick(rng, STATIC_ASSETS, STATIC_CW)
                ctx["asset_size"] = size
                at = e.end + 0.004 + i * rng.uniform(0.002, 0.03)
                sub = make_request(rng, ctx, "static", "GET", asset, page_url, at)
                if not first_page and sub.status == 200 and rng.random() < 0.55:
                    sub.status, sub.body_bytes, sub.gzip_ratio = 304, 0, "-"
                events.append(sub)
            ctx["asset_size"] = None
            if first_page and rng.random() < 0.3:
                events.append(make_request(rng, ctx, "meta", "GET", "/favicon.ico",
                                           page_url, e.end + 0.05))

        # background XHR
        if profile in ("product", "cart", "search") and rng.random() < 0.45:
            ep = rng.choice(["/api/v1/cart", "/api/v1/recommendations", "/api/v1/session",
                             "/api/v1/inventory"])
            events.append(make_request(rng, ctx, "api", "GET", ep, page_url,
                                       e.end + rng.uniform(0.05, 0.9)))

        if e.status >= 500 and rng.random() < 0.42:
            break                                      # user gives up after an error
        think = lognorm(rng, 9.0, 0.95, 0.4, 900)
        t = e.end + think
        if think > KEEPALIVE_TIMEOUT:
            rotate_conn(ctx)                            # idle too long, socket was closed
        page = pick(rng, *PAGE_CHAIN[page])
        referer = page_url
        first_page = False
        depth += 1


def api_session(rng, ctx, t0, events):
    t = t0
    n = rng.randint(3, 40)
    for i in range(n):
        ep = rng.choice([
            "/api/v1/products?limit=24&offset=%d" % (24 * rng.randint(0, 40)),
            "/api/v1/cart", "/api/v1/session", "/api/v1/orders",
            "/api/v1/inventory?sku=%d" % rng.randint(100000, 999999),
            "/api/v1/recommendations?ctx=home", "/api/v1/search?q=%s" %
            pick(rng, SEARCH_TERMS, SEARCH_CW).replace(" ", "%20"),
        ])
        method = "GET"
        r = rng.random()
        if r < 0.12:
            method = "POST"
        elif r < 0.15:
            method = "PUT"
        elif r < 0.165:
            method = "DELETE"
        e = make_request(rng, ctx, "api", method, ep, "-", t)
        events.append(e)
        gap = lognorm(rng, 0.55, 1.1, 0.01, 120)
        t = e.end + gap
        if gap > KEEPALIVE_TIMEOUT:
            rotate_conn(ctx)


def bot_session(rng, ctx, t0, events):
    t = t0
    if rng.random() < 0.12:
        events.append(make_request(rng, ctx, "meta", "GET", "/robots.txt", "-", t))
        t += rng.uniform(0.3, 3.0)
    if rng.random() < 0.06:
        events.append(make_request(rng, ctx, "meta", "GET", "/sitemap.xml", "-", t))
        t += rng.uniform(0.3, 3.0)
    for i in range(rng.randint(2, 28)):
        r = rng.random()
        if r < 0.68:
            pid = 1000 + rng.randint(0, 2399)
            uri, prof = f"/product/{pid}-{pick(rng, CATEGORIES, CATEGORIES_CW)}-item", "product"
        elif r < 0.9:
            uri, prof = f"/category/{pick(rng, CATEGORIES, CATEGORIES_CW)}", "category"
        else:
            uri, prof = "/", "home"
        e = make_request(rng, ctx, prof, "GET" if rng.random() < 0.96 else "HEAD", uri, "-", t)
        events.append(e)
        gap = lognorm(rng, 2.2, 0.9, 0.2, 60)
        t = e.end + gap
        if gap > KEEPALIVE_TIMEOUT:
            rotate_conn(ctx)


def scanner_session(rng, ctx, t0, events):
    t = t0
    for i in range(rng.randint(1, 24)):
        uri = pick(rng, SCANNER_PATHS, SCANNER_CW)
        method = "GET"
        if rng.random() < 0.18:
            method = rng.choice(["POST", "HEAD", "OPTIONS", "PROPFIND"])
        e = make_request(rng, ctx, "scanner", method, uri, "-", t)
        events.append(e)
        t = e.end + lognorm(rng, 0.4, 1.2, 0.01, 90)
        if rng.random() < 0.6:
            rotate_conn(ctx)                 # scanners mostly don't bother with keepalive


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------

_SAFE = set(range(0x20, 0x7F)) - {0x22, 0x5C}


def esc_default(s):
    """nginx `escape=default`: escape ", \\, and anything outside printable ASCII."""
    if all(ord(c) in _SAFE for c in s):
        return s
    out = []
    for c in s:
        o = ord(c)
        if o in _SAFE:
            out.append(c)
        elif c == '"':
            out.append('\\x22')
        elif c == '\\':
            out.append('\\x5C')
        else:
            out.extend('\\x%02X' % b for b in c.encode("utf-8"))
    return "".join(out)


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_time_local(dt):
    return "%02d/%s/%d:%02d:%02d:%02d +0000" % (
        dt.day, MONTHS[dt.month - 1], dt.year, dt.hour, dt.minute, dt.second)


def render_combined(e, dt):
    return '%s - %s [%s] "%s %s %s" %d %d "%s" "%s"' % (
        e.ip, e.user, fmt_time_local(dt), e.method, esc_default(e.uri), e.proto,
        e.status, e.body_bytes, esc_default(e.referer), esc_default(e.ua))


def render_json(e, dt, total_ms):
    # $msec is truncated to the millisecond, never rounded: rounding up could push it into
    # the next second and disagree with $time_local / $time_iso8601 on the same request.
    return json.dumps({
        "time_iso8601": dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "msec": "%d.%03d" % divmod(total_ms, 1000),
        "hostname": e.node,
        "remote_addr": e.ip,
        "remote_user": e.user,
        "http_x_forwarded_for": e.xff,
        "request_method": e.method,
        "request_uri": e.uri,
        "server_protocol": e.proto,
        "scheme": e.scheme,
        "host": SERVER_NAME,
        "server_name": SERVER_NAME,
        "status": e.status,
        "body_bytes_sent": e.body_bytes,
        "bytes_sent": e.bytes_sent,
        "request_length": e.req_len,
        "request_time": e.req_time,
        "upstream_addr": e.upstream,
        "upstream_status": e.up_status,
        "upstream_response_time": e.up_rt,
        "upstream_connect_time": e.up_ct,
        "upstream_header_time": e.up_ht,
        "http_referer": e.referer,
        "http_user_agent": e.ua,
        "request_id": e.req_id,
        "ssl_protocol": e.tls_proto,
        "ssl_cipher": e.tls_cipher,
        "gzip_ratio": e.gzip_ratio,
        "connection": e.conn,
        "connection_requests": e.conn_reqs,
    }, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------------------
# error.log
# --------------------------------------------------------------------------------------


def worker_pid(rng, nodes_meta, node, epoch):
    m = nodes_meta[node]
    return rng.choice(m["pids_before"] if epoch < m["reload_at"] else m["pids_after"])


def error_lines_for(rng, e, dt, epoch, nodes_meta):
    """Return zero or more (epoch, line) error-log entries correlated with `e`."""
    out = []
    if e.profile == "health":
        return out                      # probes never produce nginx error-log entries here
    pid = worker_pid(rng, nodes_meta, e.node, epoch)
    cid = e.conn
    req = '%s %s %s' % (e.method, e.uri, e.proto)
    tail = ('client: %s, server: %s, request: "%s"' % (e.ip, SERVER_NAME, req))
    up_first = e.upstream.split(",")[0].strip()
    up_url = 'http://%s%s' % (up_first, e.uri.split("?")[0])
    host_tail = ', host: "%s"' % SERVER_NAME
    ref_tail = (', referrer: "%s"' % e.referer) if e.referer != "-" else ""

    def emit(level, msg, ts=None):
        out.append((ts if ts is not None else epoch,
                    "%s [%s] %d#%d: *%d %s" % (fmt_err_time(dt), level, pid, 0, cid, msg)))

    if e.status == 504:
        emit("error", "upstream timed out (110: Connection timed out) while reading "
                      "response header from upstream, %s, upstream: \"%s\"%s%s"
                      % (tail, up_url, host_tail, ref_tail))
    elif e.status == 502:
        emit("error", "connect() failed (111: Connection refused) while connecting to "
                      "upstream, %s, upstream: \"%s\"%s%s"
                      % (tail, up_url, host_tail, ref_tail))
        # nginx retries the next peer; that one fails too, hence the 502 reaching the client
        second = e.upstream.split(",")[1].strip() if "," in e.upstream else up_first
        emit("error", "connect() failed (111: Connection refused) while connecting to "
                      "upstream, %s, upstream: \"http://%s%s\"%s%s"
                      % (tail, second, e.uri.split("?")[0], host_tail, ref_tail),
             ts=epoch + 0.001)
    elif e.status == 503:
        if rng.random() < 0.55:
            zone = "perip" if e.profile != "login_post" else "login"
            emit("error", "limiting requests, excess: %.3f by zone \"%s\", %s%s%s"
                          % (rng.uniform(1.0, 24.0), zone, tail, host_tail, ref_tail))
        else:
            emit("error", "no live upstreams while connecting to upstream, %s, upstream: "
                          "\"%s\"%s%s" % (tail, up_url, host_tail, ref_tail))
    elif e.status == 429 and rng.random() < 0.4:
        emit("error", "limiting requests, excess: %.3f by zone \"login\", %s%s%s"
                      % (rng.uniform(1.0, 9.0), tail, host_tail, ref_tail))
    elif e.status == 499 and rng.random() < 0.35:
        emit("info", "client prematurely closed connection while %s, %s, upstream: \"%s\"%s%s"
                     % (rng.choice(["sending request to upstream",
                                    "reading upstream response header"]),
                        tail, up_url, host_tail, ref_tail))
    elif e.status == 500 and rng.random() < 0.55:
        which = rng.random()
        if which < 0.5:
            emit("error", "upstream sent too big header while reading response header from "
                          "upstream, %s, upstream: \"%s\"%s%s"
                          % (tail, up_url, host_tail, ref_tail))
        elif which < 0.8:
            emit("error", "upstream prematurely closed connection while reading response "
                          "header from upstream, %s, upstream: \"%s\"%s%s"
                          % (tail, up_url, host_tail, ref_tail))
        else:
            emit("error", "recv() failed (104: Connection reset by peer) while reading "
                          "response header from upstream, %s, upstream: \"%s\"%s%s"
                          % (tail, up_url, host_tail, ref_tail))
    elif e.status == 403 and rng.random() < 0.5:
        emit("error", "access forbidden by rule, %s%s" % (tail, host_tail))
    elif e.status == 404 and e.profile in ("product", "category", "static") and rng.random() < 0.22:
        path = "/var/www/shop/public" + e.uri.split("?")[0]
        emit("error", "open() \"%s\" failed (2: No such file or directory), %s%s%s"
                      % (path, tail, host_tail, ref_tail))
    elif e.status == 444 and rng.random() < 0.3:
        emit("info", "client closed connection while waiting for request, client: %s, "
                     "server: %s:443" % (e.ip, "0.0.0.0"))

    # ---- [warn] level ----------------------------------------------------------------
    # These three are the everyday warnings a real nginx error_log is mostly made of. They
    # are not failures: each one is a tuning signal attached to a request that succeeded (or
    # to a peer being taken out of rotation), which is exactly why grouping an error log by
    # `level` tells you so little.

    # 1. Response bigger than proxy_buffers, so nginx spools it to disk. With the stock
    #    `proxy_buffers 8 4k` this happens on every proxied response over 32 KB, and nginx
    #    logs it every single time.
    if up_first != "-" and e.raw > PROXY_BUFFERS_BYTES:
        out.append((epoch, "%s [warn] %d#%d: *%d an upstream response is buffered to a "
                           "temporary file /var/cache/nginx/proxy_temp/%d/%02d/%010d while "
                           "reading upstream, %s, upstream: \"%s\"%s%s"
                    % (fmt_err_time(dt), pid, 0, cid, rng.randint(0, 9), rng.randint(0, 99),
                       rng.randint(1, 9999999999), tail, up_url, host_tail, ref_tail)))

    # 2. limit_req burst: within the burst nginx DELAYS the request and warns; only past the
    #    burst does it reject with 503 and log [error] "limiting requests". Same mechanism,
    #    two levels -- the warn lines hang off requests that succeeded.
    if e.status in (200, 302) and e.profile in ("api", "login_post", "search") \
            and rng.random() < 0.004:
        zone = "login" if e.profile == "login_post" else "perip"
        out.append((epoch, "%s [warn] %d#%d: *%d delaying request, excess: %.3f by zone "
                           "\"%s\", %s%s%s"
                    % (fmt_err_time(dt), pid, 0, cid, rng.uniform(0.01, 0.99), zone,
                       tail, host_tail, ref_tail)))

    # 3. max_fails reached for a peer, so nginx drops it from the pool for fail_timeout.
    #    Pairs with the connection-refused 502s and the no-live-upstreams 503s.
    if e.status in (502, 503) and rng.random() < 0.35:
        out.append((epoch + 0.002,
                    "%s [warn] %d#%d: *%d upstream server temporarily disabled while "
                    "connecting to upstream, %s, upstream: \"%s\"%s%s"
                    % (fmt_err_time(dt), pid, 0, cid, tail, up_url, host_tail, ref_tail)))
    return out


def fmt_err_time(dt):
    return "%d/%02d/%02d %02d:%02d:%02d" % (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def lifecycle_lines(rng, nodes_meta, conn_max):
    """nginx lifecycle / TLS noise that is not tied to a specific access-log entry."""
    out = []
    for node, m in nodes_meta.items():
        # a rolling config reload mid-morning: old workers drain, new ones take over
        t = m["reload_at"]
        dt = DAY + timedelta(seconds=t)
        master = m["master_pid"]
        cli_pid = rng.randint(2000, 60000)
        out.append((t, "%s [notice] %d#%d: signal process started" % (fmt_err_time(dt), cli_pid, 0)))
        out.append((t + 0.03, "%s [notice] %d#%d: signal 1 (SIGHUP) received from %d, reconfiguring"
                    % (fmt_err_time(dt), master, 0, cli_pid)))
        out.append((t + 0.05, "%s [notice] %d#%d: reconfiguring" % (fmt_err_time(dt), master, 0)))
        out.append((t + 0.4, "%s [notice] %d#%d: using the \"epoll\" event method"
                    % (fmt_err_time(dt), master, 0)))
        out.append((t + 0.41, "%s [notice] %d#%d: start worker processes"
                    % (fmt_err_time(dt), master, 0)))
        for pid in m["pids_after"]:
            out.append((t + 0.42, "%s [notice] %d#%d: start worker process %d"
                        % (fmt_err_time(DAY + timedelta(seconds=t + 0.42)), master, 0, pid)))
        for i, pid in enumerate(m["pids_before"]):
            gt = t + 0.5 + i * rng.uniform(0.01, 0.2)
            out.append((gt, "%s [notice] %d#%d: gracefully shutting down"
                        % (fmt_err_time(DAY + timedelta(seconds=gt)), pid, 0)))
            xt = gt + rng.uniform(0.2, 18.0)          # drains in-flight requests first
            out.append((xt, "%s [notice] %d#%d: exiting"
                        % (fmt_err_time(DAY + timedelta(seconds=xt)), pid, 0)))
            out.append((xt + 0.01, "%s [notice] %d#%d: exit"
                        % (fmt_err_time(DAY + timedelta(seconds=xt + 0.01)), pid, 0)))
            out.append((xt + 0.02, "%s [notice] %d#%d: signal 17 (SIGCHLD) received from %d"
                        % (fmt_err_time(DAY + timedelta(seconds=xt + 0.02)), master, 0, pid)))

        # TLS handshake failures, sprinkled through the day
        for _ in range(rng.randint(90, 160)):
            t = rng.random() * 86400
            dt = DAY + timedelta(seconds=t)
            pid = worker_pid(rng, nodes_meta, node, t)
            # failed handshakes never reach the access log, so these ids are interleaved
            # with, but distinct from, the ones that do
            cid = rng.randint(CONN_BASE[node] + 1, conn_max[node])
            kind = rng.random()
            # All three variants fail before nginx can log a request, so they legitimately
            # have no access-log counterpart -- and, like real nginx, carry no `request:`
            # field. Anything that WOULD produce an access-log line belongs in
            # error_lines_for() instead, tied to a real event.
            if kind < 0.45:
                msg = ("SSL_do_handshake() failed (SSL: error:0A000102:SSL routines::unsupported "
                       "protocol) while SSL handshaking, client: %s, server: 0.0.0.0:443"
                       % _rand_public_v4(rng))
                lvl = "crit"
            elif kind < 0.75:
                msg = ("SSL_do_handshake() failed (SSL: error:0A000126:SSL routines::unexpected "
                       "eof while reading) while SSL handshaking, client: %s, server: 0.0.0.0:443"
                       % _rand_public_v4(rng))
                lvl = "info"
            else:
                msg = ("client timed out (110: Connection timed out) while SSL handshaking, "
                       "client: %s, server: 0.0.0.0:443" % _rand_public_v4(rng))
                lvl = "info"
            out.append((t, "%s [%s] %d#%d: *%d %s" % (fmt_err_time(dt), lvl, pid, 0, cid, msg)))

        # worker connection pressure, a handful of times at peak
        for _ in range(rng.randint(2, 6)):
            t = rng.uniform(11 * 3600, 21 * 3600)
            dt = DAY + timedelta(seconds=t)
            out.append((t, "%s [alert] %d#%d: %d worker_connections are not enough"
                        % (fmt_err_time(dt), worker_pid(rng, nodes_meta, node, t), 0, 4096)))
    return out


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=500_000)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    args = ap.parse_args()

    rng = random.Random(SEED)
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    print("building ip pool ...")
    ips, ips_cw = build_ip_pool(rng)

    kinds = ["human_desktop", "human_mobile", "api_client", "good_bot", "scanner"]
    kinds_cw = cumweights([40, 33, 13, 9, 5])

    events = []

    conn_counter = dict(CONN_BASE)

    # steady health-check beat from the load balancer / kubelet: every 10s, 2 probers
    print("generating health checks ...")
    for probe_ip in ("10.0.1.21", "10.0.1.22"):
        ctx = new_ctx(rng, "api_client", ips, ips_cw)
        ctx.update(ip=probe_ip, ua=pick(rng, UA_MONITOR, UA_MONITOR_CW), tls=False,
                   proto="HTTP/1.1", tls_proto="-", tls_cipher="-", xff="-", gzip=False,
                   counter=conn_counter)
        for i in range(8640):
            ctx["node"] = NGINX_NODES[i % 3]
            rotate_conn(ctx)                    # each probe opens a fresh connection
            events.append(make_request(rng, ctx, "health", "GET", "/health", "-",
                                       i * 10.0 + rng.uniform(0, 0.4)))

    print("generating sessions ...")
    target = args.requests
    while len(events) < target:
        kind = pick(rng, kinds, kinds_cw)
        ctx = new_ctx(rng, kind, ips, ips_cw)
        ctx["node"] = rng.choice(NGINX_NODES)
        ctx["counter"] = conn_counter
        rotate_conn(ctx)
        ctx["asset_size"] = None
        t0 = session_start(rng)
        if kind in ("human_desktop", "human_mobile"):
            human_session(rng, ctx, t0, events)
        elif kind == "api_client":
            api_session(rng, ctx, t0, events)
        elif kind == "good_bot":
            bot_session(rng, ctx, t0, events)
        else:
            scanner_session(rng, ctx, t0, events)

    # keep only what falls inside the 24h window, then order by completion time
    events = [e for e in events if 0.0 <= e.end < 86400.0]
    events.sort(key=lambda e: e.end)
    print(f"  {len(events):,} requests in window")

    nodes_meta = {}
    for i, n in enumerate(NGINX_NODES):
        nodes_meta[n] = {
            "master_pid": rng.randint(900, 1400),
            "pids_before": sorted(rng.sample(range(1400, 40000), 8)),
            "pids_after": sorted(rng.sample(range(40000, 65000), 8)),
            # rolling reload: one node at a time, a few minutes apart
            "reload_at": 9 * 3600 + 41 * 60 + i * 214 + rng.uniform(0, 40),
        }
    err = lifecycle_lines(rng, nodes_meta, conn_counter)

    print("writing access logs ...")
    p_comb = os.path.join(outdir, "access.log")
    p_json = os.path.join(outdir, "access.json.log")
    p_err = os.path.join(outdir, "error.log")

    epoch0_ms = int(DAY.timestamp()) * 1000
    buf_c, buf_j = [], []
    with open(p_comb, "w", encoding="utf-8", newline="\n") as fc, \
            open(p_json, "w", encoding="utf-8", newline="\n") as fj:
        for e in events:
            # floor to the millisecond once, then derive both renderings from that single
            # value so the two files can never disagree about when a request finished
            end_ms = int(e.end * 1000)
            dt = DAY + timedelta(milliseconds=end_ms)
            buf_c.append(render_combined(e, dt))
            buf_j.append(render_json(e, dt, epoch0_ms + end_ms))
            err.extend(error_lines_for(rng, e, dt, e.end, nodes_meta))
            if len(buf_c) >= 20000:
                fc.write("\n".join(buf_c) + "\n")
                fj.write("\n".join(buf_j) + "\n")
                buf_c.clear()
                buf_j.clear()
        if buf_c:
            fc.write("\n".join(buf_c) + "\n")
            fj.write("\n".join(buf_j) + "\n")

    print("writing error log ...")
    err.sort(key=lambda x: x[0])
    with open(p_err, "w", encoding="utf-8", newline="\n") as fe:
        fe.write("\n".join(line for _, line in err) + "\n")

    # ------------------------------------------------------------------ summary
    from collections import Counter
    st = Counter(e.status for e in events)
    fam = Counter(str(e.status)[0] + "xx" for e in events)
    print(f"\naccess.log       {len(events):,} lines  {os.path.getsize(p_comb) / 1e6:8.1f} MB")
    print(f"access.json.log  {len(events):,} lines  {os.path.getsize(p_json) / 1e6:8.1f} MB")
    print(f"error.log        {len(err):,} lines  {os.path.getsize(p_err) / 1e6:8.1f} MB")
    print("\nstatus families:")
    for k in sorted(fam):
        print(f"  {k}  {fam[k]:>8,}  {100.0 * fam[k] / len(events):5.2f}%")
    print("\ntop statuses:", ", ".join(f"{c}={n:,}" for c, n in st.most_common(12)))
    print("distinct client ips:", len({e.ip for e in events}))


if __name__ == "__main__":
    main()
