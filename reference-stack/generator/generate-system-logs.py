#!/usr/bin/env python3
"""
Generate the syslog/auth corpus behind the Elastic `system` integration's four log dashboards.

  data/system/auth.log      sshd, sudo, useradd/groupadd   -> logs-system.auth
  data/system/syslog.log    systemd, CRON, kernel, ...      -> logs-system.syslog

Both are classic syslog format:

    Aug 17 00:00:01 web-edge-01 sshd[14231]: Accepted publickey for deploy from ...

The hosts are the SAME machines the rest of the corpus describes -- the three nginx edge
nodes, an apache docs node and the mysql primary -- so `system` monitors the fleet the other
four integrations serve, rather than being a disconnected fifth dataset.

Two pipeline behaviours measured against `_simulate`, not assumed, because they differ
*within this one integration* and they decide what the shifter has to rewrite:

  logs-system.syslog   RE-DERIVES @timestamp from the message text, discarding the value on
                       the document (processors have no `@timestamp == null` guard).
  logs-system.auth     KEEPS the document's @timestamp if there is one -- its date
                       processors are gated on `ctx['@timestamp'] == null` -- and only falls
                       back to the text otherwise.

So the loader must do BOTH: rewrite the timestamp inside the line (for syslog) and set the
document's @timestamp (for auth). Getting only one right silently breaks one of the two
streams. Note also that syslog format carries **no year**, so a document with no @timestamp
is dated to whatever year the pipeline happens to run in -- another reason to set it.

Deterministic: fixed seed, no wall-clock reads.

Usage:  python3 generate-system-logs.py
"""
import argparse, json, os, random, sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402

SEED = 20260921
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# the fleet the other four integrations already describe
HOSTS = ["web-edge-01", "web-edge-02", "web-edge-03", "docs-web-01", "mysql-primary-01"]
HOST_CW = ng.cumweights([26, 24, 22, 16, 12])

# ---------------------------------------------------------------- ssh
# Operators log in with keys; humans occasionally with a password.
SSH_OK = [("deploy", "publickey"), ("ci-runner", "publickey"), ("deploy", "publickey"),
          ("andre", "publickey"), ("andre", "password"), ("ops", "password")]
SSH_OK_CW = ng.cumweights([30, 22, 14, 12, 12, 10])
# What actually knocks on a public SSH port all day.
SSH_BAD_USERS = ["root", "admin", "oracle", "postgres", "ubuntu", "test", "git", "pi",
                 "jenkins", "ftpuser", "mysql", "user", "guest", "support"]
SSH_BAD_CW = ng.cumweights([34, 14, 6, 6, 9, 7, 4, 4, 3, 3, 3, 3, 2, 2])

# ---------------------------------------------------------------- sudo
SUDO_USERS = ["deploy", "andre", "ops", "ci-runner", "intern"]
SUDO_USERS_CW = ng.cumweights([32, 26, 20, 16, 6])
SUDO_COMMANDS = [
    ("/usr/bin/systemctl restart nginx", 16),
    ("/usr/bin/systemctl status nginx", 14),
    ("/usr/bin/apt update", 11),
    ("/usr/bin/journalctl -u nginx -n 200", 10),
    ("/bin/systemctl reload php8.2-fpm", 8),
    ("/usr/bin/tail -f /var/log/nginx/error.log", 8),
    ("/usr/sbin/nginx -t", 7),
    ("/usr/bin/docker ps", 6),
    ("/bin/chown -R www-data:www-data /var/www", 5),
    ("/usr/bin/certbot renew", 4),
    ("/usr/bin/mysqldump --all-databases", 3),
    ("/bin/cat /etc/shadow", 2),
]
SUDO_CMD_CW = ng.cumweights([w for _c, w in SUDO_COMMANDS])
# `sudo` logs an error clause instead of running the command
SUDO_ERRORS = [("user NOT in sudoers", 52), ("3 incorrect password attempts", 31),
               ("command not allowed", 17)]
SUDO_ERR_CW = ng.cumweights([w for _e, w in SUDO_ERRORS])

# ---------------------------------------------------------------- useradd / groupadd
# New accounts are rare and arrive in a maintenance window, which is what makes
# "New users over time" a readable chart rather than uniform noise.
NEW_ACCOUNTS = [
    ("svc-metrics", 1101, "/home/svc-metrics", "/usr/sbin/nologin"),
    ("svc-backup", 1102, "/home/svc-backup", "/usr/sbin/nologin"),
    ("jkowalski", 1103, "/home/jkowalski", "/bin/bash"),
    ("mrossi", 1104, "/home/mrossi", "/bin/bash"),
    ("intern", 1105, "/home/intern", "/bin/bash"),
    ("svc-exporter", 1106, "/home/svc-exporter", "/usr/sbin/nologin"),
    ("aschmidt", 1107, "/home/aschmidt", "/bin/zsh"),
    ("svc-deploy", 1108, "/home/svc-deploy", "/bin/sh"),
    ("tnguyen", 1109, "/home/tnguyen", "/bin/bash"),
    ("svc-agent", 1110, "/home/svc-agent", "/usr/sbin/nologin"),
    ("lmartin", 1111, "/home/lmartin", "/bin/bash"),
    ("svc-scraper", 1112, "/home/svc-scraper", "/usr/sbin/nologin"),
]

