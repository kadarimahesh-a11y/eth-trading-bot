import os
import json
import time
import datetime
import threading
import hmac
import hashlib
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
import ssl

STATE_LOCK = threading.Lock()  # Protects BOT_STATE from race conditions

# ==========================================
# CONFIGURATION
# ==========================================
# Strategy Configs
TIMEZONE_LOWERCASE = "ist" # Assumes India Time (UTC+5:30)
SIZE_CONTRACTS = 1

# Web Server Port
PORT = int(os.environ.get("PORT", 8080))
STATE_FILE = "state.json"

BASE_URL = "https://cdn.india.deltaex.org"

# Global state to easily share between the trading thread and the HTTP Server thread
BOT_STATE = {
    "api_key": "",
    "api_secret": "",
    "bot_running": False,
    "logs": [],
    "daily_entries": 0,
    "max_daily_entries": 3,
    "active_legs": {},
    "total_initial_premium": 0.0,
    "current_pnl": 0.0,
    "status": "Awaiting API Keys...",
    "connection": "Disconnected",
    "trading_done_for_day": False,
    "wallet_balance": 0.0,
    "account_balance": 0.0,
    "next_action": "Waiting for initialization...",
    "order_history": [],
    "eth_spot": 0.0,
    "option_chain": [],
    "nearest_expiry": "",
    "reentry_pending_at": None,   # timestamp for non-blocking 1-min re-entry wait
    "entry_time": "21:31",
    "exit_time": "13:00"
}
def log(msg):
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    BOT_STATE["logs"].insert(0, formatted)
    if len(BOT_STATE["logs"]) > 100:  # Keep only recent logs
        BOT_STATE["logs"].pop()

def save_state():
    """Saves critical botanical state to a local JSON file."""
    try:
        with STATE_LOCK:
            # Only save what's needed to recover a session
            to_save = {
                "api_key": BOT_STATE.get("api_key", ""),
                "api_secret": BOT_STATE.get("api_secret", ""),
                "bot_running": BOT_STATE.get("bot_running", False),
                "daily_entries": BOT_STATE.get("daily_entries", 0),
                "active_legs": BOT_STATE.get("active_legs", {}),
                "total_initial_premium": BOT_STATE.get("total_initial_premium", 0.0),
                "trading_done_for_day": BOT_STATE.get("trading_done_for_day", False),
                "order_history": BOT_STATE.get("order_history", []),
                "entry_time": BOT_STATE.get("entry_time", "21:31"),
                "exit_time": BOT_STATE.get("exit_time", "13:00"),
                "lots_per_trade": BOT_STATE.get("lots_per_trade", 1)
            }
        with open(STATE_FILE, "w") as f:
            json.dump(to_save, f)
    except Exception as e:
        print(f"Error saving state: {e}")

def load_state():
    """Loads state from local JSON file if it exists."""
    global SIZE_CONTRACTS
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                with STATE_LOCK:
                    BOT_STATE.update(data)
                SIZE_CONTRACTS = BOT_STATE.get("lots_per_trade", 1)
                if BOT_STATE.get("api_key"):
                    log("Session state restored from state.json.")
        except Exception as e:
            log(f"Error loading state: {e}")

