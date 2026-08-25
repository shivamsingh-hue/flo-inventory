"""
app.py — DIC-LIVE GUIDED PUTAWAY V3.0 — Flask/SQLite edition
Runs on Render. Reads flo_inventory.db + physical_shelve.csv + inventory_lbh.csv + casper_ids.csv
"""
import sqlite3, os, csv, io, json, re, time, urllib.request
from flask import Flask, render_template_string, request, Response, jsonify
import datetime, threading

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dic-flo-2024-render")

DB   = os.path.join(os.path.dirname(__file__), "flo_inventory.db")
PHYS = os.path.join(os.path.dirname(__file__), "physical_shelve.csv")
LBH  = os.path.join(os.path.dirname(__file__), "inventory_lbh.csv")
CID  = os.path.join(os.path.dirname(__file__), "casper_ids.csv")

GITHUB_RAW   = "https://raw.githubusercontent.com/shivamsingh-hue/flo-inventory/main/"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

_cache_ts   = {}   # file -> last fetch time
CACHE_TTL   = 280  # seconds

# ── GitHub fetch ──────────────────────────────────────────
def refresh_file(filename, local_path):
    now = time.time()
    # DB: re-fetch every 60s max; other files: cache for CACHE_TTL
    ttl = 60 if filename == "flo_inventory.db" else CACHE_TTL
    if now - _cache_ts.get(filename, 0) < ttl and os.path.exists(local_path):
        return
    try:
        bust = int(time.time())
        req = urllib.request.Request(
            GITHUB_RAW + filename + f"?bust={bust}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Cache-Control": "no-cache, no-store",
                     "Pragma": "no-cache",
                     "User-Agent": "flo-app"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(local_path, "wb") as f:
            f.write(data)
        _cache_ts[filename] = time.time()
        print(f"[OK] Refreshed {filename} ({len(data)//1024} KB)")
    except Exception as e:
        print(f"[ERR] {filename}: {e}")

def refresh_all():
    refresh_file("flo_inventory.db",      DB)
    refresh_file("physical_shelve.csv",   PHYS)
    refresh_file("inventory_lbh.csv",     LBH)
    refresh_file("casper_ids.csv",        CID)

# ── Casper ID loader ──────────────────────────────────────
def load_casper_ids():
    """Returns dict: casper_id -> {name, dept}"""
    refresh_file("casper_ids.csv", CID)
    result = {}
    if not os.path.exists(CID):
        return result
    try:
        with open(CID, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # headers: Casper, Employee Name, Deparment
                cid  = str(row.get("Casper","") or "").strip()
                name = str(row.get("Employee Name","") or "").strip()
                dept = str(row.get("Deparment","") or "").strip()
                if cid:
                    result[cid] = {"name": name or cid, "dept": dept}
    except Exception as e:
        print(f"[ERR] casper_ids: {e}")
    return result

# ── Physical Shelve loader ────────────────────────────────
def load_physical_shelve():
    """Returns dict: shelf_label -> {type, cufeet}"""
    refresh_file("physical_shelve.csv", PHYS)
    result = {}
    if not os.path.exists(PHYS):
        return result
    try:
        with open(PHYS, newline='', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            raw_headers = next(reader, [])
            # Normalize headers: strip spaces
            headers = [h.strip() for h in raw_headers]
            print(f"[PHYS] Headers: {headers}")
            # physical_shelve.csv has 3 columns:
            # Col 0: "Shelve Type" (e.g. "Lower Shelve", "Bulk Location", "SAAR Location")
            # Col 1: "Shelf" (full label e.g. F0-PZ01-A01-R01-A1)
            # Col 2: "Cufeet"
            idx_type  = 0
            idx_shelf = 1
            idx_cuft  = 2
            for row in reader:
                if len(row) < 2: continue
                full_type = str(row[idx_type]).strip() if idx_type < len(row) else "Standard Location"
                shelf     = str(row[idx_shelf]).strip() if idx_shelf < len(row) else ""
                cuft_r    = str(row[idx_cuft]).strip() if idx_cuft < len(row) else ""
                if not shelf: continue
                if not full_type: full_type = "Standard Location"
                try:    cufeet = float(cuft_r)
                except: cufeet = 0.0
                result[shelf] = {"type": full_type, "cufeet": cufeet}
        print(f"[PHYS] Loaded {len(result)} shelves. Sample types: {list(set(v['type'] for v in list(result.values())[:20]))}")
    except Exception as e:
        print(f"[ERR] physical_shelve: {e}")
    return result

# ── LBH loader ────────────────────────────────────────────
def load_lbh():
    """Returns dict: fsn -> cuft_per_unit"""
    refresh_file("inventory_lbh.csv", LBH)
    result = {}
    if not os.path.exists(LBH):
        return result
    try:
        with open(LBH, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                fsn = str(row.get("product_detail_fsn","") or "").strip()
                try:    cuft = float(str(row.get("Cuft_per_Unit","0") or "0").strip())
                except: cuft = 0.0
                if fsn and cuft > 0:
                    result[fsn] = cuft
    except Exception as e:
        print(f"[ERR] lbh: {e}")
    return result

# ── SQLite ────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def extract_ean(raw):
    if not raw: return []
    s = str(raw).strip()
    s = re.sub(r'\.0+$', '', s)
    if re.match(r'^\d{8,15}$', s): return [s]
    runs = re.findall(r'\d{8,15}', s)
    out = set()
    for r in runs:
        if len(r) <= 14: out.add(r)
        elif len(r) == 15:
            out.add(r[:13]); out.add(r[2:]); out.add(r[:14])
    return list(out)

def remark_for_qty(qty):
    if qty <= 0:  return "Free Shelve"
    if qty <= 10: return "Partial Space"
    if qty <= 30: return "Semi Space"
    return "Full Space"

def load_inventory():
    refresh_all()
    if not os.path.exists(DB):
        return {"loc_qty": {}, "loc_items": {}, "pull_ts": None}
    try:
        conn = get_db()
        pull_ts = None
        try:
            r = conn.execute("SELECT MAX(_pull_ts) FROM inventory").fetchone()
            pull_ts = r[0] if r else None
        except: pass
        cur  = conn.execute("SELECT * FROM inventory")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        conn.close()

        def fc(names):
            for n in names:
                for i, c in enumerate(cols):
                    if c.strip().lower() == n.lower(): return i
            return -1

        i_fsn   = fc(["FSN","fsn"])
        i_wid   = fc(["WID","wid"])
        i_shelf = fc(["Shelf","shelf","storage_location_label","location"])
        i_qty   = fc(["Qty","qty","inventory_item_quantity","quantity"])
        i_prod  = fc(["Product","product","product_detail_product_title","title"])

        loc_qty   = {}
        loc_items = {}

        for row in rows:
            row = list(row)
            fsn0 = str(row[i_fsn]).strip() if i_fsn >= 0 else ""
            if re.match(r'^(previous|next|FSN)$', fsn0, re.I): continue
            shelf = str(row[i_shelf]).strip() if i_shelf >= 0 else ""
            if not shelf or re.search(r'[←→]', shelf): continue
            qty = 0
            if i_qty >= 0 and row[i_qty] not in (None,"","N/A"):
                try: qty = float(row[i_qty])
                except: qty = 0
            prod = str(row[i_prod]).strip() if i_prod >= 0 else ""
            wid  = str(row[i_wid]).strip() if i_wid >= 0 else "N/A"
            eans = extract_ean(prod)
            ean  = eans[0] if eans else "N/A"
            fsn  = fsn0 or "N/A"

            loc_qty[shelf] = loc_qty.get(shelf, 0) + qty
            if shelf not in loc_items: loc_items[shelf] = []
            loc_items[shelf].append({
                "fsn": fsn, "wid": wid, "ean": ean, "eans": eans,
                "rawProd": prod, "itemQty": qty
            })

        return {"loc_qty": loc_qty, "loc_items": loc_items, "pull_ts": pull_ts}
    except Exception as e:
        print("load_inventory error:", e)
        return {"loc_qty": {}, "loc_items": {}, "pull_ts": None}

def build_locations(inv, phys, lbh):
    locs      = []
    loc_qty   = inv.get("loc_qty", {})
    loc_items = inv.get("loc_items", {})

    # Universe = all physical shelves + any FLO-only shelves
    universe = {}
    for shelf, info in phys.items():
        universe[shelf] = info
    for shelf in loc_qty:
        if shelf not in universe:
            universe[shelf] = {"type": "FLO Feed", "cufeet": 0.0}

    for shelf, info in universe.items():
        qty    = loc_qty.get(shelf, 0)
        parts  = shelf.split("-")
        floor  = parts[0] if parts else "Unknown"
        pz     = parts[1] if len(parts) > 1 else ""
        aisle  = parts[2] if len(parts) > 2 else ""
        remark = remark_for_qty(qty)
        cufeet = info.get("cufeet", 0.0)

        # LBH calc: sum(qty * cuft_per_unit) for items in this shelf
        total_qty_cuft = 0.0
        items = loc_items.get(shelf, [])
        for it in items:
            cuft_unit = lbh.get(it["fsn"], 0.0)
            total_qty_cuft += it["itemQty"] * cuft_unit

        if cufeet and cufeet > 0:
            util_pct  = min(round((total_qty_cuft / cufeet) * 100, 2), 100)
            avail_pct = round(100 - util_pct, 2)
        else:
            util_pct  = ""
            avail_pct = ""

        locs.append({
            "label":        shelf,
            "floor":        floor,
            "pz":           pz,
            "aisle":        aisle,
            "type":         info.get("type", "Standard Location"),
            "totalQty":     qty,
            "remark":       remark,
            "cufeet":       cufeet if cufeet else "",
            "totalQtyCuft": round(total_qty_cuft, 4) if total_qty_cuft else "",
            "availCuftPct": avail_pct,
        })
    return locs

def build_raw(inv, locs):
    raw       = []
    loc_items = inv.get("loc_items", {})
    for loc in locs:
        shelf = loc["label"]
        items = loc_items.get(shelf, [])
        if items and loc["totalQty"] > 0:
            for it in items:
                raw.append({**loc, **it})
        else:
            raw.append({**loc, "fsn":"N/A","wid":"N/A","ean":"N/A",
                        "eans":[],"itemQty":0,"rawProd":""})
    return raw

def build_metrics(locs):
    floor_map = {}
    for loc in locs:
        fl = loc["floor"]
        r  = loc["remark"]
        if fl not in floor_map:
            floor_map[fl] = {"floor":fl,"free":0,"partial":0,"semi":0,"full":0}
        if   "Free"    in r: floor_map[fl]["free"]    += 1
        elif "Partial" in r: floor_map[fl]["partial"] += 1
        elif "Semi"    in r: floor_map[fl]["semi"]    += 1
        elif "Full"    in r: floor_map[fl]["full"]    += 1
    return sorted(floor_map.values(), key=lambda x: x["floor"])

# ── Action log ────────────────────────────────────────────
action_log = []

def build_action_summary():
    tz_offset = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now       = datetime.datetime.now(tz_offset)
    today_str = now.strftime("%Y-%m-%d")
    h = now.hour
    if 9 <= h < 19: shift_label = "🌅 Morning Shift • 9 AM – 7 PM"
    elif h >= 20:   shift_label = "🌙 Night Shift • 8 PM – 5 AM"
    else:           shift_label = "🌙 Night Shift • 8 PM – 5 AM"

    by_action = {}; by_user = {}; recent = []
    for entry in action_log:
        act  = entry["action"]; user = entry["user"]; ts = entry["ts"]
        is_today = ts.startswith(today_str)
        if act not in by_action: by_action[act] = {"action":act,"total":0,"today":0}
        by_action[act]["total"] += 1
        if is_today: by_action[act]["today"] += 1
        if user not in by_user:
            by_user[user] = {"name":user,"casperId":user,"dept":"","count":0,"actions":{}}
        by_user[user]["count"] += 1
        by_user[user]["actions"][act] = by_user[user]["actions"].get(act,0) + 1
        recent.append({"time":ts[11:16],"name":user,"shelf":entry["shelf"],"action":act,"dept":""})

    by_user_today = sorted(
        [{"name":v["name"],"casperId":v["casperId"],"dept":v["dept"],"count":v["count"],
          "actions":[{"action":a,"count":c} for a,c in v["actions"].items()]}
         for v in by_user.values()], key=lambda x: -x["count"])[:10]

    return {
        "byAction": list(by_action.values()),
        "byActionDept": [],
        "byUserToday": by_user_today,
        "recent": list(reversed(recent[-20:])),
        "gtlTracking": [], "putawayTracking": [],
        "shiftLabel": shift_label,
        "grandTotal": len(action_log),
        "grandToday": sum(1 for e in action_log if e["ts"].startswith(today_str))
    }

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(FRONTEND)

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    cid  = str(data.get("id","")).strip()
    if not cid:
        return jsonify({"ok": False, "msg": "Enter Casper ID"})
    casper_ids = load_casper_ids()
    if not casper_ids:
        # If CSV not loaded yet, allow any login
        return jsonify({"ok": True, "name": cid, "dept": "N/A"})
    if cid in casper_ids:
        info = casper_ids[cid]
        return jsonify({"ok": True, "name": info.get("name", cid),
                        "dept": info.get("dept","N/A")})
    return jsonify({"ok": False, "msg": "❌ Invalid Casper ID. Access Denied."})

@app.route("/api/data")
def api_data():
    inv     = load_inventory()
    phys    = load_physical_shelve()
    lbh     = load_lbh()
    locs    = build_locations(inv, phys, lbh)
    raw     = build_raw(inv, locs)
    metrics = build_metrics(locs)
    total_qty = sum(inv.get("loc_qty",{}).values())
    pull_ts   = inv.get("pull_ts","")
    ref_label = ""
    if pull_ts:
        try:
            dt = datetime.datetime.fromisoformat(pull_ts)
            ref_label = dt.strftime("%d-%b %H:%M")
        except: ref_label = pull_ts
    action_summary = build_action_summary()
    resp = jsonify({
        "rawData":          raw,
        "pureLocationsData":locs,
        "summaryMetrics":   metrics,
        "actionSummary":    action_summary,
        "totalQty":         total_qty,
        "lastRefresh":      ref_label
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/action", methods=["POST"])
def api_action():
    data     = request.json or {}
    shelf_id = data.get("shelfId","")
    action   = data.get("action","")
    user     = data.get("user","")
    now      = datetime.datetime.now().isoformat(timespec="seconds")
    action_log.append({"ts":now,"shelf":shelf_id,"action":action,"user":user})
    return jsonify({"ok": True})

@app.route("/api/find")
def api_find():
    q   = request.args.get("q","").strip().lower()
    inv = load_inventory()
    phys = load_physical_shelve()
    lbh  = load_lbh()
    raw  = build_raw(inv, build_locations(inv, phys, lbh))
    if len(q) < 3: return jsonify([])
    results = []
    for item in raw:
        if item["label"].lower() == q: results.append(item); continue
        if item.get("fsn","").lower() == q: results.append(item); continue
        if item.get("wid","").lower() == q: results.append(item); continue
        if any(str(e).lower() == q for e in item.get("eans",[])): results.append(item)
    return jsonify(results)

@app.route("/download")
def download():
    inv  = load_inventory()
    phys = load_physical_shelve()
    lbh  = load_lbh()
    locs = build_locations(inv, phys, lbh)
    raw  = build_raw(inv, locs)
    out  = io.StringIO()
    w    = csv.writer(out)
    w.writerow(["Shelf","Floor","Zone","Aisle","Shelve Type","Total Qty","Remark",
                "Cufeet","Used Cuft","Avail %","FSN","WID","EAN","Item Qty"])
    for r in raw:
        w.writerow([r.get("label",""),r.get("floor",""),r.get("pz",""),r.get("aisle",""),
                    r.get("type",""),r.get("totalQty",""),r.get("remark",""),
                    r.get("cufeet",""),r.get("totalQtyCuft",""),r.get("availCuftPct",""),
                    r.get("fsn",""),r.get("wid",""),r.get("ean",""),r.get("itemQty","")])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=flo_inventory.csv"})

# ── Keep-alive ────────────────────────────────────────────
def _keep_alive():
    time.sleep(300)
    while True:
        try: urllib.request.urlopen("https://flo-inventory.onrender.com/", timeout=10)
        except: pass
        time.sleep(840)

threading.Thread(target=_keep_alive, daemon=True).start()

# ── Frontend ──────────────────────────────────────────────
FRONTEND = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>ANJANEYA-LIVE GUIDED PUTAWAY V3.O</title>
  <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box}
    body{font-family:'Poppins',sans-serif;background:#f0f2f5;margin:0;padding:10px}
    .container{background:#fff;width:100%;max-width:480px;border-radius:15px;box-shadow:0 10px 25px rgba(0,0,0,.1);margin:auto;overflow:hidden}
    body.login-active{padding:0!important;background:#0D1F3C!important}
    #login-page{position:fixed;inset:0;background:linear-gradient(135deg,#0D1F3C 0%,#1a3a6b 100%);display:flex;align-items:stretch;z-index:9999;min-height:100vh}
    .lo-overlay{width:100%;display:flex;flex-direction:column;align-items:center;justify-content:space-between;padding:22px 18px 18px;box-sizing:border-box}
    .lo-brand-line1{font-size:13px;font-weight:700;color:#FFD700;letter-spacing:.4px;margin-bottom:4px;text-align:center}
    .lo-brand-line2{font-size:11px;font-weight:600;color:rgba(255,255,255,.82);letter-spacing:1px;text-transform:uppercase;text-align:center}
    .lo-card{width:100%;max-width:380px;background:rgba(6,16,45,.85);border:1.5px solid rgba(255,215,0,.3);border-radius:20px;padding:26px 22px 22px;box-shadow:0 20px 60px rgba(0,0,0,.6);text-align:center}
    .lo-icon{font-size:34px;margin-bottom:14px}
    .lo-title{font-size:17px;font-weight:700;color:#fff;margin-bottom:4px}
    .lo-sub{font-size:11px;color:rgba(255,255,255,.5);margin-bottom:18px}
    .lo-label{display:block;text-align:left;font-size:10px;font-weight:700;letter-spacing:1.2px;color:rgba(255,215,0,.85);text-transform:uppercase;margin-bottom:8px}
    .lo-input{width:100%;height:52px;background:rgba(255,255,255,.06);border:1.5px solid rgba(255,215,0,.35);border-radius:12px;padding:0 16px;font-size:15px;color:#fff;outline:none;font-family:'Poppins',sans-serif;margin-bottom:14px}
    .lo-input:focus{border-color:#FFD700;background:rgba(255,255,255,.1)}
    .lo-btn{width:100%;height:52px;background:linear-gradient(135deg,#F5C518,#FFD700,#E8B800);color:#0A1220;border:none;border-radius:12px;font-size:15px;font-weight:800;cursor:pointer;font-family:'Poppins',sans-serif}
    .lo-btn:disabled{opacity:.5;cursor:not-allowed}
    .lo-msg{min-height:18px;margin-top:12px;font-size:12px;font-weight:600;color:#ff6b6b;text-align:center}
    .lo-footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:8px}
    .lo-footer-badge{background:rgba(255,215,0,.12);border:1px solid rgba(255,215,0,.28);border-radius:20px;padding:3px 11px;font-size:9.5px;font-weight:700;color:rgba(255,215,0,.8)}
    .maroon-hdr{background:#7B1818}
    .gold-stripe{height:5px;background:repeating-linear-gradient(90deg,#FFD700 0,#FFD700 14px,#7B1818 14px,#7B1818 24px)}
    .hdr-inner{padding:12px 14px 10px}
    .hdr-h1{font-size:13px;font-weight:800;color:#fff;letter-spacing:.2px}
    .hdr-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}
    .hdr-stat{background:rgba(255,255,255,.1);border-radius:8px;padding:7px 10px}
    .hdr-sv{font-size:15px;font-weight:800;color:#FFD700}
    .hdr-sl{font-size:8px;color:rgba(255,255,255,.45);margin-top:1px;letter-spacing:.3px;text-transform:uppercase}
    .user-bar{background:#4a0f0f;padding:7px 14px;display:flex;align-items:center;justify-content:space-between;border-top:1px solid rgba(255,255,255,.08)}
    .user-bar-label{color:rgba(255,215,0,.92);font-size:10px;font-weight:700;max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .logout-btn{background:#FFD700;color:#1a0000;border:none;border-radius:6px;padding:4px 11px;font-size:10px;font-weight:800;cursor:pointer;font-family:'Poppins',sans-serif}
    .flo-footer{background:#7B1818;padding:8px 14px;text-align:center}
    .flo-footer-inner{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:3px}
    .flo-badge{display:inline-flex;align-items:center;background:#F7D117;border-radius:6px;padding:4px 12px}
    .flo-badge span{font-size:11px;font-weight:900;color:#e85d00;letter-spacing:.4px}
    .flo-tagline{font-size:9px;color:rgba(255,255,255,.65);font-weight:700;letter-spacing:.4px}
    .home-cards{background:#fdf5f5;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
    .home-card{background:#fff;border-radius:14px;padding:13px 14px;display:flex;align-items:center;gap:12px;border:1px solid #f0dede;cursor:pointer;box-shadow:0 3px 10px rgba(107,15,26,.1)}
    .home-card:active{transform:scale(.98)}
    .hc-icon{width:40px;height:40px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:19px;flex-shrink:0}
    .hc-body{flex:1;min-width:0}
    .hc-title{font-size:13px;font-weight:800;color:#1a0000}
    .hc-desc{font-size:9px;color:#666;font-weight:600;margin:2px 0 5px}
    .hc-pills{display:flex;flex-wrap:wrap;gap:3px}
    .hc-pill{font-size:8.5px;font-weight:700;padding:2px 7px;border-radius:10px}
    .hc-arrow{color:#c9a0a0;font-size:18px;font-weight:700;flex-shrink:0}
    .sec-hdr{padding:8px 16px 7px;text-align:center}
    .sec-title{font-size:15px;font-weight:900;color:#FFD700;letter-spacing:.4px}
    .sec-sub{font-size:9px;font-weight:800;color:rgba(255,255,255,.9);margin-top:2px;letter-spacing:.6px;text-transform:uppercase}
    .back-btn{display:flex;align-items:center;gap:6px;background:none;border:none;font-weight:700;font-size:12px;cursor:pointer;padding:0;margin-bottom:12px;font-family:'Poppins',sans-serif}
    .view-tabs{display:flex;background:#e8f0fe;border-radius:8px;margin-bottom:15px;padding:4px;gap:4px}
    .tab-btn{flex:1;border:none;padding:8px 4px;font-size:10px;font-weight:700;cursor:pointer;border-radius:6px;background:transparent;text-transform:uppercase;text-align:center;font-family:'Poppins',sans-serif}
    label{font-weight:600;font-size:10px;margin-bottom:3px;display:block;text-transform:uppercase}
    input[type=text]{width:100%;padding:10px;border-radius:8px;border:1px solid #ddd;margin-bottom:12px;font-family:'Poppins',sans-serif;font-size:13px}
    .scan-box{border:2px solid #7B1818!important;background:#fff9f9;font-weight:bold}
    select{width:100%;padding:8px 4px;border-radius:6px;font-size:11px;cursor:pointer;font-family:'Poppins',sans-serif;border:1px solid #ddd}
    .pill-box{width:100%;border-radius:6px;padding:4px;min-height:40px;max-height:110px;overflow-y:auto;display:flex;flex-wrap:wrap;gap:4px}
    .pill-box.pb-zone{border:2px solid #9b59b6;background:#f4ecf7}
    .pill-box.pb-aisle{border:2px solid #e67e22;background:#fdedec}
    .pill-box.pb-remark{border:2px solid #27ae60;background:#e9f7ef}
    .pill-box.pb-shelve{border:2px solid #16a085;background:#e8f8f5}
    .pill-hint{color:#999;font-size:10px;padding:6px 4px;align-self:center;width:100%;text-align:center}
    .pill-btn{background:#fff;border:1px solid #ccc;color:#333;padding:7px 11px;font-size:11px;font-weight:700;border-radius:5px;cursor:pointer;min-width:40px;text-align:center;font-family:'Poppins',sans-serif}
    .pill-btn.active.pill-zone{background:#9b59b6;color:#fff;border-color:#9b59b6}
    .pill-btn.active.pill-aisle{background:#e67e22;color:#fff;border-color:#e67e22}
    .pill-btn.active.pill-remark{background:#27ae60;color:#fff;border-color:#27ae60}
    .pill-btn.active.pill-shelve{background:#16a085;color:#fff;border-color:#16a085}
    .card{background:#fff;border-left:5px solid #1a73e8;padding:12px;margin-bottom:10px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,.05)}
    .status-tag{font-size:10px;font-weight:700;float:right;padding:2px 6px;border-radius:4px;text-transform:uppercase}
    .st-free{background:#e6f4ea;color:#1e8e3e}
    .st-partial{background:#fff4e5;color:#e67e22}
    .st-semi{background:#f4ece8;color:#7e5233}
    .st-full{background:#fce8e6;color:#c0392b}
    .product-details{background:#f8f9fa;border:1px dashed #ccc;padding:6px 10px;margin-top:6px;border-radius:6px;font-size:11px}
    .pd-grid{display:flex;gap:10px;align-items:flex-start}
    .pd-product{flex:1.2;min-width:0;color:#333;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-word}
    .pd-meta{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
    .ean-highlight{font-weight:bold;color:#6a1b9a;background:#f3e5f5;padding:1px 5px;border-radius:4px}
    .qty-highlight{font-weight:bold;color:#1a73e8;background:#e8f0fe;padding:1px 5px;border-radius:4px;margin-left:2px}
    .card-lbh{border-left-color:#27ae60}
    .st-lbh-free{background:#e6f4ea;color:#1e8e3e}
    .st-lbh-semi{background:#e8f0fe;color:#1a73e8}
    .st-lbh-partial{background:#fff4e5;color:#e67e22}
    .st-lbh-full{background:#fce8e6;color:#c0392b}
    .lbh-bar-bg{background:#e0e0e0;border-radius:6px;height:10px;width:100%;overflow:hidden;margin-top:7px}
    .lbh-bar-fill{height:10px;border-radius:6px}
    .lbh-stats{display:flex;justify-content:space-between;font-size:10px;font-weight:700;margin-top:3px}
    .btn-container{display:flex;gap:5px;margin-top:10px}
    .btn-action{flex:1;border:none;padding:8px 4px;border-radius:5px;font-size:10px;font-weight:600;cursor:pointer;color:#fff;font-family:'Poppins',sans-serif}
    .btn-red{background:#c0392b}.btn-dark{background:#2c3e50}.btn-orange{background:#e67e22}.btn-green{background:#27ae60}
    .btn-itempick{background:#2980b9}.btn-itemput{background:#16a085}
    .gtl-sub{background:#fff8ee;border:1px dashed #e67e22;border-radius:7px;padding:6px 6px 2px;margin-top:4px}
    .accordion-hdr{cursor:pointer;padding:9px 12px;border-radius:7px;margin:6px 10px 0;display:flex;align-items:center;justify-content:space-between;user-select:none}
    .accordion-hdr-p1{background:#f5ecec;border:1px solid #e8c8c8}
    .accordion-hdr-p2{background:#fff3ef;border:1px dashed #e8c8c8;margin-top:10px}
    .accordion-hdr-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.5px}
    .section-box{background:#fafafa;padding:10px;border-radius:8px;border:1px solid #e0e0e0;margin-bottom:10px}
    .section-title{font-size:11px;font-weight:bold;color:#555;display:block;margin-bottom:6px;text-transform:uppercase;text-align:center}
    .summary-table{width:100%;border-collapse:collapse;margin-top:5px;font-size:11px}
    .summary-table th{color:#fff;font-weight:bold;padding:8px 3px;text-align:center}
    .summary-table td{padding:8px 4px;text-align:center;border-bottom:1px solid #ddd;font-weight:600}
    .badge-counter{display:inline-block;padding:2px 6px;border-radius:4px;color:#fff;font-size:11px;min-width:24px;text-align:center;font-weight:bold}
    .bg-free{background:#27ae60}.bg-partial{background:#e67e22}.bg-semi{background:#7e5233}.bg-full{background:#c0392b}.bg-rowtotal{background:#443a3a}.bg-today{background:#1a73e8}
    .action-pill{display:inline-block;padding:3px 8px;border-radius:4px;color:#fff;font-size:10px;font-weight:700}
    .ap-gtl{background:#e67e22}.ap-putaway{background:#2c3e50}.ap-free{background:#27ae60}.ap-fullx{background:#c0392b}.ap-itempick{background:#2980b9}.ap-itemput{background:#16a085}.ap-other{background:#777}
    .activity-row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:7px 8px;border-bottom:1px solid #eee}
    .sync-btn{background:#555;color:#fff;border:none;padding:8px;border-radius:6px;font-size:11px;font-weight:bold;width:100%;cursor:pointer;font-family:'Poppins',sans-serif;margin-top:6px}
    .loading-text{text-align:center;font-weight:bold;color:#1a73e8;animation:blink 1.2s infinite;padding:20px 0}
    @keyframes blink{0%{opacity:.4}50%{opacity:1}100%{opacity:.4}}
    .step-floor{border:2px solid #3498db!important;background:#ebf5fb!important}
    .label-floor{color:#3498db!important}.label-zone{color:#9b59b6!important}.label-aisle{color:#e67e22!important}
    .label-remark{color:#27ae60!important}.label-shelve{color:#16a085!important}
    .dl-btn{background:#1a1a2e;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px;margin-left:auto}
    .row-title{font-weight:bold;background:#f5f5f5;color:#333}
  </style>
</head>
<body class="login-active">

<!-- LOGIN -->
<div id="login-page">
  <div class="lo-overlay">
    <div>
      <div class="lo-brand-line1">🚀 One Step Towards Automation</div>
      <div class="lo-brand-line2">🏛️ DIC • S.H • Bijwasan • Delhi</div>
    </div>
    <div class="lo-card">
      <div class="lo-icon">🔐</div>
      <div class="lo-title">Casper Authentication</div>
      <div class="lo-sub">Scan or enter your Casper ID to continue</div>
      <label class="lo-label">Casper I.D</label>
      <input type="text" id="casperId" class="lo-input" placeholder="Scan or type Casper ID..." autocomplete="off" autofocus>
      <button id="loginBtn" class="lo-btn" onclick="checkLogin()">🔓 Login</button>
      <div id="loginMsg" class="lo-msg"></div>
    </div>
    <div class="lo-footer">
      <span class="lo-footer-badge">⚡ ANJANEYA-LIVE GUIDED PUTAWAY V3.O</span>
      <span style="color:rgba(255,255,255,.65);font-size:9.5px;font-weight:700;">Developed by DIC - IMT TEAM</span>
    </div>
  </div>
</div>

<!-- HOME -->
<div id="home-page" class="container" style="display:none">
  <div class="maroon-hdr">
    <div class="gold-stripe"></div>
    <div class="hdr-inner">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span class="hdr-h1">🚛 ANJANEYA-LIVE GUIDED PUTAWAY V3.O</span>
        <span id="statTime" style="font-size:9px;color:rgba(255,255,255,.9);font-weight:800;background:rgba(255,255,255,.15);border-radius:5px;padding:2px 8px;margin-left:auto">—</span>
      </div>
      <div class="hdr-stats">
        <div class="hdr-stat"><div id="statQty" class="hdr-sv">—</div><div class="hdr-sl">Total Qty in Stock</div></div>
        <div class="hdr-stat"><div id="statLoc" class="hdr-sv">—</div><div class="hdr-sl">Total Locations</div></div>
        <div class="hdr-stat"><div id="statFree" class="hdr-sv">—</div><div class="hdr-sl">Free Bins Available</div></div>
        <div class="hdr-stat"><div id="statDate" class="hdr-sv">—</div><div class="hdr-sl">Last Refresh</div></div>
      </div>
    </div>
  </div>
  <div class="home-cards">
    <div class="home-card" style="border-left:6px solid #7B1818" onclick="openSection('scan')">
      <div class="hc-icon" style="background:#fce8e9">🔍</div>
      <div class="hc-body">
        <div class="hc-title">Item Scan</div>
        <div class="hc-desc">Locate any shelf instantly</div>
        <div class="hc-pills">
          <span class="hc-pill" style="background:#e8f0fe;color:#0c447c">Location</span>
          <span class="hc-pill" style="background:#f4ecf7;color:#3c3489">FSN</span>
          <span class="hc-pill" style="background:#fff3e0;color:#633806">WID</span>
          <span class="hc-pill" style="background:#e1f5ee;color:#085041">EAN</span>
        </div>
      </div>
      <div class="hc-arrow">›</div>
    </div>
    <div class="home-card" style="border-left:6px solid #A52A2A" onclick="openSection('qty')">
      <div class="hc-icon" style="background:#fff3e0">📦</div>
      <div class="hc-body">
        <div class="hc-title">GTL / Putaway — Qty Basis</div>
        <div class="hc-desc">By unit count occupancy</div>
        <div class="hc-pills">
          <span class="hc-pill" style="background:#e6f4ea;color:#27500a">Free 0</span>
          <span class="hc-pill" style="background:#fff3e0;color:#633806">Partial 1–10</span>
          <span class="hc-pill" style="background:#f4ece8;color:#5a3920">Semi 11–30</span>
          <span class="hc-pill" style="background:#fce8e6;color:#791f1f">Full 31+</span>
        </div>
      </div>
      <div class="hc-arrow">›</div>
    </div>
    <div class="home-card" style="border-left:6px solid #C0392B" onclick="openSection('lbh')">
      <div class="hc-icon" style="background:#e6f4ea">📐</div>
      <div class="hc-body">
        <div class="hc-title">Putaway — LBH / Cufeet</div>
        <div class="hc-desc">By volume utilization %</div>
        <div class="hc-pills">
          <span class="hc-pill" style="background:#e6f4ea;color:#27500a">Free 0%</span>
          <span class="hc-pill" style="background:#e8f0fe;color:#0c447c">Semi 1–30%</span>
          <span class="hc-pill" style="background:#fff3e0;color:#633806">Partial 31–70%</span>
          <span class="hc-pill" style="background:#fce8e6;color:#791f1f">Full 71%+</span>
        </div>
      </div>
      <div class="hc-arrow">›</div>
    </div>
  </div>
  <div class="user-bar"><div class="user-bar-label" id="homeUser">👤 —</div><button class="logout-btn" onclick="logout()">⏻ Logout</button></div>
  <div class="flo-footer"><div class="flo-footer-inner"><div class="flo-badge"><span>Flipkart Minutes</span></div><span style="font-size:10px;color:rgba(255,255,255,.85);font-weight:800">· DIC IMT TEAM</span></div><div class="flo-tagline">One Step Towards Automation</div></div>
</div>

<!-- SCAN PAGE -->
<div id="scan-page" class="container" style="display:none">
  <div class="maroon-hdr"><div class="gold-stripe"></div><div class="sec-hdr"><div class="sec-title">🔍 Item Scan</div><div class="sec-sub">ANJANEYA-LIVE GUIDED PUTAWAY V3.O</div></div></div>
  <div style="padding:8px 14px">
    <button class="back-btn" onclick="goHome()" style="color:#7B1818">‹ Back</button>
    <label style="color:#7B1818;font-weight:800">⚡ Scan Location / FSN / WID / EAN</label>
    <div style="position:relative">
      <input type="text" id="scanInput" class="scan-box" placeholder="Scan or type..." oninput="scanDebounced()" style="padding-right:52px">
      <button onclick="toggleScanner()" id="camBtn" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:#7B1818;border:none;border-radius:6px;cursor:pointer;padding:6px 10px;font-size:18px" title="Camera Scanner">📷</button>
    </div>
    <div id="scannerBox" style="display:none;margin-top:8px;border-radius:10px;overflow:hidden;position:relative">
      <video id="camVideo" style="width:100%;display:block" playsinline autoplay muted></video>
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:70%;height:2px;background:#FFD700;box-shadow:0 0 8px #FFD700"></div>
      <button onclick="toggleScanner()" style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,0.6);color:#fff;border:none;border-radius:50%;width:30px;height:30px;font-size:16px;cursor:pointer">✕</button>
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <span id="scanCount" style="font-size:12px;color:#666"></span>
      <a class="dl-btn" href="/download">⬇ Download CSV</a>
    </div>
    <div id="listScan"></div>
  </div>
  <div class="user-bar"><div class="user-bar-label" id="scanUser">👤 —</div><button class="logout-btn" onclick="logout()">⏻ Logout</button></div>
  <div class="flo-footer"><div class="flo-footer-inner"><div class="flo-badge"><span>Flipkart Minutes</span></div><span style="font-size:10px;color:rgba(255,255,255,.85);font-weight:800">· DIC IMT TEAM</span></div><div class="flo-tagline">One Step Towards Automation</div></div>
</div>

<!-- QTY PAGE -->
<div id="qty-page" class="container" style="display:none">
  <div style="background:#8B1A1A"><div class="gold-stripe"></div><div class="sec-hdr"><div class="sec-title">📦 GTL / Putaway — Qty Basis</div></div></div>
  <div style="padding:6px 14px 0"><button class="back-btn" onclick="goHome()" style="color:#8B1A1A">‹ Back</button></div>
  <div style="padding:0 10px">
    <div class="view-tabs" style="background:#f5ecec">
      <button id="qTab1" class="tab-btn" onclick="switchQTab('putaway')" style="background:#8B1A1A;color:#fff">📍 GTL/Putaway</button>
      <button id="qTab2" class="tab-btn" onclick="switchQTab('action')" style="color:#8B1A1A">📊 Live Action</button>
      <button id="qTab3" class="tab-btn" onclick="switchQTab('bins')" style="color:#8B1A1A">🗄️ Bins</button>
    </div>
  </div>
  <div id="qPanePutaway">
    <div class="accordion-hdr accordion-hdr-p1" onclick="qTogglePart(1)" style="margin:4px 10px 0">
      <span class="accordion-hdr-title" style="color:#8B1A1A">📍 Part 1 — Floor / Zone / Aisle</span>
      <span id="qP1Arrow" style="font-size:14px;font-weight:700;color:#8B1A1A">▼</span>
    </div>
    <div id="qPart1">
      <div style="padding:0 10px">
        <div style="margin-bottom:8px"><label class="label-floor">Step 1: Floor</label><select id="qFloor" class="step-floor" onchange="qFloorChange()"></select></div>
        <div style="margin-bottom:8px"><label class="label-zone">Step 2: Zone</label><div id="qZoneBox" class="pill-box pb-zone"><span class="pill-hint">Select Floor first</span></div></div>
        <div style="margin-bottom:8px"><label class="label-aisle">Step 3: Aisle</label><div id="qAisleBox" class="pill-box pb-aisle"><span class="pill-hint">Select Zone first</span></div></div>
        <div style="margin-bottom:8px"><label class="label-remark">Step 4: Remark</label><div id="qRemarkBox" class="pill-box pb-remark"></div></div>
      </div>
      <div id="listQty" style="padding:0 10px"></div>
    </div>
    <div class="accordion-hdr accordion-hdr-p2" onclick="qTogglePart(2)" style="margin:8px 10px 0">
      <span class="accordion-hdr-title" style="color:#8B1A1A">📦 Part 2 — Filter by Shelve Type</span>
      <span id="qP2Arrow" style="font-size:14px;font-weight:700;color:#8B1A1A">▶</span>
    </div>
    <div id="qPart2" style="display:none;padding:0 10px">
      <div style="margin-bottom:6px"><label class="label-floor">Step 1: Floor</label><select id="qFloor2" class="step-floor" onchange="qFloor2Change()"></select></div>
      <div style="margin-bottom:8px"><label class="label-shelve">Step 2: Shelve Type</label><div id="qShelveBox" class="pill-box pb-shelve"><span class="pill-hint">Select Floor first</span></div></div>
      <div style="margin-bottom:8px"><label class="label-remark">Step 3: Remark</label><div id="qRemark2Box" class="pill-box pb-remark"></div></div>
      <div id="listQty2"></div>
    </div>
  </div>
  <div id="qPaneAction" style="display:none;padding:0 10px"><div id="qActionInner"></div><button class="sync-btn" onclick="loadData(true)">🔄 Force Sync</button></div>
  <div id="qPaneBins" style="display:none;padding:0 10px"><div id="qBinsInner"></div><button class="sync-btn" onclick="loadData(true)">🔄 Force Sync</button></div>
  <div class="user-bar" style="margin-top:10px"><div class="user-bar-label" id="qtyUser">👤 —</div><button class="logout-btn" onclick="logout()">⏻ Logout</button></div>
  <div class="flo-footer"><div class="flo-footer-inner"><div class="flo-badge"><span>Flipkart Minutes</span></div><span style="font-size:10px;color:rgba(255,255,255,.85);font-weight:800">· DIC IMT TEAM</span></div><div class="flo-tagline">One Step Towards Automation</div></div>
</div>

<!-- LBH PAGE -->
<div id="lbh-page" class="container" style="display:none">
  <div style="background:#A52A2A"><div class="gold-stripe"></div><div class="sec-hdr"><div class="sec-title">📐 Putaway — LBH / Cufeet</div></div></div>
  <div style="padding:6px 14px 0"><button class="back-btn" onclick="goHome()" style="color:#A52A2A">‹ Back</button></div>
  <div style="padding:0 10px">
    <div class="view-tabs" style="background:#f5ecec">
      <button id="lTab1" class="tab-btn" onclick="switchLTab('putaway')" style="background:#A52A2A;color:#fff">📍 Putaway LBH</button>
      <button id="lTab2" class="tab-btn" onclick="switchLTab('action')" style="color:#A52A2A">📊 Live Action</button>
      <button id="lTab3" class="tab-btn" onclick="switchLTab('bins')" style="color:#A52A2A">🗄️ Bins</button>
    </div>
  </div>
  <div id="lPanePutaway">
    <div class="accordion-hdr accordion-hdr-p1" onclick="lTogglePart(1)" style="margin:4px 10px 0">
      <span class="accordion-hdr-title" style="color:#A52A2A">📍 Part 1 — Floor / Zone / Aisle</span>
      <span id="lP1Arrow" style="font-size:14px;font-weight:700;color:#A52A2A">▼</span>
    </div>
    <div id="lPart1">
      <div style="padding:0 10px">
        <div style="margin-bottom:6px"><label class="label-floor">Step 1: Floor</label><select id="lFloor" class="step-floor" onchange="lFloorChange()"></select></div>
        <div style="margin-bottom:8px"><label class="label-zone">Step 2: Zone</label><div id="lZoneBox" class="pill-box pb-zone"><span class="pill-hint">Select Floor first</span></div></div>
        <div style="margin-bottom:8px"><label class="label-aisle">Step 3: Aisle</label><div id="lAisleBox" class="pill-box pb-aisle"><span class="pill-hint">Select Zone first</span></div></div>
        <div style="margin-bottom:8px"><label class="label-remark">Step 4: Space</label><div id="lSpaceBox" class="pill-box pb-remark"></div></div>
      </div>
      <div id="listLbh" style="padding:0 10px"></div>
    </div>
    <div class="accordion-hdr accordion-hdr-p2" onclick="lTogglePart(2)" style="margin:8px 10px 0">
      <span class="accordion-hdr-title" style="color:#A52A2A">📐 Part 2 — By Shelve Type (LBH)</span>
      <span id="lP2Arrow" style="font-size:14px;font-weight:700;color:#A52A2A">▶</span>
    </div>
    <div id="lPart2" style="display:none;padding:0 10px">
      <div style="margin-bottom:6px"><label class="label-floor">Step 1: Floor</label><select id="lFloor2" class="step-floor" onchange="lFloor2Change()"></select></div>
      <div style="margin-bottom:8px"><label class="label-shelve">Step 2: Shelve Type</label><div id="lShelveBox" class="pill-box pb-shelve"><span class="pill-hint">Select Floor first</span></div></div>
      <div style="margin-bottom:8px"><label class="label-remark">Step 3: Space %</label><div id="lSpace2Box" class="pill-box pb-remark"></div></div>
      <div id="listLbh2"></div>
    </div>
  </div>
  <div id="lPaneAction" style="display:none;padding:0 10px"><div id="lActionInner"></div><button class="sync-btn" onclick="loadData(true)">🔄 Force Sync</button></div>
  <div id="lPaneBins" style="display:none;padding:0 10px"><div id="lBinsInner"></div><button class="sync-btn" onclick="loadData(true)">🔄 Force Sync</button></div>
  <div class="user-bar" style="margin-top:10px"><div class="user-bar-label" id="lbhUser">👤 —</div><button class="logout-btn" onclick="logout()">⏻ Logout</button></div>
  <div class="flo-footer"><div class="flo-footer-inner"><div class="flo-badge"><span>Flipkart Minutes</span></div><span style="font-size:10px;color:rgba(255,255,255,.85);font-weight:800">· DIC IMT TEAM</span></div><div class="flo-tagline">One Step Towards Automation</div></div>
</div>

<script>
let USER='',USER_NAME='',RAW=[],LOCS=[],METRICS=[],ACTIONS=null;
let _scanTimer=null,_lastLoad=0;
let qZone='',qAisle='',qRemark='',qShelve='',qRemark2='';
let lZone='',lAisle='',lSpace='',lShelve='',lSpace2='';
let qPart=1,lPart=1;
const QTY_REMARKS=[{v:'',l:'All'},{v:'Free Shelve',l:'Free (0)'},{v:'Partial Space',l:'Partial 1-10'},{v:'Semi Space',l:'Semi 11-30'},{v:'Full Space',l:'Full 31+'}];
const LBH_SPACES=[{v:'',l:'All'},{v:'LBH-Free',l:'Free 0%'},{v:'LBH-Semi',l:'Semi 1-30%'},{v:'LBH-Partial',l:'Partial 31-70%'},{v:'LBH-Full',l:'Full 71%+'}];

function esc(s){return String(s).replace(/'/g,"&#39;")}

document.getElementById('casperId').addEventListener('keydown',e=>{if(e.key==='Enter')checkLogin()});

async function checkLogin(){
  const id=document.getElementById('casperId').value.trim();
  const btn=document.getElementById('loginBtn'),msg=document.getElementById('loginMsg');
  if(!id){msg.textContent='Enter Casper ID';return}
  btn.disabled=true;btn.textContent='⏳ Verifying…';msg.textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    const d=await r.json();
    if(d.ok){
      USER=id;USER_NAME=d.name||id;
      sessionStorage.setItem('did',id);sessionStorage.setItem('dname',USER_NAME);
      document.getElementById('login-page').style.display='none';
      document.body.classList.remove('login-active');document.body.style.padding='10px';
      updateBars();showHomePage();loadData(true);
    } else {msg.textContent=d.msg||'Invalid ID';}
  }catch(e){msg.textContent='Connection error'}
  btn.disabled=false;btn.textContent='🔓 Login';
}

function logout(){
  if(!confirm('Logout?'))return;
  sessionStorage.clear();USER='';USER_NAME='';RAW=[];LOCS=[];METRICS=[];ACTIONS=null;
  document.getElementById('login-page').style.display='flex';
  document.body.classList.add('login-active');document.body.style.padding='0';
  document.getElementById('casperId').value='';
  document.getElementById('loginMsg').textContent='';
}

function updateBars(){
  const lbl='👤 '+USER_NAME+' ('+USER+')';
  ['homeUser','scanUser','qtyUser','lbhUser'].forEach(id=>{const el=document.getElementById(id);if(el)el.textContent=lbl});
}

const PAGES=['login-page','home-page','scan-page','qty-page','lbh-page'];
function showPage(id){PAGES.forEach(p=>{const el=document.getElementById(p);if(el)el.style.display=(p===id?'block':'none')})}
function showHomePage(){showPage('home-page')}
function goHome(){showHomePage()}
function openSection(s){
  if(s==='scan'){showPage('scan-page');setTimeout(()=>{const el=document.getElementById('scanInput');if(el)el.focus()},100)}
  else if(s==='qty'){showPage('qty-page');switchQTab('putaway');populateFloors('q')}
  else if(s==='lbh'){showPage('lbh-page');switchLTab('putaway');populateFloors('l')}
}

async function loadData(force){
  const now=Date.now();
  if(!force&&now-_lastLoad<8000)return;
  _lastLoad=now;
  try{
    const r=await fetch('/api/data');
    const d=await r.json();
    RAW=d.rawData||[];LOCS=d.pureLocationsData||[];METRICS=d.summaryMetrics||[];ACTIONS=d.actionSummary||null;
    const qty=Number(d.totalQty||0).toLocaleString('en-IN');
    const ref=d.lastRefresh||'—';
    const spIdx=ref.lastIndexOf(' ');
    const timePart=spIdx>=0?ref.substring(spIdx+1):'';
    document.getElementById('statQty').textContent=qty;
    document.getElementById('statTime').textContent=timePart+' ✓';
    document.getElementById('statDate').textContent=ref;
    const free=METRICS.reduce((s,r)=>s+(r.free||0),0);
    const total=METRICS.reduce((s,r)=>s+(r.free||0)+(r.partial||0)+(r.semi||0)+(r.full||0),0);
    document.getElementById('statLoc').textContent=total.toLocaleString('en-IN');
    document.getElementById('statFree').textContent=free.toLocaleString('en-IN');
  }catch(e){console.error('load error',e)}
}
setInterval(()=>loadData(),300000);

function scanDebounced(){clearTimeout(_scanTimer);_scanTimer=setTimeout(doScan,220)}
async function doScan(){
  const q=document.getElementById('scanInput').value.trim();
  const div=document.getElementById('listScan');
  const cnt=document.getElementById('scanCount');
  if(q.length<3){div.innerHTML='';cnt.textContent='';return}
  const ql=q.toUpperCase();
  const hits=RAW.filter(d=>{
    if(d.label&&d.label.toUpperCase()===ql)return true;
    if(d.fsn&&d.fsn.toUpperCase()===ql)return true;
    if(d.wid&&d.wid.toUpperCase()===ql)return true;
    if(d.eans&&d.eans.some(e=>String(e).toUpperCase()===ql))return true;
    return false;
  });
  cnt.textContent=hits.length+' result(s)';
  if(!hits.length){div.innerHTML='<div style="color:#c0392b;text-align:center;padding:20px;font-weight:bold">No results found</div>';return}
  div.innerHTML=hits.map(i=>qtyCard(i,'listScan',true)).join('');
}

function switchQTab(t){
  ['putaway','action','bins'].forEach((n,i)=>{
    document.getElementById('qTab'+(i+1)).style.cssText=t===n?'background:#8B1A1A;color:#fff':'color:#8B1A1A';
    document.getElementById('qPane'+n.charAt(0).toUpperCase()+n.slice(1)).style.display=t===n?'block':'none';
  });
  if(t==='action')renderAction('qActionInner');
  if(t==='bins')renderBins('qBinsInner');
}
function qTogglePart(p){
  qPart=p;
  document.getElementById('qPart1').style.display=p===1?'block':'none';
  document.getElementById('qPart2').style.display=p===2?'block':'none';
  document.getElementById('qP1Arrow').textContent=p===1?'▼':'▶';
  document.getElementById('qP2Arrow').textContent=p===2?'▼':'▶';
}
function switchLTab(t){
  ['putaway','action','bins'].forEach((n,i)=>{
    document.getElementById('lTab'+(i+1)).style.cssText=t===n?'background:#A52A2A;color:#fff':'color:#A52A2A';
    document.getElementById('lPane'+n.charAt(0).toUpperCase()+n.slice(1)).style.display=t===n?'block':'none';
  });
  if(t==='action')renderAction('lActionInner');
  if(t==='bins')renderLbhBins('lBinsInner');
}
function lTogglePart(p){
  lPart=p;
  document.getElementById('lPart1').style.display=p===1?'block':'none';
  document.getElementById('lPart2').style.display=p===2?'block':'none';
  document.getElementById('lP1Arrow').textContent=p===1?'▼':'▶';
  document.getElementById('lP2Arrow').textContent=p===2?'▼':'▶';
}

function populateFloors(prefix){
  const floors=[...new Set(LOCS.map(d=>d.floor))].filter(Boolean).sort();
  ['','2'].forEach(sfx=>{
    const sel=document.getElementById(prefix+'Floor'+sfx);if(!sel)return;
    const old=sel.value;
    sel.innerHTML='<option value="">-- Select --</option>';
    floors.forEach(f=>sel.innerHTML+=`<option value="${f}">${f}</option>`);
    if(old)sel.value=old;
  });
  renderPills(prefix);
}
function renderPills(prefix){
  if(prefix==='q'){renderQZone();renderQAisle();renderQRemark();renderQShelve();renderQRemark2()}
  else{renderLZone();renderLAisle();renderLSpace();renderLShelve();renderLSpace2()}
}

function renderOptionPills(boxId,opts,selected,cls,fn){
  const box=document.getElementById(boxId);if(!box)return;
  box.innerHTML=opts.map(o=>`<button type="button" class="pill-btn ${cls}${o.v===selected?' active':''}" onclick="${fn}('${esc(o.v)}')">${o.l}</button>`).join('');
}
function renderQRemark(){renderOptionPills('qRemarkBox',QTY_REMARKS,qRemark,'pill-remark','qSelRemark')}
function qSelRemark(v){qRemark=v;renderQRemark();qShow()}
function renderQZone(){
  const box=document.getElementById('qZoneBox');if(!box)return;
  const f=document.getElementById('qFloor').value;
  if(!f){box.innerHTML='<span class="pill-hint">Select Floor first</span>';return}
  const zones=[...new Set(LOCS.filter(d=>d.floor===f).map(d=>d.pz))].filter(Boolean).sort();
  box.innerHTML=zones.map(z=>`<button type="button" class="pill-btn pill-zone${z===qZone?' active':''}" onclick="qSelZone('${esc(z)}')">${z}</button>`).join('')||'<span class="pill-hint">No zones</span>';
}
function qSelZone(z){qZone=z;qAisle='';renderQZone();renderQAisle();document.getElementById('listQty').innerHTML=''}
function renderQAisle(){
  const box=document.getElementById('qAisleBox');if(!box)return;
  const f=document.getElementById('qFloor').value;
  if(!f||!qZone){box.innerHTML='<span class="pill-hint">Select Zone first</span>';return}
  const aisles=[...new Set(LOCS.filter(d=>d.floor===f&&d.pz===qZone).map(d=>d.aisle))].filter(Boolean).sort();
  box.innerHTML=aisles.map(a=>`<button type="button" class="pill-btn pill-aisle${a===qAisle?' active':''}" onclick="qSelAisle('${esc(a)}')">${a}</button>`).join('')||'<span class="pill-hint">No aisles</span>';
}
function qSelAisle(a){qAisle=a;renderQAisle();qShow()}
function qFloorChange(){qZone='';qAisle='';renderQZone();renderQAisle();renderQRemark();document.getElementById('listQty').innerHTML=''}
function qShow(){
  const f=document.getElementById('qFloor').value;
  if(!f||!qZone||!qAisle){document.getElementById('listQty').innerHTML='';return}
  const filtered=LOCS.filter(d=>d.floor===f&&d.pz===qZone&&d.aisle===qAisle&&(!qRemark||d.remark.includes(qRemark)));
  document.getElementById('listQty').innerHTML=filtered.map(i=>qtyCard(i,'listQty',false)).join('')||'<div style="color:#c0392b;text-align:center;padding:20px;font-weight:bold">No results</div>';
}
function renderQShelve(){
  const box=document.getElementById('qShelveBox');if(!box)return;
  const f=document.getElementById('qFloor2').value;
  if(!f){box.innerHTML='<span class="pill-hint">Select Floor first</span>';return}
  const types=[...new Set(LOCS.filter(d=>d.floor===f).map(d=>d.type))].filter(t=>t&&t!=='FLO Feed').sort();
  box.innerHTML=types.map(t=>`<button type="button" class="pill-btn pill-shelve${t===qShelve?' active':''}" onclick="qSelShelve('${esc(t)}')">${t}</button>`).join('')||'<span class="pill-hint">No types</span>';
}
function qSelShelve(t){qShelve=t;renderQShelve();qShow2()}
function renderQRemark2(){renderOptionPills('qRemark2Box',QTY_REMARKS,qRemark2,'pill-remark','qSelRemark2')}
function qSelRemark2(v){qRemark2=v;renderQRemark2();qShow2()}
function qFloor2Change(){qShelve='';renderQShelve();renderQRemark2();document.getElementById('listQty2').innerHTML=''}
function qShow2(){
  const f=document.getElementById('qFloor2').value;
  if(!f||!qShelve){document.getElementById('listQty2').innerHTML='';return}
  const filtered=LOCS.filter(d=>d.floor===f&&d.type===qShelve&&(!qRemark2||d.remark.includes(qRemark2)));
  document.getElementById('listQty2').innerHTML=filtered.map(i=>qtyCard(i,'listQty2',false)).join('')||'<div style="color:#c0392b;text-align:center;padding:20px">No results</div>';
}

function lbhRemark(avail,qty){
  if(avail===null||avail===undefined||avail==='')return(!qty||qty===0)?'LBH-Free':'';
  const u=100-parseFloat(avail);
  if(isNaN(u)||u<=0)return'LBH-Free';if(u<=30)return'LBH-Semi';if(u<=70)return'LBH-Partial';return'LBH-Full';
}
function lbhLabel(r){const m={'LBH-Free':'Free (0%)','LBH-Semi':'Semi (1-30%)','LBH-Partial':'Partial (31-70%)','LBH-Full':'Full (71%+)'};return m[r]||r}
function lbhTagCls(r){const m={'LBH-Free':'st-lbh-free','LBH-Semi':'st-lbh-semi','LBH-Partial':'st-lbh-partial','LBH-Full':'st-lbh-full'};return m[r]||'st-lbh-free'}
function lbhBarColor(u){if(u<=0)return'#27ae60';if(u<=30)return'#3498db';if(u<=70)return'#e67e22';return'#c0392b'}
function renderLSpace(){renderOptionPills('lSpaceBox',LBH_SPACES,lSpace,'pill-remark','lSelSpace')}
function lSelSpace(v){lSpace=v;renderLSpace();lShow()}
function renderLZone(){
  const box=document.getElementById('lZoneBox');if(!box)return;
  const f=document.getElementById('lFloor').value;
  if(!f){box.innerHTML='<span class="pill-hint">Select Floor first</span>';return}
  const zones=[...new Set(LOCS.filter(d=>d.floor===f).map(d=>d.pz))].filter(Boolean).sort();
  box.innerHTML=zones.map(z=>`<button type="button" class="pill-btn pill-zone${z===lZone?' active':''}" onclick="lSelZone('${esc(z)}')">${z}</button>`).join('')||'<span class="pill-hint">No zones</span>';
}
function lSelZone(z){lZone=z;lAisle='';renderLZone();renderLAisle();document.getElementById('listLbh').innerHTML=''}
function renderLAisle(){
  const box=document.getElementById('lAisleBox');if(!box)return;
  const f=document.getElementById('lFloor').value;
  if(!f||!lZone){box.innerHTML='<span class="pill-hint">Select Zone first</span>';return}
  const aisles=[...new Set(LOCS.filter(d=>d.floor===f&&d.pz===lZone).map(d=>d.aisle))].filter(Boolean).sort();
  box.innerHTML=aisles.map(a=>`<button type="button" class="pill-btn pill-aisle${a===lAisle?' active':''}" onclick="lSelAisle('${esc(a)}')">${a}</button>`).join('')||'<span class="pill-hint">No aisles</span>';
}
function lSelAisle(a){lAisle=a;renderLAisle();lShow()}
function lFloorChange(){lZone='';lAisle='';renderLZone();renderLAisle();renderLSpace();document.getElementById('listLbh').innerHTML=''}
function lShow(){
  const f=document.getElementById('lFloor').value;
  if(!f||!lZone||!lAisle){document.getElementById('listLbh').innerHTML='';return}
  let filtered=LOCS.filter(d=>d.floor===f&&d.pz===lZone&&d.aisle===lAisle);
  if(lSpace)filtered=filtered.filter(d=>lbhRemark(d.availCuftPct,d.totalQty)===lSpace);
  document.getElementById('listLbh').innerHTML=filtered.map(i=>lbhCard(i,'listLbh')).join('')||'<div style="color:#c0392b;text-align:center;padding:20px">No results</div>';
}
function renderLShelve(){
  const box=document.getElementById('lShelveBox');if(!box)return;
  const f=document.getElementById('lFloor2').value;
  if(!f){box.innerHTML='<span class="pill-hint">Select Floor first</span>';return}
  const types=[...new Set(LOCS.filter(d=>d.floor===f).map(d=>d.type))].filter(t=>t&&t!=='FLO Feed').sort();
  box.innerHTML=types.map(t=>`<button type="button" class="pill-btn pill-shelve${t===lShelve?' active':''}" onclick="lSelShelve('${esc(t)}')">${t}</button>`).join('')||'<span class="pill-hint">No types</span>';
}
function lSelShelve(t){lShelve=t;renderLShelve();lShow2()}
function renderLSpace2(){renderOptionPills('lSpace2Box',LBH_SPACES,lSpace2,'pill-remark','lSelSpace2')}
function lSelSpace2(v){lSpace2=v;renderLSpace2();lShow2()}
function lFloor2Change(){lShelve='';renderLShelve();renderLSpace2();document.getElementById('listLbh2').innerHTML=''}
function lShow2(){
  const f=document.getElementById('lFloor2').value;
  if(!f||!lShelve){document.getElementById('listLbh2').innerHTML='';return}
  let filtered=LOCS.filter(d=>d.floor===f&&d.type===lShelve);
  if(lSpace2)filtered=filtered.filter(d=>lbhRemark(d.availCuftPct,d.totalQty)===lSpace2);
  document.getElementById('listLbh2').innerHTML=filtered.map(i=>lbhCard(i,'listLbh2')).join('')||'<div style="color:#c0392b;text-align:center;padding:20px">No results</div>';
}

function qtyTagClass(r){if(r.includes('Free'))return'st-free';if(r.includes('Partial'))return'st-partial';if(r.includes('Semi'))return'st-semi';return'st-full'}
function qtyRangeLabel(r){if(r.includes('Free'))return'0 Qty';if(r.includes('Partial'))return'1-10';if(r.includes('Semi'))return'11-30';if(r.includes('Full'))return'31+';return''}

function qtyCard(i,cid,showProduct){
  const tc=qtyTagClass(i.remark),range=qtyRangeLabel(i.remark);
  let prod='';
  if(showProduct&&i.totalQty>0&&i.rawProd){
    const cleanProd=String(i.rawProd).replace(/\{[^}]*\}/g,' ').replace(/\s+/g,' ').trim();
    const eanDisplay=(i.eans&&i.eans.length>1)?i.eans.join(', '):(i.ean||'N/A');
    prod=`<div class="product-details"><div class="pd-grid">
      <div class="pd-product"><strong>Product:</strong> ${cleanProd||'N/A'}</div>
      <div class="pd-meta">
        <div><strong>EAN:</strong> <span class="ean-highlight">${eanDisplay}</span></div>
        <div><strong>FSN:</strong> ${i.fsn||'N/A'}</div>
        <div><strong>WID:</strong> ${i.wid||'N/A'} — <span class="qty-highlight">Qty: ${i.itemQty||0}</span></div>
      </div></div></div>`;
  }
  let btns='';
  if(i.remark.includes('Free')){
    btns=`<div class="btn-container"><button class="btn-action btn-dark" onclick="doAction(this,'${esc(i.label)}','Putaway Done','${cid}')">Putaway Done</button></div>`;
  } else if(i.remark.includes('Partial')||i.remark.includes('Semi')){
    btns=`<div class="btn-container">
      <button class="btn-action btn-orange" onclick="toggleGtl(this)">GTL ▼</button>
      <button class="btn-action btn-dark" onclick="doAction(this,'${esc(i.label)}','Putaway Done','${cid}')">Putaway Done</button>
    </div>
    <div class="gtl-sub" style="display:none"><div class="btn-container" style="margin-top:4px">
      <button class="btn-action btn-itempick" onclick="doAction(this,'${esc(i.label)}','Item Pick','${cid}')">📦 Item Pick</button>
      <button class="btn-action btn-itemput" onclick="doAction(this,'${esc(i.label)}','Item Put','${cid}')">📥 Item Put</button>
    </div></div>`;
  } else {
    btns=`<div class="btn-container"><button class="btn-action btn-orange" onclick="toggleGtl(this)">GTL ▼</button></div>
    <div class="gtl-sub" style="display:none"><div class="btn-container" style="margin-top:4px">
      <button class="btn-action btn-itempick" onclick="doAction(this,'${esc(i.label)}','Item Pick','${cid}')">📦 Item Pick</button>
      <button class="btn-action btn-itemput" onclick="doAction(this,'${esc(i.label)}','Item Put','${cid}')">📥 Item Put</button>
    </div></div>`;
  }
  return `<div class="card"><span class="status-tag ${tc}">${i.remark}${range?' ('+range+')':''}</span>
    <b>📍 ${i.label}</b> <small style="color:#888">${i.type||''}</small>
    <div style="font-size:12px;margin-bottom:4px">Total Qty: ${i.totalQty}</div>
    ${prod}${btns}</div>`;
}

function lbhCard(i,cid){
  const r=lbhRemark(i.availCuftPct,i.totalQty);
  const avail=i.availCuftPct!==''&&i.availCuftPct!=null?parseFloat(i.availCuftPct):null;
  const util=avail!=null?Math.min(Math.max(100-avail,0),100):null;
  const barW=util!=null?util.toFixed(1):0;
  const barColor=util!=null?lbhBarColor(util):'#ccc';
  let btns='';
  if(r==='LBH-Free'||!r){
    btns=`<div class="btn-container"><button class="btn-action btn-dark" onclick="doAction(this,'${esc(i.label)}','Putaway Done','${cid}')">Putaway Done</button></div>`;
  } else if(r==='LBH-Semi'||r==='LBH-Partial'){
    btns=`<div class="btn-container">
      <button class="btn-action btn-orange" onclick="toggleGtl(this)">GTL ▼</button>
      <button class="btn-action btn-dark" onclick="doAction(this,'${esc(i.label)}','Putaway Done','${cid}')">Putaway Done</button>
    </div>
    <div class="gtl-sub" style="display:none"><div class="btn-container" style="margin-top:4px">
      <button class="btn-action btn-itempick" onclick="doAction(this,'${esc(i.label)}','Item Pick','${cid}')">📦 Item Pick</button>
      <button class="btn-action btn-itemput" onclick="doAction(this,'${esc(i.label)}','Item Put','${cid}')">📥 Item Put</button>
    </div></div>`;
  } else {
    btns=`<div class="btn-container"><button class="btn-action btn-orange" onclick="toggleGtl(this)">GTL ▼</button></div>
    <div class="gtl-sub" style="display:none"><div class="btn-container" style="margin-top:4px">
      <button class="btn-action btn-itempick" onclick="doAction(this,'${esc(i.label)}','Item Pick','${cid}')">📦 Item Pick</button>
      <button class="btn-action btn-itemput" onclick="doAction(this,'${esc(i.label)}','Item Put','${cid}')">📥 Item Put</button>
    </div></div>`;
  }
  return `<div class="card card-lbh"><span class="status-tag ${lbhTagCls(r)}">${lbhLabel(r)}</span>
    <b>📍 ${i.label}</b> <small style="color:#888">${i.type||''}</small>
    <div style="font-size:11px;color:#666;margin-bottom:2px">Qty: ${i.totalQty}</div>
    <div class="lbh-bar-bg"><div class="lbh-bar-fill" style="width:${barW}%;background:${barColor}"></div></div>
    <div class="lbh-stats"><span style="color:${barColor}">Used: ${util!=null?util.toFixed(1)+'%':'N/A'}</span><span style="color:#27ae60">Avail: ${avail!=null?avail.toFixed(1)+'%':'N/A'}</span></div>
    <div class="product-details" style="margin-top:4px"><strong>Shelf Cufeet:</strong> ${i.cufeet||'N/A'} | <strong>Used Cuft:</strong> ${i.totalQtyCuft||'N/A'}</div>
    ${btns}</div>`;
}

function toggleGtl(btn){
  const sub=btn.closest('.btn-container').nextElementSibling;
  if(sub&&sub.classList.contains('gtl-sub')){const s=sub.style.display==='block';sub.style.display=s?'none':'block';btn.textContent=s?'GTL ▼':'GTL ▲'}
}

async function doAction(btn,shelf,action,cid){
  btn.textContent='...';btn.disabled=true;
  const isFree=['GTL Done','Free Location','Already Free','GTL','Item Pick'].includes(action);
  const isFull=['Already Full','Putaway Done','Item Put'].includes(action);
  LOCS.forEach(l=>{if(l.label===shelf){if(isFree){l.remark='Free Shelve';l.totalQty=0;}else if(isFull)l.remark='Full Space';}});
  RAW.forEach(r=>{if(r.label===shelf){if(isFree){r.remark='Free Shelve';r.totalQty=0;}else if(isFull)r.remark='Full Space';}});
  if(cid==='listScan')doScan();
  else if(cid==='listQty')qShow();
  else if(cid==='listQty2')qShow2();
  else if(cid==='listLbh')lShow();
  else if(cid==='listLbh2')lShow2();
  try{await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shelfId:shelf,action,user:USER})});}catch(e){}
  btn.textContent='Done';
}

function actionPillClass(a){const m={'GTL Done':'ap-gtl','GTL':'ap-gtl','Item Pick':'ap-itempick','Item Put':'ap-itemput','Putaway Done':'ap-putaway','Free Location':'ap-free','Already Free':'ap-free','Already Full':'ap-fullx'};return m[a]||'ap-other'}
function renderAction(targetId){
  const wrap=document.getElementById(targetId);if(!wrap||!ACTIONS)return;
  const a=ACTIONS;
  let html=`<div class="section-box"><div style="text-align:center;font-size:12px;color:#555;padding:4px 0">Today: <b style="color:#1a73e8">${a.grandToday||0}</b> • All-time: <b>${a.grandTotal||0}</b></div>
    <span class="section-title">🛠️ Live Action Summary</span>
    <table class="summary-table"><thead><tr><th style="background:#443a3a;text-align:left;padding-left:8px">Action</th><th style="background:#1a73e8">Today</th><th style="background:#555">Total</th></tr></thead><tbody>`;
  (a.byAction||[]).forEach((x,i)=>{
    html+=`<tr style="background:${i%2?'#fff':'#fbfbfc'}"><td style="text-align:left;padding:8px"><span class="action-pill ${actionPillClass(x.action)}">${x.action}</span></td>
      <td><span class="badge-counter bg-today">${x.today}</span></td><td><span class="badge-counter" style="background:#555">${x.total}</span></td></tr>`;
  });
  html+=`</tbody></table></div>`;
  html+=`<div class="section-box"><span class="section-title">👥 Today's Squad</span>`;
  if(a.byUserToday&&a.byUserToday.length){
    html+=`<table class="summary-table"><thead><tr><th style="background:#443a3a;text-align:left;padding-left:8px">Name</th><th style="background:#443a3a">Today</th></tr></thead><tbody>`;
    a.byUserToday.forEach((u,i)=>{html+=`<tr style="background:${i%2?'#fff':'#fbfbfc'}"><td style="text-align:left;padding:6px 8px"><b>${u.name}</b><div style="color:#888;font-size:10px">${u.casperId}</div></td><td><span class="badge-counter bg-today">${u.count}</span></td></tr>`;});
    html+=`</tbody></table>`;
  } else html+='<div style="color:#999;text-align:center;padding:10px;font-size:11px">No activity today</div>';
  html+=`</div>`;
  html+=`<div class="section-box"><span class="section-title">🕒 Recent Activity</span><div style="max-height:200px;overflow-y:auto">`;
  (a.recent||[]).forEach(r=>{html+=`<div class="activity-row"><div><span class="action-pill ${actionPillClass(r.action)}">${r.action}</span><div style="font-size:11px;font-weight:600;color:#333;margin-top:3px">${r.name} • ${r.shelf}</div></div><div style="color:#888;font-size:10px">${r.time}</div></div>`;});
  html+=`</div></div>`;
  wrap.innerHTML=html;
}

function renderBins(targetId){
  const wrap=document.getElementById(targetId);if(!wrap)return;

  // ── Floor summary ──
  let rows='',gF=0,gP=0,gS=0,gFu=0;
  METRICS.forEach(r=>{
    const tot=(r.free||0)+(r.partial||0)+(r.semi||0)+(r.full||0);
    gF+=r.free||0;gP+=r.partial||0;gS+=r.semi||0;gFu+=r.full||0;
    rows+=`<tr><td class="row-title">${r.floor}</td><td><span class="badge-counter bg-free">${r.free||0}</span></td><td><span class="badge-counter bg-partial">${r.partial||0}</span></td><td><span class="badge-counter bg-semi">${r.semi||0}</span></td><td><span class="badge-counter bg-full">${r.full||0}</span></td><td><span class="badge-counter bg-rowtotal">${tot}</span></td></tr>`;
  });
  rows+=`<tr style="border-top:2px solid #443a3a"><td class="row-title" style="background:#443a3a;color:#fff">TOTAL</td><td><span class="badge-counter bg-free">${gF}</span></td><td><span class="badge-counter bg-partial">${gP}</span></td><td><span class="badge-counter bg-semi">${gS}</span></td><td><span class="badge-counter bg-full">${gFu}</span></td><td><span class="badge-counter bg-rowtotal">${gF+gP+gS+gFu}</span></td></tr>`;

  // ── Shelve Type summary ──
  const typeMap={};
  LOCS.forEach(loc=>{
    const t=loc.type||'Unknown'; const r=loc.remark||'';
    if(!typeMap[t])typeMap[t]={type:t,free:0,partial:0,semi:0,full:0};
    if(r.includes('Free'))typeMap[t].free++;
    else if(r.includes('Partial'))typeMap[t].partial++;
    else if(r.includes('Semi'))typeMap[t].semi++;
    else if(r.includes('Full'))typeMap[t].full++;
  });
  let trows='',tF=0,tP=0,tS=0,tFu=0;
  Object.keys(typeMap).sort().forEach(t=>{
    const c=typeMap[t]; const tot=c.free+c.partial+c.semi+c.full;
    tF+=c.free;tP+=c.partial;tS+=c.semi;tFu+=c.full;
    trows+=`<tr><td class="row-title">${t}</td><td><span class="badge-counter bg-free">${c.free}</span></td><td><span class="badge-counter bg-partial">${c.partial}</span></td><td><span class="badge-counter bg-semi">${c.semi}</span></td><td><span class="badge-counter bg-full">${c.full}</span></td><td><span class="badge-counter bg-rowtotal">${tot}</span></td></tr>`;
  });
  trows+=`<tr style="border-top:2px solid #443a3a"><td class="row-title" style="background:#443a3a;color:#fff">TOTAL</td><td><span class="badge-counter bg-free">${tF}</span></td><td><span class="badge-counter bg-partial">${tP}</span></td><td><span class="badge-counter bg-semi">${tS}</span></td><td><span class="badge-counter bg-full">${tFu}</span></td><td><span class="badge-counter bg-rowtotal">${tF+tP+tS+tFu}</span></td></tr>`;

  // ── Floor × Shelve Type breakdown ──
  const flTypeMap={};
  LOCS.forEach(loc=>{
    const fl=loc.floor||'Unknown'; const t=loc.type||'Unknown'; const r=loc.remark||'';
    if(!flTypeMap[fl])flTypeMap[fl]={};
    if(!flTypeMap[fl][t])flTypeMap[fl][t]={free:0,partial:0,semi:0,full:0};
    if(r.includes('Free'))flTypeMap[fl][t].free++;
    else if(r.includes('Partial'))flTypeMap[fl][t].partial++;
    else if(r.includes('Semi'))flTypeMap[fl][t].semi++;
    else if(r.includes('Full'))flTypeMap[fl][t].full++;
  });
  let brows='';
  Object.keys(flTypeMap).sort().forEach(fl=>{
    let flF=0,flP=0,flS=0,flFu=0;
    const types=flTypeMap[fl];
    brows+=`<tr><td colspan="6" style="background:#443a3a;color:#FFD700;font-weight:800;padding:6px 8px;font-size:10px">🏢 ${fl}</td></tr>`;
    Object.keys(types).sort().forEach(t=>{
      const c=types[t]; const tot=c.free+c.partial+c.semi+c.full;
      flF+=c.free;flP+=c.partial;flS+=c.semi;flFu+=c.full;
      brows+=`<tr><td style="padding-left:14px;font-size:10px;color:#555;font-weight:600">${t}</td><td><span class="badge-counter bg-free">${c.free}</span></td><td><span class="badge-counter bg-partial">${c.partial}</span></td><td><span class="badge-counter bg-semi">${c.semi}</span></td><td><span class="badge-counter bg-full">${c.full}</span></td><td><span class="badge-counter bg-rowtotal">${tot}</span></td></tr>`;
    });
    brows+=`<tr style="background:#f0e8e8"><td style="padding-left:8px;font-weight:800;font-size:10px;color:#7B1818">${fl} Subtotal</td><td><span class="badge-counter bg-free">${flF}</span></td><td><span class="badge-counter bg-partial">${flP}</span></td><td><span class="badge-counter bg-semi">${flS}</span></td><td><span class="badge-counter bg-full">${flFu}</span></td><td><span class="badge-counter bg-rowtotal">${flF+flP+flS+flFu}</span></td></tr>`;
  });

  const thRow=`<thead><tr>
    <th style="background:#443a3a;text-align:left;padding-left:8px">—</th>
    <th style="background:#27ae60">Free<br><span style="font-size:9px">0 Qty</span></th>
    <th style="background:#e67e22">Partial<br><span style="font-size:9px">1-10</span></th>
    <th style="background:#7e5233">Semi<br><span style="font-size:9px">11-30</span></th>
    <th style="background:#c0392b">Full<br><span style="font-size:9px">31+</span></th>
    <th style="background:#443a3a">Total</th>
  </tr></thead>`;

  wrap.innerHTML=`
    <div class="section-box"><span class="section-title">⚡ Capacity Metrics — By Floor</span>
      <div style="font-size:10px;color:#888;text-align:right;margin-bottom:4px">Total: ${(gF+gP+gS+gFu).toLocaleString('en-IN')} locations</div>
      <table class="summary-table">${thRow}<tbody>${rows}</tbody></table></div>
    <div class="section-box"><span class="section-title">📦 Shelve Type Totals (All Floors)</span>
      <table class="summary-table">${thRow}<tbody>${trows}</tbody></table></div>
    <div class="section-box"><span class="section-title">🏢 Floor × Shelve Type Breakdown (Qty)</span>
      <table class="summary-table">${thRow}<tbody>${brows}</tbody></table></div>`;
}

function renderLbhBins(targetId){
  const wrap=document.getElementById(targetId);if(!wrap)return;

  const lbhTH=`<thead><tr>
    <th style="background:#443a3a;text-align:left;padding-left:8px">—</th>
    <th style="background:#27ae60">Free<br><span style="font-size:9px">0%</span></th>
    <th style="background:#3498db">Semi<br><span style="font-size:9px">1-30%</span></th>
    <th style="background:#e67e22">Partial<br><span style="font-size:9px">31-70%</span></th>
    <th style="background:#c0392b">Full<br><span style="font-size:9px">71%+</span></th>
    <th style="background:#443a3a">Total</th>
  </tr></thead>`;

  // ── Floor summary ──
  const flMap={};
  LOCS.forEach(loc=>{
    const r=lbhRemark(loc.availCuftPct,loc.totalQty);const fl=loc.floor||'Unknown';
    if(!flMap[fl])flMap[fl]={free:0,semi:0,partial:0,full:0};
    if(r==='LBH-Free')flMap[fl].free++;else if(r==='LBH-Semi')flMap[fl].semi++;else if(r==='LBH-Partial')flMap[fl].partial++;else if(r==='LBH-Full')flMap[fl].full++;
  });
  let rows='',gF=0,gS=0,gP=0,gFu=0;
  Object.keys(flMap).sort().forEach(fl=>{
    const c=flMap[fl];const tot=c.free+c.semi+c.partial+c.full;
    gF+=c.free;gS+=c.semi;gP+=c.partial;gFu+=c.full;
    rows+=`<tr><td class="row-title">${fl}</td><td><span class="badge-counter bg-free">${c.free}</span></td><td><span class="badge-counter" style="background:#3498db">${c.semi}</span></td><td><span class="badge-counter bg-partial">${c.partial}</span></td><td><span class="badge-counter bg-full">${c.full}</span></td><td><span class="badge-counter bg-rowtotal">${tot}</span></td></tr>`;
  });
  rows+=`<tr style="border-top:2px solid #443a3a"><td class="row-title" style="background:#443a3a;color:#fff">TOTAL</td><td><span class="badge-counter bg-free">${gF}</span></td><td><span class="badge-counter" style="background:#3498db">${gS}</span></td><td><span class="badge-counter bg-partial">${gP}</span></td><td><span class="badge-counter bg-full">${gFu}</span></td><td><span class="badge-counter bg-rowtotal">${gF+gS+gP+gFu}</span></td></tr>`;

  // ── Shelve Type summary (LBH) ──
  const typeMap={};
  LOCS.forEach(loc=>{
    const r=lbhRemark(loc.availCuftPct,loc.totalQty); const t=loc.type||'Unknown';
    if(!typeMap[t])typeMap[t]={free:0,semi:0,partial:0,full:0};
    if(r==='LBH-Free')typeMap[t].free++;else if(r==='LBH-Semi')typeMap[t].semi++;else if(r==='LBH-Partial')typeMap[t].partial++;else if(r==='LBH-Full')typeMap[t].full++;
  });
  let trows='',tF=0,tS=0,tP=0,tFu=0;
  Object.keys(typeMap).sort().forEach(t=>{
    const c=typeMap[t]; const tot=c.free+c.semi+c.partial+c.full;
    tF+=c.free;tS+=c.semi;tP+=c.partial;tFu+=c.full;
    trows+=`<tr><td class="row-title">${t}</td><td><span class="badge-counter bg-free">${c.free}</span></td><td><span class="badge-counter" style="background:#3498db">${c.semi}</span></td><td><span class="badge-counter bg-partial">${c.partial}</span></td><td><span class="badge-counter bg-full">${c.full}</span></td><td><span class="badge-counter bg-rowtotal">${tot}</span></td></tr>`;
  });
  trows+=`<tr style="border-top:2px solid #443a3a"><td class="row-title" style="background:#443a3a;color:#fff">TOTAL</td><td><span class="badge-counter bg-free">${tF}</span></td><td><span class="badge-counter" style="background:#3498db">${tS}</span></td><td><span class="badge-counter bg-partial">${tP}</span></td><td><span class="badge-counter bg-full">${tFu}</span></td><td><span class="badge-counter bg-rowtotal">${tF+tS+tP+tFu}</span></td></tr>`;

  // ── Floor × Shelve Type (LBH) ──
  const flTypeMap={};
  LOCS.forEach(loc=>{
    const fl=loc.floor||'Unknown'; const t=loc.type||'Unknown';
    const r=lbhRemark(loc.availCuftPct,loc.totalQty);
    if(!flTypeMap[fl])flTypeMap[fl]={};
    if(!flTypeMap[fl][t])flTypeMap[fl][t]={free:0,semi:0,partial:0,full:0};
    if(r==='LBH-Free')flTypeMap[fl][t].free++;else if(r==='LBH-Semi')flTypeMap[fl][t].semi++;else if(r==='LBH-Partial')flTypeMap[fl][t].partial++;else if(r==='LBH-Full')flTypeMap[fl][t].full++;
  });
  let brows='';
  Object.keys(flTypeMap).sort().forEach(fl=>{
    let flF=0,flS=0,flP=0,flFu=0;
    const types=flTypeMap[fl];
    brows+=`<tr><td colspan="6" style="background:#443a3a;color:#FFD700;font-weight:800;padding:6px 8px;font-size:10px">🏢 ${fl}</td></tr>`;
    Object.keys(types).sort().forEach(t=>{
      const c=types[t]; const tot=c.free+c.semi+c.partial+c.full;
      flF+=c.free;flS+=c.semi;flP+=c.partial;flFu+=c.full;
      brows+=`<tr><td style="padding-left:14px;font-size:10px;color:#555;font-weight:600">${t}</td><td><span class="badge-counter bg-free">${c.free}</span></td><td><span class="badge-counter" style="background:#3498db">${c.semi}</span></td><td><span class="badge-counter bg-partial">${c.partial}</span></td><td><span class="badge-counter bg-full">${c.full}</span></td><td><span class="badge-counter bg-rowtotal">${tot}</span></td></tr>`;
    });
    brows+=`<tr style="background:#f0e8e8"><td style="padding-left:8px;font-weight:800;font-size:10px;color:#7B1818">${fl} Subtotal</td><td><span class="badge-counter bg-free">${flF}</span></td><td><span class="badge-counter" style="background:#3498db">${flS}</span></td><td><span class="badge-counter bg-partial">${flP}</span></td><td><span class="badge-counter bg-full">${flFu}</span></td><td><span class="badge-counter bg-rowtotal">${flF+flS+flP+flFu}</span></td></tr>`;
  });

  wrap.innerHTML=`
    <div class="section-box"><span class="section-title">📐 LBH Capacity — By Floor</span>
      <div style="font-size:10px;color:#888;text-align:right;margin-bottom:4px">Total: ${(gF+gS+gP+gFu).toLocaleString('en-IN')} locations</div>
      <table class="summary-table">${lbhTH}<tbody>${rows}</tbody></table></div>
    <div class="section-box"><span class="section-title">📐 LBH Shelve Type Totals (All Floors)</span>
      <table class="summary-table">${lbhTH}<tbody>${trows}</tbody></table></div>
    <div class="section-box"><span class="section-title">🏢 Floor × Shelve Type Breakdown (LBH)</span>
      <table class="summary-table">${lbhTH}<tbody>${brows}</tbody></table></div>`;
}

window.addEventListener('load',()=>{
  const sid=sessionStorage.getItem('did'),sname=sessionStorage.getItem('dname');
  if(sid){
    USER=sid;USER_NAME=sname||sid;
    document.getElementById('login-page').style.display='none';
    document.body.classList.remove('login-active');document.body.style.padding='10px';
    updateBars();showHomePage();loadData(true);
  } else {
    const inp=document.getElementById('casperId');if(inp)inp.focus();
  }
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&Date.now()-_lastLoad>30000)loadData()});
});

let _scanning=false;
async function toggleScanner(){
  if(_scanning){stopScanner();return;}
  const box=document.getElementById('scannerBox');
  const video=document.getElementById('camVideo');
  box.style.display='block';
  try{
    const stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment',width:{ideal:1280},height:{ideal:720}}});
    video.srcObject=stream;await video.play();
    _scanning=true;
    if(window.BarcodeDetector){
      const detector=new BarcodeDetector({formats:['ean_13','ean_8','code_128','qr_code','code_39']});
      const detect=async()=>{
        if(!_scanning)return;
        try{
          const codes=await detector.detect(video);
          if(codes.length>0){
            document.getElementById('scanInput').value=codes[0].rawValue;
            doScan();stopScanner();return;
          }
        }catch(e){}
        requestAnimationFrame(detect);
      };
      detect();
    }
  }catch(e){box.style.display='none';alert('Camera error: '+e.message);}
}
function stopScanner(){
  _scanning=false;
  const video=document.getElementById('camVideo');
  if(video.srcObject)video.srcObject.getTracks().forEach(t=>t.stop());
  document.getElementById('scannerBox').style.display='none';
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