# ---------------------------------------------------------------- syslog
SYSLOG_LINES = [
    ("systemd", 1, "Started Daily apt download activities.", 9),
    ("systemd", 1, "Starting Cleanup of Temporary Directories...", 8),
    ("systemd", 1, "Finished Cleanup of Temporary Directories.", 8),
    ("systemd", 1, "Reloading nginx configuration.", 5),
    ("CRON", 0, "(root) CMD (cd / && run-parts --report /etc/cron.hourly)", 14),
    ("CRON", 0, "(www-data) CMD (php /var/www/artisan schedule:run)", 12),
    ("CRON", 0, "(root) CMD (/usr/local/bin/backup.sh --incremental)", 6),
    ("kernel", None, "[%(up)s] TCP: request_sock_TCP: Possible SYN flooding on port 443. Sending cookies.", 5),
    ("kernel", None, "[%(up)s] audit: type=1400 audit(%(up)s:%(n)s): apparmor=\"DENIED\" operation=\"open\"", 4),
    ("snapd", 0, "Acquiring snap lock for auto-refresh", 4),
    ("dhclient", 0, "DHCPACK of 10.0.1.%(n)s from 10.0.1.1", 3),
    ("chronyd", 0, "Selected source 185.125.190.56 (ntp.ubuntu.com)", 3),
    ("sshd", 0, "Server listening on 0.0.0.0 port 22.", 2),
    ("systemd-logind", 0, "New session %(n)s of user deploy.", 7),
    ("systemd-logind", 0, "Removed session %(n)s.", 6),
]
SYSLOG_CW = ng.cumweights([w for _p, _pid, _m, w in SYSLOG_LINES])


