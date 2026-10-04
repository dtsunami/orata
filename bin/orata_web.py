#!/usr/bin/env python3
#
# orata_web.py -- FastAPI admin UI + test harness for the orata astdb books.
#
# DEPENDENCIES (deliberate departure from the stdlib-only rule):
#   apt install python3-fastapi python3-uvicorn
# No pip, no venv -- Debian 13's python3 is PEP 668 externally-managed.
#
# Reads via `asterisk -rx`. Writes by shelling out to orata-cnam.sh, so
# mutation semantics live in exactly one place and the CLI stays the
# source of truth. Nothing here generates or overwrites a config file.
#
# Pages:
#   /         books    -- cnam / allow / block / alexasensor
#   /diag     health   -- read-only probes, mutates nothing
#   /harness  testing  -- call simulator + explicit actions
#   /log      announce.log tail
#
# SECURITY: runs as the `asterisk` user because it needs the Asterisk CLI.
# A compromise here is a compromise of call routing. Bind to localhost or
# a WireGuard address. Never a WAN interface. See docs/web-ui.md.
#

import glob
import hmac
import html
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import zlib

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

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
ANNOUNCE_SH = CFG.get("ORATA_ANNOUNCE_SH", "/usr/local/bin/orata-announce.sh")
ANNOUNCE_CONF = CFG.get("ORATA_ANNOUNCE_CONF", "/etc/orata/announce.conf")
PJSIP_CONF = CFG.get("ORATA_PJSIP_CONF", "/etc/asterisk/pjsip.conf")
PROBE_NUM = CFG.get("ORATA_WEB_PROBE_NUMBER", "19999999999")
AST_LOG = CFG.get("ORATA_ASTERISK_LOG", "/var/log/asterisk/orata.log")

# announce.conf is the source of truth for both audio directories;
# web.conf only overrides where the UI looks. One key, one meaning.
ACFG = load_conf(ANNOUNCE_CONF)
AUDIO_DIR = (CFG.get("ORATA_WEB_AUDIO_DIR") or ACFG.get("ORATA_AUDIO_DIR")
             or "/var/lib/orata/announce")
CLIP_DIR = (CFG.get("ORATA_WEB_CLIP_DIR") or ACFG.get("ORATA_CLIP_DIR")
            or "/var/lib/orata/clips")
CLIP_SH = CFG.get("ORATA_CLIP_SH", "/usr/local/bin/orata-clip.sh")

esc = html.escape

BOOKS = [
    ("cnam", "Name book", "add", "del", "Spoken name",
     "Spoken name. An entry here also skips the robocall gate."),
    ("allow", "Whitelist", "allow-add", "allow-del", None,
     "Past the gate permanently. Self-learns when a caller presses 1."),
    ("block", "Blocklist", "block-add", "block-del", None,
     "Hung up on before anything answers, rings or speaks."),
    ("alexasensor", "Alexa sensor map", "sensor-add", "sensor-del", "endpointId",
     "Per-number sensor endpointId. Only read in sensor/both mode."),
]
BOOK_BY_FAMILY = {b[0]: b for b in BOOKS}

NUM_RE = re.compile(r"^[0-9]{3,15}$")
ROW_RE = re.compile(r"^/([^/]+)/(\S+)\s*:\s*(.*)$")


def clean_number(raw):
    """Digits only. voip.ms presents 11-digit NANP; allow 3-15 for odd cases."""
    digits = re.sub(r"[^0-9]", "", raw or "")
    return digits if NUM_RE.match(digits) else None


def clean_value(raw):
    """Strip anything that could confuse the Asterisk CLI's string parser.
    Subprocess runs without a shell, so this is not shell escaping."""
    val = re.sub(r'[\x00-\x1f"\\`$]', "", (raw or "").strip())
    return val[:64]


# =====================================================================
# subprocess plumbing
# =====================================================================

def sh(args, timeout=10):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, "not found: %s" % args[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except Exception as exc:
        return 1, str(exc)


def ast(cmd, timeout=10):
    rc, out = sh(["asterisk", "-rx", cmd], timeout=timeout)
    if rc != 0 or "Unable to connect" in out:
        return False, out.strip()
    return True, out


def asterisk_up():
    ok, out = ast("core show version", timeout=6)
    return ok and "Asterisk" in out, out.strip()


# NOTE: `dialplan eval function ${DB(...)}` returns Failure(-1) outside a
# channel on Asterisk 22 -- see test/README.md. It CANNOT verify astdb key
# agreement. Use database get for CLI readback, and a real channel (the
# selftest context) for the dialplan seam.


def db_show(family):
    ok, out = ast("database show %s" % family)
    if not ok:
        return []
    rows = []
    for line in out.splitlines():
        m = ROW_RE.match(line.strip())
        if m and m.group(1) == family:
            rows.append((m.group(2), m.group(3).strip()))
    return sorted(rows)


def db_get(family, key):
    ok, out = ast("database get %s %s" % (family, key))
    if not ok:
        return ""
    m = re.search(r"^Value:\s*(.*)$", out, re.M)
    return m.group(1).strip() if m else ""


def cnam_cmd(args):
    rc, out = sh([CNAM_SH] + args)
    return rc == 0, out.strip()


def globals_map():
    g = {}
    ok, out = ast("dialplan show globals")
    if not ok:
        return g
    for line in out.splitlines():
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*=>?\s*(.*)$", line.strip())
        if m:
            g[m.group(1)] = m.group(2).strip()
    return g


def log_tail(n=60):
    try:
        with open(LOG, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 16384))
            data = fh.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return -1


# =====================================================================
# favicon -- SVG primary, generated PNG-in-ICO fallback. No image libs.
# =====================================================================

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#12161c"/>'
    '<circle cx="16" cy="16" r="10.5" fill="none" stroke="#2dd4bf" '
    'stroke-width="2.4" opacity=".55"/>'
    '<circle cx="16" cy="16" r="4.4" fill="#2dd4bf"/></svg>'
).encode("utf-8")


def _favicon_png(size=32):
    bg, ac = (0x12, 0x16, 0x1C), (0x2D, 0xD4, 0xBF)
    half, corner = size / 2.0, size * 0.22
    r_out, r_in, r_dot = size * 0.398, size * 0.323, size * 0.150
    ss = 3

    def cover(px, py):
        dx, dy = px - half, py - half
        qx, qy = abs(dx) - (half - corner), abs(dy) - (half - corner)
        d_box = (math.hypot(max(qx, 0.0), max(qy, 0.0))
                 + min(max(qx, qy), 0.0) - corner)
        dr = math.hypot(dx, dy)
        return (1.0 if d_box <= 0 else 0.0,
                1.0 if (r_in <= dr <= r_out or dr <= r_dot) else 0.0)

    px_rows = bytearray()
    for y in range(size):
        for x in range(size):
            b_acc = a_acc = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    b, a = cover(x + (sx + 0.5) / ss, y + (sy + 0.5) / ss)
                    b_acc += b
                    a_acc += a
            n = float(ss * ss)
            b_acc /= n
            a_acc = (a_acc / n) * b_acc
            if b_acc <= 0.0:
                px_rows += b"\x00\x00\x00\x00"
                continue
            col = [int(round(bg[i] * (1 - a_acc) + ac[i] * a_acc)) for i in range(3)]
            px_rows += bytes(col) + bytes([int(round(b_acc * 255))])

    raw = b"".join(b"\x00" + bytes(px_rows[y * size * 4:(y + 1) * size * 4])
                   for y in range(size))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _favicon_ico():
    png = _favicon_png(32)
    return (struct.pack("<HHH", 0, 1, 1)
            + struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), 22) + png)


FAVICON_ICO = _favicon_ico()


# =====================================================================
# theme
# =====================================================================

CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#0b0d11;color:#e7ecf3;
 font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 -webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
 background:radial-gradient(900px 460px at 50% -240px,rgba(45,212,191,.10),transparent 70%)}
