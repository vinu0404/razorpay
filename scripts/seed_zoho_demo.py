"""One-time helper: seed fictional demo data into a Zoho Inventory org.

Not part of the connector. The connector itself is read-only; this script uses a
separate token with CREATE/UPDATE scopes, stored in .tokens/zoho_seed.json.

Usage:
    python3 scripts/seed_zoho_demo.py auth   # browser consent, stores seed token
    python3 scripts/seed_zoho_demo.py seed   # creates items, customers, orders
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / ".tokens" / "zoho_seed.json"
RESULT_FILE = ROOT / "scripts" / "seed_result.json"

SEED_SCOPES = ",".join(
    f"ZohoInventory.{res}.{op}"
    for res in ("items", "contacts", "salesorders", "packages", "shipmentorders")
    for op in ("CREATE", "UPDATE", "READ")
) + ",ZohoInventory.settings.READ"

ITEMS = [
    {"name": "Cotton T-Shirt (M)", "sku": "TSH-M", "rate": 499, "stock": 20},
    {"name": "Ceramic Coffee Mug Set", "sku": "MUG-SET", "rate": 799, "stock": 15},
    {"name": "Denim Jacket (L)", "sku": "JKT-L", "rate": 1299, "stock": 0},
    {"name": "Spiral Notebook Pack", "sku": "NBK-PK", "rate": 249, "stock": 50},
    {"name": "Canvas Tote Bag", "sku": "TOTE-01", "rate": 999, "stock": 3},
]

CUSTOMERS = [
    ("Asha", "Demo", "asha.demo@example.com"),
    ("Ravi", "Demo", "ravi.demo@example.com"),
    ("Meera", "Demo", "meera.demo@example.com"),
    ("Karan", "Demo", "karan.demo@example.com"),
    ("Neha", "Demo", "neha.demo@example.com"),
]

# (customer display name, Razorpay test payment id, order date, sku, target state)
ORDERS = [
    ("Asha Demo", "pay_TjJrWWgj7jAttQ", "2026-10-01", "TSH-M", "shipped"),
    ("Ravi Demo", "pay_TjJrIOvtskFJU8", "2026-10-02", "MUG-SET", "confirmed"),
    ("Meera Demo", "pay_TjJr1DLusM3dSE", "2026-10-03", "JKT-L", "confirmed"),
    ("Karan Demo", "pay_TjJqSqc2PW0zCs", "2026-09-26", "NBK-PK", "delivered"),
    ("Neha Demo", "pay_TjJpRryFu6TZAu", "2026-09-24", "TOTE-01", "confirmed"),
]


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
DC = ENV.get("ZOHO_DC", "in")
ACCOUNTS = f"https://accounts.zoho.{DC}"
API = f"https://www.zohoapis.{DC}/inventory/v1"


def http(method, url, data=None, headers=None, form=False):
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


# ---------- auth ----------

def cmd_auth():
    redirect = ENV["ZOHO_REDIRECT_URI"]
    parsed = urllib.parse.urlparse(redirect)
    params = {
        "scope": SEED_SCOPES,
        "client_id": ENV["ZOHO_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": redirect,
        "access_type": "offline",
        "prompt": "consent",
        "state": os.urandom(8).hex(),
    }
    auth_url = f"{ACCOUNTS}/oauth/v2/auth?" + urllib.parse.urlencode(params)
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Zoho consent received. You can close this tab.</h3>")

        def log_message(self, *a):
            pass

    server = HTTPServer((parsed.hostname, parsed.port), Handler)
    print("Opening browser for Zoho consent. If it does not open, visit:\n" + auth_url)
    webbrowser.open(auth_url)
    while "code" not in got and "error" not in got:
        server.handle_request()
    if "error" in got:
        sys.exit(f"Consent failed: {got['error']}")
    if got.get("state") != params["state"]:
        sys.exit("State mismatch; aborting.")

    status, tok = http("POST", f"{ACCOUNTS}/oauth/v2/token", {
        "code": got["code"],
        "client_id": ENV["ZOHO_CLIENT_ID"],
        "client_secret": ENV["ZOHO_CLIENT_SECRET"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }, form=True)
    if status != 200 or "refresh_token" not in tok:
        sys.exit(f"Token exchange failed ({status}): {tok.get('error', tok)}")
    tok["obtained_at"] = int(time.time())
    TOKEN_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok))
    TOKEN_FILE.chmod(0o600)
    print(f"Seed token stored in {TOKEN_FILE.relative_to(ROOT)} (api_domain={tok.get('api_domain')})")


def access_token():
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() - tok["obtained_at"] < tok.get("expires_in", 3600) - 120:
        return tok["access_token"]
    status, new = http("POST", f"{ACCOUNTS}/oauth/v2/token", {
        "refresh_token": tok["refresh_token"],
        "client_id": ENV["ZOHO_CLIENT_ID"],
        "client_secret": ENV["ZOHO_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }, form=True)
    if status != 200 or "access_token" not in new:
        sys.exit(f"Refresh failed ({status}): {new.get('error', new)}")
    tok.update(access_token=new["access_token"], obtained_at=int(time.time()))
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok["access_token"]


# ---------- seed ----------

def zoho(method, path, data=None, query=None):
    q = {"organization_id": ENV["ZOHO_ORG_ID"], **(query or {})}
    url = f"{API}{path}?{urllib.parse.urlencode(q)}"
    for attempt in range(5):
        status, body = http(method, url, data,
                            {"Authorization": f"Zoho-oauthtoken {access_token()}"})
        if status == 429:
            time.sleep(2 ** attempt)
            continue
        if body.get("code", 0) != 0 or status >= 400:
            raise RuntimeError(f"{method} {path} -> {status} {body.get('code')}: {body.get('message', body)}")
        return body
    raise RuntimeError(f"{method} {path} -> still rate limited")


def find_one(path, key, field, value):
    body = zoho("GET", path, query={field: value})
    for row in body.get(key, []):
        if str(row.get(field, "")).lower() == value.lower():
            return row
    return None


def cmd_seed():
    result = {"items": {}, "customers": {}, "orders": []}

    for it in ITEMS:
        row = find_one("/items", "items", "sku", it["sku"])
        if not row:
            payload = {
                "name": it["name"], "sku": it["sku"], "rate": it["rate"],
                "purchase_rate": round(it["rate"] * 0.5), "unit": "pcs",
                "item_type": "inventory", "product_type": "goods",
            }
            if it["stock"]:
                payload.update(initial_stock=it["stock"], initial_stock_rate=round(it["rate"] * 0.5))
            row = zoho("POST", "/items", payload)["item"]
            print(f"item created   {it['sku']}")
        else:
            print(f"item exists    {it['sku']}")
        result["items"][it["sku"]] = row["item_id"]

    for first, last, email in CUSTOMERS:
        name = f"{first} {last}"
        row = find_one("/contacts", "contacts", "contact_name", name)
        if not row:
            row = zoho("POST", "/contacts", {
                "contact_name": name, "contact_type": "customer",
                "customer_sub_type": "individual",
                "contact_persons": [{"first_name": first, "last_name": last,
                                     "email": email, "is_primary_contact": True}],
            })["contact"]
            print(f"customer made  {name}")
        else:
            print(f"customer exists {name}")
        result["customers"][name] = row["contact_id"]

    for cust, pay_id, date, sku, target in ORDERS:
        row = find_one("/salesorders", "salesorders", "reference_number", pay_id)
        if not row:
            item = ITEMS[[i["sku"] for i in ITEMS].index(sku)]
            row = zoho("POST", "/salesorders", {
                "customer_id": result["customers"][cust],
                "reference_number": pay_id, "date": date,
                "line_items": [{"item_id": result["items"][sku], "quantity": 1, "rate": item["rate"]}],
                "notes": f"Paid via Razorpay (test mode) {pay_id}",
            })["salesorder"]
            print(f"order created  {row['salesorder_number']} {pay_id}")
        so_id = row["salesorder_id"]
        so = zoho("GET", f"/salesorders/{so_id}")["salesorder"]
        entry = {"salesorder_id": so_id, "salesorder_number": so["salesorder_number"],
                 "reference_number": pay_id, "target": target}

        if so.get("status") == "draft":
            zoho("POST", f"/salesorders/{so_id}/status/confirmed")
            print(f"  confirmed    {so['salesorder_number']}")

        if target in ("shipped", "delivered") and not so.get("packages"):
            seq = so["salesorder_number"][-5:]
            pkg = zoho("POST", "/packages", {
                "package_number": f"PKG-{seq}", "date": date,
                "line_items": [{"so_line_item_id": li["line_item_id"], "quantity": li["quantity"]}
                               for li in so["line_items"]],
            }, query={"salesorder_id": so_id})["package"]
            print(f"  packaged     {pkg['package_number']}")
            ship = zoho("POST", "/shipmentorders", {
                "shipment_number": f"SHP-{seq}", "date": date, "delivery_method": "Demo Courier",
                "tracking_number": f"TEST{seq}",
            }, query={"package_ids": pkg["package_id"], "salesorder_id": so_id})["shipmentorder"]
            print(f"  shipped      {ship['shipment_number']}")
            if target == "delivered":
                zoho("POST", f"/shipmentorders/{ship['shipment_id']}/status/delivered")
                print(f"  delivered    {ship['shipment_number']}")

        result["orders"].append(entry)

    RESULT_FILE.write_text(json.dumps(result, indent=2))
    print(f"\nDone. IDs written to {RESULT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    {"auth": cmd_auth, "seed": cmd_seed}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