def stamp(dt):
    """Syslog header: `Aug 17 00:00:01`. Day is space-padded, as syslog does."""
    return "%s %2d %02d:%02d:%02d" % (MONTHS[dt.month - 1], dt.day,
                                      dt.hour, dt.minute, dt.second)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh", type=int, default=14000, help="sshd lines")
    ap.add_argument("--sudo", type=int, default=2200, help="sudo lines")
    ap.add_argument("--syslog", type=int, default=40000, help="syslog lines")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    args = ap.parse_args()
    root = os.path.abspath(args.out)
    os.makedirs(os.path.join(root, "system"), exist_ok=True)
    rng = random.Random(SEED)

    # A shared attacker IP pool so the SSH source-country breakdown has real geo spread,
    # and a much smaller set of operator IPs for the successful logins.
    attackers = [ng._rand_public_v4(rng) for _ in range(180)]
    operators = ["203.0.113.%d" % rng.randint(2, 60) for _ in range(6)] + \
                ["198.51.100.%d" % rng.randint(2, 60) for _ in range(4)]

    # ------------------------------------------------------------------ auth.log
    auth = []
    counts = {"Accepted": 0, "Failed": 0, "Invalid": 0,
              "sudo_ok": 0, "sudo_err": 0, "useradd": 0, "groupadd": 0}

    for _ in range(args.ssh):
        off = ng.session_start(rng)
        if not (0.0 <= off < 86400.0):
            continue
        host = ng.pick(rng, HOSTS, HOST_CW)
        r = rng.random()
        if r < 0.18:                                   # a real login
            user, method = ng.pick(rng, SSH_OK, SSH_OK_CW)
            ip = rng.choice(operators)
            sig = "" if method == "password" else ": RSA SHA256:%s" % ng.hexid(rng)[:24]
            msg = "Accepted %s for %s from %s port %d ssh2%s" % (
                method, user, ip, rng.randint(32768, 60999), sig)
            counts["Accepted"] += 1
        elif r < 0.72:                                 # password guessing
            user = ng.pick(rng, SSH_BAD_USERS, SSH_BAD_CW)
            ip = rng.choice(attackers)
            # `invalid user` is the variant whose grok leaves a LEADING SPACE in
            # user.name -- see the module docstring of verify-system.sh. Kept because it is
            # what the real integration produces.
            invalid = "invalid user " if user != "root" and rng.random() < 0.7 else ""
            msg = "Failed password for %s%s from %s port %d ssh2" % (
                invalid, user, ip, rng.randint(32768, 60999))
            counts["Failed"] += 1
        else:                                          # user does not exist at all
            user = ng.pick(rng, SSH_BAD_USERS, SSH_BAD_CW)
            ip = rng.choice(attackers)
            msg = "Invalid user %s from %s port %d" % (
                user, ip, rng.randint(32768, 60999))
            counts["Invalid"] += 1
        auth.append((off, host, "sshd", rng.randint(1000, 99999), msg))

    for _ in range(args.sudo):
        off = ng.session_start(rng)
        if not (0.0 <= off < 86400.0):
            continue
        host = ng.pick(rng, HOSTS, HOST_CW)
        user = ng.pick(rng, SUDO_USERS, SUDO_USERS_CW)
        cmd = ng.pick(rng, SUDO_COMMANDS, SUDO_CMD_CW)[0]
        tty = "pts/%d" % rng.randint(0, 3)
        pwd = "/home/%s" % user
        # `intern` is the account that keeps hitting the sudoers wall
        err_chance = 0.55 if user == "intern" else 0.02
        if rng.random() < err_chance:
            err = ng.pick(rng, SUDO_ERRORS, SUDO_ERR_CW)[0]
            msg = "  %s : %s ; TTY=%s ; PWD=%s ; USER=root ; COMMAND=%s" % (
                user, err, tty, pwd, cmd)
            counts["sudo_err"] += 1
        else:
            msg = "  %s : TTY=%s ; PWD=%s ; USER=root ; COMMAND=%s" % (user, tty, pwd, cmd)
            counts["sudo_ok"] += 1
        auth.append((off, host, "sudo", None, msg))

    # the maintenance window: 09:40-10:20, every new account created on the mysql primary
    base = 9 * 3600 + 40 * 60
    for i, (name, uid, home, shell) in enumerate(NEW_ACCOUNTS):
        off = base + i * rng.randint(120, 260)
        host = "mysql-primary-01" if name.startswith("svc-") else "web-edge-01"
        auth.append((off, host, "groupadd", rng.randint(9000, 9999),
                     "new group: name=%s, GID=%d" % (name, uid)))
        counts["groupadd"] += 1
        auth.append((off + 1, host, "useradd", rng.randint(9000, 9999),
                     "new user: name=%s, UID=%d, GID=%d, home=%s, shell=%s"
                     % (name, uid, uid, home, shell)))
        counts["useradd"] += 1

    auth.sort(key=lambda e: e[0])
    auth_path = os.path.join(root, "system", "auth.log")
    with open(auth_path, "w", encoding="utf-8", newline="\n") as fh:
        for off, host, proc, pid, msg in auth:
            ts = ng.DAY + timedelta(seconds=off)
            tag = proc if pid is None else "%s[%d]" % (proc, pid)
            fh.write("%s %s %s: %s\n" % (stamp(ts), host, tag, msg))

    # ------------------------------------------------------------------ syslog.log
    sysl = []
    for _ in range(args.syslog):
        off = ng.session_start(rng)
        if not (0.0 <= off < 86400.0):
            continue
        host = ng.pick(rng, HOSTS, HOST_CW)
        proc, pid0, tmpl, _w = ng.pick(rng, SYSLOG_LINES, SYSLOG_CW)
        msg = tmpl % {"up": "%d.%06d" % (int(off) + 100000, rng.randint(0, 999999)),
                      "n": rng.randint(2, 250)} if "%(" in tmpl else tmpl
        pid = None if pid0 is None else (1 if pid0 == 1 else rng.randint(300, 32000))
        sysl.append((off, host, proc, pid, msg))
    sysl.sort(key=lambda e: e[0])
    sys_path = os.path.join(root, "system", "syslog.log")
    with open(sys_path, "w", encoding="utf-8", newline="\n") as fh:
        for off, host, proc, pid, msg in sysl:
            ts = ng.DAY + timedelta(seconds=off)
            tag = proc if pid is None else "%s[%d]" % (proc, pid)
            fh.write("%s %s %s: %s\n" % (stamp(ts), host, tag, msg))

    # ------------------------------------------------------------------ summary
    print("auth.log     %s lines  %.1f MB" % (
        "{:,}".format(len(auth)), os.path.getsize(auth_path) / 1e6))
    print("syslog.log   %s lines  %.1f MB" % (
        "{:,}".format(len(sysl)), os.path.getsize(sys_path) / 1e6))
    print("\ninvariants both platforms must reproduce:")
    ssh_total = counts["Accepted"] + counts["Failed"] + counts["Invalid"]
    print("  ssh Accepted / Failed / Invalid = %s / %s / %s  (sum %s)" % (
        "{:,}".format(counts["Accepted"]), "{:,}".format(counts["Failed"]),
        "{:,}".format(counts["Invalid"]), "{:,}".format(ssh_total)))
    print("  sudo ok / error                 = %s / %s  (sum %s)" % (
        "{:,}".format(counts["sudo_ok"]), "{:,}".format(counts["sudo_err"]),
        "{:,}".format(counts["sudo_ok"] + counts["sudo_err"])))
    print("  useradd == groupadd             = %d == %d   %s" % (
        counts["useradd"], counts["groupadd"],
        "match" if counts["useradd"] == counts["groupadd"] else "MISMATCH"))
    print("  auth.log total                  = %s  %s" % (
        "{:,}".format(len(auth)),
        "match" if len(auth) == ssh_total + counts["sudo_ok"] + counts["sudo_err"]
        + counts["useradd"] + counts["groupadd"] else "MISMATCH"))
    print("  distinct hosts                  = %d" % len(HOSTS))
    print("  new accounts                    = %d" % len(NEW_ACCOUNTS))


if __name__ == "__main__":
    main()
