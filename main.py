from fastapi import FastAPI, HTTPException, Body, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime, timedelta, timezone, date
from passlib.context import CryptContext
import json, os, uuid, time, shutil, threading
import jwt  # PyJWT
from fastapi import Query


# =========================================
#                  CONFIG
# =========================================

ROOT_DIR = os.path.dirname(__file__)
DB_PATH  = os.environ.get("DB_PATH", os.path.join(ROOT_DIR, "db.json"))

AUTH_SECRET  = os.environ.get("AUTH_SECRET", "dev-secret-change-me")
JWT_ALGO     = "HS256"
JWT_TTL_DAYS = 7

pwd_ctx     = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")
auth_scheme = HTTPBearer()

# =========================================
#               DB HELPERS
# =========================================

_DB_LOCK = threading.Lock()

def _atomic_write(path: str, payload: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _empty_db() -> dict:
    return {"lines": [], "users": [], "accounts": [], "bookings": [], "recurring_rules": [], "version": 1}

def ensure_db_shapes(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("lines", [])
    data.setdefault("users", [])
    data.setdefault("accounts", [])
    data.setdefault("bookings", [])
    data.setdefault("recurring_rules", [])
    data.setdefault("version", 1)
    return data

def read_db() -> dict:
    with _DB_LOCK:
        if not os.path.exists(DB_PATH):
            data = _empty_db()
            _atomic_write(DB_PATH, data)
            return data
        with open(DB_PATH, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                # Fallback: kaputte Datei sichern und neu starten
                bak = f"{DB_PATH}.{int(time.time())}.corrupt.bak"
                shutil.copyfile(DB_PATH, bak)
                data = _empty_db()
                _atomic_write(DB_PATH, data)
            return ensure_db_shapes(data)

def write_db(data: dict):
    with _DB_LOCK:
        _atomic_write(DB_PATH, ensure_db_shapes(data))

def total_of_line(l: dict) -> float:
    subs = l.get("subitems") or []
    if isinstance(subs, list) and subs:
        return sum(float(x.get("amount") or 0) for x in subs)
    return float(l.get("base_amount") or 0)

def normalize_line(raw: dict) -> dict:
    l = dict(raw) if raw else {}
    # Pflichtfelder
    l["id"] = l.get("id") or str(uuid.uuid4())
    t = l.get("type")
    l["type"] = t if t in ("income", "expense") else "expense"
    if not l.get("category"):
        l["category"] = "sonstige ausgaben" if l["type"] == "expense" else "sonstige einnahmen"
    # amount -> base_amount Migration
    if "base_amount" not in l or l["base_amount"] is None:
        if isinstance(l.get("amount"), (int, float)):
            l["base_amount"] = float(l["amount"])
        else:
            l["base_amount"] = float(l.get("base_amount") or 0.0)
    # Subitems normalisieren
    fixed = []
    for si in (l.get("subitems") or []):
        s = dict(si) if isinstance(si, dict) else {}
        s["id"] = s.get("id") or str(uuid.uuid4())
        s["label"] = s.get("label") or ""
        try:
            s["amount"] = float(s.get("amount") or 0)
        except (TypeError, ValueError):
            s["amount"] = 0.0
        fixed.append(s)
    l["subitems"] = fixed
    # is_variable säubern
    v = l.get("is_variable", None)
    l["is_variable"] = v if v in (True, False, None) else None
    return l

def normalize_account(raw: dict, user_id: Optional[str] = None) -> dict:
    a = dict(raw) if raw else {}
    a["id"] = a.get("id") or str(uuid.uuid4())
    a["user_id"] = a.get("user_id") or user_id
    a["name"] = (a.get("name") or "").strip()
    a["institute"] = a.get("institute") or ""
    a["icon"] = a.get("icon") or "🏦"
    try:
        a["sort_order"] = float(a.get("sort_order") or 0)
    except (TypeError, ValueError):
        a["sort_order"] = 0.0
    a["archived"] = bool(a.get("archived", False))
    return a

def normalize_booking(raw: dict, account_id: Optional[str] = None) -> dict:
    b = dict(raw) if raw else {}
    b["id"] = b.get("id") or str(uuid.uuid4())
    b["account_id"] = b.get("account_id") or account_id
    d = b.get("date")
    try:
        date.fromisoformat(d)
    except (TypeError, ValueError):
        d = date.today().isoformat()
    b["date"] = d
    try:
        b["amount"] = float(b.get("amount") or 0)
    except (TypeError, ValueError):
        b["amount"] = 0.0
    b["note"] = b.get("note") or ""
    b["source"] = b.get("source") if b.get("source") in ("manual", "recurring") else "manual"
    b["recurring_rule_id"] = b.get("recurring_rule_id")
    return b

def normalize_recurring_rule(raw: dict, account_id: Optional[str] = None) -> dict:
    r = dict(raw) if raw else {}
    r["id"] = r.get("id") or str(uuid.uuid4())
    r["account_id"] = r.get("account_id") or account_id
    r["linked_line_id"] = r.get("linked_line_id")
    try:
        dom = int(r.get("day_of_month") or 1)
    except (TypeError, ValueError):
        dom = 1
    r["day_of_month"] = min(max(dom, 1), 28)
    r["active"] = bool(r.get("active", True))
    sd = r.get("start_date")
    try:
        date.fromisoformat(sd)
    except (TypeError, ValueError):
        sd = date.today().isoformat()
    r["start_date"] = sd
    ed = r.get("end_date")
    if ed:
        try:
            date.fromisoformat(ed)
        except (TypeError, ValueError):
            ed = None
    r["end_date"] = ed
    r["rule_type"] = r.get("rule_type") if r.get("rule_type") in ("Dauerauftrag", "Lastschrift") else "Dauerauftrag"
    return r

def migrate_db(data: dict) -> dict:
    data = ensure_db_shapes(data)
    new_lines = [normalize_line(l) for l in data.get("lines", [])]
    new_accounts = [normalize_account(a) for a in data.get("accounts", [])]
    new_bookings = [normalize_booking(b) for b in data.get("bookings", [])]
    new_rules = [normalize_recurring_rule(r) for r in data.get("recurring_rules", [])]
    changed = (
        new_lines != data.get("lines")
        or new_accounts != data.get("accounts")
        or new_bookings != data.get("bookings")
        or new_rules != data.get("recurring_rules")
        or "version" not in data
    )
    if changed:
        data["lines"] = new_lines
        data["accounts"] = new_accounts
        data["bookings"] = new_bookings
        data["recurring_rules"] = new_rules
        data["version"] = 1
        write_db(data)
    return data

def account_deposit_amount(line: dict) -> float:
    return abs(total_of_line(normalize_line(line)))

def run_recurring_catchup(user_id: str, data: dict) -> bool:
    """Erzeugt fehlende automatische Buchungen für aktive Daueraufträge des Users.
    Mutiert `data` in place. Gibt True zurück, wenn etwas geändert wurde."""
    today = date.today()
    accounts_by_id = {a["id"]: a for a in data["accounts"] if a.get("user_id") == user_id}
    lines_by_id = {l["id"]: l for l in data["lines"] if l.get("user_id") == user_id}
    changed = False

    for idx, raw_rule in enumerate(data["recurring_rules"]):
        if raw_rule.get("account_id") not in accounts_by_id:
            continue
        rule = normalize_recurring_rule(raw_rule)
        data["recurring_rules"][idx] = rule
        if rule != raw_rule:
            changed = True
        if not rule["active"]:
            continue

        line = lines_by_id.get(rule["linked_line_id"])
        if not line:
            rule["active"] = False
            changed = True
            continue

        try:
            start = date.fromisoformat(rule["start_date"])
        except ValueError:
            continue
        end = None
        if rule.get("end_date"):
            try:
                end = date.fromisoformat(rule["end_date"])
            except ValueError:
                end = None

        existing_months = set()
        for b in data["bookings"]:
            if b.get("recurring_rule_id") == rule["id"]:
                try:
                    bd = date.fromisoformat(b.get("date"))
                    existing_months.add((bd.year, bd.month))
                except (ValueError, TypeError):
                    pass

        y, m = start.year, start.month
        while (y, m) <= (today.year, today.month):
            occurrence = date(y, m, rule["day_of_month"])
            in_range = occurrence >= start and occurrence <= today and (end is None or occurrence <= end)
            if in_range and (y, m) not in existing_months:
                booking = normalize_booking({
                    "date": occurrence.isoformat(),
                    "amount": account_deposit_amount(line),
                    "note": line.get("label") or "",
                    "source": "recurring",
                    "recurring_rule_id": rule["id"],
                }, account_id=rule["account_id"])
                data["bookings"].append(booking)
                changed = True
            m += 1
            if m > 12:
                y, m = y + 1, 1

    return changed

# =========================================
#                  AUTH
# =========================================

def hash_pw(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_pw(pw: str, hashed: str) -> bool:
    try:
        return pwd_ctx.verify(pw, hashed)
    except Exception:
        return False

def create_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["id"],
        "name": user["username"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, AUTH_SECRET, algorithm=JWT_ALGO)

def decode_token(token: str) -> dict:
    return jwt.decode(token, AUTH_SECRET, algorithms=[JWT_ALGO])

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(auth_scheme)) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Not authenticated")
    try:
        payload = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    data = read_db()
    u = next((u for u in data["users"] if u.get("id") == payload.get("sub")), None)
    if not u:
        raise HTTPException(401, "User not found")
    return u

class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)

class UserOut(BaseModel):
    id: str
    username: str
    email: str = ""

class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"

def migrate_add_default_user():
    data = read_db()
    if not data["users"]:
        uid = str(uuid.uuid4())
        user = {
            "id": uid,
            "username": "thom7e",
            "password_hash": hash_pw("1lKaHuber#"),
            "created_at": int(time.time()),
        }
        data["users"].append(user)
        # alle existierenden lines anhängen
        for l in data["lines"]:
            if not l.get("user_id"):
                l["user_id"] = uid
        write_db(data)

def migrate_attach_orphans():
    data = read_db()
    if not data["users"]:
        return
    pref = data["users"][0]
    changed = False
    for l in data["lines"]:
        if not l.get("user_id"):
            l["user_id"] = pref["id"]
            changed = True
    if changed:
        write_db(data)

def migrate_add_email_field():
    data = read_db()
    changed = False
    for u in data["users"]:
        if "email" not in u:
            u["email"] = ""
            changed = True
    if changed:
        write_db(data)

# =========================================
#                   APP
# =========================================

@asynccontextmanager
async def lifespan(app_: FastAPI):
    write_db(migrate_db(read_db()))
    migrate_add_default_user()
    migrate_attach_orphans()
    migrate_add_email_field()
    d = read_db()
    print(f"[STARTUP] DB={DB_PATH} users={len(d['users'])} lines={len(d['lines'])}")
    yield

app = FastAPI(title="Haushaltsbuch", version="1.0.0", lifespan=lifespan)

class NoCacheHtmlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if "text/html" in response.headers.get("content-type", ""):
            response.headers["cache-control"] = "no-cache, must-revalidate"
            response.headers["pragma"] = "no-cache"
        return response

app.add_middleware(NoCacheHtmlMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Static (optional – falls du dein Frontend hier serven willst)
static_dir = os.path.join(ROOT_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

_INDEX = os.path.join(static_dir, "index.html")
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

@app.get("/")
def root():
    return FileResponse(_INDEX, headers=_NO_CACHE)

@app.get("/static/index.html")
def index_html():
    return FileResponse(_INDEX, headers=_NO_CACHE)

# =========================================
#               AUTH ROUTES
# =========================================

@app.post("/auth/register", response_model=UserOut)
def register(u: UserCreate):
    data = read_db()
    if any(x["username"].lower() == u.username.lower() for x in data["users"]):
        raise HTTPException(400, "Benutzername bereits vergeben")
    uid = str(uuid.uuid4())
    data["users"].append({
        "id": uid,
        "username": u.username,
        "email": "",
        "password_hash": hash_pw(u.password),
        "created_at": int(time.time()),
    })
    write_db(data)
    return {"id": uid, "username": u.username, "email": ""}

@app.post("/auth/login", response_model=TokenOut)
def login(u: UserCreate):
    data = read_db()
    user = next((x for x in data["users"] if x["username"].lower() == u.username.lower()), None)
    if not user or not verify_pw(u.password, user["password_hash"]):
        raise HTTPException(401, "Bad credentials")
    return {"access_token": create_token(user)}

@app.get("/me", response_model=UserOut)
def me(user=Depends(get_current_user)):
    return {"id": user["id"], "username": user["username"], "email": user.get("email") or ""}

class UserUpdate(BaseModel):
    email: Optional[str] = None

class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)

@app.patch("/me", response_model=UserOut)
def update_me(payload: UserUpdate, user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i, u in enumerate(data["users"]) if u["id"] == user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "User not found")
    if payload.email is not None:
        data["users"][idx]["email"] = payload.email.strip()
    u = data["users"][idx]
    write_db(data)
    return {"id": u["id"], "username": u["username"], "email": u.get("email") or ""}

@app.post("/auth/change-password")
def change_password(payload: PasswordChange, user=Depends(get_current_user)):
    if not verify_pw(payload.old_password, user["password_hash"]):
        raise HTTPException(400, "Aktuelles Passwort falsch")
    data = read_db()
    idx = next((i for i, u in enumerate(data["users"]) if u["id"] == user["id"]), -1)
    data["users"][idx]["password_hash"] = hash_pw(payload.new_password)
    write_db(data)
    return {"ok": True}

@app.get("/api/export/csv")
def export_csv(user=Depends(get_current_user)):
    from fastapi.responses import StreamingResponse
    import csv, io
    data = read_db()
    lines = [normalize_line(l) for l in data["lines"] if l.get("user_id") == user["id"]]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Bezeichnung", "Typ", "Kategorie", "Betrag", "Variabel", "Unterposten"])
    for l in lines:
        subs = "; ".join(f"{s.get('label','')} {s.get('amount',0):.2f}" for s in (l.get("subitems") or []))
        w.writerow([
            l.get("label", ""),
            "Einnahme" if l["type"] == "income" else "Ausgabe",
            l.get("category", ""),
            f"{total_of_line(l):.2f}".replace(".", ","),
            "ja" if l.get("is_variable") else ("nein" if l.get("is_variable") is False else ""),
            subs,
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=haushaltsbuch.csv"},
    )

# =========================================
#               API ROUTES
# =========================================

@app.get("/api/categories")
def categories(user=Depends(get_current_user)):
    data = read_db()
    lines = [l for l in data["lines"] if l.get("user_id") == user["id"]]
    return sorted({ (l.get("category") or "ohne") for l in lines })

@app.get("/api/summary")
def summary(user=Depends(get_current_user)):
    data = read_db()
    lines = [normalize_line(l) for l in data["lines"] if l.get("user_id") == user["id"]]
    inc = sum(total_of_line(l) for l in lines if l["type"] == "income")
    exp = sum(total_of_line(l) for l in lines if l["type"] == "expense")
    cats = {}
    for l in lines:
        c = l.get("category") or "ohne"
        cats.setdefault(c, 0.0)
        cats[c] += total_of_line(l) * (1 if l["type"] == "income" else -1)
    return {
        "income": inc,
        "expense": exp,
        "net": inc - exp,
        "categories": [{"category": k, "total": v} for k, v in sorted(cats.items())],
    }

@app.get("/api/groups")
def groups(user=Depends(get_current_user)):
    data = read_db()
    lines = [normalize_line(l) for l in data["lines"] if l.get("user_id") == user["id"]]
    gmap = {}
    for l in lines:
        key = (l["type"], l.get("category") or "ohne")
        g = gmap.setdefault(key, {"type": key[0], "category": key[1], "total": 0.0, "lines": []})
        g["lines"].append(l)
        amt = total_of_line(l)
        g["total"] += amt if l["type"] == "income" else -amt
    order_type = {"income": 0, "expense": 1}
    return sorted(gmap.values(), key=lambda g: (order_type.get(g["type"], 99), g["category"]))

@app.get("/api/lines")
def list_lines(sort: Optional[str] = None, user=Depends(get_current_user)):
    data = read_db()
    lines = [normalize_line(l) for l in data["lines"] if l.get("user_id") == user["id"]]
    if sort == "category":
        lines.sort(key=lambda l: (l.get("type",""), (l.get("category") or "").lower(), (l.get("label") or "").lower()))
    elif sort == "label":
        lines.sort(key=lambda l: (l.get("label") or "").lower())
    return lines

@app.post("/api/lines")
def create_line(payload: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()

    label = str((payload.get("label") or "")).strip()
    typ   = (payload.get("type") or "").strip()
    if not label or typ not in ("income", "expense"):
        raise HTTPException(422, "Felder 'label' und 'type' (income|expense) sind erforderlich.")

    category = payload.get("category") or ("sonstige ausgaben" if typ == "expense" else "sonstige einnahmen")
    try:
        base_amount = float(payload.get("base_amount") or 0.0)
    except (TypeError, ValueError):
        base_amount = 0.0

    subs = []
    for si in (payload.get("subitems") or []):
        try:
            amt = float(si.get("amount") or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        subs.append({
            "id": si.get("id") or str(uuid.uuid4()),
            "label": si.get("label") or "",
            "amount": amt,
        })

    new_line = {
        "id": str(uuid.uuid4()),
        "label": label,
        "type": typ,
        "category": category,
        "base_amount": base_amount,
        "subitems": subs,
        "is_variable": (payload.get("is_variable") if payload.get("is_variable") in (True, False) else None),
        "user_id": user["id"],
    }
    data["lines"].append(new_line)
    write_db(data)
    return new_line

@app.put("/api/lines/{line_id}")
def update_line(line_id: str, payload: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i,l in enumerate(data["lines"]) if l.get("id")==line_id and l.get("user_id")==user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "Line not found")

    cur = data["lines"][idx]
    if "label" in payload:
        cur["label"] = str((payload.get("label") or "")).strip() or cur.get("label") or ""
    if "type" in payload:
        t = (payload.get("type") or "").strip()
        if t in ("income","expense"):
            cur["type"] = t
    if "category" in payload:
        cur["category"] = payload.get("category") or ("sonstige ausgaben" if cur.get("type")=="expense" else "sonstige einnahmen")

    if "base_amount" in payload:
        try:
            cur["base_amount"] = float(payload.get("base_amount") or 0.0)
        except (TypeError, ValueError):
            pass

    if "subitems" in payload and isinstance(payload.get("subitems"), list):
        subs = []
        for si in payload.get("subitems") or []:
            try:
                amt = float(si.get("amount") or 0.0)
            except (TypeError, ValueError):
                amt = 0.0
            subs.append({
                "id": si.get("id") or str(uuid.uuid4()),
                "label": si.get("label") or "",
                "amount": amt,
            })
        cur["subitems"] = subs

    if "is_variable" in payload:
        v = payload.get("is_variable")
        cur["is_variable"] = v if v in (True, False, None) else None

    # user_id nie überschreiben
    cur["user_id"] = user["id"]
    data["lines"][idx] = cur
    write_db(data)
    return cur

@app.delete("/api/lines/{line_id}")
def delete_line(line_id: str, user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i,l in enumerate(data["lines"]) if l.get("id")==line_id and l.get("user_id")==user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "Line not found")
    data["lines"].pop(idx)
    write_db(data)
    return {"ok": True}

@app.post("/api/lines/{line_id}/subitems")
def add_subitem(line_id: str, si: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i,l in enumerate(data["lines"]) if l.get("id")==line_id and l.get("user_id")==user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "Line not found")
    line = normalize_line(data["lines"][idx])
    try:
        amt = float(si.get("amount") or 0.0)
    except (TypeError, ValueError):
        amt = 0.0
    line["subitems"].append({
        "id": si.get("id") or str(uuid.uuid4()),
        "label": si.get("label") or "",
        "amount": amt,
    })
    data["lines"][idx] = line
    write_db(data)
    return line

@app.delete("/api/lines/{line_id}/subitems/{sub_id}")
def delete_subitem(line_id: str, sub_id: str, user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i,l in enumerate(data["lines"]) if l.get("id")==line_id and l.get("user_id")==user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "Line not found")
    line = normalize_line(data["lines"][idx])
    before = len(line.get("subitems", []))
    line["subitems"] = [x for x in line.get("subitems", []) if x.get("id") != sub_id]
    if len(line["subitems"]) == before:
        raise HTTPException(404, "Subitem not found")
    data["lines"][idx] = line
    write_db(data)
    return line

@app.delete("/api/categories/{name}")
def delete_category(name: str, target: Optional[str] = Query(None), user=Depends(get_current_user)):
    """
    Kategorie 'name' für DICH entfernen:
      - Wenn 'target' gesetzt ist, werden alle deine Lines mit category==name auf 'target' umgehängt.
      - Sonst werden sie auf die Default-Kategorie gesetzt ("sonstige ausgaben" / "sonstige einnahmen" je nach type).
    Danach existiert 'name' bei dir nicht mehr (weil /api/categories nur aus deinen Lines liest).
    """
    data = read_db()
    changed = False
    for l in data["lines"]:
        if l.get("user_id") != user["id"]:
            continue
        if (l.get("category") or "").lower() == name.lower():
            if target:
                l["category"] = target
            else:
                # fallback nach type
                t = l.get("type") or "expense"
                l["category"] = "sonstige einnahmen" if t == "income" else "sonstige ausgaben"
            changed = True
    if changed:
        write_db(data)
    return {"ok": True, "renamed_to": target or None}

@app.post("/api/categories/rename")
def rename_category(payload: dict = Body(...), user=Depends(get_current_user)):
    """
    Payload: { "old": "...", "new": "..." }
    Bennent die Kategorie für DICH um (alle deine Lines).
    """
    old = (payload.get("old") or "").strip()
    new = (payload.get("new") or "").strip()
    if not old or not new:
        raise HTTPException(422, "Felder 'old' und 'new' sind erforderlich.")

    data = read_db()
    changed = False
    for l in data["lines"]:
        if l.get("user_id") != user["id"]:
            continue
        if (l.get("category") or "").lower() == old.lower():
            l["category"] = new
            changed = True
    if changed:
        write_db(data)
    return {"ok": True}

# =========================================
#            KONTEN / UNTERKONTEN
# =========================================

def _require_account(data: dict, account_id: str, user_id: str) -> dict:
    acc = next((a for a in data["accounts"] if a.get("id") == account_id and a.get("user_id") == user_id), None)
    if not acc:
        raise HTTPException(404, "Konto nicht gefunden")
    return acc

@app.get("/api/accounts")
def list_accounts(user=Depends(get_current_user)):
    data = read_db()
    if run_recurring_catchup(user["id"], data):
        write_db(data)

    accounts = [normalize_account(a) for a in data["accounts"] if a.get("user_id") == user["id"] and not a.get("archived")]
    accounts.sort(key=lambda a: (a.get("sort_order", 0), a.get("name", "").lower()))

    out = []
    total = 0.0
    for a in accounts:
        bks = sorted(
            (normalize_booking(b) for b in data["bookings"] if b.get("account_id") == a["id"]),
            key=lambda b: (b["date"], b["id"]),
        )
        running = 0.0
        spark = []
        for b in bks:
            running += b["amount"]
            spark.append(round(running, 2))
        balance = round(running, 2)
        total += balance
        out.append({**a, "balance": balance, "sparkline": spark[-30:], "bookings_count": len(bks)})

    return {"accounts": out, "total": round(total, 2)}

@app.post("/api/accounts")
def create_account(payload: dict = Body(...), user=Depends(get_current_user)):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Feld 'name' ist erforderlich.")
    data = read_db()
    acc = normalize_account({
        "name": name,
        "institute": payload.get("institute"),
        "icon": payload.get("icon"),
        "sort_order": payload.get("sort_order"),
    }, user_id=user["id"])
    data["accounts"].append(acc)
    write_db(data)
    return acc

@app.put("/api/accounts/{account_id}")
def update_account(account_id: str, payload: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i, a in enumerate(data["accounts"]) if a.get("id") == account_id and a.get("user_id") == user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "Konto nicht gefunden")
    cur = data["accounts"][idx]
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if name:
            cur["name"] = name
    if "institute" in payload:
        cur["institute"] = payload.get("institute") or ""
    if "icon" in payload:
        cur["icon"] = payload.get("icon") or "🏦"
    if "sort_order" in payload:
        try:
            cur["sort_order"] = float(payload.get("sort_order") or 0)
        except (TypeError, ValueError):
            pass
    if "archived" in payload:
        cur["archived"] = bool(payload.get("archived"))
    cur["user_id"] = user["id"]
    data["accounts"][idx] = cur
    write_db(data)
    return cur

@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str, user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i, a in enumerate(data["accounts"]) if a.get("id") == account_id and a.get("user_id") == user["id"]), -1)
    if idx < 0:
        raise HTTPException(404, "Konto nicht gefunden")
    data["accounts"].pop(idx)
    data["bookings"] = [b for b in data["bookings"] if b.get("account_id") != account_id]
    data["recurring_rules"] = [r for r in data["recurring_rules"] if r.get("account_id") != account_id]
    write_db(data)
    return {"ok": True}

@app.get("/api/accounts/{account_id}/bookings")
def list_bookings(account_id: str, user=Depends(get_current_user)):
    data = read_db()
    _require_account(data, account_id, user["id"])
    if run_recurring_catchup(user["id"], data):
        write_db(data)
    bks = sorted(
        (normalize_booking(b) for b in data["bookings"] if b.get("account_id") == account_id),
        key=lambda b: b["date"],
        reverse=True,
    )
    return bks

@app.post("/api/accounts/{account_id}/bookings")
def create_booking(account_id: str, payload: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()
    _require_account(data, account_id, user["id"])
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Feld 'amount' ist erforderlich und muss eine Zahl sein.")
    if amount == 0:
        raise HTTPException(422, "Betrag darf nicht 0 sein.")
    b = normalize_booking({
        "date": payload.get("date"),
        "amount": amount,
        "note": payload.get("note"),
        "source": "manual",
    }, account_id=account_id)
    data["bookings"].append(b)
    write_db(data)
    return b

@app.delete("/api/bookings/{booking_id}")
def delete_booking(booking_id: str, user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i, b in enumerate(data["bookings"]) if b.get("id") == booking_id), -1)
    if idx < 0:
        raise HTTPException(404, "Buchung nicht gefunden")
    acc = next((a for a in data["accounts"] if a.get("id") == data["bookings"][idx].get("account_id")), None)
    if not acc or acc.get("user_id") != user["id"]:
        raise HTTPException(404, "Buchung nicht gefunden")
    data["bookings"].pop(idx)
    write_db(data)
    return {"ok": True}

@app.get("/api/accounts/{account_id}/recurring")
def list_recurring(account_id: str, user=Depends(get_current_user)):
    data = read_db()
    _require_account(data, account_id, user["id"])
    rules = [normalize_recurring_rule(r) for r in data["recurring_rules"] if r.get("account_id") == account_id]
    lines_by_id = {l["id"]: l for l in data["lines"] if l.get("user_id") == user["id"]}
    out = []
    for r in rules:
        line = lines_by_id.get(r["linked_line_id"])
        out.append({
            **r,
            "line_label": line.get("label") if line else None,
            "line_amount": account_deposit_amount(line) if line else None,
            "line_missing": line is None,
        })
    return out

@app.post("/api/accounts/{account_id}/recurring")
def create_recurring(account_id: str, payload: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()
    _require_account(data, account_id, user["id"])
    line = next((l for l in data["lines"] if l.get("id") == payload.get("linked_line_id") and l.get("user_id") == user["id"]), None)
    if not line:
        raise HTTPException(422, "Gültige 'linked_line_id' ist erforderlich.")
    r = normalize_recurring_rule({
        "linked_line_id": line["id"],
        "day_of_month": payload.get("day_of_month"),
        "active": payload.get("active", True),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
    }, account_id=account_id)
    data["recurring_rules"].append(r)
    write_db(data)
    return r

@app.put("/api/recurring/{rule_id}")
def update_recurring(rule_id: str, payload: dict = Body(...), user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i, r in enumerate(data["recurring_rules"]) if r.get("id") == rule_id), -1)
    if idx < 0:
        raise HTTPException(404, "Dauerauftrag nicht gefunden")
    cur = data["recurring_rules"][idx]
    acc = next((a for a in data["accounts"] if a.get("id") == cur.get("account_id")), None)
    if not acc or acc.get("user_id") != user["id"]:
        raise HTTPException(404, "Dauerauftrag nicht gefunden")

    if "linked_line_id" in payload:
        line = next((l for l in data["lines"] if l.get("id") == payload.get("linked_line_id") and l.get("user_id") == user["id"]), None)
        if not line:
            raise HTTPException(422, "Gültige 'linked_line_id' ist erforderlich.")
        cur["linked_line_id"] = line["id"]
    if "day_of_month" in payload:
        try:
            cur["day_of_month"] = min(max(int(payload.get("day_of_month")), 1), 28)
        except (TypeError, ValueError):
            pass
    if "active" in payload:
        cur["active"] = bool(payload.get("active"))
    if "start_date" in payload:
        try:
            date.fromisoformat(payload.get("start_date"))
            cur["start_date"] = payload.get("start_date")
        except (TypeError, ValueError):
            pass
    if "end_date" in payload:
        ed = payload.get("end_date")
        if ed:
            try:
                date.fromisoformat(ed)
                cur["end_date"] = ed
            except (TypeError, ValueError):
                pass
        else:
            cur["end_date"] = None
    if "rule_type" in payload and payload.get("rule_type") in ("Dauerauftrag", "Lastschrift"):
        cur["rule_type"] = payload["rule_type"]

    data["recurring_rules"][idx] = cur
    write_db(data)
    return cur

@app.delete("/api/recurring/{rule_id}")
def delete_recurring(rule_id: str, user=Depends(get_current_user)):
    data = read_db()
    idx = next((i for i, r in enumerate(data["recurring_rules"]) if r.get("id") == rule_id), -1)
    if idx < 0:
        raise HTTPException(404, "Dauerauftrag nicht gefunden")
    acc = next((a for a in data["accounts"] if a.get("id") == data["recurring_rules"][idx].get("account_id")), None)
    if not acc or acc.get("user_id") != user["id"]:
        raise HTTPException(404, "Dauerauftrag nicht gefunden")
    data["recurring_rules"].pop(idx)
    write_db(data)
    return {"ok": True}
