#!/usr/bin/env python3
"""לוח בקרת תיקים — שרת משרדי.

רץ על מחשב אחד במשרד, בלי התקנות נוספות (Python 3.9 ומעלה בלבד).
שאר המחשבים במשרד נכנסים דרך הדפדפן לכתובת שהשרת מדפיס בהפעלה.

    python server.py            # פורט 8780, פתוח לרשת המשרד
    python server.py --local    # רק למחשב הזה
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import shutil
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FILES = DATA / "files"
BACKUPS = DATA / "backups"
STATIC = ROOT / "static"
VERSION_FILE = ROOT / "VERSION"
RESTART_CODE = 3  # start.bat starts the server again when it exits with this code
DB_PATH = DATA / "office.db"

COLLECTIONS = {"cases", "tasks", "deals", "docs", "style", "drafts", "inbox", "settings"}
MAX_BODY = 30 * 1024 * 1024
SESSION_DAYS = 30

_lock = threading.Lock()


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA.mkdir(exist_ok=True)
    FILES.mkdir(exist_ok=True)
    BACKUPS.mkdir(exist_ok=True)
    with db() as c:
        c.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv(col TEXT, id TEXT, data TEXT, updated TEXT, PRIMARY KEY(col, id));
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt TEXT, hash TEXT, admin INTEGER DEFAULT 0, created TEXT);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, name TEXT, ctype TEXT, size INTEGER, created TEXT, by TEXT);
            """
        )


def daily_backup():
    """One copy of the database per day; the last 60 are kept."""
    if not DB_PATH.exists():
        return
    target = BACKUPS / f"office-{datetime.now():%Y-%m-%d}.db"
    if not target.exists():
        with db() as src, sqlite3.connect(target) as dst:
            src.backup(dst)
    for old in sorted(BACKUPS.glob("office-*.db"))[:-60]:
        old.unlink()


def norm_name(n):
    """Names match even if quotes, gershayim or spacing were typed differently."""
    n = str(n or "")
    for ch in "״“”″„":
        n = n.replace(ch, '"')
    for ch in "׳‘’′`":
        n = n.replace(ch, "'")
    return " ".join(n.split()).lower()


def get_conf(key):
    with db() as c:
        r = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_conf(key, value):
    with db() as c:
        if value is None:
            c.execute("DELETE FROM config WHERE key=?", (key,))
        else:
            c.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (key, value))


def local_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "LawBoard-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def update_source():
    return (get_conf("update_repo") or "").strip().strip("/")


def check_update():
    repo = update_source()
    if not repo:
        return {"configured": False, "current": local_version()}
    raw = os.environ.get("LAWBOARD_RAW_BASE", "https://raw.githubusercontent.com")  # overridable for testing
    info = json.loads(fetch(f"{raw}/{repo}/main/version.json?t={int(time.time())}").decode())
    return {"configured": True, "repo": repo, "current": local_version(), "latest": str(info.get("version", "")),
            "notes": info.get("notes", ""), "available": str(info.get("version", "")) > local_version()}


def apply_update():
    """Downloads the repository, replaces the program files and keeps data/ untouched."""
    repo = update_source()
    zipbase = os.environ.get("LAWBOARD_ZIP_BASE", "https://codeload.github.com")
    blob = fetch(f"{zipbase}/{repo}/zip/refs/heads/main", timeout=120)
    tmp = Path(tempfile.mkdtemp(prefix="lawboard_upd_"))
    try:
        zp = tmp / "u.zip"
        zp.write_bytes(blob)
        with zipfile.ZipFile(zp) as z:
            z.extractall(tmp / "x")
        top = next((tmp / "x").iterdir())
        src = top / "lawboard" if (top / "lawboard" / "server.py").exists() else top
        if not (src / "server.py").exists() or not (src / "static" / "index.html").exists():
            raise ValueError("bad_package")
        for item in src.iterdir():
            if item.name in ("data", "seed.json", ".git", "start.bat", "reset-code.bat"):
                continue
            dest = ROOT / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return local_version()


