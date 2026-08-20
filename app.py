"""
app.py
------
Runs on Railway (cloud) — NOT on your machine.
Reads flo_inventory.db and shows it as a searchable table at your public link.
Railway starts this automatically. You don't need to touch this file.
"""

import sqlite3, os
from flask import Flask, render_template_string, request

app = Flask(__name__)
DB  = os.path.join(os.path.dirname(__file__), "flo_inventory.db")

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLO Inventory</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#f5f5f5;color:#222}
  header{background:#1a1a2e;color:#fff;padding:18px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  header h1{font-size:18px;font-weight:600;letter-spacing:.3px}
  .badge{background:#e94560;color:#fff;font-size:12px;padding:3px 10px;border-radius:20px}
  .meta{font-size:12px;color:#aaa;margin-left:auto}
  .controls{padding:14px 24px;background:#fff;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  input[type=text]{border:1px solid #ddd;border-radius:6px;padding:7px 12px;font-size:14px;width:280px;outline:none}
  input[type=text]:focus{border-color:#1a1a2e}
  .count{font-size:13px;color:#666}
  .table-wrap{overflow-x:auto;padding:16px 24px}
  table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}
  th{background:#1a1a2e;color:#fff;padding:10px 14px;text-align:left;font-weight:500;white-space:nowrap;font-size:12px;letter-spacing:.4px;text-transform:uppercase}
  td{padding:9px 14px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#f9f9f9}
  .empty{text-align:center;padding:60px;color:#999;font-size:15px}
  .ts-col{color:#888;font-size:12px}
  footer{text-align:center;padding:20px;font-size:12px;color:#aaa}
</style>
</head>
<body>

<header>
  <h1>📦 FLO Inventory</h1>
  <span class="badge">LIVE</span>
  {% if pull_ts %}
  <span class="meta">Last updated: {{ pull_ts }}</span>
  {% endif %}
</header>

<div class="controls">
  <input type="text" id="search" placeholder="Search anything..." oninput="filterTable()" autofocus>
  <span class="count" id="count-label">{{ rows|length }} rows</span>
</div>

<div class="table-wrap">
  {% if rows %}
  <table id="inv-table">
    <thead>
      <tr>
        {% for col in columns %}
        <th>{{ col }}</th>
        {% endfor %}
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        {% for cell in row %}
        <td {% if loop.index0 == 0 %}class="ts-col"{% endif %}>{{ cell }}</td>
        {% endfor %}
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">
    No data yet — the scraper hasn't pushed anything yet.<br>
    Run <code>flo_scraper.py</code> on your machine first.
  </div>
  {% endif %}
</div>

<footer>Auto-refreshes every 5 minutes · FLO Inventory System</footer>

<script>
function filterTable(){
  var q=document.getElementById('search').value.toLowerCase();
  var rows=document.querySelectorAll('#inv-table tbody tr');
  var visible=0;
  rows.forEach(function(r){
    var show=r.innerText.toLowerCase().includes(q);
    r.style.display=show?'':'none';
    if(show) visible++;
  });
  document.getElementById('count-label').textContent=visible+' rows';
}
// Auto-refresh page every 5 minutes
setTimeout(function(){ location.reload(); }, 300000);
</script>
</body>
</html>
"""


def get_data(search=""):
    if not os.path.exists(DB):
        return [], [], None
    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.row_factory = sqlite3.Row

        # get last pull timestamp
        try:
            ts_row = conn.execute("SELECT MAX(_pull_ts) FROM inventory").fetchone()
            pull_ts = ts_row[0] if ts_row else None
        except:
            pull_ts = None

        # get columns (skip internal _pull_ts — show it as first col labelled "Last Updated")
        cur = conn.execute("SELECT * FROM inventory LIMIT 1")
        if not cur.description:
            return [], [], pull_ts
        all_cols = [d[0] for d in cur.description]
        # put _pull_ts first with friendly name
        display_cols = ["Last Updated"] + [c for c in all_cols if c != "_pull_ts"]
        db_cols      = ["_pull_ts"]     + [c for c in all_cols if c != "_pull_ts"]

        query = f"SELECT {','.join(chr(34)+c+chr(34) for c in db_cols)} FROM inventory"
        rows  = conn.execute(query).fetchall()

        if search:
            search_lower = search.lower()
            rows = [r for r in rows if any(search_lower in str(v).lower() for v in r)]

        conn.close()
        return display_cols, [list(r) for r in rows], pull_ts
    except Exception as e:
        return [], [], None


@app.route("/")
def index():
    search  = request.args.get("q", "")
    columns, rows, pull_ts = get_data(search)
    return render_template_string(HTML, columns=columns, rows=rows,
                                  pull_ts=pull_ts, search=search)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