a{color:#2dd4bf;text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:20;background:rgba(11,13,17,.85);
 backdrop-filter:blur(12px);border-bottom:1px solid #1e242c}
.hbar{max-width:62rem;margin:0 auto;padding:.65rem 1.25rem;display:flex;
 align-items:center;gap:.7rem;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:.5rem;font-weight:650}
.brand svg{display:block;border-radius:6px}
.tag{font-size:.68rem;color:#8a94a4;font-weight:500;padding:.08rem .42rem;
 border:1px solid #242c36;border-radius:999px;letter-spacing:.04em}
nav{margin-left:auto;display:flex;gap:.15rem;flex-wrap:wrap}
nav a{padding:.38rem .7rem;border-radius:8px;color:#9aa4b2;font-size:.88rem;font-weight:500}
nav a:hover{background:#161b22;color:#e7ecf3;text-decoration:none}
nav a.on{background:#142926;color:#2dd4bf}
.wrap{position:relative;z-index:1;max-width:62rem;margin:0 auto;padding:0 1.25rem 4rem}
h1{font-size:1.3rem;margin:1.5rem 0 .2rem;letter-spacing:-.01em}
.lede{color:#8a94a4;font-size:.88rem;margin:0 0 1.3rem;max-width:46rem}
.card{background:#12161c;border:1px solid #1f262f;border-radius:12px;margin:0 0 1rem;
 overflow:hidden}
.card>h2{margin:0;padding:.7rem 1rem;font-size:.75rem;font-weight:600;letter-spacing:.07em;
 text-transform:uppercase;color:#93a0b0;border-bottom:1px solid #1f262f;background:#141922;
 display:flex;align-items:center;gap:.6rem}
.card>h2 .note{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400;
 font-size:.76rem;color:#6f7b8a}
.body{padding:1rem}.body.flush{padding:0}.body.tight{padding:.65rem 1rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th{text-align:left;font-weight:500;font-size:.68rem;letter-spacing:.07em;
 text-transform:uppercase;color:#7b8695;padding:.5rem 1rem;border-bottom:1px solid #1f262f}
td{padding:.5rem 1rem;border-bottom:1px solid #171d25;vertical-align:middle}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#151a22}
td.act{text-align:right;width:1%;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86rem}
.dim{color:#7b8695}
.empty{color:#5d6876;font-style:italic;padding:.9rem 1rem;font-size:.88rem}
.in{background:#0e1218;border:1px solid #2a3340;color:#e7ecf3;padding:.42rem .6rem;
 border-radius:8px;font:inherit;font-size:.9rem;outline:none;transition:border-color .12s}
.in:focus{border-color:#2dd4bf;box-shadow:0 0 0 3px rgba(45,212,191,.12)}
.in::placeholder{color:#5d6876}
.btn{background:#1b222b;border:1px solid #2a3340;color:#cfd7e2;padding:.4rem .75rem;
 border-radius:8px;cursor:pointer;font:inherit;font-size:.85rem;font-weight:500;
 transition:background .12s,border-color .12s}
.btn:hover{background:#232c37;border-color:#3a4553}
.btn.primary{background:#142926;border-color:#1f5c54;color:#2dd4bf}
.btn.primary:hover{background:#18332f;border-color:#2a7d72}
.btn.danger{color:#ff8080;border-color:#3d2429}
.btn.danger:hover{background:#2a1a1e;border-color:#5a3239}
.btn.sm{padding:.22rem .55rem;font-size:.78rem}
form.add{display:flex;gap:.45rem;flex-wrap:wrap;padding:.75rem 1rem;
 border-top:1px solid #1f262f;background:#0f1319}
form.inline{display:inline}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
.pill{display:inline-block;padding:.1rem .48rem;border-radius:999px;font-size:.66rem;
 font-weight:700;letter-spacing:.07em;text-transform:uppercase;border:1px solid;
 white-space:nowrap}
.p-pass{color:#3ecf8e;background:rgba(62,207,142,.10);border-color:rgba(62,207,142,.30)}
.p-fail{color:#ff6b6b;background:rgba(255,107,107,.10);border-color:rgba(255,107,107,.30)}
.p-warn{color:#f5a623;background:rgba(245,166,35,.10);border-color:rgba(245,166,35,.30)}
.p-skip{color:#7b8695;background:rgba(123,134,149,.10);border-color:rgba(123,134,149,.28)}
.p-info{color:#5aa9ff;background:rgba(90,169,255,.10);border-color:rgba(90,169,255,.30)}
.chk{display:grid;grid-template-columns:4.6rem 1fr;gap:.2rem .8rem;padding:.6rem 1rem;
 border-bottom:1px solid #171d25;align-items:start}
.chk:last-child{border-bottom:0}
.chk .nm{font-weight:550;font-size:.9rem}
.chk .dt{grid-column:2;color:#8a94a4;font-size:.83rem;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-word}
.chk .hn{grid-column:2;color:#b58a3c;font-size:.82rem;margin-top:.15rem}
.chk.s-fail{background:rgba(255,107,107,.035)}
.summary{display:flex;gap:.45rem;flex-wrap:wrap;align-items:center}
.verdict{margin:0 0 1rem;padding:.75rem 1rem;border-radius:10px;font-size:.88rem;
 border:1px solid #1f262f;background:#12161c}
.verdict.bad{border-color:rgba(255,107,107,.3);background:rgba(255,107,107,.06)}
.verdict.good{border-color:rgba(62,207,142,.3);background:rgba(62,207,142,.06)}
.verdict b{color:#e7ecf3}
.flash{padding:.55rem .85rem;border-radius:9px;margin:1rem 0 0;font-size:.86rem;
 border:1px solid rgba(62,207,142,.3);background:rgba(62,207,142,.08);color:#9fe8c6}
.flash.err{border-color:rgba(255,107,107,.3);background:rgba(255,107,107,.08);color:#ffb3b3}
pre{background:#0a0d11;border:1px solid #1b222a;border-radius:9px;padding:.8rem;
 overflow-x:auto;font-size:.78rem;line-height:1.5;color:#9aa6b4;margin:0;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.lf{color:#ff8080}.lo{color:#3ecf8e}.ls{color:#6f7b8a}
.step{display:flex;gap:.75rem;padding:.55rem 1rem;border-bottom:1px solid #171d25;
 align-items:flex-start}
.step:last-child{border-bottom:0}
.step.off{opacity:.38}
.step .ic{flex:0 0 1.15rem;text-align:center;font-size:.9rem;line-height:1.5}
.step .tx{flex:1;font-size:.88rem}
.step .tx .sub{color:#7b8695;font-size:.8rem;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.step.hit .ic{color:#2dd4bf}
.step.end{background:#0f1319}
.hint{font-size:.8rem;color:#6f7b8a;margin:.5rem 0 0}
.snum{display:inline-flex;align-items:center;justify-content:center;width:1.4rem;
 height:1.4rem;border-radius:50%;background:#1b222b;border:1px solid #2a3340;
 color:#9aa4b2;font-size:.72rem;font-weight:700;flex:0 0 auto}
.card.done>h2 .snum{background:#142926;border-color:#1f5c54;color:#2dd4bf}
.card.now{border-color:rgba(245,166,35,.35)}
.card.now>h2{background:#1d1a12}
.card.done{opacity:.72}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.3rem .9rem;font-size:.86rem;
 margin:.6rem 0}
.kv dt{color:#8a94a4}
.kv dd{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
 word-break:break-all}
.card .body p{margin:.5rem 0}
.card .body p:first-child{margin-top:0}
.card .body ol,.card .body ul{margin:.5rem 0;padding-left:1.2rem}
.card .body li{margin:.25rem 0;font-size:.89rem}
pre.cmd{background:#0a0d11;border-color:#223042;color:#cfe3dd;font-size:.8rem}
footer{color:#5d6876;font-size:.78rem;margin-top:2rem;text-align:center}
@media(max-width:34rem){nav a{padding:.35rem .5rem;font-size:.82rem}
 .chk{grid-template-columns:1fr}.chk .dt,.chk .hn{grid-column:1}}
"""

SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0b0d11">
<title>{{TITLE}}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="alternate icon" href="/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="/favicon.svg">
<style>{{CSS}}</style>
</head><body>
<header><div class="hbar">
<span class="brand">
<svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
<rect width="32" height="32" rx="7" fill="#12161c"/>
<circle cx="16" cy="16" r="10.5" fill="none" stroke="#2dd4bf" stroke-width="2.4" opacity=".55"/>
<circle cx="16" cy="16" r="4.4" fill="#2dd4bf"/></svg>
orata <span class="tag">{{HOST}}</span></span>
<nav>{{NAV}}</nav></div></header>
<div class="wrap">{{FLASH}}{{BODY}}
<footer>orata &middot; astdb is the source of truth &middot; changes apply to the next call</footer>
</div></body></html>
"""

TABS = [("/setup", "Setup"), ("/devices", "Devices"), ("/", "Books"),
        ("/diag", "Diagnostics"), ("/harness", "Harness"),
        ("/audio", "Audio"), ("/log", "Log")]


def render(active, title, body, flash=""):
    nav = "".join('<a href="%s"%s>%s</a>' % (p, ' class="on"' if p == active else "", esc(n))
                  for p, n in TABS)
    return HTMLResponse(
        SHELL.replace("{{CSS}}", CSS).replace("{{TITLE}}", esc(title))
        .replace("{{HOST}}", esc(os.uname().nodename)).replace("{{NAV}}", nav)
        .replace("{{FLASH}}", flash).replace("{{BODY}}", body),
        headers={"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


def pill(s):
    return '<span class="pill p-%s">%s</span>' % (s, s)


def flash_of(request):
    msg = request.query_params.get("msg", "")
    if not msg:
        return ""
    err = request.query_params.get("err") == "1"
    return '<div class="flash%s">%s</div>' % (" err" if err else "", esc(msg))


# =====================================================================
# app + auth
# =====================================================================

app = FastAPI(title="orata", docs_url=None, redoc_url=None, openapi_url=None)


def require_auth(request: Request):
    tok = (request.headers.get("x-orata-token")
           or request.cookies.get("orata_token")
           or request.query_params.get("token", ""))
    # Fail closed: an unconfigured token must not mean "open to all".
    if not TOKEN or not hmac.compare_digest(tok, TOKEN):
        raise HTTPException(status_code=401, detail="orata: missing or bad token")
    return True


@app.exception_handler(HTTPException)
async def on_http_error(request: Request, exc: HTTPException):
    return PlainTextResponse("orata: %s\n" % exc.detail, status_code=exc.status_code)


def back(msg, err=False, to="/"):
    sep = "&" if "?" in to else "?"
    return RedirectResponse(
        "%s%smsg=%s%s" % (to, sep, urllib.parse.quote_plus(msg),
                          "&err=1" if err else ""), status_code=303)


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return Response(FAVICON_ICO, media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})


# =====================================================================
# books
# =====================================================================

def render_book(family, heading, add_cmd, del_cmd, val_label, note):
    rows = db_show(family)
    out = ['<div class="card"><h2>%s<span class="note">%s</span></h2>'
           % (esc(heading), esc(note))]
    if rows:
        out.append('<div class="body flush"><table><thead><tr><th>Number</th>')
        if val_label:
            out.append("<th>%s</th>" % esc(val_label))
        out.append("<th></th></tr></thead><tbody>")
        for num, val in rows:
            out.append('<tr><td class="mono">%s</td>' % esc(num))
            if val_label:
                out.append("<td>%s</td>" % esc(val))
            out.append('<td class="act"><form class="inline" method="post" action="/del">'
                       '<input type="hidden" name="family" value="%s">'
                       '<input type="hidden" name="number" value="%s">'
                       '<button class="btn sm danger" type="submit">delete</button>'
                       "</form></td></tr>" % (esc(family), esc(num)))
        out.append("</tbody></table></div>")
    else:
        out.append('<div class="empty">no entries</div>')
    out.append('<form class="add" method="post" action="/add">'
               '<input type="hidden" name="family" value="%s">'
               '<input class="in mono" name="number" placeholder="15551234567" '
               'size="15" required>' % esc(family))
    if val_label:
        out.append('<input class="in" name="value" placeholder="%s" size="18" required>'
                   % esc(val_label))
    out.append('<button class="btn primary" type="submit">add</button></form></div>')
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
def page_books(request: Request, _=Depends(require_auth)):
    # token arrived in the query string -> stash it in a cookie, clean the URL
    if request.query_params.get("token"):
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("orata_token", TOKEN, max_age=2592000, httponly=True,
                     samesite="strict", path="/")
        return r
    up, _v = asterisk_up()
    body = ["<h1>Call books</h1>",
            '<p class="lede">Four key/value books in astdb. Every write goes through '
            '<span class="mono">orata-cnam.sh</span>, so the CLI stays authoritative. '
            'Enter numbers exactly as voip.ms presents them &mdash; usually 11 digits, '
            '<span class="mono">15551234567</span>.</p>']
    if not up:
        body.append('<div class="verdict bad"><b>Asterisk is not reachable.</b> '
                    "The books cannot be read or written. Check "
                    '<span class="mono">systemctl status asterisk</span>.</div>')
    else:
        pending = setup_incomplete()
        if pending:
            body.append('<div class="verdict bad"><b>Setup is not finished</b> &mdash; '
                        '%s. Names added here will not be spoken until calls arrive. '
                        '<a href="/setup">Open Setup</a>.</div>' % esc(pending))
    body += [render_book(*b) for b in BOOKS]
    return render("/", "orata - books", "".join(body), flash_of(request))


@app.post("/add")
def do_add(family: str = Form(...), number: str = Form(...),
           value: str = Form(""), _=Depends(require_auth)):
    book = BOOK_BY_FAMILY.get(family)
    if not book:
        return back("unknown book", True)
    num = clean_number(number)
    if not num:
        return back("invalid number - digits only, 3 to 15", True)
    args = [book[2], num]
    if book[4]:
        val = clean_value(value)
        if not val:
            return back("invalid value", True)
        args.append(val)
    ok, out = cnam_cmd(args)
    return back("added %s to %s" % (num, family) if ok
                else "add failed: %s" % (out[:80] or "orata-cnam.sh error"), not ok)


@app.post("/del")
def do_del(family: str = Form(...), number: str = Form(...), _=Depends(require_auth)):
    book = BOOK_BY_FAMILY.get(family)
    if not book:
        return back("unknown book", True)
    num = clean_number(number)
    if not num:
        return back("invalid number", True)
    ok, out = cnam_cmd([book[3], num])
    return back("deleted %s from %s" % (num, family) if ok
                else "delete failed: %s" % (out[:80] or "error"), not ok)


# =====================================================================
# diagnostics -- strictly read-only
# =====================================================================

def chk(s, n, d="", h=""):
    return {"s": s, "n": n, "d": d, "h": h}


def _sound(stem):
    for base in ("/var/lib/asterisk/sounds/en", "/usr/share/asterisk/sounds/en"):
        hits = glob.glob(os.path.join(base, stem + ".*"))
        if hits:
            return hits[0]
    return None


def diag_platform():
    out = []
    up, ver = asterisk_up()
    out.append(chk("pass", "Asterisk running", ver.splitlines()[0] if ver else "")
               if up else
               chk("fail", "Asterisk running", ver[:200] or "no response",
                   "systemctl status asterisk"))
    root = ""
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                p = line.split()
                if len(p) > 1 and p[1] == "/":
                    root = p[0]
                    break
    except OSError:
        pass
    if root.startswith("/dev/mmcblk"):
        out.append(chk("warn", "Root filesystem", root,
                       "Booting from SD. CDR logging kills cards; the repo names USB "
                       "SSD boot as an invariant."))
    elif root:
        out.append(chk("pass", "Root filesystem", root))
    else:
        out.append(chk("skip", "Root filesystem", "could not read /proc/mounts"))

    rc, tout = sh(["vcgencmd", "get_throttled"], timeout=5)
    m = re.search(r"0x([0-9a-fA-F]+)", tout or "")
    if rc == 0 and m:
        bits = int(m.group(1), 16)
        flags = [lbl for bit, lbl in
                 ((0, "under-voltage now"), (1, "freq capped now"), (2, "throttled now"),
                  (3, "soft temp limit now"), (16, "under-voltage since boot"),
                  (17, "capped since boot"), (18, "throttled since boot"),
                  (19, "soft temp since boot")) if bits & (1 << bit)]
        out.append(chk("pass", "Power / throttling", "0x0") if bits == 0 else
                   chk("warn", "Power / throttling", "0x%x -- %s" % (bits, ", ".join(flags)),
                       "Pi 4 under-voltage corrupts storage. Use a 5V/3A supply."))
    else:
        out.append(chk("skip", "Power / throttling", "vcgencmd unavailable"))
    return out


def diag_dialplan():
    out = []
    ok, dp = ast("dialplan show from-voipms")
    if not ok or "no existence" in dp.lower():
        return [chk("fail", "from-voipms context", "did not load",
                    "Parse error in extensions.conf; asterisk -rx 'dialplan reload'")]
    out.append(chk("pass", "from-voipms context",
                   "%d priorities loaded" % len(re.findall(r"\[pbx_config\]", dp))))
    missing = [a for a in ("NoOp", "System", "Dial", "VoiceMail", "Read", "Playback")
               if a + "(" not in dp]
    out.append(chk("fail", "Dialplan applications", "missing: " + ", ".join(missing),
                   "A priority was dropped -- the call flow is incomplete.") if missing
               else chk("pass", "Dialplan applications",
                        "NoOp, System, Dial, VoiceMail, Read, Playback present"))
    labels = [l for l in ("blocked", "ring", "reject")
              if ("[%s]" % l) in dp or ("(%s)" % l) in dp]
    out.append(chk("pass", "Goto labels", "blocked, ring, reject all resolve")
               if len(labels) == 3 else
               chk("warn", "Goto labels", "found: %s" % (", ".join(labels) or "none"),
                   "Label rendering varies by Asterisk version; confirm with "
                   "asterisk -rx 'dialplan show from-voipms'"))
    ok, inte = ast("dialplan show internal")
    out.append(chk("pass", "internal context", "loaded")
               if ok and "no existence" not in inte.lower()
               else chk("fail", "internal context", "did not load"))
    ok, t99 = ast("dialplan show *99@internal")
    out.append(chk("pass", "*99 announce test", "present") if ok and "System(" in t99
               else chk("warn", "*99 announce test", "not found",
                        "The dial-in announcement test will not work."))
    g = globals_map()
    strict = g.get("STRICT", "?")
    out.append(chk("warn", "STRICT mode", "STRICT=1 -- invitation only",
                   "Unknown callers are rejected without being offered the gate.")
               if strict == "1" else
               chk("pass", "STRICT mode", "STRICT=%s -- gate is offered" % strict))
    out.append(chk("info", "Ring targets", g.get("RINGALL", "(unset)")))
    if selftest_loaded():
        out.append(chk("warn", "orata-selftest context", "loaded",
                       "Fine while testing. Remove the #include from extensions.conf "
                       "before you call this production."))
    else:
        out.append(chk("info", "orata-selftest context", "not loaded",
                       "Install it to enable the live channel probes on the Harness "
                       "tab. See test/README.md."))
    return out


def diag_pjsip():
    out = []
    ok, eps = ast("pjsip show endpoints")
    if not ok:
        return [chk("fail", "PJSIP endpoints", "could not query")]
    missing = [e for e in ("voipms", "pc", "mobile", "desk")
               if not re.search(r"Endpoint:\s+%s[\s/]" % re.escape(e), eps)]
    out.append(chk("fail", "PJSIP endpoints", "missing: " + ", ".join(missing),
                   "pjsip.conf parse problem; asterisk -rx 'pjsip reload'") if missing
               else chk("pass", "PJSIP endpoints", "voipms, pc, mobile, desk parsed"))

    ok, regs = ast("pjsip show registrations")
    status = None
    if ok:
        for line in regs.splitlines():
            if "voipms" in line and "/sip:" in line:
                m = re.search(r"(Registered|Unregistered|Rejected|Auth Sent|No Auth|"
                              r"Stopped)", line)
                status = m.group(1) if m else line.strip()[-40:]
                break
    if status == "Registered":
        out.append(chk("pass", "Trunk registration", "Registered to voip.ms"))
    elif status:
        out.append(chk("fail", "Trunk registration", status,
                       "This is the blocker. Fill in POP + sub-account credentials in "
                       "%s, then asterisk -rx 'core reload'" % PJSIP_CONF))
    else:
        out.append(chk("fail", "Trunk registration", "no registration object found",
                       "Check [voipms_reg] in %s" % PJSIP_CONF))
    conf = pjsip_live_text()
    if conf is None:
        out.append(chk("skip", "voip.ms credentials", "cannot read " + PJSIP_CONF))
    else:
        stubs = pjsip_stubs()
        out.append(chk("fail", "voip.ms credentials", "still stubbed: " + ", ".join(stubs),
                       "Edit %s directly -- do NOT commit credentials to the repo."
                       % PJSIP_CONF) if stubs
                   else chk("pass", "voip.ms credentials", "no stub values remain"))
        if re.search(r"^\s*external_media_address", conf, re.M):
            out.append(chk("info", "NAT", "external_media_address is set"))
    return out


def diag_announce():
    acfg = load_conf(ANNOUNCE_CONF)
    if not acfg:
        return [chk("fail", "announce.conf", "unreadable or empty: " + ANNOUNCE_CONF,
                    "cp etc/announce.conf /etc/orata/ && chown asterisk")]
    out = [chk("info", "Alexa mode", acfg.get("ORATA_ALEXA_MODE", "arc"))]
    mode = acfg.get("ORATA_ALEXA_MODE", "arc")
    out.append(chk("pass", "orata-announce.sh", ANNOUNCE_SH)
               if os.access(ANNOUNCE_SH, os.X_OK) else
               chk("fail", "orata-announce.sh", "not executable: " + ANNOUNCE_SH,
                   "cp bin/orata-announce.sh /usr/local/bin/ && chmod 755"))
    if mode in ("arc", "both"):
        arc = acfg.get("ORATA_ARC", "/usr/local/bin/alexa_remote_control.sh")
        out.append(chk("pass", "alexa_remote_control.sh", arc) if os.access(arc, os.X_OK)
                   else chk("warn", "alexa_remote_control.sh", "not executable: " + arc,
                            "Announcements log 'alexa SKIP'. Fetch it, then "
                            "sudo -u asterisk %s -a" % arc))
    if mode in ("sensor", "both"):
        have = all(acfg.get(k) for k in ("ORATA_LWA_CLIENT_ID", "ORATA_LWA_CLIENT_SECRET",
                                         "ORATA_LWA_REFRESH_TOKEN"))
        out.append(chk("pass", "LWA credentials", "client id/secret/refresh token set")
                   if have else
                   chk("warn", "LWA credentials", "incomplete in announce.conf",
                       "Sensor path logs 'sensor SKIP'. See docs/alexa-sensor.md"))
    out.append(chk("pass", "ntfy fallback", "configured") if acfg.get("ORATA_NTFY_URL")
               else chk("warn", "ntfy fallback", "ORATA_NTFY_URL is empty",
                        "This is the channel that still works when Alexa auth rots."))
    if acfg.get("ORATA_RECORD_AUDIO", "1") == "1":
        tts = acfg.get("ORATA_TTS") or next(
            (c for c in ("espeak-ng", "espeak") if shutil.which(c)), "")
        out.append(chk("pass", "local audio render", "%s -> %s" % (tts, AUDIO_DIR))
                   if tts else
                   chk("warn", "local audio render", "no espeak-ng/espeak on PATH",
                       "Announcements log 'audio SKIP' and the Audio tab stays empty. "
                       "apt install espeak-ng"))
        n = len(audio_files(500))
        out.append(chk("pass" if n else "info", "recordings", "%d in %s" % (n, AUDIO_DIR),
                       "" if n else "Fire one from the Harness tab."))
    else:
        out.append(chk("info", "local audio render", "ORATA_RECORD_AUDIO=0"))
    size = log_size()
    if size < 0:
        out.append(chk("warn", "announce.log", "missing: " + LOG,
                       "mkdir -p /var/log/orata && chown asterisk:asterisk /var/log/orata"))
    else:
        tail = log_tail(200)
        fails = sum(1 for l in tail if "FAIL" in l)
        out.append(chk("warn" if fails else "pass", "announce.log",
                       "%d bytes, %d line(s), %d FAIL" % (size, len(tail), fails),
                       "Check the Log tab." if fails else ""))
    return out


def diag_media():
    out = []
    p1 = _sound("custom/press-one")
    out.append(chk("pass", "press-one prompt", p1) if p1 else
               chk("fail", "press-one prompt", "not found in either sounds dir",
                   "The robocall gate has no prompt. Generate it with espeak+sox into "
                   "/var/lib/asterisk/sounds/en/custom/"))
    vg = _sound("vm-goodbye")
    out.append(chk("pass", "vm-goodbye prompt", vg) if vg else
               chk("fail", "vm-goodbye prompt", "not found",
                   "Rejected callers hear nothing."))
    # A role file assigned from the web UI satisfies the dialplan on its
    # own; only prompts with neither a role nor a sound file are missing.
    missing = [p for p in ("rec-start", "rec-menu", "rec-saved")
               if not _sound("custom/" + p) and not role_installed(p)]
    out.append(chk("pass", "*96 prompts", "rec-start, rec-menu, rec-saved resolvable")
               if not missing else
               chk("warn", "*96 prompts", "no sound file and no override: "
                   + ", ".join(missing),
                   "Dialling *96 gives silence at those steps. Set them on the Audio "
                   "tab, or install the sound files -- see README."))
    if not shutil.which("sox"):
        out.append(chk("warn", "sox", "not on PATH",
                       "The Audio tab's 'synthesise' button needs it to resample "
                       "espeak's 22 kHz output to 8 kHz. apt install sox"))
    ok, fm = ast("features show")
    out.append(chk("pass", "3434 in-call record", "recordclip is armed")
               if ok and "recordclip" in fm else
               chk("warn", "3434 in-call record", "recordclip not in the feature map",
                   "Pressing 3434 mid-call does nothing. cp asterisk/features.conf "
                   "to /etc/asterisk/ and run: asterisk -rx 'module reload features'"))
    nrec = len([n for n in (os.listdir(CLIP_DIR) if os.path.isdir(CLIP_DIR) else [])
                if n.startswith("name-") and n.endswith(".wav")])
    out.append(chk("info", "recorded caller names", "%d" % nrec))
    ok, vm = ast("voicemail show users")
    out.append(chk("pass", "Mailbox 100", "exists")
               if ok and re.search(r"^\s*\S+\s+100\b", vm, re.M) else
               chk("fail", "Mailbox 100", "not found",
                   "VoiceMail(100@default) fails on every unanswered call. Add it to "
                   "/etc/asterisk/voicemail.conf."))
    return out


DIAG = [("Platform", diag_platform), ("Dialplan", diag_dialplan),
        ("SIP trunk", diag_pjsip), ("Announcement path", diag_announce),
        ("Media &amp; voicemail", diag_media)]


@app.get("/diag", response_class=HTMLResponse)
def page_diag(request: Request, _=Depends(require_auth)):
    sections, counts, blockers = [], {"pass": 0, "fail": 0, "warn": 0, "skip": 0, "info": 0}, []
    t0 = time.time()
    for title, fn in DIAG:
        try:
            checks = fn()
        except Exception as exc:
            checks = [chk("fail", title + " probe crashed", repr(exc))]
        rows = []
        for c in checks:
            counts[c["s"]] = counts.get(c["s"], 0) + 1
            if c["s"] == "fail":
                blockers.append(c["n"])
            rows.append('<div class="chk s-%s">%s<span class="nm">%s</span>%s%s</div>'
                        % (c["s"], pill(c["s"]), esc(c["n"]),
                           '<span class="dt">%s</span>' % esc(c["d"]) if c["d"] else "",
                           '<span class="hn">&rarr; %s</span>' % esc(c["h"]) if c["h"] else ""))
        sections.append('<div class="card"><h2>%s</h2><div class="body flush">%s</div></div>'
                        % (title, "".join(rows)))
    if counts["fail"]:
        verdict = ('<div class="verdict bad"><b>%d check(s) failing.</b> Blocking: %s.</div>'
                   % (counts["fail"], esc(", ".join(blockers[:6]))))
    elif counts["warn"]:
        verdict = ('<div class="verdict good"><b>No failures.</b> %d warning(s) &mdash; '
                   "worth reading, not blocking.</div>" % counts["warn"])
    else:
        verdict = '<div class="verdict good"><b>All checks pass.</b></div>'
    summary = '<div class="summary">' + "".join(
        '%s <span class="dim">%d</span>&nbsp;&nbsp;' % (pill(k), counts[k])
        for k in ("pass", "fail", "warn", "skip", "info") if counts[k]) \
        + '<span class="dim">&middot; %.1fs</span></div>' % (time.time() - t0)
    body = ("<h1>Diagnostics</h1>"
            '<p class="lede">Read-only. Nothing here mutates astdb, config or call state '
            "&mdash; safe to run at any time, including mid-call. Reload to re-run.</p>"
            + verdict + '<div class="card"><h2>Summary</h2><div class="body tight">'
            + summary + "</div></div>" + "".join(sections))
    return render("/diag", "orata - diagnostics", body, flash_of(request))


# =====================================================================
# harness
# =====================================================================

def simulate(number):
    """Mirror [from-voipms] in extensions.conf against live astdb state."""
    g = globals_map()
    strict = g.get("STRICT", "0") == "1"
    acfg = load_conf(ANNOUNCE_CONF)
    blocked = db_get("block", number) == "1"
    cnam = db_get("cnam", number)
    allowed = db_get("allow", number) == "1"
    sensor = db_get("alexasensor", number)
    steps = []

    def step(hit, text, sub=""):
        steps.append((hit, text, sub))

    step(True, "NoOp(IN %s)" % number, "call arrives in from-voipms")
    step(blocked, "GotoIf(DB(block/%s) = 1) &rarr; blocked" % number,
         "block/%s = %s" % (number, db_get("block", number) or "(unset)"))
    if blocked:
        step(True, "NoOp(BLOCKED) &rarr; Hangup()",
             "never answers -- nothing rings, nothing speaks")
        return steps, ("BLOCKED",
                       "Hung up on before Answer(). The caller gets a carrier decline "
                       "and no confirmation that this line is live.")

    step(True, "Set(CNAM=DB(cnam/%s))" % number,
         "cnam/%s = %s" % (number, cnam or "(unset)"))
    step(not cnam, 'ExecIf(CNAM = "" ? Set(CNAM=CALLERID(name)))',
         "carrier CNAM is unknowable from here -- assumed empty")
    step(bool(sensor), "Set(ASENSOR=DB(alexasensor/%s))" % number,
         "alexasensor/%s = %s" % (number, sensor or "(unset)"))
    step(allowed, "GotoIf(DB(allow/%s) = 1) &rarr; ring" % number,
         "allow/%s = %s" % (number, db_get("allow", number) or "(unset)"))
    gated = not allowed
    if not allowed:
        step(bool(cnam), 'GotoIf(CNAM != "") &rarr; ring',
             "name book hit" if cnam else "no name resolved")
        gated = not cnam

    if gated:
        step(strict, "GotoIf(STRICT = 1) &rarr; reject", "STRICT=%s" % g.get("STRICT", "0"))
        if strict:
            step(True, "Playback(vm-goodbye) &rarr; Hangup()")
            return steps, ("REJECTED",
                           "STRICT mode is on, so an unknown caller is dropped without "
                           "being offered the gate. A doctor's office or delivery driver "
                           "gets no way through.")
        step(True, "Answer() &rarr; Wait(1) &rarr; Read(digit,custom/press-one,1,,1,7)",
             "the robocall gate -- 7 second window to press 1")
        step(True, "pressed 1 &rarr; Set(DB(allow/%s)=1)" % number,
             "self-learns: whitelisted from now on")
        step(True, "did not press 1 &rarr; Playback(vm-goodbye) &rarr; Hangup()")

    if cnam:
        phrase = "Call from %s" % cnam
    elif acfg.get("ORATA_SPEAK_DIGITS") == "1":
        phrase = "Call from " + ", ".join(number)
    else:
        phrase = "Call from an unknown number"
    ep = sensor or acfg.get("ORATA_SENSOR_DEFAULT", "")
    step(True, "(ring) System(orata-announce.sh &hellip; &amp;)",
         "backgrounded -- the dialplan does not wait on Amazon")
    step(True, "Dial(%s, 30)" % g.get("RINGALL", "(RINGALL unset)"))
    step(True, "VoiceMail(100@default,u)", "after 30s unanswered")
    detail = ("Announcement: <b>%s</b><br>Sensor: <span class=\"mono\">%s</span>"
              "<br>Alexa mode: <span class=\"mono\">%s</span>"
              % (esc(phrase), esc(ep or "(none, and no ORATA_SENSOR_DEFAULT)"),
                 esc(acfg.get("ORATA_ALEXA_MODE", "arc"))))
    if gated:
        detail = "Unknown caller &mdash; must press 1 within 7s first.<br>" + detail
    return steps, ("GATE then RING" if gated else "RING", detail)


def render_sim(number):
    steps, (outcome, detail) = simulate(number)
    rows = "".join('<div class="step %s"><span class="ic">%s</span>'
                   '<span class="tx">%s%s</span></div>'
                   % ("hit" if hit else "off", "&#9679;" if hit else "&#9675;", text,
                      '<br><span class="sub">%s</span>' % sub if sub else "")
                   for hit, text, sub in steps)
    return ('<div class="card"><h2>Trace for %s<span class="note">outcome: %s</span></h2>'
            '<div class="body flush">%s<div class="step end"><span class="ic">&#9654;</span>'
            '<span class="tx">%s</span></div></div></div>'
            % (esc(number), esc(outcome), rows, detail))


def action_roundtrip():
    """Write a probe key to all four books, read each back, delete.

    This proves orata-cnam.sh -> astdb. It does NOT prove the dialplan reads
    the same keys: DB() only resolves on a live channel. Use the selftest
    probe below for that seam.
    """
    n, lines, good = PROBE_NUM, [], True
    cases = [("cnam", ["add", n, "orata probe"], "orata probe"),
             ("allow", ["allow-add", n], "1"),
             ("block", ["block-add", n], "1"),
             ("alexasensor", ["sensor-add", n, "orata-probe"], "orata-probe")]
    for family, args, expect in cases:
        ok, out = cnam_cmd(args)
        if not ok:
            good = False
            lines.append(("fail", family,
                          "orata-cnam.sh %s failed: %s" % (args[0], out[:120])))
            continue
        got = db_get(family, n)
        if got == expect:
            lines.append(("pass", family, "%s/%s = %r" % (family, n, got)))
        else:
            good = False
            lines.append(("fail", family, "read back %r, expected %r" % (got, expect)))
    for sub in ("del", "allow-del", "block-del", "sensor-del"):
        cnam_cmd([sub, n])
    leftover = [f for f in ("cnam", "allow", "block", "alexasensor") if db_get(f, n)]
    if leftover:
        good = False
        lines.append(("fail", "cleanup", "probe key left in: " + ", ".join(leftover)))
    else:
        lines.append(("pass", "cleanup", "probe %s removed from all four books" % n))
    lines.append(("info", "scope",
                  "proves CLI -> astdb only. DB() resolves only on a live channel, "
                  "so the dialplan seam needs the selftest probe."))
    return good, lines


def selftest_loaded():
    ok, out = ast("dialplan show orata-selftest")
    return ok and "no existence" not in out.lower() and "7101" in out


def action_selftest(exten):
    """Drive a real channel through [from-voipms] and scrape the verbose log.

    This is the only honest test of the gate: `channel originate
    Local/NUM@from-voipms` leaves CALLERID(num) empty, so the 71xx
    extensions in selftest.conf Set() it first and Goto() in.
    """
    if not selftest_loaded():
        return False, [("fail", "orata-selftest context", "not loaded"),
                       ("info", "install",
                        "cp test/selftest.conf /etc/asterisk/orata-selftest.conf, add "
                        "#include, then asterisk -rx 'dialplan reload'. See "
                        "test/README.md.")]
    before = 0
    try:
        before = os.path.getsize(AST_LOG)
    except OSError:
        pass
    ok, out = ast("channel originate Local/%s@orata-selftest application Wait 9"
                  % exten, timeout=25)
    rows = [("pass" if ok else "fail", "originate %s" % exten,
             out.strip()[:200] or "dispatched")]
    time.sleep(3.0)
    new = []
    try:
        with open(AST_LOG, "rb") as fh:
            fh.seek(before)
            new = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        rows.append(("warn", "asterisk log", "cannot read " + AST_LOG))
    hits = [l for l in new
            if any(k in l for k in ("SELFTEST", "BLOCKED", "Executing", "IN "))]
    if hits:
        for line in hits[-30:]:
            rows.append(("info", "log", line.strip()[:220]))
    elif new:
        rows.append(("warn", "log", "%d new line(s) but nothing recognisable -- is "
                     "verbose(5) enabled in logger.conf?" % len(new)))
    else:
        rows.append(("warn", "log", "no new lines in %s. The verbose log must be on: "
                     "orata.log => notice,warning,error,verbose(5)" % AST_LOG))
    return ok, rows


def result_card(title, rows, extra=""):
    body = "".join('<div class="chk s-%s">%s<span class="nm">%s</span>'
                   '<span class="dt">%s</span></div>' % (s, pill(s), esc(n), esc(d))
                   for s, n, d in rows)
    return ('<div class="card"><h2>%s</h2><div class="body flush">%s</div>%s</div>'
            % (esc(title), body, extra))


@app.get("/harness", response_class=HTMLResponse)
def page_harness(request: Request, _=Depends(require_auth)):
    return render("/harness", "orata - harness",
                  harness_body(request.query_params.get("sim", ""), ""),
                  flash_of(request))


def harness_body(raw_sim, result_html):
    num = clean_number(raw_sim)
    body = ["<h1>Test harness</h1>",
            '<p class="lede">Nothing here runs on page load. The simulator is read-only; '
            "the actions below mutate state or put traffic on the wire, and each says "
            "which.</p>"]
    if result_html:
        body.append(result_html)
    body.append('<div class="card"><h2>Call simulator<span class="note">read-only '
                "&middot; mirrors [from-voipms]</span></h2><div class=\"body\">"
                '<p class="hint" style="margin-top:0">Enter a number and see which branch '
                "of the dialplan it takes against live astdb state. This is the check to "
                "run before you block someone or hand the number out.</p>"
                '<form class="row" method="get" action="/harness">'
                '<input class="in mono" name="sim" placeholder="15551234567" size="16" '
                'value="%s" required>'
                '<button class="btn primary" type="submit">trace this call</button>'
                "</form></div></div>" % esc(raw_sim, quote=True))
    if raw_sim and not num:
        body.append('<div class="verdict bad">Not a usable number. Digits only, '
                    "3&ndash;15 of them.</div>")
    elif num:
        body.append(render_sim(num))

    body.append(
        '<div class="card"><h2>Actions</h2><div class="body">'
        '<div class="row" style="align-items:flex-start;margin-bottom:1rem">'
        '<form method="post" action="/harness/roundtrip">'
        '<button class="btn primary" type="submit">run astdb round-trip</button></form>'
        '<span class="hint" style="margin:0;flex:1;min-width:16rem">Writes probe key '
        '<span class="mono">' + esc(PROBE_NUM) + "</span> to all four books, reads each "
        'back through <span class="mono">dialplan eval</span>, then deletes it. Proves '
        "orata-cnam.sh and extensions.conf agree on key names. Self-cleaning.</span></div>"

        '<div class="row" style="align-items:flex-start;margin-bottom:1rem">'
        '<form class="row" method="post" action="/harness/announce">'
        '<input class="in mono" name="number" value="15551234567" size="13" required>'
        '<input class="in" name="name" value="Test Caller" size="13">'
        '<button class="btn primary" type="submit">fire announcement</button></form>'
        '<span class="hint" style="margin:0;flex:1;min-width:16rem">Runs '
        '<span class="mono">orata-announce.sh</span> synchronously and shows the log '
        "lines it produced. Same thing <span class=\"mono\">*99</span> does, without a "
        "phone. <b>Hits ntfy and Alexa for real.</b></span></div>"

        '<div class="row" style="align-items:flex-start;margin-bottom:1rem">'
        '<form method="post" action="/harness/reload">'
        '<button class="btn" type="submit">core reload</button></form>'
        '<span class="hint" style="margin:0;flex:1;min-width:16rem">Re-reads Asterisk '
        "config from disk. Safe mid-call, but it will re-arm the trunk registration "
        "&mdash; run it after editing pjsip.conf.</span></div>"
        "</div></div>")

    # --- live-channel probes ---
    loaded = selftest_loaded()
    btns = "".join(
        '<form class="inline" method="post" action="/harness/selftest" '
        'style="margin-right:.4rem">'
        '<input type="hidden" name="exten" value="%s">'
        '<button class="btn%s" type="submit"%s>%s</button></form>'
        % (ex, " primary" if loaded else "", "" if loaded else " disabled", esc(label))
        for ex, label in (("7101", "known caller"), ("7102", "blocked caller"),
                          ("7103", "unknown caller"), ("7001", "DB() reads")))
    if loaded:
        note = ("Originates a real channel into <span class=\"mono\">[from-voipms]</span> "
                "with the caller ID set, then scrapes the Asterisk verbose log. This is "
                "the <b>only</b> honest test of the gate &mdash; "
                "<span class=\"mono\">DB()</span> does not resolve outside a channel. "
                "Needs <span class=\"mono\">verbose(5)</span> in logger.conf.")
        warn = ""
    else:
        note = ("The <span class=\"mono\">orata-selftest</span> context is not loaded, "
                "so these are disabled.")
        warn = ('<div class="verdict bad" style="margin:.8rem 0 0">Install it: copy '
                '<span class="mono">test/selftest.conf</span> to '
                '<span class="mono">/etc/asterisk/orata-selftest.conf</span>, append the '
                '<span class="mono">#include</span> line (with a leading newline), then '
                '<span class="mono">dialplan reload</span>. See test/README.md. '
                '<b>Remove the #include when you are done testing.</b></div>')
    body.append('<div class="card"><h2>Live channel probes'
                '<span class="note">originates real calls</span></h2>'
                '<div class="body"><div style="margin-bottom:.6rem">%s</div>'
                '<p class="hint" style="margin:0">%s</p>%s</div></div>'
                % (btns, note, warn))
    return "".join(body)


@app.post("/harness/roundtrip", response_class=HTMLResponse)
def act_roundtrip(request: Request, _=Depends(require_auth)):
    good, lines = action_roundtrip()
    card = result_card(
        "astdb round-trip -- %s" % ("all keys agree" if good else "MISMATCH"), lines)
    return render("/harness", "orata - harness", harness_body("", card))


@app.post("/harness/announce", response_class=HTMLResponse)
def act_announce(number: str = Form(...), name: str = Form(""), _=Depends(require_auth)):
    num = clean_number(number)
    if not num:
        return render("/harness", "orata - harness",
                      harness_body("", '<div class="verdict bad">Invalid number.</div>'))
    before = max(0, log_size())
    rc, out = sh([ANNOUNCE_SH, num, clean_value(name), ""], timeout=60)
    time.sleep(0.3)
    try:
        with open(LOG, "rb") as fh:
            fh.seek(before)
            new = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        new = []
    rows = [("pass" if rc == 0 else "fail", "exit code", str(rc)
             + ("" if rc == 0 else "  -- announce MUST always exit 0"))]
    if not new:
        rows.append(("warn", "new log lines", "none -- is %s writable by asterisk?" % LOG))
    for line in new:
        s = "fail" if "FAIL" in line else ("skip" if "SKIP" in line else "pass")
        rows.append((s, "log", line))
    if out:
        rows.append(("info", "stdout/stderr", out[:300]))
    extra = ""
    for line in new:
        nm = audio_name_in(line)
        if nm:
            extra = ('<div class="body"><p class="hint" style="margin-top:0">'
                     "Local render of the phrase &mdash; what the Echo was asked to "
                     "say, not proof it said it. See the "
                     '<span class="mono">alexa OK</span> line above for that.</p>'
                     "%s</div>" % audio_player(nm))
    return render("/harness", "orata - harness",
                  harness_body("", result_card("Announcement for %s" % num, rows, extra)))


@app.post("/harness/selftest", response_class=HTMLResponse)
def act_selftest(exten: str = Form(...), _=Depends(require_auth)):
    if exten not in ("7101", "7102", "7103", "7001", "7002", "7003"):
        return render("/harness", "orata - harness",
                      harness_body("", '<div class="verdict bad">Unknown selftest '
                                        "extension.</div>"))
    expect = {
        "7101": "known caller -- must SKIP the gate: no Answer(), no Read()",
        "7102": "blocked caller -- must Hangup() with NO Answer()",
        "7103": "unknown caller -- must reach Read(custom/press-one)",
        "7001": "raw DB() reads through a live channel",
    }.get(exten, "")
    ok, rows = action_selftest(exten)
    if expect:
        rows.insert(0, ("info", "expected", expect))
    return render("/harness", "orata - harness",
                  harness_body("", result_card("Selftest %s" % exten, rows)))


@app.post("/harness/reload", response_class=HTMLResponse)
def act_reload(_=Depends(require_auth)):
    ok, out = ast("core reload", timeout=30)
    rows = [("pass" if ok else "fail", "core reload",
             (out.strip()[:300] or "reloaded") if ok else out[:300])]
    return render("/harness", "orata - harness",
                  harness_body("", result_card("Asterisk reload", rows)))


# =====================================================================
# setup -- guided first-run walkthrough
# =====================================================================

STUBS = (("123456_trunk", "sub-account username"),
         ("CHANGEME", "trunk password"),
         ("newyork.voip.ms", "POP hostname"),
         ("REPLACE_WITH_LONG_RANDOM", "device password(s)"))


def pjsip_text():
    """Raw file contents, or None if unreadable."""
    try:
        with open(PJSIP_CONF) as fh:
            return fh.read()
    except OSError:
        return None


def pjsip_live_text():
    """Contents with ';' comments stripped.

    The shipped header quotes every stub string verbatim --
    '; REPLACE: newyork.voip.ms with your nearest POP' -- so a naive substring
    search reports a fully configured file as still stubbed, forever.
    Only live settings count.
    """
    conf = pjsip_text()
    if conf is None:
        return None
    return "\n".join(re.sub(r";.*$", "", ln) for ln in conf.splitlines())


def pjsip_stubs():
    conf = pjsip_live_text()
    if conf is None:
        return []
    return [lbl for needle, lbl in STUBS if needle in conf]


def reg_status():
    ok, regs = ast("pjsip show registrations")
    if not ok:
        return None
    for line in regs.splitlines():
        if "voipms" in line and "/sip:" in line:
            m = re.search(r"(Registered|Unregistered|Rejected|Auth Sent|No Auth|"
                          r"Stopped)", line)
            return m.group(1) if m else line.strip()[-40:]
    return None


def device_contacts():
    """Which of pc/mobile/desk have a live registration."""
    ok, out = ast("pjsip show contacts")
    if not ok:
        return []
    return [e for e in ("pc", "mobile", "desk") if "%s/sip:" % e in out]


def lan_ip():
    rc, out = sh(["hostname", "-I"], timeout=5)
    if rc == 0 and out.strip():
        return out.split()[0]
    return BIND if BIND not in ("0.0.0.0", "::") else "THE-PI-IP"


def setup_incomplete():
    """Short reason string for the nudge banner, or '' when nothing is pending."""
    if pjsip_stubs():
        return "voip.ms credentials are still at their stub values"
    if reg_status() != "Registered":
        return "the trunk is not registered"
    return ""


def scard(num, title, state, body):
    """state: done | now | todo"""
    lbl = {"done": "pass", "now": "do this now", "todo": "waiting"}[state]
    cls = {"done": "p-pass", "now": "p-warn", "todo": "p-skip"}[state]
    return ('<div class="card %s"><h2><span class="snum">%s</span>%s'
            '<span class="note"><span class="pill %s">%s</span></span></h2>'
            '<div class="body">%s</div></div>'
            % (state if state != "todo" else "", num, esc(title), cls, lbl, body))


def setup_body():
    stubs = pjsip_stubs()
    reg = reg_status()
    contacts = device_contacts()
    acfg = load_conf(ANNOUNCE_CONF)
    arc = acfg.get("ORATA_ARC", "/usr/local/bin/alexa_remote_control.sh")
    arc_ok = os.access(arc, os.X_OK)
    ntfy_ok = bool(acfg.get("ORATA_NTFY_URL"))
    mode = acfg.get("ORATA_ALEXA_MODE", "arc")
    ip = lan_ip()

    done_creds = not stubs
    done_reg = reg == "Registered"
    done_dev = bool(contacts)
    done_alexa = (mode == "off") or arc_ok or (mode in ("sensor", "both") and all(
        acfg.get(k) for k in ("ORATA_LWA_CLIENT_ID", "ORATA_LWA_CLIENT_SECRET",
                              "ORATA_LWA_REFRESH_TOKEN")))

    # exactly one step is "now" -- the first unfinished one. The name book is
    # "done" once it has any entry; it is never really finished, but an empty
    # book is the state worth nudging about.
    done_books = bool(db_show("cnam"))
    pending = [not done_creds, not done_reg, not done_dev, not done_books,
               not done_alexa]
    first = next((i for i, p in enumerate(pending) if p), None)

    def st(idx, done):
        return "done" if done else ("now" if idx == first else "todo")

    out = ['<h1>Setup</h1>',
           '<p class="lede">Six steps from a running Asterisk to a ringing phone. '
           'Each one checks itself &mdash; reload this page after you change '
           'something and the state updates. This page never edits a file; it tells '
           'you which file to edit and what to put in it.</p>']

    if stubs:
        out.append('<div class="verdict bad"><b>Not yet carrying calls.</b> '
                   'Still stubbed in <span class="mono">%s</span>: %s.</div>'
                   % (esc(PJSIP_CONF), esc(", ".join(stubs))))
    elif done_reg:
        out.append('<div class="verdict good"><b>Trunk is registered.</b> '
                   'Inbound calls will reach the dialplan.</div>')

    # ---- step 0 : the portal ----------------------------------------
    out.append(scard(
        "0", "voip.ms portal \u2014 do this before anything registers", "now" if first == 0
        else "done",
        '<p>This is the only step that can cost real money if skipped. A '
        'compromised PBX gets drained to premium-rate numbers overnight. '
        'Portal menu labels move around, so these are things to look for rather '
        'than exact paths.</p>'
        '<ul>'
        '<li><b>Spending limit / auto-recharge cap.</b> Set a hard monthly ceiling.</li>'
        '<li><b>Block international dialling.</b> Under account restrictions. If you '
        'never call abroad, this removes the entire payoff of a breach.</li>'
        '<li><b>Create a sub-account</b> for the trunk &mdash; <i>Sub Accounts \u2192 '
        'Create Sub Account</i>. Device type SIP, auth User/Password. '
        '<b>Never put your main login in pjsip.conf.</b> The username looks like '
        '<span class="mono">123456_trunk</span>, where 123456 is your main account '
        'number.</li>'
        '<li><b>Allowed codecs</b> on the sub-account: <span class="mono">ulaw</span> '
        'and <span class="mono">g722</span>, to match pjsip.conf.</li>'
        '<li><b>IP whitelist</b> the Pi\u2019s public IP if it is static. Skip if your '
        'ISP rotates it &mdash; a stale entry silently kills the trunk.</li>'
        '<li><b>Route the DID</b> to the sub-account &mdash; <i>DID Numbers \u2192 '
        'Manage DID</i>. A DID still pointed at the main account will never reach '
        'this Pi.</li>'
        '<li><b>Pick the nearest POP</b> and note its hostname, e.g. '
        '<span class="mono">chicago.voip.ms</span>. Check latency from the Pi:</li>'
        '</ul>'
        '<pre class="cmd">ping -c3 chicago.voip.ms\nping -c3 newyork.voip.ms\nping -c3 seattle.voip.ms</pre>'
        '<p class="hint">Lowest round-trip wins. The difference is audible on a '
        'long call.</p>'))

    # ---- step 1 : credentials ---------------------------------------
    rows = "".join(
        '<li><span class="mono">%s</span> &rarr; %s</li>' % (esc(n), esc(l))
        for n, l in STUBS)
    out.append(scard(
        "1", "Put the credentials into pjsip.conf", st(0, done_creds),
        ('<p>Four placeholder strings to replace. Every occurrence &mdash; the POP '
         'hostname appears in four places and the username in three.</p>'
         '<pre class="cmd">sudo nano %s</pre>'
         '<ul>%s</ul>'
         '<dl class="kv">'
         '<dt>Portal field</dt><dd>pjsip.conf</dd>'
         '<dt>Sub-account username</dt><dd>username / client_uri / from_user</dd>'
         '<dt>Sub-account password</dt><dd>password=</dd>'
         '<dt>POP hostname</dt><dd>contact / server_uri / client_uri / match</dd>'
         '</dl>'
         '<p>The three device passwords are yours to invent, not voip.ms\u2019s. '
         'Generate one per device:</p>'
         '<pre class="cmd">head -c 18 /dev/urandom | base64</pre>'
         '<p class="hint">Dictionary attacks on <span class="mono">1001/1001</span> '
         'are how most SIP boxes fall. These never get typed twice &mdash; paste them '
         'into the softphone once.</p>'
         + ('' if done_creds else
            '<p class="hint"><b>Still present:</b> %s</p>' % esc(", ".join(stubs))))
        % (esc(PJSIP_CONF), rows)))

    # ---- step 2 : register ------------------------------------------
    regtxt = esc(reg or "no registration object found")
    out.append(scard(
        "2", "Reload and confirm registration", st(1, done_reg),
        '<p>Then reload &mdash; the <a href="/harness">Harness</a> tab has a '
        '<b>core reload</b> button, or:</p>'
        '<pre class="cmd">sudo asterisk -rx "core reload"\nsudo asterisk -rx "pjsip show registrations"</pre>'
        '<dl class="kv"><dt>Current</dt><dd>%s</dd></dl>'
        '<p>Want <span class="mono">Registered</span>. Common failures:</p>'
        '<ul>'
        '<li><span class="mono">Rejected</span> / <span class="mono">No Auth</span> '
        '&mdash; wrong username or password, or the IP whitelist is blocking you.</li>'
        '<li><span class="mono">No response received</span> &mdash; POP hostname is '
        'wrong or unreachable.</li>'
        '<li>Registered but no inbound audio &mdash; the Pi is behind NAT. Uncomment '
        '<b>both</b> <span class="mono">external_media_address</span> and '
        '<span class="mono">external_signaling_address</span>.</li>'
        '</ul>' % regtxt))

    # ---- step 3 : a phone -------------------------------------------
    have = ", ".join(contacts) if contacts else "none registered yet"
    out.append(scard(
        "3", "Register one softphone", st(2, done_dev),
        '<p>Every field you need is on the <a href="/devices"><b>Devices</b></a> tab '
        '&mdash; server address, username, the actual password read from '
        'pjsip.conf, and download links for MicroSIP, Linphone and Groundwire. '
        'Start with the PC.</p>'
        '<dl class="kv">'
        '<dt>SIP server / domain</dt><dd>%s</dd>'
        '<dt>Transport</dt><dd>UDP</dd>'
        '<dt>Registered now</dt><dd>%s</dd>'
        '</dl>'
        '<p>Then dial <span class="mono">*99</span> from the softphone to fire a test '
        'announcement, and call your DID from a mobile. Ignore Alexa until that '
        'works.</p>'
        '<p class="hint">Remote phones belong on WireGuard. Do not forward 5060.</p>'
        % (esc(ip), esc(have))))

    # ---- step 4 : the books -----------------------------------------
    out.append(scard(
        "4", "Load the name book", "now" if first == 3 else "todo",
        '<p>A number in the <b>Name book</b> gets its name spoken and skips the '
        'robocall gate. Everyone else is asked to press 1, once, and is remembered '
        'after that.</p>'
        '<ul>'
        '<li>Add family and anyone who must never be gated &mdash; '
        '<a href="/">Books</a> tab.</li>'
        '<li>Enter numbers <b>exactly as voip.ms presents them</b>, normally 11 '
        'digits: <span class="mono">15551234567</span>. The wrong format fails '
        'silently.</li>'
        '<li>Unsure? Use the <a href="/harness">call simulator</a> &mdash; it traces '
        'any number through the dialplan against live state without placing a '
        'call.</li>'
        '</ul>'
        '<p class="hint">After a real call, the Asterisk console and '
        '<span class="mono">announce.log</span> show the exact digits that '
        'arrived. Copy from there.</p>'))

    # ---- step 5 : announcements -------------------------------------
    alexa_rows = (
        '<dl class="kv"><dt>Mode</dt><dd>%s</dd><dt>ARC script</dt><dd>%s</dd>'
        '<dt>ntfy</dt><dd>%s</dd></dl>'
        % (esc(mode), "installed" if arc_ok else esc("missing: " + arc),
           "configured" if ntfy_ok else "not set"))
    out.append(scard(
        "5", "Announcements", st(4, done_alexa),
        '<p>Edit <span class="mono">%s</span>:</p>'
        '<pre class="cmd">sudo nano %s</pre>'
        '%s'
        '<p><b>ntfy first</b> &mdash; set <span class="mono">ORATA_NTFY_URL</span> to '
        '<span class="mono">https://ntfy.sh/</span> plus a long unguessable topic. '
        'It never breaks, and it is what still works when Amazon auth rots. Public '
        'topics are readable by anyone who guesses the name.</p>'
        '<p><b>Then Alexa.</b> Fetch '
        '<span class="mono">alexa_remote_control.sh</span>, install it to '
        '<span class="mono">%s</span>, then authenticate <b>as the asterisk '
        'user</b> or the cookie lands in the wrong home directory:</p>'
        '<pre class="cmd">sudo -u asterisk %s -a</pre>'
        '<p>That starts a proxy on port 5601; log in to Amazon from a LAN browser. '
        'Verify with the <b>fire announcement</b> button on '
        '<a href="/harness">Harness</a>, then dial <span class="mono">*99</span>.</p>'
        '<p class="hint">Two Alexa traps: Do Not Disturb suppresses announcements '
        'entirely, and announcement volume follows device volume &mdash; an overnight '
        'volume routine makes calls inaudible.</p>'
        % (esc(ANNOUNCE_CONF), esc(ANNOUNCE_CONF), alexa_rows, esc(arc), esc(arc))))

    # ---- housekeeping ------------------------------------------------
    out.append('<div class="card"><h2>Before you call it done</h2><div class="body">'
               '<ul>'
               '<li><b>Change the voicemail PIN.</b> Mailbox 100 ships with '
               '<span class="mono">1357</span> in '
               '<span class="mono">/etc/asterisk/voicemail.conf</span>.</li>'
               '<li><b>Remove the selftest include</b> from '
               '<span class="mono">extensions.conf</span> if you added it, and purge '
               'the test numbers from the books.</li>'
               '<li><b>Confirm no port-forward</b> reaches this Pi on 5060. Inbound '
               'calls ride the outbound registration; nothing needs opening.</li>'
               '<li>Run <a href="/diag">Diagnostics</a> once more &mdash; it should be '
               'free of failures.</li>'
               '</ul></div></div>')
    return "".join(out)


@app.get("/setup", response_class=HTMLResponse)
def page_setup(request: Request, _=Depends(require_auth)):
    up, _v = asterisk_up()
    if not up:
        body = ('<h1>Setup</h1><div class="verdict bad"><b>Asterisk is not '
                'reachable.</b> Nothing below can be checked. '
                '<span class="mono">systemctl status asterisk</span></div>')
    else:
        body = setup_body()
    return render("/setup", "orata - setup", body, flash_of(request))


# =====================================================================
# devices -- softphone connection details, read-only
# =====================================================================

CLIENT_LINKS = (
    ("MicroSIP", "https://www.microsip.org/downloads", "Windows",
     "The portable build needs no install. Start here."),
    ("Linphone", "https://www.linphone.org/", "Linux / macOS / Windows",
     "Free, cross-platform."),
    ("Groundwire", "https://www.acrobits.net/groundwire/", "iOS / Android",
     "Paid, around $10. The only mobile client with reliable push for a "
     "self-hosted PBX -- free ones miss calls when backgrounded."),
)

PORTAL_LINKS = (
    ("voip.ms portal", "https://voip.ms/m/index.php", ""),
    ("Sub accounts", "https://voip.ms/m/subaccount.php",
     "Where the trunk credentials come from. Never use the main login."),
    ("Manage DID", "https://voip.ms/m/managedid.php",
     "Point the DID at the sub-account, or inbound calls never reach this Pi."),
    ("Server / POP list", "https://voip.ms/m/serverinfo.php",
     "Hostnames for the latency check."),
)

# Order matters only for display.
DEVICE_HINTS = {
    "pc": "Desktop softphone -- MicroSIP or Linphone.",
    "mobile": "Phone -- Groundwire. Over WireGuard when away from home.",
    "desk": "Hardware desk phone, or a third softphone.",
}


def pjsip_sections():
    """name -> {key: value}, merged across repeated [name] blocks.

    pjsip.conf declares each device three times -- endpoint, auth, aor -- all
    under the same section name. Merging them is what lets one card show the
    whole device.
    """
    conf = pjsip_text()
    if conf is None:
        return {}
    secs, cur = {}, None
    for raw in conf.splitlines():
        line = re.sub(r";.*$", "", raw).strip()
        if not line:
            continue
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            cur = m.group(1)
            secs.setdefault(cur, {})
            continue
        if cur and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in secs[cur]:
                secs[cur][k] = secs[cur][k] + ", " + v   # allow=ulaw, allow=g722
            else:
                secs[cur][k] = v
    return secs


def contact_map():
    """endpoint -> (status, rtt, uri) for whatever is currently registered."""
    ok, out = ast("pjsip show contacts")
    cmap = {}
    if not ok:
        return cmap
    for line in out.splitlines():
        # The header row ("Contact:  <Aor/ContactUri...> <Hash> <Status>") also
        # satisfies the shape, so require a real sip: URI.
        m = re.match(r"\s*Contact:\s+([^/\s<]+)/(sip:\S+)\s+\S+\s+(\S+)\s*([\d.]+)?",
                     line)
        if m:
            cmap[m.group(1)] = (m.group(3), m.group(4) or "", m.group(2))
    return cmap


def kvdump(d, title="all parsed settings"):
    rows = "".join('<dt>%s</dt><dd>%s</dd>' % (esc(k), esc(v))
                   for k, v in sorted(d.items()))
    return ('<details><summary class="hint" style="cursor:pointer">%s</summary>'
            '<dl class="kv">%s</dl></details>' % (esc(title), rows))


def device_card(name, sec, cmap, ip):
    cid = sec.get("callerid", "")
    m = re.search(r"<(\d+)>", cid)
    ext = m.group(1) if m else "?"
    user = sec.get("username", name)
    pw = sec.get("password", "")
    status, rtt, uri = cmap.get(name, ("", "", ""))

    if status.startswith("Avail"):
        badge = '<span class="pill p-pass">registered</span>'
        live = ('<dl class="kv"><dt>Contact</dt><dd>%s</dd>'
                '<dt>Round trip</dt><dd>%s ms</dd></dl>'
                % (esc(uri), esc(rtt or "n/a")))
    elif status:
        badge = '<span class="pill p-warn">%s</span>' % esc(status)
        live = ""
    else:
        badge = '<span class="pill p-skip">not registered</span>'
        live = ""

    pwblock = ('<details><summary class="hint" style="cursor:pointer">'
               'show password</summary><pre class="cmd">%s</pre></details>'
               % esc(pw)) if pw else '<span class="dim">not set</span>'

    return ('<div class="card"><h2>%s &mdash; extension %s'
            '<span class="note">%s</span></h2><div class="body">'
            '<p>%s</p>'
            '<dl class="kv">'
            '<dt>SIP server</dt><dd>%s</dd>'
            '<dt>Domain</dt><dd>%s</dd>'
            '<dt>Port</dt><dd>5060</dd>'
            '<dt>Username / Login</dt><dd>%s</dd>'
            '<dt>Display name</dt><dd>%s</dd>'
            '<dt>Transport</dt><dd>%s</dd>'
            '<dt>Codecs</dt><dd>%s</dd>'
            '<dt>Password</dt><dd>%s</dd>'
            '</dl>%s%s</div></div>'
            % (esc(name), esc(ext), badge,
               esc(DEVICE_HINTS.get(name, "")),
               esc(ip), esc(ip), esc(user), esc(cid) or esc(name),
               esc(sec.get("transport", "transport-udp").replace("transport-", "").upper()),
               esc(sec.get("allow", "ulaw, g722")),
               pwblock, live, kvdump(sec)))


def devices_body():
    secs = pjsip_sections()
    cmap = contact_map()
    ip = lan_ip()
    out = ['<h1>Devices</h1>',
           '<p class="lede">Every field a softphone asks for, read live from '
           '<span class="mono">%s</span>. Nothing on this page is editable &mdash; '
           'changing a device means editing that file and running '
           '<span class="mono">pjsip reload</span>.</p>' % esc(PJSIP_CONF)]

    if not secs:
        out.append('<div class="verdict bad"><b>Cannot read %s.</b> The web UI runs as '
                   'the <span class="mono">asterisk</span> user and the file is mode '
                   '640 &mdash; check ownership.</div>' % esc(PJSIP_CONF))
        return "".join(out)

    out.append('<div class="card"><h2>MicroSIP &mdash; field by field</h2>'
               '<div class="body">'
               '<p>Menu (&#9776;, top right) &rarr; <b>Add Account</b>. '
               'Leave anything not listed at its default.</p>'
               '<dl class="kv">'
               '<dt>Account Name</dt><dd>orata <span class="dim">(just a label)</span></dd>'
               '<dt>SIP Server</dt><dd>%s</dd>'
               '<dt>SIP Proxy</dt><dd><span class="dim">leave empty</span></dd>'
               '<dt>Username</dt><dd>the device username below</dd>'
               '<dt>Domain</dt><dd>%s</dd>'
               '<dt>Login</dt><dd>same as Username</dd>'
               '<dt>Password</dt><dd>from the device card below</dd>'
               '<dt>Transport</dt><dd>UDP</dd>'
               '</dl>'
               '<p>Status bottom-left should reach <b>Online</b> within a few seconds. '
               'Then dial <span class="mono">*99</span> for a test announcement.</p>'
               '<p class="hint">This Pi answers on more than one address. Use '
               '<span class="mono">%s</span> &mdash; the one serving this page.</p>'
               '</div></div>' % (esc(ip), esc(ip), esc(ip)))

    devs = [(n, s) for n, s in secs.items()
            if "callerid" in s and "auth" in s]
    order = {"pc": 0, "mobile": 1, "desk": 2}
    devs.sort(key=lambda t: (order.get(t[0], 9), t[0]))
    for name, sec in devs:
        out.append(device_card(name, sec, cmap, ip))

    pwset = {s.get("password") for _n, s in devs if s.get("password")}
    if len(pwset) == 1 and len(devs) > 1:
        out.append('<div class="verdict bad"><b>All devices share one password.</b> '
                   'A leaked softphone config hands over every extension. Give each '
                   'its own: <span class="mono">head -c 18 /dev/urandom | base64</span>'
                   '</div>')

    # --- clients ---
    rows = "".join(
        '<div class="chk"><span class="pill p-info">%s</span>'
        '<span class="nm"><a href="%s" target="_blank" rel="noreferrer">%s</a></span>'
        '<span class="dt">%s</span></div>' % (esc(plat), esc(url), esc(nm), esc(note))
        for nm, url, plat, note in CLIENT_LINKS)
    out.append('<div class="card"><h2>Softphone downloads</h2>'
               '<div class="body flush">%s</div></div>' % rows)

    # --- trunk ---
    tr = secs.get("voipms", {})
    auth = secs.get("voipms_auth", {})
    aor = secs.get("voipms_aor", {})
    ident = secs.get("voipms_identify", {})
    reg = reg_status()
    rbadge = ('<span class="pill p-pass">registered</span>' if reg == "Registered"
              else '<span class="pill p-fail">%s</span>' % esc(reg or "not registered"))
    out.append('<div class="card"><h2>voip.ms trunk<span class="note">%s</span></h2>'
               '<div class="body">'
               '<dl class="kv">'
               '<dt>Sub-account</dt><dd>%s</dd>'
               '<dt>POP (contact)</dt><dd>%s</dd>'
               '<dt>from_domain</dt><dd>%s</dd>'
               '<dt>identify match</dt><dd>%s</dd>'
               '<dt>Codecs</dt><dd>%s</dd>'
               '</dl>'
               '<p class="hint"><b>identify match</b> is how inbound calls are '
               'recognised as coming from the trunk. If it does not equal the POP, '
               'calls never enter <span class="mono">[from-voipms]</span> &mdash; the '
               'gate, the name book and announcements are all bypassed, while '
               'registration still reports fine.</p>%s</div></div>'
               % (rbadge, esc(auth.get("username", "?")),
                  esc(aor.get("contact", "?")), esc(tr.get("from_domain", "?")),
                  esc(ident.get("match", "?")), esc(tr.get("allow", "?")),
                  kvdump(tr, "all parsed endpoint settings")))

    prows = "".join(
        '<div class="chk"><span class="pill p-info">link</span>'
        '<span class="nm"><a href="%s" target="_blank" rel="noreferrer">%s</a></span>'
        '%s</div>' % (esc(url), esc(nm),
                      '<span class="dt">%s</span>' % esc(note) if note else "")
        for nm, url, note in PORTAL_LINKS)
    out.append('<div class="card"><h2>voip.ms portal</h2>'
               '<div class="body flush">%s</div>'
               '<div class="body tight"><p class="hint" style="margin:0">Portal menu '
               'labels move around; these deep links are the stable part. The REST API '
               'could automate some of this &mdash; deliberately not wired up, since it '
               'would mean storing an API key that can spend money.</p></div>'
               '</div>' % prows)

    out.append('<div class="card"><h2>Adding a device</h2><div class="body">'
               '<p>Six lines in <span class="mono">%s</span>, using the templates '
               'already in the file:</p>'
               '<pre class="cmd">[kitchen](endpoint-tpl)\nauth=kitchen\naors=kitchen\n'
               'callerid=Kitchen &lt;104&gt;\n[kitchen](auth-tpl)\nusername=kitchen\n'
               'password=PASTE_A_RANDOM_ONE\n[kitchen](aor-tpl)</pre>'
               '<pre class="cmd">head -c 18 /dev/urandom | base64\n'
               'sudo asterisk -rx "pjsip reload"</pre>'
               '<p class="hint">Then add the extension to '
               '<span class="mono">RINGALL</span> in extensions.conf if it should ring '
               'on inbound calls.</p></div></div>' % esc(PJSIP_CONF))
    return "".join(out)


@app.get("/devices", response_class=HTMLResponse)
def page_devices(request: Request, _=Depends(require_auth)):
    return render("/devices", "orata - devices", devices_body(), flash_of(request))


# =====================================================================
# announcement audio
#
# orata-announce.sh renders every phrase to a WAV under AUDIO_DIR. This
# serves them back so you can hear what was announced without standing
# next to an Echo. Playback here is NOT evidence Amazon spoke anything --
# only the "alexa OK" log line is that.
# =====================================================================

# The sole trusted shape. Anything else is refused rather than sanitised.
AUDIO_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]{3,15}\.wav$")


def audio_name_in(line):
    """Pull a servable recording name out of an 'audio OK <path>' log line."""
    m = re.search(r"audio OK\s+(\S+\.wav)", line)
    if not m:
        return ""
    name = os.path.basename(m.group(1))
    return name if AUDIO_RE.match(name) else ""


def audio_files(limit=50):
    """(name, mtime, size), newest first. Names are filtered, never joined
    with anything the user typed."""
    try:
        names = [n for n in os.listdir(AUDIO_DIR) if AUDIO_RE.match(n)]
    except OSError:
        return []
    rows = []
    for n in names:
        try:
            st = os.stat(os.path.join(AUDIO_DIR, n))
        except OSError:
            continue
        rows.append((n, st.st_mtime, st.st_size))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def audio_player(name):
    return ('<audio controls preload="none" style="height:2rem;vertical-align:middle" '
            'src="/audio/file?f=%s"></audio>' % urllib.parse.quote(name))


@app.get("/audio/file")
def audio_file(f: str = "", _=Depends(require_auth)):
    name = os.path.basename(f or "")
    if not AUDIO_RE.match(name):
        raise HTTPException(status_code=400, detail="bad recording name")
    try:
        with open(os.path.join(AUDIO_DIR, name), "rb") as fh:
            data = fh.read()
    except OSError:
        raise HTTPException(status_code=404, detail="no such recording")
    return Response(data, media_type="audio/wav",
                    headers={"Cache-Control": "private, max-age=3600",
                             "Content-Disposition": 'inline; filename="%s"' % name,
                             "X-Content-Type-Options": "nosniff"})


@app.get("/audio", response_class=HTMLResponse)
def page_audio(request: Request, _=Depends(require_auth)):
    rows = audio_files(50)
    body = ["<h1>Announcement audio</h1>",
            '<p class="lede">Every announcement is also rendered to a WAV on the Pi, '
            "newest first. This is how you check what was said without an Echo in "
            "earshot &mdash; but it is a <b>local render of the phrase</b>, not proof "
            'Amazon spoke it. For that, read the <span class="mono">alexa OK</span> '
            'line on the <a href="/log">Log</a> tab.</p>']
    if not rows:
        body.append('<div class="card"><h2>Recordings</h2><div class="body">'
                    '<div class="empty">nothing in %s yet</div>'
                    '<p class="hint">Needs <span class="mono">ORATA_RECORD_AUDIO=1</span> '
                    "in announce.conf and "
                    '<span class="mono">apt install espeak-ng</span>. Then fire one from '
                    'the <a href="/harness">Harness</a> tab, or take a call.</p>'
                    "</div></div>" % esc(AUDIO_DIR))
    else:
        cells = []
        for name, mtime, size in rows:
            stem = name[:-4]
            d, t, num = stem.split("-", 2)
            when = "%s-%s-%s %s:%s:%s" % (d[0:4], d[4:6], d[6:8],
                                          t[0:2], t[2:4], t[4:6])
            cells.append(
                '<div class="chk s-pass"><span class="nm mono">%s</span>'
                '<span class="dt">%s &middot; %s &middot; %d bytes</span>'
                '<span class="hn">%s</span></div>'
                % (esc(num), esc(when), esc(name), size, audio_player(name)))
        body.append('<div class="card"><h2>Recordings'
                    '<span class="note">newest %d &middot; ring buffer</span></h2>'
                    '<div class="body flush">%s</div></div>'
                    % (len(rows), "".join(cells)))
        body.append('<p class="hint">Older files are deleted automatically by '
                    '<span class="mono">orata-announce.sh</span> &mdash; see '
                    '<span class="mono">ORATA_AUDIO_KEEP</span>. Nothing here is '
                    "call audio; no conversation is ever recorded.</p>")
    body.append(prompts_card())
    body.append(names_card())
    body.append(clips_card())
    return render("/audio", "orata - audio", "".join(body), flash_of(request))


# ---------------------------------------------------------------------
# recorded clips -- your own voice, captured by dialling *96
#
# Same rigidity as the renders above: names are timestamps, validated by
# regex, never built from anything typed. Mutations shell out to
# orata-clip.sh so the CLI stays the write path.
# ---------------------------------------------------------------------

CLIP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}\.wav$")
CLIP_SERVE_RE = re.compile(
    r"^(?:[0-9]{8}-[0-9]{6}|role-[a-z0-9-]{1,24}|name-[0-9]{3,15})\.wav$")

# (role, title, what it is, suggested text). Order is the order callers
# hear them. Keep in step with VALID_ROLES in orata-clip.sh.
CLIP_ROLES = [
    ("press-one", "Robocall gate", "Unknown callers hear this before pressing 1.",
     "Press 1 to continue."),
    ("rec-start", "*96 record", "Played before recording starts.",
     "Speak after the beep, then press hash."),
    ("rec-menu", "*96 review menu", "The replay / save / re-record menu.",
     "Press 1 to replay, 2 to save, 3 to re-record, or star to cancel."),
    ("rec-saved", "*96 saved", "Confirmation after a clip is saved.",
     "Saved."),
]


def clip_label(stem):
    try:
        with open(os.path.join(CLIP_DIR, stem + ".txt")) as fh:
            return fh.read().strip()[:120]
    except OSError:
        return ""


def role_source(role):
    try:
        with open(os.path.join(CLIP_DIR, "role-%s.txt" % role)) as fh:
            return fh.read().strip()[:40]
    except OSError:
        return ""


def role_installed(role):
    return os.path.exists(os.path.join(CLIP_DIR, "role-%s.wav" % role))


def clip_files(limit=100):
    try:
        names = [n for n in os.listdir(CLIP_DIR) if CLIP_RE.match(n)]
    except OSError:
        return []
    rows = []
    for n in names:
        try:
            st = os.stat(os.path.join(CLIP_DIR, n))
        except OSError:
            continue
        rows.append((n[:-4], st.st_mtime, st.st_size))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def clip_player(name):
    return ('<audio controls preload="none" style="height:2rem;vertical-align:middle" '
            'src="/clips/file?f=%s"></audio>' % urllib.parse.quote(name))


def clip_secs(size):
    """8 kHz 16-bit mono PCM, minus a 44-byte header. Approximate on purpose."""
    return max(0.0, (size - 44) / 16000.0)


def prompts_card():
    """Every spoken prompt, editable as text or replaceable by a recording.

    Writes land in CLIP_DIR, never in the Asterisk sounds tree -- that is
    root-owned and outside the service unit's ReadWritePaths, so the UI
    could not write there even if it wanted to. The dialplan checks for a
    role file and falls back to the shipped sound, which means an empty
    role is always safe."""
    rows = clip_files(100)
    out = ['<div class="card"><h2>Spoken prompts'
           '<span class="note">applies to the next call</span></h2>'
           '<div class="body">',
           '<p class="hint" style="margin-top:0">Type the words and press '
           '<b>synthesise</b> for a robot voice, or record one by dialling '
           '<span class="mono">*96</span> and assign it below. Unset prompts fall '
           "back to the sound files installed with the project, so clearing one can "
           "never leave a caller in silence.</p>"]
    for role, title, blurb, suggested in CLIP_ROLES:
        inst = role_installed(role)
        src = role_source(role) if inst else ""
        # Prefill with the current text if it was synthesised, so editing
        # a prompt is a tweak rather than a retype.
        cur = src[6:] if src.startswith("text: ") else ""
        pick = "".join(
            '<option value="%s">%s%s</option>'
            % (esc(stem), esc(stem),
               " -- " + esc(clip_label(stem)) if clip_label(stem) else "")
            for stem, _m, _s in rows)
        out.append(
            '<div style="border-top:1px solid #222;padding:.75rem 0">'
            '<div class="row" style="align-items:center">'
            '<b>%s</b>%s<span class="hint" style="margin:0;flex:1;min-width:12rem">%s</span>'
            "</div>"
            '<div class="row" style="margin-top:.45rem">'
            '<form class="row" method="post" action="/clips/say" style="margin:0">'
            '<input type="hidden" name="role" value="%s">'
            '<input class="in" name="text" value="%s" placeholder="%s" size="34">'
            '<button class="btn primary" type="submit">synthesise</button></form>'
            "%s%s</div></div>"
            % (esc(title),
               ("  " + clip_player("role-%s.wav" % role)) if inst else "",
               esc(src if inst else blurb),
               esc(role), esc(cur, quote=True), esc(suggested, quote=True),
               # assign-from-recording, only offered when clips exist
               ('<form class="row" method="post" action="/clips/assign" '
                'style="margin:0"><input type="hidden" name="role" value="%s">'
                '<select class="in" name="name">%s</select>'
                '<button class="btn" type="submit">use recording</button></form>'
                % (esc(role), pick)) if rows else "",
               ('<form class="inline" method="post" action="/clips/unassign">'
                '<input type="hidden" name="role" value="%s">'
                '<button class="btn" type="submit">reset</button></form>'
                % esc(role)) if inst else ""))
    out.append("</div></div>")
    return "".join(out)


def name_source(num):
    try:
        with open(os.path.join(CLIP_DIR, "name-%s.txt" % num)) as fh:
            return fh.read().strip()[:200]
    except OSError:
        return ""


def name_recorded(num):
    return os.path.exists(os.path.join(CLIP_DIR, "name-%s.wav" % num))


def names_card():
    """Per-caller spoken names, in a real voice.

    The name book (astdb) stays the source of truth for WHO is calling and
    whether the gate is skipped; this only changes how the name SOUNDS in
    the local render. A caller with no recording here falls back to espeak,
    so the two can never disagree about identity."""
    book = db_show("cnam")
    clips = clip_files(100)
    out = ['<div class="card"><h2>Recorded caller names'
           '<span class="note">local audio only</span></h2><div class="body">',
           '<p class="hint" style="margin-top:0">Say the name yourself instead of '
           'letting espeak mangle it &mdash; &ldquo;Call from <i>Yoshita</i>&rdquo; '
           "pronounced properly. Applies to the WAV on this page and to "
           "&ldquo;play on handsets&rdquo;.</p>"
           '<p class="hint"><b>It does not change what Alexa says.</b> Amazon\'s '
           "announcement API takes text, not audio, so an Echo still speaks the "
           "espeak phrase. This is for the local render and the handsets.</p>"]
    if not book:
        out.append('<div class="empty">the name book is empty &mdash; '
                   'add someone on the <a href="/">Books</a> tab first</div>')
    for num, name in book:
        rec = name_recorded(num)
        src = name_source(num)
        cur = src[6:] if src.startswith("text: ") else ""
        pick = "".join(
            '<option value="%s">%s%s</option>'
            % (esc(stem), esc(stem),
               " -- " + esc(clip_label(stem)) if clip_label(stem) else "")
            for stem, _m, _s in clips)
        out.append(
            '<div style="border-top:1px solid #222;padding:.75rem 0">'
            '<div class="row" style="align-items:center">'
            '<span class="mono">%s</span><b>%s</b>%s'
            '<span class="hint" style="margin:0;flex:1;min-width:10rem">%s</span></div>'
            '<div class="row" style="margin-top:.45rem">'
            '<form class="row" method="post" action="/names/say" style="margin:0">'
            '<input type="hidden" name="number" value="%s">'
            '<input class="in" name="text" value="%s" placeholder="%s" size="18">'
            '<button class="btn" type="submit">synthesise</button></form>'
            "%s%s</div></div>"
            % (esc(num), esc(name),
               ("  " + clip_player("name-%s.wav" % num)) if rec else "",
               esc(src if rec else "generated by espeak"),
               esc(num), esc(cur, quote=True), esc(name, quote=True),
               ('<form class="row" method="post" action="/names/set" style="margin:0">'
                '<input type="hidden" name="number" value="%s">'
                '<select class="in" name="name">%s</select>'
                '<button class="btn" type="submit">use recording</button></form>'
                % (esc(num), pick)) if clips else "",
               ('<form class="inline" method="post" action="/names/del">'
                '<input type="hidden" name="number" value="%s">'
                '<button class="btn" type="submit">reset</button></form>'
                % esc(num)) if rec else ""))
    out.append('<p class="hint">Record a name by dialling <span class="mono">*96</span>, '
               "or press <span class=\"mono\">3434</span> mid-call to capture one from "
               "the conversation &mdash; then pick it from the dropdown.</p>"
               "</div></div>")
    return "".join(out)


@app.post("/names/say")
def act_name_say(number: str = Form(...), text: str = Form(""),
                 _=Depends(require_auth)):
    num = clean_number(number)
    if not num:
        return back("invalid number", True, "/audio")
    txt = clean_value(text)
    if not txt:
        return _clip_cmd(["name-del", num], "cleared -- back to the generated name")
    return _clip_cmd(["name-say", num, txt], "name synthesised")


@app.post("/names/set")
def act_name_set(number: str = Form(...), name: str = Form(...),
                 _=Depends(require_auth)):
    num = clean_number(number)
    if not num:
        return back("invalid number", True, "/audio")
    return _clip_cmd(["name-set", num, os.path.basename(name)],
                     "recording installed as the spoken name")


@app.post("/names/del")
def act_name_del(number: str = Form(...), _=Depends(require_auth)):
    num = clean_number(number)
    if not num:
        return back("invalid number", True, "/audio")
    return _clip_cmd(["name-del", num], "back to the generated name")


def clips_card():
    rows = clip_files(100)
    out = ['<div class="card"><h2>Recorded clips'
           '<span class="note">dial *96</span></h2><div class="body">',
           '<p class="hint" style="margin-top:0">Your own voice instead of espeak. '
           'Dial <span class="mono">*96</span> from any registered handset, speak, '
           'press <span class="mono">#</span>, then '
           '<span class="mono">1</span> replay &middot; '
           '<span class="mono">2</span> save &middot; '
           '<span class="mono">3</span> re-record &middot; '
           '<span class="mono">*</span> cancel. Nothing enters the library until you '
           'press 2, so you can re-record as many times as you like.</p>']

    if not rows:
        out.append('<div class="empty">no clips in %s yet</div>' % esc(CLIP_DIR))
    for stem, mtime, size in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        lbl = clip_label(stem)
        assign = "".join(
            '<form class="inline" method="post" action="/clips/assign" '
            'style="margin-right:.4rem"><input type="hidden" name="role" value="%s">'
            '<input type="hidden" name="name" value="%s">'
            '<button class="btn" type="submit">use as %s</button></form>'
            % (esc(r), esc(stem), esc(t.lower())) for r, t, _b in CLIP_ROLES)
        out.append(
            '<div style="border-top:1px solid #222;padding:.75rem 0">'
            '<div class="row" style="align-items:center">'
            '<span class="mono">%s</span>'
            '<span class="hint" style="margin:0">%s &middot; %.1fs &middot; %d bytes</span>'
            "%s</div>"
            '<div class="row" style="margin-top:.45rem">'
            '<form class="row" method="post" action="/clips/label" style="margin:0">'
            '<input type="hidden" name="name" value="%s">'
            '<input class="in" name="label" value="%s" placeholder="label this clip" '
            'size="26"><button class="btn" type="submit">save label</button></form>'
            '<form class="inline" method="post" action="/clips/broadcast" '
            'style="margin-right:.4rem"><input type="hidden" name="name" value="%s">'
            '<button class="btn" type="submit">play on handsets</button></form>'
            "%s"
            '<form class="inline" method="post" action="/clips/del">'
            '<input type="hidden" name="name" value="%s">'
            '<button class="btn" type="submit">delete</button></form>'
            "</div></div>"
            % (esc(stem), esc(when), clip_secs(size), size, clip_player(stem + ".wav"),
               esc(stem), esc(lbl, quote=True), esc(stem), assign, esc(stem)))

    out.append('<p class="hint">&ldquo;Play on handsets&rdquo; originates a call to '
               'each endpoint in <span class="mono">ORATA_PAGE_ENDPOINTS</span> and '
               "plays the clip when it is answered &mdash; the phones <b>ring</b>, "
               "this is not auto-answer paging.</p></div></div>")
    return "".join(out)


@app.get("/clips/file")
def clip_file(f: str = "", _=Depends(require_auth)):
    name = os.path.basename(f or "")
    if not CLIP_SERVE_RE.match(name):
        raise HTTPException(status_code=400, detail="bad clip name")
    try:
        with open(os.path.join(CLIP_DIR, name), "rb") as fh:
            data = fh.read()
    except OSError:
        raise HTTPException(status_code=404, detail="no such clip")
    return Response(data, media_type="audio/wav",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": 'inline; filename="%s"' % name,
                             "X-Content-Type-Options": "nosniff"})


def _clip_cmd(args, ok_msg):
    rc, out = sh([CLIP_SH] + args, timeout=30)
    if rc == 0:
        return back(ok_msg, False, "/audio")
    return back((out.strip().splitlines() or ["failed"])[-1][:140], True, "/audio")


@app.post("/clips/label")
def act_clip_label(name: str = Form(...), label: str = Form(""),
                   _=Depends(require_auth)):
    return _clip_cmd(["label", os.path.basename(name), clean_value(label)],
                     "label saved")


@app.post("/clips/del")
def act_clip_del(name: str = Form(...), _=Depends(require_auth)):
    return _clip_cmd(["del", os.path.basename(name)], "clip deleted")


@app.post("/clips/assign")
def act_clip_assign(role: str = Form(...), name: str = Form(...),
                    _=Depends(require_auth)):
    return _clip_cmd(["assign", os.path.basename(role), os.path.basename(name)],
                     "clip installed -- it applies to the next call")


@app.post("/clips/say")
def act_clip_say(role: str = Form(...), text: str = Form(""),
                 _=Depends(require_auth)):
    txt = clean_value(text)
    if not txt:
        # Empty box means "stop overriding", which is less surprising than
        # synthesising silence.
        return _clip_cmd(["unassign", os.path.basename(role)],
                         "prompt cleared -- back to the built-in")
    return _clip_cmd(["say", os.path.basename(role), txt],
                     "prompt synthesised -- it applies to the next call")


@app.post("/clips/unassign")
def act_clip_unassign(role: str = Form(...), _=Depends(require_auth)):
    return _clip_cmd(["unassign", os.path.basename(role)],
                     "reverted to the built-in prompt")


@app.post("/clips/broadcast")
def act_clip_broadcast(name: str = Form(...), _=Depends(require_auth)):
    return _clip_cmd(["broadcast", os.path.basename(name)], "sent to the handsets")


# =====================================================================
# log
# =====================================================================

@app.get("/log", response_class=HTMLResponse)
def page_log(request: Request, _=Depends(require_auth)):
    lines = log_tail(200)
    if lines:
        out = []
        for line in lines:
            cls = "lf" if "FAIL" in line else ("lo" if " OK " in line
                                               else ("ls" if "SKIP" in line else ""))
            txt = '<span class="%s">%s</span>' % (cls, esc(line)) if cls else esc(line)
            nm = audio_name_in(line)
            if nm:
                txt += ('   <a href="/audio/file?f=%s">play</a>'
                        % urllib.parse.quote(nm))
            out.append(txt)
        pre = "<pre>%s</pre>" % "\n".join(out)
    else:
        pre = '<div class="empty">empty or unreadable: %s</div>' % esc(LOG)
    size = log_size()
    body = ("<h1>announce.log</h1>"
            '<p class="lede">Last 200 lines of <span class="mono">%s</span> (%s). '
            "This is where <span class=\"mono\">alexa FAIL</span> and "
            "<span class=\"mono\">sensor FAIL</span> show up. There is no logrotate for "
            "this file yet &mdash; it grows forever.</p>"
            '<div class="card"><h2>Tail</h2><div class="body">%s</div></div>'
            % (esc(LOG), "%d bytes" % size if size >= 0 else "missing", pre))
    return render("/log", "orata - log", body, flash_of(request))


# =====================================================================
# entry point
# =====================================================================

def main():
    if not TOKEN:
        sys.stderr.write(
            "orata-web: ORATA_WEB_TOKEN is empty in %s -- refusing to start.\n"
            "Generate one with:  head -c 24 /dev/urandom | base64\n" % CONF_PATH)
        sys.exit(1)
    if BIND in ("0.0.0.0", "::") and CFG.get("ORATA_WEB_ALLOW_ANY_BIND") != "1":
        sys.stderr.write(
            "orata-web: refusing to bind %s. This UI edits call routing and runs as\n"
            "the asterisk user. Bind 127.0.0.1 or a WireGuard address.\n"
            "Set ORATA_WEB_ALLOW_ANY_BIND=1 to override.\n" % BIND)
        sys.exit(1)
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info", access_log=True,
                server_header=False, date_header=True)


if __name__ == "__main__":
    main()