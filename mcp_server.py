#!/usr/bin/env python3
"""מחבר בין אפליקציית Claude לבין לוח בקרת תיקים (MCP על stdio, בלי ספריות חיצוניות).

אפליקציית Claude מפעילה את הקובץ הזה בעצמה, אחרי שלוחצים בהגדרות על
"חיבור לאפליקציית Claude". הוא קורא וכותב ישירות במסד הנתונים של המשרד
(data/office.db), באותו מבנה שהמערכת עצמה משתמשת בו, כך שכל שינוי מופיע
במסכי המערכת תוך כמה שניות.
"""
import json
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "office.db"
BY = "Claude"

KINDS = {"hearing": "דיון", "defense": "כתב הגנה", "appeal": "ערעור", "leave": "בקשת רשות ערעור",
         "setaside": "ביטול פס\"ד", "response": "תגובה לבקשה", "reply": "תשובה לתגובה",
         "affidavits": "תצהירים", "discovery": "הליכים מקדמיים", "submission": "הגשה / הודעה",
         "internal": "משימה פנימית"}
STAGES = {"draft": "טיוטה נשלחה", "signed": "ההסכם נחתם", "cheque": "שיק בנקאי / תשלום",
          "poa": "ייפויי כוח", "caveat": "הערת אזהרה", "tax": "דיווח למיסוי", "taxpaid": "אישורי מס",
          "city": "אישור עירייה", "ila": "אישור רמ\"י / חברה משכנת", "deed": "שטר מכר",
          "tabu": "רישום בטאבו", "keys": "מסירת חזקה"}
CLOSE_REASONS = ["ניתן פסק דין", "פשרה / הסכם", "נמחק", "הוחלף ייצוג", "הועבר לטיפול אחר", "אחר"]

INSTRUCTIONS = """אתה עוזר במשרד עורכי דין ישראלי, ומחובר ל"לוח בקרת תיקים" של המשרד.
- ענה בעברית. תאריכים בכלים הם בפורמט YYYY-MM-DD.
- לפני פעולה שקשה לבטל (סגירת תיק, מחיקה, העברת משימות רבות) תאר בקצרה מה תעשה ובקש אישור.
- כשמבקשים "מה דחוף" או "מה יש היום", התחל בכלי overview.
- כשמזכירים תיק בשם או במספר, אתר אותו עם find_cases או get_case. אם יש כמה התאמות, שאל לאיזה התכוונו.
- לניסוח טיוטה: קרא את החומר עם get_case_documents, נסח בעברית משפטית לפי תקסד"א התשע"ט-2018 והנוהג, אל תמציא עובדות או פסיקה, סמן חסרים ב-[להשלים: ...] ואסמכתאות לא ודאיות ב-[לאמת אסמכתא]. אחר כך הצע לשמור עם save_draft.
- כל שינוי שאתה עושה נרשם ביומן התיק בשם "Claude".
- "הליכים מקדמיים" (דיון מקדמי, גילוי מסמכים, עיון, שאלונים, דיווח, רשימת בקשות): השתמש ב-set_preliminary. כשמסמך בנט המשפט הוא תצהיר גילוי מסמכים, שאלון, מענה לשאלון או דיווח על דיון מקדמי, תייק אותו עם kind "הליכים מקדמיים" וסמן את השלב. החלטה שקובעת מועד אחר להליכים המקדמיים: court_due.
- "סנכרון נט המשפט": המשתמש מחובר לנט המשפט בכרום עם הכרטיס החכם. עבור (עם Claude in Chrome) על ההחלטות והדיונים החדשים, ולכל מסמך חדש (החלטות, פסקי דין, פרוטוקולים, כתבי טענות, בקשות, תגובות, תצהירים): זהה את התיק לפי מספר ההליך, קרא את הטקסט המלא, ותייק עם add_case_document עם הסוג (kind) ומי הגיש (filed_by). מועדים הוסף רק מתוך החלטות ופסקי דין. דיונים מהיומן הוסף עם create_task (kind=hearing). בסוף הצג טבלת סיכום: תיק, מה תויק, אילו מועדים נוספו, ומה לא זוהה. אל תגיש ואל תשנה דבר בנט המשפט עצמו."""


# ---------- data access (same tables the office server uses) ----------
def db():
    if not DB_PATH.exists():
        raise RuntimeError(f"לא נמצא מסד הנתונים של המערכת ({DB_PATH}). ודאו שהמערכת הותקנה והופעלה לפחות פעם אחת.")
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today().isoformat()


def rows(col):
    with db() as c:
        return [{**json.loads(r["data"]), "id": r["id"]} for r in c.execute("SELECT id, data FROM kv WHERE col=?", (col,))]


def get(col, did):
    with db() as c:
        r = c.execute("SELECT data FROM kv WHERE col=? AND id=?", (col, did)).fetchone()
    return {**json.loads(r["data"]), "id": did} if r else None