class DeltaClient:
    def __init__(self, key, secret, base_url):
        self.key = key
        self.secret = secret
        self.base_url = base_url

    def _signature(self, method, endpoint, payload_str):
        timestamp = str(int(time.time()))
        sig_data = method + timestamp + endpoint + payload_str
        signature = hmac.new(
            self.secret.encode('utf-8'),
            sig_data.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return timestamp, signature

    def request(self, method, endpoint, payload=None):
        url = self.base_url + endpoint
        headers = { 'Accept': 'application/json' }
        payload_str = ""
        payload_bytes = None
        
        if payload:
            payload_str = json.dumps(payload)
            payload_bytes = payload_str.encode('utf-8')
            headers['Content-Type'] = 'application/json'
            
        if self.key and self.secret:
            timestamp, sig = self._signature(method, endpoint, payload_str)
            headers['api-key'] = self.key
            headers['timestamp'] = timestamp
            headers['signature'] = sig

        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method=method)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        try:
            with urllib.request.urlopen(req, context=ctx) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.URLError as e:
            log(f"API Error: {e}")
            if hasattr(e, 'read'):
                try:
                    log(f"API Response: {e.read().decode('utf-8')}")
                except: pass
            return None

    def get_eth_options(self):
        res = self.request('GET', '/v2/products')
        if res and res.get('success'):
            return [
                p for p in res["result"] 
                if p.get("underlying_asset", {}).get("symbol") == "ETH" and p.get("contract_type") in ["call_options", "put_options"]
            ]
        return []
        
    def get_tickers(self):
        res = self.request('GET', '/v2/tickers')
        if res and res.get('success'):
            return res["result"]
        return []

    # Alias — used by option chain fetcher (same endpoint, no duplicate API call)
    def get_all_eth_tickers(self):
        return self.get_tickers()

    def get_wallet_balances(self):
        res = self.request('GET', '/v2/wallet/balances')
        if res and res.get('success'):
            return res["result"]
        return []

    def place_order(self, product_id, side, size=1, order_type="market_order"):
        """
        Place a real order on Delta Exchange.
        side: 'buy' or 'sell'
        order_type: 'market_order' or 'limit_order'
        Returns the order result dict on success, None on failure.
        """
        payload = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "time_in_force": "ioc"  # Immediate or Cancel - best for market orders
        }
        res = self.request('POST', '/v2/orders', payload=payload)
        if res and res.get('success'):
            order = res.get('result', {})
            log(f"ORDER PLACED ✔ | {side.upper()} | product_id={product_id} | "
                f"fill=${order.get('average_fill_price', '?')} | state={order.get('state', '?')}")
            return order
        else:
            log(f"ORDER FAILED ✘ | {side.upper()} product_id={product_id} | Response: {res}")
            return None

    def close_position(self, product_id, size=1):
        """Place a BUY order to close an existing short option position."""
        return self.place_order(product_id, side="buy", size=size, order_type="market_order")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ETH Automated Options Trader</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');
        
        :root {
            --bg-grad: linear-gradient(135deg, #0f111a 0%, #171a25 100%);
            --glass-bg: rgba(255, 255, 255, 0.03);
            --glass-border: rgba(255, 255, 255, 0.08);
            --accent: #5e6ad2;
            --accent-glow: rgba(94, 106, 210, 0.4);
            --text-main: #f0f0f0;
            --text-muted: #8e95b0;
            --success: #22c55e;
            --danger: #ef4444;
        }

        body {
            margin: 0;
            font-family: 'Outfit', sans-serif;
            background: var(--bg-grad);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        .navbar {
            padding: 1.5rem 3rem;
            background: var(--glass-bg);
            border-bottom: 1px solid var(--glass-border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            backdrop-filter: blur(10px);
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        }

        .navbar h1 {
            margin: 0;
            font-weight: 700;
            font-size: 1.5rem;
            letter-spacing: -0.5px;
            display: flex;
            align-items: center; gap: 12px;
        }

        .navbar h1::before {
            content: '';
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--accent);
            box-shadow: 0 0 10px var(--accent-glow);
        }

        .status-badge {
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(34, 197, 94, 0.1);
            color: var(--success);
            border: 1px solid rgba(34, 197, 94, 0.2);
        }

        .status-badge.disconnected {
            background: rgba(239, 68, 68, 0.1);
            color: var(--danger);
            border-color: rgba(239, 68, 68, 0.2);
        }

        .container {
            padding: 3rem;
            flex-grow: 1;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2rem;
            max-width: 1400px;
            margin: 0 auto;
            width: 100%;
            box-sizing: border-box;
        }

        .card {
            background: var(--glass-bg);
            border: 1px solid var(--glass-border);
            border-radius: 16px;
            padding: 2rem;
            backdrop-filter: blur(10px);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.2);
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .card:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 40px 0 rgba(0, 0, 0, 0.3);
        }

        .card h2 {
            margin-top: 0;
            margin-bottom: 1.5rem;
            font-size: 1.2rem;
            color: var(--text-muted);
            font-weight: 400;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .stat-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1.5rem;
        }

        .stat-box {
            background: rgba(0, 0, 0, 0.2);
            padding: 1.5rem;
            border-radius: 12px;
            border: 1px solid var(--glass-border);
        }

        .stat-value {
            font-size: 2rem;
            font-weight: 700;
            margin: 0.5rem 0;
        }

        .stat-label {
            font-size: 0.9rem;
            color: var(--text-muted);
        }

        .terminal {
            background: #090a0f;
            border-radius: 12px;
            padding: 1.5rem;
            font-family: 'Consolas', monospace;
            font-size: 0.85rem;
            color: #a3acb9;
            height: 300px;
            overflow-y: auto;
            border: 1px solid var(--glass-border);
        }

        .terminal p {
            margin: 4px 0;
            line-height: 1.4;
        }

        .positions-container {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .position-card {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(0, 0, 0, 0.2);
            padding: 1.2rem;
            border-radius: 12px;
            border: 1px solid var(--glass-border);
        }

        .sym { font-weight: 600; font-size: 1.1rem; }
        .sl-tag { color: var(--danger); font-size: 0.9rem; }
        .success-text { color: var(--success); }
        .danger-text { color: var(--danger); }
        
        .table-container { overflow-x: auto; margin-top: 1rem; }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th, td { padding: 12px; border-bottom: 1px solid var(--glass-border); font-size: 0.9rem; }
        th { color: var(--text-muted); font-weight: 400; text-transform: uppercase; letter-spacing: 1px; font-size: 0.8rem; }
        .rule-list { margin: 0; padding-left: 1.5rem; color: #d1d5db; font-size: 0.95rem; line-height: 1.6; }
        .rule-list li { margin-bottom: 0.5rem; }
        .rule-list li::marker { color: var(--accent); }
        .action-box { background: rgba(94, 106, 210, 0.1); border: 1px solid var(--accent); padding: 1rem 1.5rem; border-radius: 12px; margin-bottom: 1.5rem; display: flex; align-items: center; gap: 12px; }
        .action-box .icon { font-size: 1.5rem; }
        .action-box .text-col { display: flex; flex-direction: column; }
        .action-box .lbl { font-size: 0.8rem; color: var(--accent); text-transform: uppercase; font-weight: 600; letter-spacing: 1px;}
        .action-box .val { font-size: 1.1rem; font-weight: 600; color: #fff; }

        /* Option Chain Styles */
        .chain-table th, .chain-table td { padding: 8px 12px; font-size: 0.85rem; text-align: center; }
        .chain-table .call-col { text-align: center; color: #22c55e; }
        .chain-table .put-col { text-align: center; color: #ef4444; }
        .chain-table .strike-col { color: #fff; font-weight: 700; background: rgba(94, 106, 210, 0.15); }
        .chain-table .atm-row { background: rgba(94, 106, 210, 0.08); border-left: 2px solid var(--accent); border-right: 2px solid var(--accent); }
        .chain-header-call { background: rgba(34, 197, 94, 0.08); color: var(--success) !important; text-align: center; }
        .chain-header-put { background: rgba(239, 68, 68, 0.08); color: var(--danger) !important; text-align: center; }
        .chain-header-strike { text-align: center; color: var(--accent) !important; }
        .metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
        .metric-box { background: rgba(0,0,0,0.2); border-radius: 10px; padding: 1rem 1.2rem; border: 1px solid var(--glass-border); text-align: center; }
        .metric-box .m-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .metric-box .m-value { font-size: 1.3rem; font-weight: 700; margin-top: 0.3rem; }
        .spot-badge { display: inline-block; background: rgba(94,106,210,0.2); border: 1px solid var(--accent); color: var(--accent); padding: 4px 12px; border-radius: 20px; font-size: 0.9rem; font-weight: 600; margin-left: 1rem; }

        @keyframes pulse {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.1); opacity: 0.7; }
            100% { transform: scale(1); opacity: 1; }
        }
        .dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: currentColor; display: inline-block;
            animation: pulse 2s infinite;
        }

        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }
        .modal-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8); backdrop-filter: blur(10px);
            display: flex; justify-content: center; align-items: center; z-index: 1000;
        }
        .modal {
            background: #171a25; padding: 3rem; border-radius: 16px; width: 400px;
            border: 1px solid var(--glass-border); box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }
        .modal h2 { margin-top: 0; margin-bottom: 1.5rem; text-align: center; }
        .modal input, .modal select {
            width: 100%; padding: 12px; margin-bottom: 1rem; border-radius: 8px;
            background: rgba(255,255,255,0.05); border: 1px solid var(--glass-border);
            color: #fff; font-family: 'Outfit', sans-serif; box-sizing: border-box;
        }
        .modal button {
            width: 100%; padding: 14px; background: var(--accent); border: none;
            color: #fff; font-family: 'Outfit', sans-serif; font-weight: 600;
            border-radius: 8px; cursor: pointer; transition: background 0.2s;
        }
        .modal button:hover { background: #6b78e6; }
        
        .hidden { display: none !important; }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>
    <div id="auth-modal" class="modal-overlay">
        <div class="modal">
            <h2>Start Trading Bot</h2>
            <select id="net-type">
                <option value="testnet">Delta Exchange Testnet</option>
                <option value="mainnet">Delta Exchange Mainnet</option>
            </select>
            <input type="text" id="api-key" placeholder="Enter API Key">
            <input type="password" id="api-secret" placeholder="Enter API Secret">
            <div style="display:flex; gap:0.75rem; align-items:center; margin-bottom:1rem;">
                <label style="color:var(--text-muted); font-size:0.9rem; white-space:nowrap;">Lots per order:</label>
                <input type="number" id="lot-size" value="1" min="1" max="100" style="width:70px; margin-bottom:0; text-align:center; font-weight:600; font-size:1rem; padding:8px; border-radius:8px; background:rgba(255,255,255,0.05); border:1px solid var(--glass-border); color:#fff; font-family:'Outfit', sans-serif;">
                <span style="color:var(--text-muted); font-size:0.82rem;">(1 lot = 1 Call + 1 Put)</span>
            </div>
            <div style="display:flex; gap:1rem; margin-bottom:1.5rem;">
                <div style="flex:1;">
                    <label style="color:var(--text-muted); font-size:0.8rem; display:block; margin-bottom:4px;">Entry Time (IST)</label>
                    <input type="time" id="entry-time" value="21:31" style="width:100%; padding:10px; margin-bottom:0; border-radius:8px; background:rgba(255,255,255,0.05); border:1px solid var(--glass-border); color:#fff; font-family:'Outfit', sans-serif;">
                </div>
                <div style="flex:1;">
                    <label style="color:var(--text-muted); font-size:0.8rem; display:block; margin-bottom:4px;">Exit Time (IST)</label>
                    <input type="time" id="exit-time" value="13:00" style="width:100%; padding:10px; margin-bottom:0; border-radius:8px; background:rgba(255,255,255,0.05); border:1px solid var(--glass-border); color:#fff; font-family:'Outfit', sans-serif;">
                </div>
            </div>
            <button onclick="startBot()">Connect & Run</button>
        </div>
    </div>

    <div class="navbar">
        <h1>Delta ETH Automator</h1>
        <div id="conn-status" class="status-badge disconnected">
            <span class="dot"></span>
            <span id="conn-text">Awaiting Keys</span>
        </div>
    </div>

    <div class="container">
        <!-- Overview Card -->
        <div class="card" style="grid-column: span 2;">
            <h2>Dashboard Overview</h2>
            
            <div class="action-box">
                <div class="icon">🤖</div>
                <div class="text-col">
                    <span class="lbl">Bot Current Action</span>
                    <span class="val" id="next-action">Waiting for initialization...</span>
                </div>
            </div>

            <div class="stat-grid" style="grid-template-columns: repeat(6, 1fr);">
                <div class="stat-box">
                    <div class="stat-label">Account Balance</div>
                    <div class="stat-value" id="account-balance" style="color: #fff;">0.00 <span style="font-size:0.8rem;color:var(--text-muted);">USDT</span></div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Wallet Balance</div>
                    <div class="stat-value" id="wallet-balance" style="color: #fff;">0.00 <span style="font-size:0.8rem;color:var(--text-muted);">USDT</span></div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Bot Status</div>
                    <div class="stat-value" id="bot-status" style="font-size: 1.2rem; margin-top: 1rem; color: var(--accent);">Initialize</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Max Profit</div>
                    <div class="stat-value" id="premium">0.00 USD</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Current PNL</div>
                    <div class="stat-value" id="pnl">0.00 USD</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Daily Entries</div>
                    <div class="stat-value" id="entries">0 / 3</div>
                </div>
            </div>
        </div>

        <!-- Strategy Rules Card -->
        <div class="card">
            <h2>Strategy Rules</h2>
            <ul class="rule-list">
                <li><strong>Schedule:</strong> Monday to Friday only</li>
                <li><strong>Entry:</strong> <span id="rule-entry-time">09:31 PM</span> IST — Sell 1 Call + 1 Put (premium &gt; $50)</li>
                <li><strong>Stop Loss:</strong> 80% above entry premium per leg (SL = entry &times; 1.8)</li>
                <li><strong>Re-Entry:</strong> Wait 1 min after SL hit, then re-enter. Max 3 times/day.</li>
                <li><strong>Take Profit:</strong> Exit ALL when PnL &ge; 50% of total premium collected</li>
                <li><strong>Max Loss:</strong> Exit ALL when loss &ge; total premium collected</li>
                <li><strong>Leg Exit:</strong> Close single leg if premium drops below $5 (margin release)</li>
                <li><strong>Hard Cut-off:</strong> Exit ALL remaining positions at <span id="rule-exit-time">01:00 PM</span> IST</li>
            </ul>
        </div>

        <!-- Legs Card -->
        <div class="card">
            <h2>Active Legs</h2>
            <div id="positions" class="positions-container">
                <p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">No active positions right now.</p>
            </div>
        </div>

        <!-- Option Chain Card -->
        <div class="card" style="grid-column: span 2;">
            <h2>
                ETH ATM Option Chain
                <span class="spot-badge" id="eth-spot-badge">ETH: Loading...</span>
                <span style="font-size:0.75rem;color:var(--text-muted);margin-left:12px;" id="chain-expiry"></span>
            </h2>

            <!-- Payoff Metrics -->
            <div class="metric-grid" id="payoff-metrics">
                <div class="metric-box">
                    <div class="m-label">Max Profit</div>
                    <div class="m-value success-text" id="m-maxprofit">--</div>
                </div>
                <div class="metric-box">
                    <div class="m-label">Max Loss</div>
                    <div class="m-value danger-text" id="m-maxloss">Unlimited</div>
                </div>
                <div class="metric-box">
                    <div class="m-label">Upper Breakeven</div>
                    <div class="m-value" id="m-upbe" style="color:#f59e0b;">--</div>
                </div>
                <div class="metric-box">
                    <div class="m-label">Lower Breakeven</div>
                    <div class="m-value" id="m-lobe" style="color:#f59e0b;">--</div>
                </div>
            </div>

            <!-- Payoff Chart -->
            <div style="position:relative;height:280px;margin-bottom:1.5rem;">
                <canvas id="payoff-chart"></canvas>
                <div id="chart-placeholder" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:var(--text-muted);text-align:center;">
                    <div style="font-size:2rem;">📊</div>
                    <div>Payoff chart appears when positions are open</div>
                </div>
            </div>

            <!-- Option Chain Table -->
            <div class="table-container">
                <table class="chain-table">
                    <thead>
                        <tr>
                            <th class="chain-header-call" colspan="3">CALLS</th>
                            <th class="chain-header-strike">STRIKE</th>
                            <th class="chain-header-put" colspan="3">PUTS</th>
                        </tr>
                        <tr>
                            <th class="call-col">IV %</th>
                            <th class="call-col">Delta</th>
                            <th class="call-col">Mark $</th>
                            <th class="chain-header-strike">Price</th>
                            <th class="put-col">Mark $</th>
                            <th class="put-col">Delta</th>
                            <th class="put-col">IV %</th>
                        </tr>
                    </thead>
                    <tbody id="chain-table-body">
                        <tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:2rem;">Loading option chain...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Order History Card -->
        <div class="card" style="grid-column: span 2;">
            <h2>Session Order History</h2>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Symbol</th>
                            <th>Entry Price</th>
                            <th>Close Price</th>
                            <th>Exit Status</th>
                        </tr>
                    </thead>
                    <tbody id="order-history">
                        <tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No completed trades in this session.</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Logs Card -->
        <div class="card">
            <h2>System Logs</h2>
            <div class="terminal" id="terminal">
            </div>
        </div>
    </div>

    <script>
        async function fetchState() {
            try {
                const res = await fetch('/api/state');
                const state = await res.json();
                
                // Update Connection
                document.getElementById('conn-status').className = state.connection === "Connected" ? 'status-badge' : 'status-badge disconnected';
                document.getElementById('conn-text').innerText = state.connection;

                // Stats
                document.getElementById('account-balance').innerHTML = (state.account_balance || 0).toFixed(2) + ' <span style="font-size:0.8rem;color:var(--text-muted);">BAL</span>';
                document.getElementById('wallet-balance').innerHTML = (state.wallet_balance || 0).toFixed(2) + ' <span style="font-size:0.8rem;color:var(--text-muted);">AVAIL</span>';
                document.getElementById('next-action').innerText = state.next_action || "Waiting for initialization...";
                document.getElementById('bot-status').innerText = state.status;
                document.getElementById('premium').innerText = state.total_initial_premium.toFixed(2) + " USD";
                
                // Update dynamic rules
                if (state.entry_time) {
                    const [h, m] = state.entry_time.split(':');
                    const displayTime = (h % 12 || 12) + ":" + m + (h >= 12 ? ' PM' : ' AM');
                    document.getElementById('rule-entry-time').innerText = displayTime;
                }
                if (state.exit_time) {
                    const [h, m] = state.exit_time.split(':');
                    const displayTime = (h % 12 || 12) + ":" + m + (h >= 12 ? ' PM' : ' AM');
                    document.getElementById('rule-exit-time').innerText = displayTime;
                }
                
                const pnlEl = document.getElementById('pnl');
                pnlEl.innerText = state.current_pnl.toFixed(2) + " USD";
                pnlEl.className = "stat-value " + (state.current_pnl >= 0 ? 'success-text' : 'danger-text');
                
                document.getElementById('entries').innerText = state.daily_entries + " / 3";

                // Compute + render payoff chart
                renderPayoffChart(state);

                // Positions
                let posHtml = "";
                const legs = Object.keys(state.active_legs);
                if (legs.length === 0) {
                    posHtml = '<p style="color: var(--text-muted); text-align: center; margin-top: 2rem;">No active positions right now.</p>';
                } else {
                    legs.forEach(symbol => {
                        const leg = state.active_legs[symbol];
                        if(leg.status === "open"){
                            posHtml += `
                                <div class="position-card">
                                    <div>
                                        <div class="sym">${symbol} <span style="font-size: 0.8rem; color:#8e95b0; border: 1px solid #8e95b0; padding: 2px 6px; border-radius: 4px; margin-left: 8px;">SELL</span></div>
                                        <div style="font-size: 0.9rem; color: #8e95b0; margin-top: 4px;">Entry: $${leg.entry_price}</div>
                                    </div>
                                    <div style="text-align: right;">
                                        <div class="sl-tag">SL @ $${leg.stoploss}</div>
                                    </div>
                                </div>
                            `;
                        }
                    });
                }
                document.getElementById('positions').innerHTML = posHtml;

                // Order History
                let histHtml = "";
                if (state.order_history && state.order_history.length > 0) {
                    [...state.order_history].reverse().forEach(ord => {
                        histHtml += `
                            <tr>
                                <td style="color: var(--text-muted); text-align: center;">${ord.time}</td>
                                <td style="font-weight: 600;">${ord.symbol}</td>
                                <td>$${ord.entry_price}</td>
                                <td>${ord.close_price === "Market" ? "Market" : "$" + parseFloat(ord.close_price).toFixed(1)}</td>
                                <td><span style="background: rgba(255,255,255,0.1); padding: 4px 8px; border-radius: 4px; font-size: 0.8rem;">${ord.status}</span></td>
                            </tr>
                        `;
                    });
                } else {
                    histHtml = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No completed trades in this session.</td></tr>';
                }
                document.getElementById('order-history').innerHTML = histHtml;

                // Logs
                let logsHtml = "";
                state.logs.forEach(msg => {
                    logsHtml += `<p>${msg}</p>`;
                });
                document.getElementById('terminal').innerHTML = logsHtml;

            } catch (err) {
                console.error("Endpoint poll failed", err);
            }
        }

        async function startBot() {
            const btn = document.querySelector('.modal button');
            const key = document.getElementById('api-key').value;
            const secret = document.getElementById('api-secret').value;
            const net = document.getElementById('net-type').value;
            if(!key || !secret) { alert("Please enter both keys!"); return; }
            
            btn.innerText = "Connecting...";
            try {
                const lots = parseInt((document.getElementById('lot-size') || {value:'1'}).value) || 1;
                const entry_time = document.getElementById('entry-time').value;
                const exit_time = document.getElementById('exit-time').value;
                if (lots < 1 || lots > 100) { alert("Lots must be between 1 and 100."); btn.innerText = "Connect & Run"; return; }
                await fetch('/api/start', {
                    method: 'POST',
                    body: JSON.stringify({ key, secret, net, lots, entry_time, exit_time })
                });
                document.getElementById('auth-modal').classList.add('hidden');
                document.getElementById('conn-text').innerText = "Connecting...";
                setInterval(fetchState, 1500);
                setTimeout(fetchOptionChain, 1000);
                setInterval(fetchOptionChain, 10000);
            } catch (err) {
                alert("Failed to communicate with bot.");
                btn.innerText = "Connect & Run";
            }
        }

        // 🕙 Option Chain 🕙
        async function fetchOptionChain() {
            try {
                const res  = await fetch('/api/optionchain');
                const data = await res.json();
                const spot  = data.eth_spot || 0;
                const chain = data.option_chain || [];
                const expiry = data.nearest_expiry || '';

                document.getElementById('eth-spot-badge').innerText =
                    spot > 0 ? `ETH: $${spot.toFixed(2)}` : 'ETH: --';
                document.getElementById('chain-expiry').innerText =
                    expiry ? `Expiry: ${expiry.substring(0, 10)}` : '';

                // Pair calls & puts by strike
                const callMap = {}, putMap = {};
                chain.forEach(o => {
                    if (o.type === 'call_options') callMap[o.strike] = o;
                    else                           putMap[o.strike]  = o;
                });
                const allStrikes = [...new Set(chain.map(o => o.strike))].sort((a,b) => a - b);
                const strikeStep  = allStrikes.length > 1 ? allStrikes[1] - allStrikes[0] : 50;

                let chainHtml = '';
                allStrikes.forEach(strike => {
                    const call   = callMap[strike] || {};
                    const put    = putMap[strike]  || {};
                    const isAtm  = spot > 0 && Math.abs(strike - spot) < strikeStep * 0.75;
                    const atmCls = isAtm ? 'atm-row' : '';
                    chainHtml += `
                        <tr class="${atmCls}">
                            <td class="call-col">${call.iv    ? call.iv.toFixed(1) + '%'    : '--'}</td>
                            <td class="call-col">${call.delta !== undefined ? call.delta.toFixed(3) : '--'}</td>
                            <td class="call-col" style="font-weight:600;">${call.mark_price ? '$' + call.mark_price.toFixed(2) : '--'}</td>
                            <td class="strike-col">${isAtm ? '▶ ' : ''}$${strike.toLocaleString()}</td>
                            <td class="put-col"  style="font-weight:600;">${put.mark_price  ? '$' + put.mark_price.toFixed(2)  : '--'}</td>
                            <td class="put-col">${put.delta  !== undefined ? put.delta.toFixed(3)  : '--'}</td>
                            <td class="put-col">${put.iv     ? put.iv.toFixed(1) + '%'     : '--'}</td>
                        </tr>`;
                });
                document.getElementById('chain-table-body').innerHTML =
                    chainHtml || '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:1.5rem;">No option data — waiting for next refresh.</td></tr>';

            } catch(e) { console.error('Option chain fetch error', e); }
        }

        // 🕙 Payoff Chart 🕙
        let payoffChart = null;
        function renderPayoffChart(state) {
            const legs    = state.active_legs || {};
            const spot    = state.eth_spot    || 0;
            const openLegs = Object.values(legs).filter(l => l.status === 'open' && l.strike && l.strike > 0);

            const chartEl      = document.getElementById('payoff-chart');
            const placeholder  = document.getElementById('chart-placeholder');

            if (openLegs.length === 0 || spot === 0) {
                placeholder.style.display = 'block';
                chartEl.style.display     = 'none';
                document.getElementById('m-maxprofit').innerText = '--';
                document.getElementById('m-upbe').innerText      = '--';
                document.getElementById('m-lobe').innerText      = '--';
                if (payoffChart) { payoffChart.destroy(); payoffChart = null; }
                return;
            }
            placeholder.style.display = 'none';
            chartEl.style.display     = 'block';

            // Compute payoff array
            const low   = spot * 0.65;
            const high  = spot * 1.35;
            const steps = 120;
            const step  = (high - low) / steps;
            const prices = [], payoffs = [];

            for (let i = 0; i <= steps; i++) {
                const p = low + i * step;
                let pnl = 0;
                openLegs.forEach(leg => {
                    const prem   = parseFloat(leg.entry_price) || 0;
                    const strike = parseFloat(leg.strike)      || 0;
                    if (leg.option_type === 'call_options') pnl += prem - Math.max(0, p - strike);
                    else                                    pnl += prem - Math.max(0, strike - p);
                });
                prices.push(p.toFixed(0));
                payoffs.push(+pnl.toFixed(2));
            }

            // Breakeven metrics
            const totalPrem = openLegs.reduce((s,l) => s + parseFloat(l.entry_price||0), 0);
            const calls  = openLegs.filter(l => l.option_type === 'call_options').sort((a,b) => a.strike - b.strike);
            const puts   = openLegs.filter(l => l.option_type === 'put_options').sort( (a,b) => b.strike - a.strike);
            const upBE   = calls.length ? (parseFloat(calls[0].strike) + totalPrem).toFixed(2) : '--';
            const loBE   = puts.length  ? (parseFloat(puts[0].strike)  - totalPrem).toFixed(2) : '--';
            document.getElementById('m-maxprofit').innerText = '$' + totalPrem.toFixed(2);
            document.getElementById('m-upbe').innerText = upBE !== '--' ? '$' + upBE : '--';
            document.getElementById('m-lobe').innerText = loBE !== '--' ? '$' + loBE : '--';

            // Build chart
            if (payoffChart) { payoffChart.destroy(); payoffChart = null; }
            payoffChart = new Chart(chartEl, {
                type: 'line',
                data: {
                    labels: prices,
                    datasets: [
                        {
                            label: 'P&L at Expiry',
                            data: payoffs,
                            borderColor: '#5e6ad2',
                            backgroundColor: (ctx) => {
                                const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 260);
                                g.addColorStop(0, 'rgba(94,106,210,0.35)');
                                g.addColorStop(1, 'rgba(94,106,210,0.0)');
                                return g;
                            },
                            fill: true, tension: 0.4, pointRadius: 0, borderWidth: 2
                        },
                        {
                            label: 'Zero',
                            data: prices.map(() => 0),
                            borderColor: 'rgba(239,68,68,0.5)',
                            borderDash: [6, 4],
                            pointRadius: 0, borderWidth: 1, fill: false
                        }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: '#171a25',
                            borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1,
                            callbacks: { label: ctx => ` P&L: $${ctx.raw.toFixed(2)}` }
                        }
                    },
                    scales: {
                        x: { grid: { color:'rgba(255,255,255,0.05)' }, ticks: { color:'#8e95b0', maxTicksLimit: 10 } },
                        y: { grid: { color:'rgba(255,255,255,0.05)' }, ticks: { color:'#8e95b0', callback: v => '$'+v } }
                    }
                }
            });
        }
        
    </script>
