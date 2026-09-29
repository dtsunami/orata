#!/usr/bin/env python3
#
# orata-web.py -- minimal LAN admin UI for the orata astdb books.
#
# stdlib only. No framework, no pip, no venv, no database. One file.
#
# Reads via `asterisk -rx "database show ..."`. Writes by shelling out to
# orata-cnam.sh, so mutation semantics live in exactly one place and the
# CLI stays the source of truth.
#
# SECURITY: this runs as the `asterisk` user because it needs the Asterisk
# CLI. A compromise here is a compromise of call routing. Bind it to
# localhost or a WireGuard address. Never a WAN interface. Never a
# port-forward. See docs/web-ui.md.
#

import hmac
import html
import os
import re
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF_PATH = os.environ.get("ORATA_WEB_CONF", "/etc/orata/web.conf")


def load_conf(path):
    cfg = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


CFG = load_conf(CONF_PATH)
BIND = CFG.get("ORATA_WEB_BIND", "127.0.0.1")
PORT = int(CFG.get("ORATA_WEB_PORT", "8088"))
TOKEN = CFG.get("ORATA_WEB_TOKEN", "")
CNAM_SH = CFG.get("ORATA_CNAM_SH", "/usr/local/bin/orata-cnam.sh")
LOG = CFG.get("ORATA_WEB_LOG", "/var/log/orata/announce.log")

# family -> (heading, add subcommand, del subcommand, second field label)
# a second label of None means the entry is just a number
BOOKS = [
    ("cnam", "Name book", "add", "del", "Spoken name"),
    ("allow", "Whitelist (skips the robocall gate)", "allow-add", "allow-del", None),
    ("block", "Blocklist (hung up on, never rings)", "block-add", "block-del", None),
    ("alexasensor", "Alexa sensor map", "sensor-add", "sensor-del", "endpointId"),
]
BOOK_BY_FAMILY = {b[0]: b for b in BOOKS}

NUM_RE = re.compile(r"^[0-9]{3,15}$")
ROW_RE = re.compile(r"^/([^/]+)/(\S+)\s*:\s*(.*)$")


def clean_number(raw):
    """Digits only. voip.ms presents 11-digit NANP; allow 3-15 for odd cases."""
    digits = re.sub(r"[^0-9]", "", raw or "")
    return digits if NUM_RE.match(digits) else None


def clean_value(raw):
    """
    Strip anything that could confuse the Asterisk CLI's own string parser.
    We invoke subprocess without a shell, so this is not shell escaping --
    it is protection against `database put` seeing stray quotes.
    """
    val = (raw or "").strip()
    val = re.sub(r'[\x00-\x1f"\\`$]', "", val)
    return val[:64]


