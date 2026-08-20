#!/usr/bin/env python3
"""
flo_scraper.py
--------------
Runs on YOUR Windows machine (Desktop).
Pulls FLO inventory every 5 minutes → saves to flo_inventory.db
Then auto-pushes the DB to GitHub so Railway can serve it live.

How to run:
    python flo_scraper.py

Stop it:
    Press Ctrl+C
"""

import os, time, datetime, sqlite3, subprocess
from urllib3.exceptions import ReadTimeoutError
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException, TimeoutException

# ─────────────────────────────────────────────
# SETTINGS  ← only change things in this block
# ─────────────────────────────────────────────
CHROMEDRIVER  = r"C:\Users\shivamsingh.n\Desktop\chromedriver.exe"
DB_PATH       = r"C:\Users\shivamsingh.n\Desktop\flo-inventory\flo_inventory.db"
REPO_DIR      = r"C:\Users\shivamsingh.n\Desktop\flo-inventory"   # your git repo folder
INTERVAL      = 300   # seconds between pulls (5 minutes)
PUSH_TO_GIT   = True  # set False if you don't want auto-push
# ─────────────────────────────────────────────

PROFILE_DIR = os.path.join(os.path.expanduser("~"), "flo_automation_profile")
LIMIT, MAX_PAGES = 5000, 100
PAGE_LOAD_TIMEOUT, LOAD_TIMEOUT, LOAD_RETRIES = 180, 60, 3

URL_TEMPLATE = (
    "http://10.24.1.53/inventory/view_store_inventory"
    "?_={ms}&catalogue_enabled=true"
    "&filters[flag_for_calendar_with_datetime]=true"
    "&filters[fsn]=&filters[location]=&filters[location_type]=store"
    "&filters[seller_id]=&filters[storage_zone_id]=&filters[wid]=+"
    "&limit={limit}&page={page}&rpm_enabled=true&ts={ms}"
)
LOGIN_URL = "http://10.24.1.53/inventory/view_store_inventory"

EXTRACT_JS = """
var ts=document.querySelectorAll('table');
if(!ts.length) return {headers:[],rows:[]};
var best=ts[0],n=best.rows.length;
for(var i=0;i<ts.length;i++){if(ts[i].rows.length>n){best=ts[i];n=ts[i].rows.length;}}
var headers=[];
var ths=best.querySelectorAll('th');
for(var h=0;h<ths.length;h++){headers.push((ths[h].innerText||'').trim());}
var rows=[];
for(var r=0;r<best.rows.length;r++){
  var tds=best.rows[r].querySelectorAll('td');
  if(tds.length){var row=[];for(var c=0;c<tds.length;c++){row.push((tds[c].innerText||'').trim());}rows.push(row);}
}
return {headers:headers,rows:rows};
"""


# ── browser ────────────────────────────────
def make_browser():
    if not os.path.exists(CHROMEDRIVER):
        raise FileNotFoundError(
            f"chromedriver.exe not found at:\n{CHROMEDRIVER}\n\n"
            "Download: https://storage.googleapis.com/chrome-for-testing-public/"
            "149.0.7827.115/win64/chromedriver-win64.zip\n"
            "Extract chromedriver.exe to your Desktop."
        )
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-session-crashed-bubble")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    return webdriver.Chrome(service=Service(executable_path=CHROMEDRIVER), options=opts)


def extract_table(driver):
    try:
        data = driver.execute_script(EXTRACT_JS) or {}
    except WebDriverException:
        raise
    except Exception:
        data = {}
    return data.get("headers") or None, data.get("rows") or []


def page_url(page):
    ms = str(int(time.time() * 1000))
    return URL_TEMPLATE.format(ms=ms, limit=LIMIT, page=page)


def load_page(driver, url):
    for attempt in range(1, LOAD_RETRIES + 1):
        try:
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            driver.get(url)
            end, prev, stable = time.time() + LOAD_TIMEOUT, -1, 0
            while time.time() < end:
                _, rows = extract_table(driver)
                n = len(rows)
                stable = (stable + 1) if n == prev else 0
                if stable >= 2:
                    break
                prev = n
                time.sleep(1)
            return extract_table(driver)
        except (ReadTimeoutError, TimeoutException) as e:
            if attempt < LOAD_RETRIES:
                print(f"[WARN] Timeout attempt {attempt}/{LOAD_RETRIES}. Retry in 15s...")
                time.sleep(15)
            else:
                raise


def scrape_all(driver):
    all_rows, all_headers, last_sig, page_size = [], None, None, None
    for page in range(1, MAX_PAGES + 1):
        headers, rows = load_page(driver, page_url(page))
        if not rows:
            break
        sig = (tuple(rows[0]), tuple(rows[-1]), len(rows))
        if sig == last_sig:
            break
        last_sig = sig
        if all_headers is None and headers:
            all_headers = headers
        if page_size is None:
            page_size = len(rows)
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
    return all_headers, all_rows


# ── data cleanup ───────────────────────────
def dedupe_columns(headers, rows):
    if not headers:
        return headers, rows
    cut = next((i for i in range(1, len(headers)) if headers[i] == headers[0]), None)
    if cut and cut >= 3:
        headers, rows = headers[:cut], [r[:cut] for r in rows]
    return headers, rows