def claude_config_paths():
    """Where the Claude desktop app keeps its settings on this computer."""
    if os.name == "nt":
        found = []
        local = os.environ.get("LOCALAPPDATA")
        if local:  # the Microsoft Store edition keeps them inside its package folder
            found += [p / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json" for p in Path(local, "Packages").glob("Claude_*")]
        return found + [Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"]
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    return [Path.home() / ".config" / "Claude" / "claude_desktop_config.json"]


def claude_status():
    for p in claude_config_paths():
        try:
            if "lawboard" in json.loads(p.read_text(encoding="utf-8")).get("mcpServers", {}):
                return True
        except (OSError, ValueError):
            pass
    return False


def claude_connect():
    """Adds this system to the Claude app's connectors, keeping everything else in the file."""
    entry = {"command": sys.executable, "args": [str(ROOT / "mcp_server.py")]}
    paths = claude_config_paths()
    targets = [p for p in paths if p.parent.exists()] or [paths[-1]]
    for p in targets:
        p.parent.mkdir(parents=True, exist_ok=True)
        cfg = {}
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            cfg = json.loads(raw) if raw.strip() else {}
            shutil.copy2(p, p.with_suffix(".json.bak"))
        cfg.setdefault("mcpServers", {})["lawboard"] = entry
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return [str(p) for p in targets]


def get_admins():
    try:
        return json.loads(get_conf("admins") or "{}")
    except ValueError:
        return {}


def save_admins(admins):
    set_conf("admins", json.dumps(admins, ensure_ascii=False))


def set_admin(name, pin):
    salt = secrets.token_hex(16)
    a = get_admins()
    a[name] = {"salt": salt, "hash": hash_pin(pin, salt)}
    save_admins(a)


def get_pins():
    try:
        return json.loads(get_conf("pins") or "{}")
    except ValueError:
        return {}


def set_pin(name, pin):
    pins = get_pins()
    if pin:
        salt = secrets.token_hex(16)
        pins[name] = {"salt": salt, "hash": hash_pin(pin, salt)}
    else:
        pins.pop(name, None)
    set_conf("pins", json.dumps(pins, ensure_ascii=False))


def set_code(code):
    """An empty code means the system opens without a code."""
    if not code:
        set_conf("code_hash", None)
        set_conf("code_salt", None)
        return
    salt = secrets.token_hex(16)
    set_conf("code_salt", salt)
    set_conf("code_hash", hash_pin(code, salt))
    with db() as c:
        c.execute("DELETE FROM sessions")


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()


def import_seed():
    seed = ROOT / "seed.json"
    if not seed.exists():
        return 0
    with db() as c:
        if c.execute("SELECT 1 FROM kv WHERE col='cases' LIMIT 1").fetchone():
            return 0
        rows = json.loads(seed.read_text(encoding="utf-8"))
        for col in ("cases", "tasks"):
            for doc in rows.get(col, []):
                doc = dict(doc)
                did = doc.pop("id")
                c.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (col, did, json.dumps(doc, ensure_ascii=False), now()))
        return sum(len(rows.get(k, [])) for k in ("cases", "tasks"))


class Handler(BaseHTTPRequestHandler):
    server_version = "OfficeBoard/1.0"

    # ---------- plumbing ----------
    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers.items() if isinstance(headers, dict) else headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, msg):
        self.send_json({"error": msg}, status)

    def body_bytes(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("too_large")
        return self.rfile.read(n) if n else b""

    def body_json(self):
        if hasattr(self, "_body"):
            return self._body
        raw = self.body_bytes()
        self._body = json.loads(raw.decode() or "{}")
        return self._body

    body_json_cached = body_json

    def unlocked(self):
        """True when the office has no code, or this browser already entered it."""
        if not get_conf("code_hash"):
            return True
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        tok = jar.get("sid")
        if not tok:
            return False
        with db() as c:
            row = c.execute("SELECT created FROM sessions WHERE token=?", (tok.value,)).fetchone()
        return bool(row) and time.time() - row["created"] < SESSION_DAYS * 86400

    def admin(self):
        """Name of the administrator signed in on this browser, or None."""
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        tok = jar.get("adm")
        if not tok:
            return None
        with db() as c:
            row = c.execute("SELECT name, created FROM sessions WHERE token=?", (tok.value,)).fetchone()
        if not row or time.time() - row["created"] > SESSION_DAYS * 86400 or not row["name"].startswith("admin:"):
            return None
        name = row["name"][6:]
        return name if name in get_admins() else None

    def session_user(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        tok = jar.get("usr")
        if not tok:
            return None
        with db() as c:
            row = c.execute("SELECT name, created FROM sessions WHERE token=?", (tok.value,)).fetchone()
        if not row or time.time() - row["created"] > SESSION_DAYS * 86400 or not row["name"].startswith("user:"):
            return None
        return row["name"][5:]

    def who(self):
        return self.session_user() or self.admin() or ""

    def serve_file(self, path, ctype=None, download_name=None):
        if not path.exists() or not path.is_file():
            return self.fail(404, "not_found")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + _q(download_name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- routing ----------
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        url = urlparse(self.path)
        p = unquote(url.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if method == "GET" and p in ("/", "/index.html"):
                return self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            if method == "GET" and p in ("/capture-receive", "/claude-import"):
                return self.serve_file(STATIC / "receive.html", "text/html; charset=utf-8")
            if method == "GET" and p.startswith("/static/"):
                f = (STATIC / p[len("/static/"):]).resolve()
                if STATIC.resolve() not in f.parents:
                    return self.fail(404, "not_found")
                return self.serve_file(f)
            if p == "/api/me" and method == "GET":
                return self.send_json({"setup": not get_conf("configured"), "locked": not self.unlocked()})
            if p == "/api/setup" and method == "POST":
                return self.api_setup()
            if p == "/api/unlock" and method == "POST":
                return self.api_unlock()
            if not get_conf("configured"):
                return self.fail(409, "setup")
            if not self.unlocked():
                return self.fail(401, "locked")
            u = {"name": self.who()}
            if p == "/api/update" and method == "GET":
                try:
                    return self.send_json(check_update())
                except Exception:
                    return self.fail(502, "לא הצלחתי להגיע לשרת העדכונים. בדקו את החיבור לאינטרנט.")
            if p == "/api/update/source" and method == "POST":
                if not self.admin():
                    return self.fail(403, "רק מנהל")
                repo = re.sub(r"^https?://github.com/", "", str(self.body_json().get("repo", "")).strip()).strip("/")
                set_conf("update_repo", repo or None)
                return self.send_json({"ok": True})
            if p == "/api/update/apply" and method == "POST":
                if not self.admin():
                    return self.fail(403, "רק מנהל יכול לעדכן")
                try:
                    v = apply_update()
                except Exception:
                    return self.fail(502, "העדכון נכשל. המערכת הקיימת לא השתנתה. נסו שוב מאוחר יותר.")
                self.send_json({"ok": True, "version": v})
                threading.Timer(1.0, lambda: os._exit(RESTART_CODE)).start()
                return
            if p == "/api/claude" and method == "GET":
                return self.send_json({"connected": claude_status()})
            if p == "/api/claude/connect" and method == "POST":
                if not self.admin():
                    return self.fail(403, "רק מנהל")
                try:
                    return self.send_json({"ok": True, "files": claude_connect()})
                except ValueError:
                    return self.fail(500, "קובץ ההגדרות של אפליקציית Claude פגום. פתחו אותו ותקנו, או מחקו אותו ונסו שוב.")
                except OSError:
                    return self.fail(500, "לא הצלחתי לכתוב את הגדרות אפליקציית Claude במחשב הזה.")
            if p == "/api/version" and method == "GET":
                return self.send_json({"version": local_version()})
            if p == "/api/user" and method == "GET":
                pins = get_pins()
                return self.send_json({"user": self.session_user(), "withCode": sorted(set(pins) | set(get_admins()))})
            if p == "/api/user/login" and method == "POST":
                return self.api_user_login()
            if p == "/api/user/logout" and method == "POST":
                return self.send_json({"ok": True}, headers={"Set-Cookie": "usr=; Path=/; Max-Age=0"})
            if p == "/api/user/pin" and method == "POST":
                if not self.admin():
                    return self.fail(403, "רק מנהל קובע קודים לעובדים")
                b = self.body_json()
                pin = str(b.get("pin", "")).strip()
                if pin and len(pin) < 4:
                    return self.fail(400, "קוד של 4 ספרות לפחות")
                set_pin(str(b.get("name", "")), pin)
                return self.send_json({"ok": True})
            if p == "/api/admin" and method == "GET":
                return self.send_json({"admin": self.admin(), "admins": sorted(get_admins())})
            if p == "/api/admin/login" and method == "POST":
                return self.api_admin_login()
            if p == "/api/admin/logout" and method == "POST":
                return self.send_json({"ok": True}, headers={"Set-Cookie": "adm=; Path=/; Max-Age=0"})
            if p == "/api/admin/set" and method == "POST":
                return self.api_admin_set()
            if p == "/api/admin/remove" and method == "POST":
                return self.api_admin_remove()
            if p == "/api/code" and method == "POST":
                if not self.admin():
                    return self.fail(403, "רק מנהל יכול לשנות את קוד המשרד")
                return self.api_set_code()
            if p == "/api/files" and method == "POST":
                return self.api_file_upload(q, u)
            m = re.fullmatch(r"/files/([0-9a-f]{32})", p)
            if m and method == "GET":
                return self.api_file_get(m.group(1))
            m = re.fullmatch(r"/api/files/([0-9a-f]{32})", p)
            if m and method == "DELETE":
                if not self.admin():
                    return self.fail(403, "רק מנהל יכול למחוק")
                return self.api_file_delete(m.group(1))
            m = re.fullmatch(r"/api/c/([a-z]+)(?:/([^/]+))?", p)
            if m and m.group(1) in COLLECTIONS:
                return self.api_collection(method, m.group(1), m.group(2), q, u)
            return self.fail(404, "not_found")
        except ValueError as e:
            if str(e) == "too_large":
                return self.fail(413, "too_large")
            return self.fail(400, "bad_request")
        except (json.JSONDecodeError, KeyError, TypeError):
            return self.fail(400, "bad_request")

    # ---------- office code (optional) ----------
    def session_cookie(self):
        tok = secrets.token_hex(32)
        with db() as c:
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, "office", time.time()))
        return {"Set-Cookie": f"sid={tok}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}"}

    def api_setup(self):
        b = self.body_json()
        with _lock:
            if get_conf("configured"):
                return self.fail(403, "already_set_up")
            people = [" ".join(str(x).split()) for x in b.get("people", []) if str(x).strip()][:60]
            office = " ".join(str(b.get("officeName", "")).split())[:120]
            with db() as c:
                c.execute("INSERT OR REPLACE INTO kv VALUES('settings','main',?,?)",
                          (json.dumps({"officeName": office, "people": people}, ensure_ascii=False), now()))
            set_code(str(b.get("code", "")).strip())
            adm, apin = " ".join(str(b.get("adminName", "")).split()), str(b.get("adminPin", "")).strip()
            if adm and len(apin) >= 4:
                set_admin(adm, apin)
            with db() as c:
                c.execute("INSERT OR REPLACE INTO kv VALUES('settings','people',?,?)",
                          (json.dumps({"list": people}, ensure_ascii=False), now()))
            set_conf("configured", "1")
        imported = import_seed()
        self.send_json({"ok": True, "imported": imported}, headers=self.session_cookie())

    def api_unlock(self):
        code = str(self.body_json().get("code", "")).strip()
        time.sleep(0.4)  # slows down guessing
        h, salt = get_conf("code_hash"), get_conf("code_salt")
        if h and not hmac.compare_digest(h, hash_pin(code, salt)):
            return self.fail(403, "הקוד לא נכון. נסו שוב, או בקשו את הקוד ממי שהתקין את המערכת.")
        self.send_json({"ok": True}, headers=self.session_cookie())

    def api_user_login(self):
        """An employee picks their name; employees with a personal code must type it."""
        b = self.body_json()
        name, pin = str(b.get("name", "")).strip(), str(b.get("pin", "")).strip()
        rec = get_pins().get(name) or get_admins().get(name)
        if rec:
            time.sleep(0.4)
            if not hmac.compare_digest(rec["hash"], hash_pin(pin, rec["salt"])):
                return self.fail(403, "הקוד האישי לא נכון.")
        tok = secrets.token_hex(32)
        with db() as c:
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, "user:" + name, time.time()))
        self.send_json({"ok": True}, headers={"Set-Cookie": f"usr={tok}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}"})

    def api_admin_login(self):
        b = self.body_json()
        name, pin = str(b.get("name", "")), str(b.get("pin", "")).strip()
        time.sleep(0.4)
        a = get_admins().get(name)
        if not a or not hmac.compare_digest(a["hash"], hash_pin(pin, a["salt"])):
            return self.fail(403, "הקוד האישי לא נכון.")
        tok = secrets.token_hex(32)
        with db() as c:
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, "admin:" + name, time.time()))
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok + "u", "user:" + name, time.time()))
        age = f"HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}"
        self.send_json({"ok": True}, headers=[("Set-Cookie", f"adm={tok}; {age}"), ("Set-Cookie", f"usr={tok}u; {age}")])

    def api_admin_set(self):
        """Add an administrator or change one's code. The first one can be set by anyone."""
        b = self.body_json()
        name, pin = " ".join(str(b.get("name", "")).split()), str(b.get("pin", "")).strip()
        me = self.admin()
        if get_admins() and not me:
            return self.fail(403, "רק מנהל יכול להגדיר מנהלים")
        if not name or len(pin) < 4:
            return self.fail(400, "צריך שם וקוד של 4 ספרות לפחות")
        set_admin(name, pin)
        self.send_json({"ok": True})

    def api_admin_remove(self):
        name = str(self.body_json().get("name", ""))
        if not self.admin():
            return self.fail(403, "רק מנהל יכול להסיר מנהלים")
        a = get_admins()
        if name in a and len(a) == 1:
            return self.fail(400, "חייב להישאר לפחות מנהל אחד")
        a.pop(name, None)
        save_admins(a)
        self.send_json({"ok": True})

    def api_set_code(self):
        set_code(str(self.body_json().get("code", "")).strip())
        self.send_json({"ok": True}, headers=self.session_cookie())

    # ---------- files ----------
    def api_file_upload(self, q, u):
        data = self.body_bytes()
        if not data:
            return self.fail(400, "empty")
        name = q.get("name") or "file"
        ctype = self.headers.get("Content-Type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        fid = uuid.uuid4().hex
        (FILES / fid).write_bytes(data)
        with db() as c:
            c.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (fid, name, ctype, len(data), now(), u["name"]))
        self.send_json({"id": fid, "url": f"/files/{fid}", "name": name, "size": len(data)})

    def api_file_get(self, fid):
        with db() as c:
            row = c.execute("SELECT name, ctype FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            return self.fail(404, "not_found")
        self.serve_file(FILES / fid, row["ctype"], row["name"])

    def api_file_delete(self, fid):
        with db() as c:
            c.execute("DELETE FROM files WHERE id=?", (fid,))
        try:
            (FILES / fid).unlink()
        except FileNotFoundError:
            pass
        self.send_json({"ok": True})

    # ---------- generic document store ----------
    def api_collection(self, method, col, did, q, u):
        did = unquote(did) if did else None
        admin = self.admin()
        me = self.who()
        # employees see and change only their own tasks; the admin sees everything and routes
        if col == "tasks" and not admin:
            if not me:
                if method == "GET" and not did:
                    return self.send_json([])
                return self.fail(403, "בחרו את שמכם כדי לעבוד עם משימות")
            r = None
            if did:
                with db() as c:
                    r = c.execute("SELECT data FROM kv WHERE col='tasks' AND id=?", (did,)).fetchone()
                if r and json.loads(r["data"]).get("assignee", "") != me:
                    return self.fail(403, "המשימה הזו שייכת לעובד אחר")
            if method in ("POST", "PUT") and not r:
                body = self.body_json_cached()
                if body.get("assignee", me) != me:
                    return self.fail(403, "רק מנהל יכול לפתוח משימה לעובד אחר")
                body["assignee"] = me
        if method in ("PUT", "PATCH", "DELETE", "POST") and not admin:
            if col == "settings" and did != "people":
                return self.fail(403, "רק מנהל יכול לשנות הגדרות")
            if method == "DELETE" and col in ("cases", "deals", "docs", "style"):
                return self.fail(403, "רק מנהל יכול למחוק")
            if col == "tasks" and did and method in ("PUT", "PATCH") and "assignee" in self.body_json_cached():
                with db() as c:
                    r = c.execute("SELECT data FROM kv WHERE col='tasks' AND id=?", (did,)).fetchone()
                cur = json.loads(r["data"]).get("assignee", "") if r else ""
                new = self.body_json_cached().get("assignee", "")
                if new != cur and new != self.who():
                    return self.fail(403, "רק מנהל יכול להעביר משימה לעובד אחר")
            if col == "settings" and did == "people":
                with db() as c:
                    r = c.execute("SELECT data FROM kv WHERE col='settings' AND id='people'").fetchone()
                old = set(json.loads(r["data"]).get("list", [])) if r else set()
                new = set(self.body_json_cached().get("list", []))
                if not old <= new:
                    return self.fail(403, "רק מנהל יכול להסיר עובדים")
        with _lock, db() as c:
            if method == "GET" and not did:
                rows = c.execute("SELECT id, data FROM kv WHERE col=?", (col,)).fetchall()
                out = []
                f, v = q.get("f"), q.get("v")
                for r in rows:
                    d = json.loads(r["data"])
                    if f and str(d.get(f, "")) != v:
                        continue
                    if col == "tasks" and not admin and d.get("assignee", "") != me:
                        continue
                    if q.get("lite") and col in ("docs", "style", "inbox"):
                        d.pop("text", None)
                    d["id"] = r["id"]
                    out.append(d)
                return self.send_json(out)
            if method == "GET":
                r = c.execute("SELECT data FROM kv WHERE col=? AND id=?", (col, did)).fetchone()
                return self.send_json({"exists": bool(r), "data": json.loads(r["data"]) if r else None})
            if method == "POST" and not did:
                b = self.body_json()
                did = uuid.uuid4().hex[:20]
                b.setdefault("by", u["name"])
                c.execute("INSERT INTO kv VALUES(?,?,?,?)", (col, did, json.dumps(b, ensure_ascii=False), now()))
                return self.send_json({"id": did})
            if method == "PUT" and did:
                b = self.body_json()
                c.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (col, did, json.dumps(b, ensure_ascii=False), now()))
                return self.send_json({"ok": True})
            if method == "PATCH" and did:
                b = self.body_json()
                r = c.execute("SELECT data FROM kv WHERE col=? AND id=?", (col, did)).fetchone()
                if not r:
                    return self.fail(404, "not_found")
                d = json.loads(r["data"])
                d.update(b)
                c.execute("UPDATE kv SET data=?, updated=? WHERE col=? AND id=?", (json.dumps(d, ensure_ascii=False), now(), col, did))
                return self.send_json({"ok": True})
            if method == "DELETE" and did:
                c.execute("DELETE FROM kv WHERE col=? AND id=?", (col, did))
                return self.send_json({"ok": True})
        return self.fail(405, "method")