def put(col, did, data):
    data = {k: v for k, v in data.items() if k != "id"}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (col, did, json.dumps(data, ensure_ascii=False), now()))


def patch(col, did, changes):
    d = get(col, did) or {}
    d.update(changes)
    d["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    put(col, did, d)
    return d


def new_id():
    return uuid.uuid4().hex[:20]


def add_days(s, n):
    return (date.fromisoformat(s) + timedelta(days=n)).isoformat()


def roll_weekend(s):
    d = date.fromisoformat(s)
    while d.weekday() in (4, 5):  # Friday, Saturday
        d += timedelta(days=1)
    return d.isoformat()


def check_date(s, field="תאריך"):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{field} לא תקין: {s}. צריך YYYY-MM-DD.")


# ---------- lookups ----------
def find(col, ref, fields):
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError("לא צוין תיק או עסקה.")
    items = rows(col)
    exact = [x for x in items if x["id"] == ref or any(str(x.get(f, "")).strip() == ref for f in fields)]
    if len(exact) == 1:
        return exact[0]
    part = exact or [x for x in items if any(ref in str(x.get(f, "")) for f in fields)]
    if len(part) == 1:
        return part[0]
    if not part:
        raise ValueError(f"לא נמצא \"{ref}\". נסו שם לקוח, מספר הליך או מספר תיק משרד.")
    options = "; ".join(f"{x.get('client') or x.get('name')} ({x.get('fileNo') or ''} {x.get('number') or x.get('property') or ''}) id={x['id']}" for x in part[:12])
    raise ValueError(f"נמצאו {len(part)} התאמות ל\"{ref}\". ציינו איזו: {options}")


def find_case(ref):
    return find("cases", ref, ["fileNo", "number", "client", "title"])


def find_deal(ref):
    return find("deals", ref, ["fileNo", "name", "property", "otherLawyer"])


def case_brief(c):
    log = c.get("log") or []
    return {"id": c["id"], "client": c.get("client", ""), "fileNo": c.get("fileNo", ""),
            "proceeding": " ".join(x for x in [c.get("procType", ""), c.get("number", "")] if x),
            "parties": c.get("title", ""), "side": c.get("side", ""), "owner": c.get("owner", ""),
            "status": c.get("status", "פעיל"), "action": c.get("action", ""),
            "lastStatus": log[-1]["text"] if log else (c.get("notes") or "")[-300:]}


def task_brief(t):
    who = get("cases", t.get("caseId", "")) if t.get("caseId") else (get("deals", t.get("dealId", "")) if t.get("dealId") else None)
    return {"id": t["id"], "title": t.get("title", ""), "kind": KINDS.get(t.get("kind"), t.get("kind", "")),
            "due": t.get("due", ""), "time": t.get("time", ""), "assignee": t.get("assignee", ""),
            "done": bool(t.get("done")), "case": (who or {}).get("client") or (who or {}).get("name", ""),
            "caseId": t.get("caseId") or t.get("dealId", "")}


def log_to(col, did, text):
    d = get(col, did)
    log = (d.get("log") or []) + [{"date": today(), "text": text, "by": BY}]
    patch(col, did, {"log": log[-300:], "statusAt": today()})


def new_task(data):
    tid = new_id()
    put("tasks", tid, {"done": False, "note": "", "time": "", "assignee": "", "source": "Claude",
                       "createdAt": now(), "updatedAt": now(), **data})
    return tid


# ---------- tools ----------
def t_overview(a):
    T = today()
    who = a.get("assignee") or ""
    open_t = [t for t in rows("tasks") if not t.get("done") and t.get("due") and (not who or t.get("assignee") == who)]
    late = sorted([t for t in open_t if t["due"] < T], key=lambda t: t["due"])
    tod = [t for t in open_t if t["due"] == T]
    week = sorted([t for t in open_t if T < t["due"] <= add_days(T, 7)], key=lambda t: t["due"])
    cases = [c for c in rows("cases") if c.get("status") != "סגור"]
    no_date = [c for c in cases if not any(t.get("caseId") == c["id"] for t in open_t)] if not who else []
    deals = [d for d in rows("deals") if d.get("status") != "הושלם"]
    return {"today": T, "late": [task_brief(t) for t in late], "today_tasks": [task_brief(t) for t in tod],
            "next_7_days": [task_brief(t) for t in week], "open_cases": len(cases),
            "open_cases_without_any_date": [c.get("client") for c in no_date][:40],
            "active_deals": len(deals), "unassigned_open_tasks": sum(1 for t in open_t if not t.get("assignee"))}


def t_find_cases(a):
    q, st, owner = str(a.get("query") or ""), a.get("status") or "open", a.get("owner") or ""
    out = []
    for c in rows("cases"):
        closed = c.get("status") == "סגור"
        if (st == "open" and closed) or (st == "closed" and not closed):
            continue
        if owner and c.get("owner") != owner:
            continue
        if q and q not in " ".join(str(c.get(k, "")) for k in ("client", "fileNo", "number", "title", "court", "judge", "action", "notes")):
            continue
        out.append(case_brief(c))
    return {"count": len(out), "cases": out[:80]}


def t_get_case(a):
    c = find_case(a.get("case"))
    tasks = [task_brief(t) for t in rows("tasks") if t.get("caseId") == c["id"]]
    docs = [{"id": d["id"], "name": d.get("name"), "kind": d.get("kind"), "chars": d.get("chars")} for d in rows("docs") if d.get("caseId") == c["id"]]
    return {**case_brief(c), "court": c.get("court", ""), "judge": c.get("judge", ""),
            "history_from_excel": c.get("notes", ""), "log": (c.get("log") or [])[-40:],
            "closed": {"reason": c.get("closeReason"), "date": c.get("closedAt"), "judgment": c.get("judgmentDate")} if c.get("status") == "סגור" else None,
            "tasks": sorted(tasks, key=lambda t: (t["done"], t["due"])), "documents": docs}


def t_add_case_update(a):
    c = find_case(a.get("case"))
    log = (c.get("log") or []) + [{"date": check_date(a.get("date")) or today(), "text": str(a["text"]).strip(), "by": BY}]
    ch = {"log": log[-300:], "statusAt": today()}
    if a.get("action") is not None:
        ch["action"] = str(a["action"])
    patch("cases", c["id"], ch)
    return {"ok": True, "case": c.get("client"), "added": a["text"]}


def t_update_case(a):
    c = find_case(a.get("case"))
    allowed = {"owner", "action", "court", "judge", "side", "number", "procType", "fileNo", "title", "client"}
    ch = {k: str(v) for k, v in (a.get("fields") or {}).items() if k in allowed}
    if not ch:
        raise ValueError("לא צוינו שדות לעדכון. אפשר: " + ", ".join(sorted(allowed)))
    patch("cases", c["id"], {**ch, "statusAt": today()})
    log_to("cases", c["id"], "עודכנו פרטים: " + ", ".join(f"{k}={v}" for k, v in ch.items()))
    return {"ok": True, "case": c.get("client"), "changed": ch}


def t_create_case(a):
    cid = new_id()
    put("cases", cid, {"client": a["client"], "fileNo": a.get("fileNo", ""), "procType": a.get("procType", ""),
                       "number": a.get("number", ""), "title": a.get("parties", ""), "side": a.get("side", ""),
                       "court": a.get("court", ""), "judge": a.get("judge", ""), "owner": a.get("owner", ""),
                       "action": a.get("action", ""), "status": "פעיל", "notes": "",
                       "log": [{"date": today(), "text": "התיק נפתח", "by": BY}], "statusAt": today(),
                       "createdAt": now(), "updatedAt": now()})
    return {"ok": True, "id": cid}


def t_close_case(a):
    c = find_case(a.get("case"))
    reason = a.get("reason") or "אחר"
    if reason not in CLOSE_REASONS:
        raise ValueError("סיבת סגירה צריכה להיות אחת מ: " + ", ".join(CLOSE_REASONS))
    closed_at = check_date(a.get("date")) or today()
    jd = check_date(a.get("judgment_date"), "תאריך פסק הדין") if reason == "ניתן פסק דין" else ""
    cancelled = 0
    for t in rows("tasks"):
        if t.get("caseId") == c["id"] and not t.get("done"):
            patch("tasks", t["id"], {"done": True, "doneAt": now(), "note": (t.get("note", "") + " · בוטל בסגירת התיק").strip(" ·")})
            cancelled += 1
    log = (c.get("log") or []) + [{"date": closed_at, "text": f"התיק נסגר ({reason}): {a.get('text', '')}".strip(), "by": BY}]
    patch("cases", c["id"], {"status": "סגור", "closeReason": reason, "closedAt": closed_at, "judgmentDate": jd or "",
                             "log": log, "statusAt": today(), "action": ""})
    appeal = None
    if jd and a.get("open_appeal_task", True):
        due = roll_weekend(add_days(jd, 60))
        new_task({"caseId": c["id"], "kind": "appeal", "title": "מועד אחרון לערעור — להחליט עם הלקוח אם מערערים",
                  "due": due, "assignee": c.get("owner", ""), "effort": 0,
                  "note": "60 יום מפסק הדין. לאמת מול מועד ההמצאה ופגרות."})
        appeal = due
    return {"ok": True, "case": c.get("client"), "cancelled_open_tasks": cancelled, "appeal_task_due": appeal}


def t_reopen_case(a):
    c = find_case(a.get("case"))
    patch("cases", c["id"], {"status": "פעיל", "closeReason": "", "closedAt": ""})
    log_to("cases", c["id"], "התיק נפתח מחדש")
    return {"ok": True}


def t_list_tasks(a):
    T, scope, who = today(), a.get("scope") or "open", a.get("assignee") or ""
    out = []
    for t in rows("tasks"):
        if not t.get("due") or (who and t.get("assignee") != who):
            continue
        if scope == "done":
            if not t.get("done"):
                continue
        else:
            if t.get("done"):
                continue
            n = (date.fromisoformat(t["due"]) - date.fromisoformat(T)).days
            if (scope == "late" and n >= 0) or (scope == "today" and n != 0) or (scope == "week" and not 0 <= n <= 7) or (scope == "month" and not 0 <= n <= 31):
                continue
            if scope == "unassigned" and t.get("assignee"):
                continue
        out.append(task_brief(t))
    out.sort(key=lambda t: t["due"])
    return {"count": len(out), "tasks": out[:120]}


def t_create_task(a):
    target = a.get("case") or a.get("deal")
    link = {}
    if a.get("deal"):
        d = find_deal(a["deal"])
        link = {"dealId": d["id"], "caseId": ""}
        owner = d.get("owner", "")
    else:
        c = find_case(target)
        link = {"caseId": c["id"]}
        owner = c.get("owner", "")
    kind = a.get("kind") or "submission"
    if kind not in KINDS:
        raise ValueError("סוג משימה לא מוכר. אפשר: " + ", ".join(f"{k} ({v})" for k, v in KINDS.items()))
    due = check_date(a.get("due"), "מועד")
    if not due:
        raise ValueError("חסר מועד (due).")
    data = {**link, "kind": kind, "title": a.get("title") or KINDS[kind], "due": due, "time": a.get("time", ""),
            "assignee": a.get("assignee") or owner, "note": a.get("note", "")}
    if a.get("effort") is not None:
        data["effort"] = int(a["effort"])
    if data["assignee"]:
        data.update({"assignedBy": BY, "assignedAt": now(), "seen": False})
    tid = new_task(data)
    col, oid = ("deals", link["dealId"]) if "dealId" in link else ("cases", link["caseId"])
    log_to(col, oid, f"נוספה משימה: {data['title']} עד {due}")
    return {"ok": True, "task_id": tid, "due": due, "assignee": data["assignee"]}


def t_update_task(a):
    t = get("tasks", str(a.get("task_id") or ""))
    if not t:
        raise ValueError("משימה לא נמצאה. השתמשו ב-task_id מתוך list_tasks או get_case.")
    ch = {}
    if "done" in a:
        ch["done"] = bool(a["done"])
        ch["doneAt"] = now() if a["done"] else ""
    if a.get("due"):
        ch["due"] = check_date(a["due"], "מועד")
    for k in ("title", "note", "time"):
        if k in a:
            ch[k] = str(a[k])
    if "assignee" in a:
        ch.update({"assignee": str(a["assignee"]), "assignedBy": BY, "assignedAt": now(), "seen": False})
    if not ch:
        raise ValueError("לא צוין מה לשנות.")
    patch("tasks", t["id"], ch)
    col, oid = ("cases", t.get("caseId")) if t.get("caseId") else ("deals", t.get("dealId"))
    if oid and get(col, oid):
        desc = "בוצע: " + t.get("title", "") if ch.get("done") else f"עודכנה משימה \"{t.get('title', '')}\": " + ", ".join(f"{k}={v}" for k, v in ch.items() if k in ("due", "assignee", "title"))
        log_to(col, oid, desc)
    return {"ok": True, "changed": {k: v for k, v in ch.items() if k not in ("doneAt", "assignedAt", "assignedBy", "seen")}}


def deal_brief(d):
    st = d.get("stages") or {}
    rel = [k for k in STAGES if (st.get(k) or {}).get("s") != "na"]
    done = [k for k in rel if (st.get(k) or {}).get("s") == "done"]
    nxt = next((STAGES[k] for k in rel if k not in done), None)
    return {"id": d["id"], "name": d.get("name"), "fileNo": d.get("fileNo", ""), "type": d.get("dealType", ""),
            "property": d.get("property", ""), "ourSide": d.get("ourSide", ""), "otherLawyer": d.get("otherLawyer", ""),
            "contact": d.get("otherContact", ""), "owner": d.get("owner", ""), "status": d.get("status", "פעיל"),
            "progress": f"{len(done)}/{len(rel)}", "next_stage": nxt, "action": d.get("action", "")}


def t_find_deals(a):
    q, st = str(a.get("query") or ""), a.get("status") or "active"
    out = []
    for d in rows("deals"):
        fin = d.get("status") == "הושלם"
        if (st == "active" and fin) or (st == "done" and not fin):
            continue
        if q and q not in " ".join(str(d.get(k, "")) for k in ("name", "fileNo", "property", "otherLawyer", "ourSide", "notes", "action")):
            continue
        out.append(deal_brief(d))
    return {"count": len(out), "deals": out[:80]}


def t_get_deal(a):
    d = find_deal(a.get("deal"))
    st = d.get("stages") or {}
    return {**deal_brief(d), "price": d.get("price", ""), "notes": d.get("notes", ""),
            "stages": {STAGES[k]: ((st.get(k) or {}).get("s") or "todo") + (f" {st[k].get('d')}" if (st.get(k) or {}).get("d") else "") for k in STAGES},
            "log": (d.get("log") or [])[-30:],
            "tasks": [task_brief(t) for t in rows("tasks") if t.get("dealId") == d["id"]]}


def t_set_deal_stage(a):
    d = find_deal(a.get("deal"))
    key = a.get("stage")
    if key not in STAGES:
        key = next((k for k, v in STAGES.items() if str(a.get("stage", "")) in v), None)
    if not key:
        raise ValueError("שלב לא מוכר. אפשר: " + ", ".join(f"{k} ({v})" for k, v in STAGES.items()))
    state = a.get("state") or "done"
    when = check_date(a.get("date")) or today()
    val = {"s": "done", "d": when} if state == "done" else ({"s": "na"} if state == "na" else {})
    stages = {**(d.get("stages") or {}), key: val}
    was = ((d.get("stages") or {}).get(key) or {}).get("s")
    patch("deals", d["id"], {"stages": stages, "statusAt": today()})
    log_to("deals", d["id"], ("בוצע: " if state == "done" else "סומן כלא רלוונטי: " if state == "na" else "בוטל הסימון: ") + STAGES[key])
    tax_task = None
    if key == "signed" and state == "done" and was != "done" and ((stages.get("tax") or {}).get("s") != "done"):
        if not any(t.get("dealId") == d["id"] and not t.get("done") and "מיסוי" in t.get("title", "") for t in rows("tasks")):
            tax_task = roll_weekend(add_days(when, 30))
            new_task({"dealId": d["id"], "caseId": "", "kind": "submission", "title": "דיווח לרשות המסים (מיסוי מקרקעין)",
                      "due": tax_task, "assignee": d.get("owner", ""), "effort": 2,
                      "note": "הצהרה למיסוי מקרקעין תוך 30 יום מיום החתימה. לאמת את המועד."})
    return {"ok": True, "deal": d.get("name"), "stage": STAGES[key], "state": state, "tax_report_task_due": tax_task}


def t_add_deal_update(a):
    d = find_deal(a.get("deal"))
    log_to("deals", d["id"], str(a["text"]).strip())
    if a.get("action") is not None:
        patch("deals", d["id"], {"action": str(a["action"])})
    return {"ok": True}


def t_create_deal(a):
    did = new_id()
    put("deals", did, {"name": a["name"], "fileNo": a.get("fileNo", ""), "dealType": a.get("type", "מכר"),
                       "property": a.get("property", ""), "ourSide": a.get("ourSide", ""), "otherLawyer": a.get("otherLawyer", ""),
                       "otherContact": a.get("contact", ""), "owner": a.get("owner", ""), "price": a.get("price", ""),
                       "action": a.get("action", ""), "notes": a.get("notes", ""), "status": "פעיל", "stages": {},
                       "log": [{"date": today(), "text": "העסקה נפתחה", "by": BY}], "statusAt": today(),
                       "createdAt": now(), "updatedAt": now()})
    return {"ok": True, "id": did}


def t_get_case_documents(a):
    ref = a.get("case") or a.get("deal")
    try:
        target = find_case(ref) if a.get("case") else find_deal(ref)
    except ValueError:
        target = find_deal(ref) if a.get("case") else find_case(ref)
    budget = int(a.get("max_chars") or 80000)
    docs = sorted([d for d in rows("docs") if d.get("caseId") == target["id"]], key=lambda d: d.get("docDate") or d.get("uploadedAt") or "", reverse=True)
    out = []
    for d in docs:
        if budget <= 500:
            out.append({"name": d.get("name"), "kind": d.get("kind"), "text": "[לא נכלל — חרגנו מהאורך. בקשו max_chars גדול יותר]"})
            continue
        txt = (d.get("text") or "")[:budget]
        budget -= len(txt)
        out.append({"name": d.get("name"), "kind": d.get("kind"), "date": d.get("docDate", ""), "text": txt})
    styles = [{"type": s.get("docType"), "name": s.get("name"), "text": (s.get("text") or "")[:12000]} for s in rows("style")][:3]
    settings = get("settings", "main") or {}
    return {"target": target.get("client") or target.get("name"), "details": case_brief(target) if a.get("case") else deal_brief(target),
            "documents": out, "office_style_examples": styles, "office_signature": settings.get("signature", "")}


def t_save_draft(a):
    ref = a.get("case") or a.get("deal")
    target = find_case(ref) if a.get("case") else find_deal(ref)
    key = a.get("task_id") or ("case-" + target["id"])
    put("drafts", key, {"caseId": target["id"], "taskId": a.get("task_id", ""), "doc": a.get("doc_type", "טיוטה"),
                        "facts": "", "text": str(a["text"])[:100000], "savedAt": now(), "by": BY})
    log_to("cases" if a.get("case") else "deals", target["id"], f"נשמרה טיוטה: {a.get('doc_type', 'טיוטה')}")
    return {"ok": True, "message": "הטיוטה נשמרה. אפשר לפתוח אותה במערכת דרך כפתור ✎ טיוטה."}


def t_add_case_document(a):
    """Files a decision or other court document in a case, with its deadlines, skipping documents already filed."""
    c = find_case(a.get("case"))
    name = str(a.get("name") or a.get("kind") or "החלטה").strip()[:120]
    if a.get("filed_by") and a["filed_by"] not in name:
        name = f"{name} — {a['filed_by']}"
    text = str(a.get("text") or "")
    given = check_date(a.get("date"), "תאריך המסמך") or today()
    kind = a.get("kind") or "החלטה"
    for d in rows("docs"):
        if d.get("caseId") == c["id"] and (d.get("name") == name and d.get("docDate") == given or (text and d.get("text", "")[:300] == text[:300])):
            return {"ok": True, "duplicate": True, "message": f"המסמך כבר קיים בתיק {c.get('client')}"}
    did = new_id()
    put("docs", did, {"caseId": c["id"], "name": name, "kind": kind, "docDate": given, "text": text[:100000],
                      "truncated": len(text) > 100000, "chars": len(text), "pages": 0, "source": "נט המשפט", "filedBy": a.get("filed_by", ""),
                      "href": a.get("url", ""), "uploadedAt": now(), "by": BY})
    added = []
    for dl in a.get("deadlines") or []:
        due = check_date(dl.get("due"), "מועד")
        k = dl.get("kind") if dl.get("kind") in KINDS else "submission"
        if not due or any(t.get("caseId") == c["id"] and not t.get("done") and t.get("due") == due and t.get("kind") == k for t in rows("tasks")):
            continue
        new_task({"caseId": c["id"], "kind": k, "title": dl.get("title") or KINDS[k], "due": due, "time": dl.get("time", ""),
                  "assignee": c.get("owner", ""), "source": "נט המשפט", "verify": True,
                  "note": f"מתוך {kind} מיום {given}. {dl.get('how', '')} — לאמת".strip()})
        added.append(due)
    log_to("cases", c["id"], f"התקבל מנט המשפט: {name} ({given})" + (f", נוספו {len(added)} מועדים" if added else ""))
    return {"ok": True, "case": c.get("client"), "document_id": did, "deadlines_added": added}


def t_list_people(a):
    p = get("settings", "people") or {}
    return {"people": p.get("list") or (get("settings", "main") or {}).get("people", [])}


# ---------- preliminary procedures (same rules as the "הליכים מקדמיים" screen) ----------
PRE = [("meet", "דיון מקדמי בין הצדדים", "last", 30), ("disc", "תצהיר גילוי מסמכים", "last", 30),
       ("qSend", "שליחת שאלון לצד השני", "last", 30), ("qAns", "מענה בתצהיר לשאלון שקיבלנו", "qIn", 30),
       ("insp", "עיון במסמכים", "discX", 30), ("report", "דיווח לבית המשפט על הדיון המקדמי", "pre", -20),
       ("reqs", "רשימת בקשות (כולל בקשות בעניין הליכים מקדמיים)", "pre", -20)]


def in_recess(s):
    return s[5:7] == "08" or (s[5:7] == "07" and int(s[8:10]) >= 16)


def pre_due(pre, key, base_key, days):
    step = (pre.get("steps") or {}).get(key) or {}
    if step.get("due"):
        return step["due"]
    base = pre.get(base_key) or ""
    if base_key == "discX" and not base and ((pre.get("steps") or {}).get("disc") or {}).get("s") == "done":
        base = pre["steps"]["disc"].get("d", "")
    if not base:
        return ""
    d = base
    if days > 0:
        n = 0
        while n < days:
            d = add_days(d, 1)
            if not in_recess(d):
                n += 1
        return roll_weekend(d)
    d = add_days(d, days)
    while datetime.strptime(d, "%Y-%m-%d").weekday() in (4, 5):
        d = add_days(d, -1)
    return d


def pre_sync(c):
    pre = c.get("pre") or {}
    tasks = [t for t in rows("tasks") if t.get("caseId") == c["id"] and t.get("preStep")]
    ids = dict(pre.get("tasks") or {})
    out = []
    for key, label, base_key, days in PRE:
        st = ((pre.get("steps") or {}).get(key) or {}).get("s", "")
        due = pre_due(pre, key, base_key, days)
        t = next((t for t in tasks if t.get("preStep") == key), None)
        if st in ("", "other") and due and not pre.get("off"):
            if not t:
                ids[key] = new_task({"caseId": c["id"], "preStep": key, "kind": "discovery", "title": label, "due": due,
                          "assignee": c.get("owner", ""), "effort": 1, "source": "הליכים מקדמיים", "verify": False})
            elif t.get("due") != due or t.get("done"):
                patch("tasks", t["id"], {"due": due, "done": False, "updatedAt": now()})
            out.append({"step": label, "due": due})
        elif t and not t.get("done"):
            patch("tasks", t["id"], {"done": True, "doneAt": today(), "updatedAt": now()})
        if t:
            ids[key] = t["id"]
    patch("cases", c["id"], {"pre": {**pre, "tasks": ids}})
    return out


def t_set_preliminary(a):
    c = find_case(a.get("case"))
    pre = dict(c.get("pre") or {})
    for arg, key in (("last_pleading", "last"), ("pretrial", "pre"), ("questionnaire_received", "qIn"), ("discovery_exchanged", "discX")):
        if a.get(arg):
            pre[key] = check_date(a[arg])
    if "not_applicable" in a:
        pre["off"] = bool(a["not_applicable"])
        pre["offWhy"] = str(a.get("reason", "")) if pre["off"] else ""
    if a.get("step"):
        keys = {k: l for k, l, _, _ in PRE}
        if a["step"] not in keys:
            raise ValueError("שלב לא מוכר. אחד מ: " + ", ".join(keys))
        state = {"done": "done", "na": "na", "other": "other", "todo": ""}[a.get("state", "done")]
        steps = dict(pre.get("steps") or {})
        old = steps.get(a["step"]) or {}
        steps[a["step"]] = {**old, "s": state, "d": check_date(a["date"]) if a.get("date") else (today() if state == "done" else ""),
                            "due": check_date(a["court_due"]) if a.get("court_due") else old.get("due", ""),
                            "note": str(a.get("note", old.get("note", "")))}
        pre["steps"] = steps
        log_to("cases", c["id"], f"הליכים מקדמיים: {keys[a['step']]} — " + {"done": "בוצע", "na": "לא נדרש", "other": "אחר", "": "טרם"}[state])
    patch("cases", c["id"], {"pre": pre, "updatedAt": now()})
    open_steps = pre_sync({**c, "pre": pre})
    return {"ok": True, "case": c.get("client"), "open_steps": open_steps, "not_applicable": bool(pre.get("off"))}


S_CASE = {"type": "string", "description": "שם הלקוח, מספר הליך (למשל 12345-01-26) או מספר תיק משרד"}
S_DATE = {"type": "string", "description": "YYYY-MM-DD"}
TOOLS = [
    ("overview", "מה דחוף: משימות באיחור, להיום ול-7 הימים הקרובים, תיקים בלי מועד.", t_overview,
     {"assignee": {"type": "string", "description": "רק המשימות של עובד מסוים (לא חובה)"}}, []),
    ("find_cases", "חיפוש תיקי בית משפט (פתוחים כברירת מחדל) עם סטטוס אחרון ופעולה לביצוע.", t_find_cases,
     {"query": {"type": "string"}, "status": {"type": "string", "enum": ["open", "closed", "all"]}, "owner": {"type": "string"}}, []),
    ("get_case", "כל הפרטים על תיק: הליך, צדדים, יומן סטטוסים, משימות ומסמכים.", t_get_case, {"case": S_CASE}, ["case"]),
    ("add_case_update", "הוספת עדכון סטטוס ליומן התיק (מה קרה), ואופציונלית עדכון 'פעולה לביצוע'.", t_add_case_update,
     {"case": S_CASE, "text": {"type": "string"}, "date": S_DATE, "action": {"type": "string"}}, ["case", "text"]),
    ("update_case", "עדכון פרטי תיק: owner (אחראי), action, court, judge, side, number, procType, fileNo, title, client.", t_update_case,
     {"case": S_CASE, "fields": {"type": "object"}}, ["case", "fields"]),
    ("create_case", "פתיחת תיק חדש.", t_create_case,
     {"client": {"type": "string"}, "fileNo": {"type": "string"}, "procType": {"type": "string"}, "number": {"type": "string"},
      "parties": {"type": "string"}, "side": {"type": "string"}, "court": {"type": "string"}, "judge": {"type": "string"},
      "owner": {"type": "string"}, "action": {"type": "string"}}, ["client"]),
    ("close_case", "סגירת תיק (עובר לתיקים סגורים). בפסק דין נפתחת משימת ערעור ל-60 יום. בקשו אישור לפני.", t_close_case,
     {"case": S_CASE, "reason": {"type": "string", "enum": CLOSE_REASONS}, "text": {"type": "string"}, "date": S_DATE,
      "judgment_date": S_DATE, "open_appeal_task": {"type": "boolean"}}, ["case", "reason"]),
    ("reopen_case", "פתיחה מחדש של תיק סגור.", t_reopen_case, {"case": S_CASE}, ["case"]),
    ("list_tasks", "רשימת משימות לפי טווח: late, today, week, month, open, unassigned, done.", t_list_tasks,
     {"scope": {"type": "string", "enum": ["late", "today", "week", "month", "open", "unassigned", "done"]}, "assignee": {"type": "string"}}, []),
    ("create_task", "יצירת משימה או מועד בתיק.", t_create_task,
     {"case": S_CASE, "title": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
      "due": S_DATE, "time": {"type": "string", "description": "HH:MM, לדיונים"}, "assignee": {"type": "string"},
      "note": {"type": "string"}, "effort": {"type": "integer", "description": "ימי עבודה לפני המועד"}}, ["due"]),
    ("update_task", "עדכון משימה: done=true לסימון כבוצע, due, assignee (העברה לעובד), title, note, time.", t_update_task,
     {"task_id": {"type": "string"}, "done": {"type": "boolean"}, "due": S_DATE, "assignee": {"type": "string"},
      "title": {"type": "string"}, "note": {"type": "string"}, "time": {"type": "string"}}, ["task_id"]),
    ("set_preliminary", "הליכים מקדמיים בתיק (תקסד\"א התשע\"ט-2018): קביעת מועד כתב הטענות האחרון וקדם המשפט, סימון שלב כבוצע / לא נדרש, "
     "מועד שנקבע בהחלטה, או סימון שההליכים לא חלים בתיק. משימות המועדים נפתחות ומתעדכנות לבד. שלבים: "
     + ", ".join(f"{k}={l}" for k, l, _, _ in PRE), t_set_preliminary,
     {"case": S_CASE, "last_pleading": S_DATE, "pretrial": S_DATE, "questionnaire_received": S_DATE, "discovery_exchanged": S_DATE,
      "not_applicable": {"type": "boolean"}, "reason": {"type": "string"},
      "step": {"type": "string", "enum": [k for k, _, _, _ in PRE]}, "state": {"type": "string", "enum": ["done", "na", "other", "todo"]},
      "date": S_DATE, "court_due": S_DATE, "note": {"type": "string"}}, ["case"]),
    ("get_case_documents", "הטקסט של מסמכי התיק (החלטות, כתבי טענות), דוגמאות לסגנון המשרד ופרטי החתימה — לניסוח טיוטות.", t_get_case_documents,
     {"case": S_CASE, "max_chars": {"type": "integer"}}, ["case"]),
    ("save_draft", "שמירת טיוטה בתיק, כדי שתופיע במערכת.", t_save_draft,
     {"case": S_CASE, "doc_type": {"type": "string"}, "text": {"type": "string"}, "task_id": {"type": "string"}}, ["text"]),
    ("add_case_document", "תיוק מסמך מנט המשפט בתיק (החלטה, פסק דין, פרוטוקול, כתב טענות, בקשה, תגובה, תצהיר): הטקסט המלא נשמר במסמכי התיק, ומועדים שבהחלטה נוספים כמשימות מסומנות 'לאמת'. "
     "חשב מועדים: 'תוך X ימים' = X ימים קלנדריים מיום ההחלטה, בלי ימי פגרת קיץ (16.7–31.8), ומועד שנופל בשישי/שבת עובר לראשון. מסמך שכבר תויק לא ישוכפל.", t_add_case_document,
     {"case": S_CASE, "name": {"type": "string", "description": "למשל: החלטה מיום 20.10.2026"}, "kind": {"type": "string", "enum": ["החלטה", "פסק דין", "פרוטוקול", "כתב תביעה", "כתב הגנה", "כתב תשובה", "בקשה", "תגובה", "תשובה לתגובה", "תצהיר", "חוות דעת", "הודעה", "זימון לדיון", "אחר"]},
      "filed_by": {"type": "string", "description": "מי הגיש: הנתבעים / התובעת / בית המשפט"},
      "date": S_DATE, "text": {"type": "string", "description": "הטקסט המלא של המסמך"}, "url": {"type": "string"},
      "deadlines": {"type": "array", "items": {"type": "object", "properties": {"due": S_DATE, "kind": {"type": "string", "enum": list(KINDS)},
                                                                             "title": {"type": "string"}, "time": {"type": "string"}, "how": {"type": "string"}}}}},
     ["case", "text"]),
    ("list_people", "שמות העובדים במשרד (לשיוך משימות).", t_list_people, {}, []),
]
BY_NAME = {t[0]: t for t in TOOLS}


# ---------- MCP over stdio (newline-delimited JSON-RPC 2.0) ----------
def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    m, rid, p = req.get("method"), req.get("id"), req.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "lawboard", "version": "1.0"}, "instructions": INSTRUCTIONS}
    if m == "ping":
        return {}
    if m == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": props, "required": req_}}
                          for n, d, _, props, req_ in TOOLS]}
    if m == "tools/call":
        tool = BY_NAME.get(p.get("name"))
        if not tool:
            return {"content": [{"type": "text", "text": "כלי לא מוכר"}], "isError": True}
        try:
            res = tool[2](p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=1)}]}
        except (ValueError, KeyError, RuntimeError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    if rid is None:
        return None  # notification
    raise LookupError(m)


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            res = handle(req)
            if req.get("id") is not None and res is not None:
                send({"jsonrpc": "2.0", "id": req["id"], "result": res})
        except LookupError:
            if req.get("id") is not None:
                send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "method not found"}})
        except Exception as e:  # keep the connection alive whatever happens
            if req.get("id") is not None:
                send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