</body>
</html>
"""

class BotAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # Suppress standard HTTP logs

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
        elif self.path == '/api/state':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            safe = {k: v for k, v in BOT_STATE.items() if k not in ['api_key', 'api_secret']}
            self.wfile.write(json.dumps(safe).encode('utf-8'))
        elif self.path == '/api/optionchain':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            payload = {
                "eth_spot": BOT_STATE.get("eth_spot", 0),
                "option_chain": BOT_STATE.get("option_chain", []),
                "nearest_expiry": BOT_STATE.get("nearest_expiry", "")
            }
            self.wfile.write(json.dumps(payload).encode('utf-8'))
    
    def do_POST(self):
        if self.path == '/api/start':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            BOT_STATE["api_key"]     = data.get('key', '')
            BOT_STATE["api_secret"]  = data.get('secret', '')
            BOT_STATE["lots_per_trade"] = max(1, int(data.get('lots', 1)))
            BOT_STATE["entry_time"] = data.get('entry_time', '21:31')
            BOT_STATE["exit_time"] = data.get('exit_time', '13:00')

            global BASE_URL, SIZE_CONTRACTS
            SIZE_CONTRACTS = BOT_STATE["lots_per_trade"]  # sync global used by orders
            log(f"Lot size set to {SIZE_CONTRACTS} lot(s) per leg.")

            if data.get('net') == 'mainnet':
                BASE_URL = "https://api.delta.exchange"
            else:
                BASE_URL = "https://cdn.india.deltaex.org"
                
            BOT_STATE["bot_running"] = True
            log("API Keys received via Web UI. Starting Bot loops...")
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status":"ok"}).encode('utf-8'))
            save_state()
        else:
            self.send_response(404)
            self.end_headers()

def run_http_server():
    server = HTTPServer(('0.0.0.0', PORT), BotAPIHandler)
    log(f"HTTP Dashboard started. Visit http://localhost:{PORT}")
    server.serve_forever()

def get_ist_time():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    # UTC+5:30
    ist_time = now_utc + datetime.timedelta(hours=5, minutes=30)
    return ist_time

def fetch_and_update_balance(client):
    """Fetch wallet balance from Delta Exchange and update BOT_STATE."""
    try:
        bals = client.get_wallet_balances()
        if bals and isinstance(bals, list) and len(bals) > 0:
            # Log all assets found for debugging
            asset_names = [b.get('asset_symbol', b.get('asset', '?')) for b in bals]
            log(f"[DEBUG] Wallet assets found: {asset_names}")
            # Pick the primary balance asset (USDT preferred, fallback to first non-zero)
            chosen = None
            for b in bals:
                sym = str(b.get("asset_symbol", b.get("asset", b.get("symbol", ""))))
                wb = float(b.get("available_balance", 0) or 0)
                ab = float(b.get("balance", wb) or wb)
                if sym in ["USDT", "INR", "tUSDT"] and ab > 0:
                    chosen = (wb, ab)
                    break
            if not chosen:
                for b in bals:
                    wb = float(b.get("available_balance", 0) or 0)
                    ab = float(b.get("balance", wb) or wb)
                    if ab > 0:
                        chosen = (wb, ab)
                        break
            if chosen:
                BOT_STATE["wallet_balance"] = chosen[0]
                BOT_STATE["account_balance"] = chosen[1]
                log(f"Balance updated: Total={chosen[1]:.2f} | Available={chosen[0]:.2f}")
            else:
                log("[DEBUG] Wallet API returned data but all balances are 0.")
        else:
            log(f"[DEBUG] Wallet API returned no data or failed: {bals}")
    except Exception as e:
        log(f"Wallet Fetch Error: {e}")

def fetch_option_chain_data(client):
    """Fetch ETH ATM option chain, spot price and update BOT_STATE."""
    try:
        tickers = client.get_all_eth_tickers()
        if not tickers:
            return
        ticker_map = {t["symbol"]: t for t in tickers}

        # 🕙 Find ETH spot/perp price 🕙
        eth_spot = 0.0
        # Priority: known perp symbols
        for sym in ["ETHUSD", "ETH_USDT", "ETHUSDQ", "ETHUSDT", ".DETHUSDT"]:
            t = ticker_map.get(sym, {})
            price = float(t.get("close", 0) or t.get("mark_price", 0) or 0)
            if price > 100:
                eth_spot = price
                break
        if eth_spot == 0:
            for t in tickers:
                sym = t.get("symbol", "")
                price = float(t.get("mark_price", 0) or 0)
                if "ETH" in sym and "USD" in sym and price > 100 and "-" in sym:
                    eth_spot = price
                    break

        # 🕙 Get ETH options 🕙
        options = client.get_eth_options()
        if not options:
            return

        chain = []
        for opt in options:
            sym = opt.get("symbol", "")
            t = ticker_map.get(sym, {})
            mark = float(t.get("mark_price", 0) or 0)
            strike = float(opt.get("strike_price", 0))
            iv = float(t.get("ask_iv", t.get("bid_iv", t.get("iv", 0))) or 0)
            delta = float(t.get("delta", 0) or 0)
            chain.append({
                "symbol": sym,
                "strike": strike,
                "type": opt.get("contract_type"),
                "expiry": opt.get("settlement_time", ""),
                "mark_price": round(mark, 2),
                "iv": round(iv, 2),
                "delta": round(delta, 4),
            })

        if not chain:
            return

        # 🕙 Filter to nearest expiry 🕙
        chain.sort(key=lambda x: x["expiry"])
        nearest_expiry = chain[0]["expiry"]
        atm_chain = [o for o in chain if o["expiry"] == nearest_expiry]
        atm_chain.sort(key=lambda x: x["strike"])

        # Slice ±6 strikes around ATM
        if eth_spot > 0 and atm_chain:
            atm_idx = min(range(len(atm_chain)), key=lambda i: abs(atm_chain[i]["strike"] - eth_spot))
            start = max(0, atm_idx - 6)
            end   = min(len(atm_chain), atm_idx + 7)
            display = atm_chain[start:end]
        else:
            display = atm_chain[:14]

        BOT_STATE["eth_spot"]       = eth_spot
        BOT_STATE["option_chain"]   = display
        BOT_STATE["nearest_expiry"] = nearest_expiry
        log(f"Option chain updated: {len(display)} options | ETH Spot: ${eth_spot:.2f}")

    except Exception as e:
        log(f"Option chain fetch error: {e}")


def trading_bot_loop():
    # Wait until keys are configured from UI
    while not BOT_STATE["bot_running"]:
        time.sleep(1)

    log(f"[DEBUG] DeltaClient created — Key: '{BOT_STATE['api_key'][:6]}...' URL: {BASE_URL}")
    client = DeltaClient(BOT_STATE["api_key"], BOT_STATE["api_secret"], BASE_URL)
    
    # Check connection using a public endpoint first
    test_req = client.get_tickers()
    if test_req:
        BOT_STATE["connection"] = "Connected"
        log("Delta Exchange API Connection Verified.")
    else:
        BOT_STATE["connection"] = "Failed"
        log("Cannot connect to Delta Exchange API. Re-check your keys.")

    # Fetch balance IMMEDIATELY on startup — don't wait for the loop
    fetch_and_update_balance(client)

    # Fetch option chain immediately
    fetch_option_chain_data(client)

    balance_counter = 0
    chain_counter   = 0

    while True:
        try:
            now_ist = get_ist_time()
            BOT_STATE["status"] = f"Monitoring (IST Time: {now_ist.strftime('%H:%M:%S')})"

            # Refresh balance every 30 iterations (~60s)
            balance_counter += 1
            if balance_counter >= 30:
                fetch_and_update_balance(client)
                balance_counter = 0

            # Refresh option chain every 60 iterations (~120s)
            chain_counter += 1
            if chain_counter >= 60:
                fetch_option_chain_data(client)
                chain_counter = 0

            if BOT_STATE["trading_done_for_day"]:
                BOT_STATE["next_action"] = "Trading done for day. Waiting for midnight reset."
            elif BOT_STATE["reentry_pending_at"]:
                secs_left = max(0, int(BOT_STATE["reentry_pending_at"] - time.time()))
                BOT_STATE["next_action"] = f"SL Hit — Re-entry in {secs_left}s..."
            elif not BOT_STATE["active_legs"]:
                is_weekday = now_ist.weekday() < 5  # 0=Mon, 4=Fri
                entry_h, entry_m = map(int, BOT_STATE["entry_time"].split(':'))
                exit_h, exit_m = map(int, BOT_STATE["exit_time"].split(':'))
                
                if not is_weekday:
                    BOT_STATE["next_action"] = "Weekend — No trading (Mon-Fri only)"
                elif not (now_ist.hour > entry_h or (now_ist.hour == entry_h and now_ist.minute >= entry_m)):
                    BOT_STATE["next_action"] = f"Waiting for Entry at {BOT_STATE['entry_time']} IST (Mon-Fri)"
                elif now_ist.hour > exit_h or (now_ist.hour == exit_h and now_ist.minute >= exit_m):
                    # This logic handles if current time is past exit but we haven't reached entry yet (if overnight)
                    # Or if it's simply past the exit time of the same day.
                    # Given the original logic, it seems they want to block entries after the exit time.
                    BOT_STATE["next_action"] = f"Past {BOT_STATE['exit_time']} — No more entries today"
                else:
                    BOT_STATE["next_action"] = "Watching for re-entry conditions..."
            else:
                BOT_STATE["next_action"] = "Monitoring Open Legs (SL / Target / 1 PM Exit)"

            # 🕙 Non-blocking re-entry after SL 🕙
            # Check open legs only (closed legs remain in dict, can't use `not active_legs`)
            open_legs_exist = any(v["status"] == "open" for v in BOT_STATE["active_legs"].values())
            if BOT_STATE["reentry_pending_at"] and not open_legs_exist:
                if time.time() >= BOT_STATE["reentry_pending_at"]:
                    BOT_STATE["reentry_pending_at"] = None
                    log("60s wait complete. Executing re-entry now...")
                    execute_entry(client)

            # 🕙 Entry condition: Dynamic Time, Mon-Fri only 🕙
            entry_h, entry_m = map(int, BOT_STATE["entry_time"].split(':'))
            is_entry_time = (now_ist.hour == entry_h and now_ist.minute == entry_m
                             and now_ist.weekday() < 5)
            has_open = any(v["status"] == "open" for v in BOT_STATE["active_legs"].values())
            if is_entry_time and not has_open and not BOT_STATE["trading_done_for_day"] and not BOT_STATE["reentry_pending_at"]:
                if BOT_STATE["daily_entries"] < BOT_STATE["max_daily_entries"]:
                    execute_entry(client)
            
            # Reset daily flags at midnight (guard flag prevents 30x firing)
            if now_ist.hour == 0 and now_ist.minute == 0 and now_ist.second < 4:
                with STATE_LOCK:
                    BOT_STATE["trading_done_for_day"] = False
                    BOT_STATE["daily_entries"]         = 0
                    BOT_STATE["active_legs"]           = {}
                    BOT_STATE["total_initial_premium"] = 0.0
                    BOT_STATE["current_pnl"]           = 0.0
                    BOT_STATE["reentry_pending_at"]    = None
                log("Midnight reset complete. Bot ready for new trading day.")
                time.sleep(4)  # skip remaining seconds of this minute

            if BOT_STATE["active_legs"] and not BOT_STATE["trading_done_for_day"]:
                monitor_open_legs(client, now_ist)
                
            time.sleep(2)
        except Exception as e:
            log(f"Bot loop error: {e}")
            time.sleep(5)

def execute_entry(client):
    log("Fetching options logic...")
    options = client.get_eth_options()
    tickers = client.get_tickers()
    
    ticker_map = {t["symbol"]: float(t.get("mark_price", 0) or 0) for t in tickers}
    
    valid_options = []
    for opt in options:
        sym = opt["symbol"]
        if ticker_map.get(sym, 0.0) > 50:
            valid_options.append({
                "symbol": sym,
                "product_id": opt["id"],
                "contract_type": opt["contract_type"],
                "mark_price": ticker_map[sym],
                "expiry": opt["settlement_time"],
                "strike_price": float(opt.get("strike_price", 0))  # Bug 4 fix: include strike
            })

    if not valid_options:
        log("No valid options found with premium > 50 USD.")
        return

    # Sort by nearest expiry first
    valid_options.sort(key=lambda x: x["expiry"])
    target_expiry = valid_options[0]["expiry"]
    options_for_expiry = [o for o in valid_options if o["expiry"] == target_expiry]

    calls = [o for o in options_for_expiry if o["contract_type"] == "call_options"]
    puts  = [o for o in options_for_expiry if o["contract_type"] == "put_options"]

    # Bug 5 fix: pick ATM strike (closest to current ETH spot), not highest premium
    eth_spot = BOT_STATE.get("eth_spot", 0)
    if eth_spot > 0:
        calls.sort(key=lambda x: abs(x["strike_price"] - eth_spot))
        puts.sort(key=lambda  x: abs(x["strike_price"] - eth_spot))
    else:
        # Fallback: highest premium if spot unknown
        calls.sort(key=lambda x: -x["mark_price"])
        puts.sort(key=lambda  x: -x["mark_price"])

    best_call = calls[0] if calls else None
    best_put  = puts[0]  if puts  else None
    
    legs_to_execute = [l for l in [best_call, best_put] if l]
    
    if not legs_to_execute:
        log("Unable to find matching calls/puts.")
        return

    for leg in legs_to_execute:
        sl_price = round(leg['mark_price'] * 1.8)
        log(f"Placing SELL order: {leg['symbol']} | premium=${leg['mark_price']} | SL=${sl_price}")

        # 🕙 REAL ORDER TO DELTA EXCHANGE 🕙
        order = client.place_order(leg["product_id"], side="sell", size=SIZE_CONTRACTS)

        if order is None:
            log(f"SKIPPING {leg['symbol']} — order placement failed. Check API key permissions / margin.")
            continue  # Don't add to active_legs if order failed

        # Use actual fill price if available, else mark price
        fill_price = float(order.get("average_fill_price") or leg['mark_price'])

        sl_price = round(fill_price * 1.8)
        BOT_STATE["active_legs"][leg["symbol"]] = {
            "product_id": leg["product_id"],
            "entry_price": fill_price,
            "stoploss": sl_price,
            "strike": float(leg.get("strike_price", 0)),
            "option_type": leg.get("contract_type", ""),
            "order_id": order.get("id", ""),
            "status": "open"
        }
        BOT_STATE["total_initial_premium"] += fill_price
        log(f"LIVE POSITION: SELL {leg['symbol']} @ ${fill_price:.2f} | Strike={leg.get('strike_price','?')} | SL=${sl_price} | Order ID={order.get('id','?')}")
        save_state()

    BOT_STATE["daily_entries"] += 1

def monitor_open_legs(client, now_ist):
    # 🕙 Dynamic hard exit 🕙
    exit_h, exit_m = map(int, BOT_STATE["exit_time"].split(':'))
    if now_ist.hour == exit_h and now_ist.minute == exit_m:
        log(f"Hard Exit: {BOT_STATE['exit_time']} IST reached. Closing all positions.")
        close_all_positions(client, f"Time Exit ({BOT_STATE['exit_time']})")
        return

    tickers = client.get_tickers()
    ticker_map = {t["symbol"]: float(t.get("mark_price", 0) or 0) for t in tickers}

    open_legs = {k: v for k, v in BOT_STATE["active_legs"].items() if v["status"] == "open"}
    if not open_legs: return

    current_total_value = 0.0
    sl_hit_this_cycle = False

    for symbol, leg_data in open_legs.items():
        mark = ticker_map.get(symbol)
        if mark is None: continue
        current_total_value += mark

        # Premium below $5 — close that leg
        if mark < 5.0:
            log(f"Premium for {symbol} < $5 USD. Closing leg via market BUY.")
            client.close_position(leg_data["product_id"], size=SIZE_CONTRACTS)
            BOT_STATE["active_legs"][symbol]["status"] = "closed"
            BOT_STATE["order_history"].append({
                "symbol": symbol,
                "entry_price": leg_data["entry_price"],
                "close_price": mark,
                "status": "Closed (Pr < 5)",
                "time": now_ist.strftime('%H:%M:%S')
            })
            save_state()
            continue

        # Stoploss hit — close that leg
        if mark >= leg_data["stoploss"]:
            log(f"STOPLOSS HIT! {symbol} mark=${mark:.2f} >= SL=${leg_data['stoploss']}. Placing BUY to close.")
            client.close_position(leg_data["product_id"], size=SIZE_CONTRACTS)
            BOT_STATE["active_legs"][symbol]["status"] = "closed"
            BOT_STATE["order_history"].append({
                "symbol": symbol,
                "entry_price": leg_data["entry_price"],
                "close_price": mark,
                "status": "SL Hit",
                "time": now_ist.strftime('%H:%M:%S')
            })
            save_state()
            sl_hit_this_cycle = True

    current_pnl = BOT_STATE["total_initial_premium"] - current_total_value
    BOT_STATE["current_pnl"] = current_pnl

    max_profit   = BOT_STATE["total_initial_premium"]   # premium collected = max profit
    target_profit = max_profit * 0.5                     # exit at 50% of max profit
    max_loss      = -max_profit                          # exit when loss equals full premium

    if current_pnl >= target_profit:
        log(f"TARGET HIT: PnL ${current_pnl:.2f} ≥ 50% target ${target_profit:.2f}. Exiting all.")
        close_all_positions(client, "Target Profit (50%)")
        return

    if current_pnl <= max_loss:
        log(f"MAX LOSS HIT: PnL ${current_pnl:.2f} ≤ max loss -${abs(max_loss):.2f}. Exiting all.")
        close_all_positions(client, "Max Loss Reached")
        return

    if sl_hit_this_cycle:
        if BOT_STATE["daily_entries"] >= BOT_STATE["max_daily_entries"]:
            log("All 3 entries used after SL. Final exit for the day.")
            close_all_positions(client, "Max Entries (3) Reached")
        else:
            remaining = BOT_STATE["max_daily_entries"] - BOT_STATE["daily_entries"]
            log(f"SL Hit. Scheduling re-entry in 60s. ({remaining} entries remaining today)")
            BOT_STATE["reentry_pending_at"] = time.time() + 60

def close_all_positions(client, reason="Forced Exit"):
    """Place BUY orders to close all open short positions on Delta Exchange."""
    now_ist = get_ist_time()
    open_legs = {sym: leg for sym, leg in BOT_STATE["active_legs"].items() if leg["status"] == "open"}

    if open_legs:
        log(f"Closing {len(open_legs)} open leg(s) via market orders. Reason: {reason}")

    for sym, leg in open_legs.items():
        log(f"Placing BUY (close) order for {sym}...")
        order = client.close_position(leg["product_id"], size=SIZE_CONTRACTS)
        fill_price = float(order.get("average_fill_price") or 0) if order else 0
        display_price = f"${fill_price:.2f}" if fill_price else "Market"

        leg["status"] = "closed"
        BOT_STATE["order_history"].append({
            "symbol": sym,
            "entry_price": leg["entry_price"],
            "close_price": display_price,
            "status": reason,
            "time": now_ist.strftime('%H:%M:%S')
        })

    BOT_STATE["reentry_pending_at"] = None
    BOT_STATE["trading_done_for_day"] = True
    log(f"All positions closed. Reason: {reason}")
    save_state()

if __name__ == "__main__":
    log(f"HTTP Dashboard started. Visit http://localhost:{PORT}")
    log("Awaiting API key initialization from the web interface...")

    load_state()

    # Start web server thread
    web_thread = threading.Thread(target=run_http_server, daemon=True)
    web_thread.start()

    # Start bot operations in main thread
    trading_bot_loop()