def db_show(family):
    try:
        out = subprocess.run(
            ["asterisk", "-rx", "database show %s" % family],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        m = ROW_RE.match(line.strip())
        if m and m.group(1) == family:
            rows.append((m.group(2), m.group(3).strip()))
    return sorted(rows)


def cnam_cmd(args):
    try:
        r = subprocess.run([CNAM_SH] + args, capture_output=True,
                           text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def log_tail(n=40):
    try:
        with open(LOG, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            data = fh.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>orata</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font:15px/1.45 system-ui,-apple-system,sans-serif;margin:0;padding:1.5rem;
     max-width:56rem;background:#111;color:#ddd}
h1{font-size:1.15rem;margin:0 0 .25rem}
.sub{color:#666;font-size:.8rem;margin:0 0 2rem}
h2{font-size:.95rem;margin:2rem 0 .4rem;color:#8cf;font-weight:600}
table{border-collapse:collapse;width:100%%}
td,th{padding:.35rem .5rem;border-bottom:1px solid #2a2a2a;text-align:left}
th{color:#777;font-weight:normal;font-size:.8rem}
td.num{font-family:ui-monospace,monospace}
td.act{text-align:right;width:1%%;white-space:nowrap}
input{background:#1c1c1c;border:1px solid #444;color:#ddd;padding:.3rem .4rem;
      border-radius:3px;font:inherit}
button{background:#2a2a2a;border:1px solid #555;color:#ddd;padding:.25rem .6rem;
       border-radius:3px;cursor:pointer;font:inherit;font-size:.85rem}
button:hover{background:#3a3a3a}
button.del{color:#c66;border-color:#533}
form.add{margin-top:.6rem;display:flex;gap:.4rem;flex-wrap:wrap}
form.inline{display:inline}
pre{background:#0a0a0a;border:1px solid #2a2a2a;padding:.75rem;overflow-x:auto;
    font-size:.78rem;color:#9a9}
.empty{color:#555;font-style:italic;padding:.35rem .5rem}
.flash{background:#1e2a1e;border:1px solid #3a5a3a;padding:.5rem .75rem;
       border-radius:3px;margin-bottom:1rem;font-size:.85rem}
.flash.err{background:#2a1e1e;border-color:#5a3a3a;color:#e99}
</style></head><body>
<h1>orata</h1>
<p class="sub">astdb call books &middot; changes take effect on the next call,
no reload needed</p>
%(flash)s
%(books)s
<h2>announce.log &mdash; last 40 lines</h2>
<pre>%(log)s</pre>
</body></html>
"""


def render_book(family, heading, add_cmd, del_cmd, val_label):
    rows = db_show(family)
    out = ["<h2>%s</h2>" % html.escape(heading)]
    if rows:
        out.append("<table><tr><th>Number</th>")
        if val_label:
            out.append("<th>%s</th>" % html.escape(val_label))
        out.append("<th></th></tr>")
        for num, val in rows:
            out.append('<tr><td class="num">%s</td>' % html.escape(num))
            if val_label:
                out.append("<td>%s</td>" % html.escape(val))
            out.append(
                '<td class="act"><form class="inline" method="post" action="/del">'
                '<input type="hidden" name="family" value="%s">'
                '<input type="hidden" name="number" value="%s">'
                '<button class="del" type="submit">delete</button></form></td></tr>'
                % (html.escape(family), html.escape(num))
            )
        out.append("</table>")
    else:
        out.append('<div class="empty">empty</div>')

    out.append('<form class="add" method="post" action="/add">')
    out.append('<input type="hidden" name="family" value="%s">' % html.escape(family))
    out.append('<input name="number" placeholder="15551234567" size="16" required>')
    if val_label:
        out.append('<input name="value" placeholder="%s" size="20" required>'
                   % html.escape(val_label))
    out.append('<button type="submit">add</button></form>')
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    server_version = "orata"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # --- auth ---------------------------------------------------------
    def presented_token(self):
        tok = self.headers.get("X-Orata-Token", "")
        if tok:
            return tok
        cookie = self.headers.get("Cookie", "") or ""
        m = re.search(r"orata_token=([^;]+)", cookie)
        if m:
            return urllib.parse.unquote(m.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get("token") or [""])[0]

    def authed(self):
        # Fail closed: an unconfigured token must not mean "open to all".
        if not TOKEN:
            return False
        return hmac.compare_digest(self.presented_token(), TOKEN)

    def deny(self):
        body = b"orata: missing or bad token\n"
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to="/", cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header(
                "Set-Cookie",
                "orata_token=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                % urllib.parse.quote(cookie),
            )
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- routes -------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.authed():
            return self.deny()
        # token arrived in the query string -> stash it and clean the URL
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("token"):
            return self.redirect("/", cookie=TOKEN)
        if path != "/":
            self.send_error(404)
            return
        self.page()

    def do_POST(self):
        if not self.authed():
            return self.deny()
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            self.send_error(413)
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        def field(name):
            return (form.get(name) or [""])[0]

        family = field("family")
        book = BOOK_BY_FAMILY.get(family)
        if not book:
            return self.redirect("/?msg=bad+book&err=1")

        _, _, add_cmd, del_cmd, val_label = book
        number = clean_number(field("number"))
        if not number:
            return self.redirect("/?msg=invalid+number&err=1")

        if path == "/add":
            args = [add_cmd, number]
            if val_label:
                value = clean_value(field("value"))
                if not value:
                    return self.redirect("/?msg=invalid+value&err=1")
                args.append(value)
            ok = cnam_cmd(args)
            msg = "added %s" % number if ok else "add failed"
        elif path == "/del":
            ok = cnam_cmd([del_cmd, number])
            msg = "deleted %s" % number if ok else "delete failed"
        else:
            self.send_error(404)
            return

        self.redirect("/?msg=%s%s" % (urllib.parse.quote(msg),
                                      "" if ok else "&err=1"))

    def page(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = (q.get("msg") or [""])[0]
        err = bool(q.get("err"))
        flash = ""
        if msg:
            flash = '<div class="flash%s">%s</div>' % (
                " err" if err else "", html.escape(msg))
        books = "".join(render_book(*b) for b in BOOKS)
        body = PAGE % {
            "flash": flash,
            "books": books,
            "log": html.escape("\n".join(log_tail()) or "(empty)"),
        }
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)


def main():
    if not TOKEN:
        sys.stderr.write(
            "orata-web: ORATA_WEB_TOKEN is empty in %s -- refusing to start.\n"
            "Generate one with:  head -c 24 /dev/urandom | base64\n" % CONF_PATH)
        sys.exit(1)
    if BIND in ("0.0.0.0", "::") and CFG.get("ORATA_WEB_ALLOW_ANY_BIND") != "1":
        sys.stderr.write(
            "orata-web: refusing to bind %s. This UI edits call routing and\n"
            "runs as the asterisk user. Bind to 127.0.0.1 or a WireGuard\n"
            "address. Set ORATA_WEB_ALLOW_ANY_BIND=1 to override.\n" % BIND)
        sys.exit(1)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    sys.stderr.write("orata-web listening on %s:%d\n" % (BIND, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()