def _q(s):
    from urllib.parse import quote
    return quote(s, safe="")


def reset_pin():
    """Removes the office code and the administrators (run on the office computer)."""
    set_code("")
    save_admins({})
    set_conf("pins", "{}")
    print(f"\n  Done. The office code, administrator codes and employee codes were removed. Data folder: {DATA}")
    print("  Open the system, go to Settings, and set the administrator again.\n")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="לוח בקרת תיקים")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--local", action="store_true", help="נגיש רק מהמחשב הזה")
    ap.add_argument("--reset", action="store_true", help="הסרת קוד המשרד")
    a = ap.parse_args()
    init_db()
    if a.reset:
        return reset_pin()
    daily_backup()
    host = "127.0.0.1" if a.local else "0.0.0.0"
    for attempt in range(20):  # after an update the previous process may still hold the port briefly
        try:
            srv = ThreadingHTTPServer((host, a.port), Handler)
            break
        except OSError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    print(f"\n  לוח בקרת תיקים פועל. גרסה {local_version()}")
    print(f"  במחשב הזה:        http://127.0.0.1:{a.port}")
    ip = lan_ip()
    if ip and not a.local:
        print(f"  במחשבים אחרים במשרד: http://{ip}:{a.port}")
    print(f"  Data folder: {DATA}")
    print("  לעצירה: סגרו את החלון הזה.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
