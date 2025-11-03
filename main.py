from fastapi import FastAPI, HTTPException, Body, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime, timedelta, timezone
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
    return {"lines": [], "users": [], "version": 1}

def ensure_db_shapes(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("lines", [])
    data.setdefault("users", [])
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

def migrate_db(data: dict) -> dict:
    data = ensure_db_shapes(data)
    new_lines = [normalize_line(l) for l in data.get("lines", [])]
    if new_lines != data.get("lines") or "version" not in data:
        data["lines"]   = new_lines
        data["version"] = 1
        write_db(data)
    return data

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
    # bevorzugt thom7e
    pref = next((u for u in data["users"] if u.get("username","").lower()=="thom7e"), data["users"][0])
    changed = False
    for l in data["lines"]:
        if not l.get("user_id"):
            l["user_id"] = pref["id"]
            changed = True
    if changed:
        write_db(data)

# =========================================
#                   APP
# =========================================

app = FastAPI(title="Haushaltsbuch", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Static (optional – falls du dein Frontend hier serven willst)
static_dir = os.path.join(ROOT_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html", status_code=307)

@app.on_event("startup")
def _startup():
    # DB-Struktur & Default-User sicherstellen
    write_db(migrate_db(read_db()))
    migrate_add_default_user()
    migrate_attach_orphans()
    d = read_db()
    print(f"[STARTUP] DB={DB_PATH} users={len(d['users'])} lines={len(d['lines'])}")

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
        "password_hash": hash_pw(u.password),
        "created_at": int(time.time()),
    })
    write_db(data)
    return {"id": uid, "username": u.username}

@app.post("/auth/login", response_model=TokenOut)
def login(u: UserCreate):
    data = read_db()
    user = next((x for x in data["users"] if x["username"].lower() == u.username.lower()), None)
    if not user or not verify_pw(u.password, user["password_hash"]):
        raise HTTPException(401, "Bad credentials")
    return {"access_token": create_token(user)}

@app.get("/me", response_model=UserOut)
def me(user=Depends(get_current_user)):
    return {"id": user["id"], "username": user["username"]}

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
    data = read_db(); ensure_db_shapes(data)
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

    data = read_db(); ensure_db_shapes(data)
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