def safe_col(h, i):
    h = (h or "").strip().replace(" ", "_").replace("/", "_")
    h = "".join(c for c in h if c.isalnum() or c == "_")
    return h if h and not h[0].isdigit() else f"col_{i+1}"


def normalize(all_headers, all_rows):
    headers, rows = dedupe_columns(all_headers, all_rows)
    headers = [safe_col(h, i) for i, h in enumerate(headers or [])]
    width = len(headers) if headers else (len(rows[0]) if rows else 1)
    norm = [(list(r) + [""] * (width - len(r)))[:width] for r in rows]
    return headers, norm


# ── SQLite ─────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn, headers):
    cols = ", ".join(f'"{h}" TEXT' for h in headers)
    conn.execute(f'CREATE TABLE IF NOT EXISTS inventory (_pull_ts TEXT, {cols})')
    existing = {r[1].lower() for r in conn.execute("PRAGMA table_info(inventory)")}
    for h in headers:
        if h.lower() not in existing:
            conn.execute(f'ALTER TABLE inventory ADD COLUMN "{h}" TEXT')
            print(f"[DB] New column added: {h}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pull_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            pull_ts   TEXT,
            row_count INTEGER
        )
    """)
    conn.commit()


def write_sqlite(all_headers, all_rows):
    headers, norm = normalize(all_headers, all_rows)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        ensure_schema(conn, headers)
        conn.execute("DELETE FROM inventory")
        ph = ",".join("?" for _ in range(len(headers) + 1))
        cols = '","'.join(["_pull_ts"] + headers)
        conn.executemany(
            f'INSERT INTO inventory ("{cols}") VALUES ({ph})',
            [[ts] + list(r) for r in norm]
        )
        conn.execute("INSERT INTO pull_log(pull_ts,row_count) VALUES(?,?)", (ts, len(norm)))
        conn.commit()
    return len(norm)


# ── git push ───────────────────────────────
def git_push():
    try:
        subprocess.run(["git", "-C", REPO_DIR, "add", "flo_inventory.db"], check=True, capture_output=True)
        subprocess.run(["git", "-C", REPO_DIR, "commit", "-m", "auto: update inventory db"], capture_output=True)
        result = subprocess.run(["git", "-C", REPO_DIR, "push"], capture_output=True, text=True)
        if result.returncode == 0:
            print("       ✓ Pushed to GitHub")
        else:
            print(f"       Git push note: {result.stderr.strip()[:80]}")
    except Exception as e:
        print(f"       Git push failed: {e}")


# ── login / keepalive ──────────────────────
def ensure_logged_in(driver):
    while True:
        try:
            _, rows = load_page(driver, page_url(1))
        except (WebDriverException, ReadTimeoutError, TimeoutException):
            print(">>> Chrome closed or timed out — reopening...")
            try: driver.quit()
            except: pass
            driver = make_browser()
            continue
        if rows:
            return driver
        try: driver.get(LOGIN_URL)
        except: continue
        print("\n>>> Please LOG INTO FLO in the Chrome window now.")
        print(">>> Then come back here and press ENTER.")
        input(">>> Press ENTER when logged in... ")


def keepalive_sleep(driver, seconds):
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(30, remaining))
        try: _ = driver.current_url
        except: break


# ── main ───────────────────────────────────
def main():
    print("=" * 50)
    print("  FLO Inventory Scraper — SQLite Edition")
    print(f"  DB  → {DB_PATH}")
    print(f"  Git → {'enabled' if PUSH_TO_GIT else 'disabled'}")
    print("=" * 50)

    driver = make_browser()
    fails  = 0

    try:
        driver = ensure_logged_in(driver)
        print("\n✓ Logged in. Starting 5-minute pulls. Press Ctrl+C to stop.\n")

        while True:
            now = datetime.datetime.now().strftime("%H:%M:%S")
            try:
                all_headers, all_rows = scrape_all(driver)
                fails = 0
            except (WebDriverException, ReadTimeoutError, TimeoutException) as e:
                fails += 1
                print(f"[{now}] Browser error (attempt {fails}): {e}")
                try: driver.quit()
                except: pass
                driver = make_browser()
                if fails <= 3:
                    print("       Chrome restarted. Retrying in 10s...")
                    time.sleep(10)
                    continue
                fails = 0
                keepalive_sleep(driver, INTERVAL)
                continue

            if all_rows:
                n = write_sqlite(all_headers, all_rows)
                print(f"[{now}] ✓ Saved {n} rows to SQLite")
                if PUSH_TO_GIT:
                    git_push()
            else:
                print(f"[{now}] No data — you may be logged out. Log in via Chrome.")

            nxt = (datetime.datetime.now() + datetime.timedelta(seconds=INTERVAL)).strftime("%H:%M")
            print(f"       Next pull at {nxt}\n")
            keepalive_sleep(driver, INTERVAL)

    except KeyboardInterrupt:
        print("\nStopped cleanly.")
    finally:
        try: driver.quit()
        except: pass


if __name__ == "__main__":
    main()
