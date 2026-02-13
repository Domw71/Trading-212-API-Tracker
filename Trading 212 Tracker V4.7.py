# Trading212 Portfolio Pro — v4.7 (Added rich-text Notes tab with formatting & save/load)
# Updated: New "Notes" tab with bold/italic/underline, font size/family/color, alignment, bullet support
# ------------------------------------------------------------
# Features & fixes summary (most recent):
# • Double-click ticker in Historical Highs & Lows opens price history graph
# • FIXED: Low-priced tickers now show correct current price (e.g. £0.0300)
# • NEW: Anomaly logging for sudden changes (> £2 or >2%) in total_assets
# • NEW: Rich-text "Notes" tab (bold, italic, underline, font size/family/color, alignment, save/load to JSON)
# • Persistent full history tracking (no truncation)
# • Optional downsampling for very long All Time charts
# • Lowered ZERO_PL_THRESHOLD to 0.001
# • NEW: Rolling median smoothing on Net Gain History chart to reduce small API glitches
# • NEW: Button to automatically fetch & import full historical CSV report from Trading 212 API
#   → Splits long ranges into ≤1-year chunks to respect API limit
# ------------------------------------------------------------
import os
import json
import time
import threading
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import pandas as pd
import requests
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog, messagebox, BOTH, X, LEFT, RIGHT, Y, EW, NS
from tkinter import StringVar, END
from tkinter import font as tkfont
from tkinter import colorchooser
try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import *
    BOOTSTRAP = True
except ImportError:
    BOOTSTRAP = False
try:
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.dates as mdates
    import matplotlib.text as mtext
    import numpy as np
    MATPLOTLIB = True
except ImportError:
    MATPLOTLIB = False
try:
    import mplcursors
    MPLCURSORS_AVAILABLE = True
except ImportError:
    MPLCURSORS_AVAILABLE = False
    print("Note: mplcursors not installed → hover tooltips disabled. Install with: pip install mplcursors")

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
APP_NAME = "Trading212 Portfolio Pro v4.7 (REV: 30)"
DATA_DIR = "data"
CSV_FILE = os.path.join(DATA_DIR, "transactions.csv")
CACHE_FILE = os.path.join(DATA_DIR, "positions_cache.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MIN_MAX_FILE = os.path.join(DATA_DIR, "min_max.json")
NET_GAIN_HISTORY_FILE = os.path.join(DATA_DIR, "net_gain_history.json")
PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.json")
ANOMALY_LOG_FILE = os.path.join(DATA_DIR, "anomaly_log.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")

BASE_URL = "https://live.trading212.com/api/v0"
CACHE_TTL = 30
AUTO_REFRESH_INTERVAL_SEC = 60
MAX_BAR_TICKERS = 25
STALE_THRESHOLD_MIN = 10
CONCENTRATION_THRESHOLD_PCT = 25
ZERO_PL_THRESHOLD = 0.001
HISTORY_SOFT_LIMIT = 20000
CHART_DOWNSAMPLE_THRESHOLD = 3000
ANOMALY_THRESHOLD_ABS = 2.0
ANOMALY_THRESHOLD_PCT = 0.02

NETGAIN_SMOOTH_WINDOW = 5

os.makedirs(DATA_DIR, exist_ok=True)

# ────────────────────────────────────────────────
# MODELS
# ────────────────────────────────────────────────
@dataclass
class ApiCredentials:
    key: str = ""
    secret: str = ""

@dataclass
class Position:
    ticker: str
    quantity: float
    avg_price: float
    current_price: float
    est_value: float
    unrealised_pl: float
    total_cost: float
    portfolio_pct: float = 0.0

# ────────────────────────────────────────────────
# SECRETS / CACHE / HISTORY HELPERS
# ────────────────────────────────────────────────
class Secrets:
    @staticmethod
    def load() -> ApiCredentials:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                return ApiCredentials(data.get('api_key', ''), data.get('api_secret', ''))
        return ApiCredentials()

    @staticmethod
    def save(creds: ApiCredentials):
        with open(SETTINGS_FILE, 'w') as f:
            json.dump({'api_key': creds.key, 'api_secret': creds.secret}, f, indent=2)

class Cache:
    @staticmethod
    def load() -> Optional[Dict]:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        return None

    @staticmethod
    def save(data: List[Dict]):
        with open(CACHE_FILE, 'w') as f:
            json.dump({'ts': time.time(), 'positions': data}, f)

    @staticmethod
    def is_valid(cache: Optional[Dict]) -> bool:
        return cache is not None and (time.time() - cache.get('ts', 0) < CACHE_TTL)

def load_min_max() -> Dict:
    if os.path.exists(MIN_MAX_FILE):
        with open(MIN_MAX_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_min_max(data: Dict):
    with open(MIN_MAX_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_net_gain_history() -> List[Dict]:
    if not os.path.exists(NET_GAIN_HISTORY_FILE):
        return []
    try:
        with open(NET_GAIN_HISTORY_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, dict) and 'ts' in p and 'net_gain' in p]
    except:
        pass
    return []

def save_net_gain_history(history: List[Dict]):
    history = sorted(history, key=lambda x: x.get('ts', 0))
    with open(NET_GAIN_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def load_price_history() -> Dict[str, List[Dict]]:
    if not os.path.exists(PRICE_HISTORY_FILE):
        return {}
    try:
        with open(PRICE_HISTORY_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {k: [p for p in v if isinstance(p, dict) and 'ts' in p and 'price' in p]
                        for k, v in data.items()}
    except:
        pass
    return {}

def save_price_history(history: Dict[str, List[Dict]]):
    for k in list(history):
        history[k] = sorted(history[k], key=lambda x: x.get('ts', 0))
    with open(PRICE_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def log_anomaly(now_ts: float, prev_assets: float, new_assets: float, net_gain: float, reason: str = ""):
    change_abs = new_assets - prev_assets
    change_pct = abs(change_abs / prev_assets) if prev_assets > 0 else 0
    if abs(change_abs) < ANOMALY_THRESHOLD_ABS and change_pct < ANOMALY_THRESHOLD_PCT:
        return
    entry = {
        "ts": now_ts,
        "iso_time": datetime.fromtimestamp(now_ts).isoformat(),
        "prev_assets": round(prev_assets, 2),
        "new_assets": round(new_assets, 2),
        "change_abs": round(change_abs, 2),
        "change_pct": round(change_pct * 100, 2),
        "net_gain": round(net_gain, 2),
        "reason": reason
    }
    anomalies = []
    if os.path.exists(ANOMALY_LOG_FILE):
        try:
            with open(ANOMALY_LOG_FILE, 'r') as f:
                anomalies = json.load(f)
        except:
            pass
    anomalies.append(entry)
    if len(anomalies) > 1000:
        anomalies = anomalies[-1000:]
    with open(ANOMALY_LOG_FILE, 'w') as f:
        json.dump(anomalies, f, indent=2)
    print(f"ANOMALY LOGGED → {entry['iso_time']} | change £{entry['change_abs']:+.2f} ({entry['change_pct']:+.2f}%) | {reason}")

# ────────────────────────────────────────────────
# TRANSACTIONS REPOSITORY
# ────────────────────────────────────────────────
class TransactionsRepo:
    def __init__(self):
        self.path = CSV_FILE

    def load(self) -> pd.DataFrame:
        if not os.path.exists(self.path):
            return pd.DataFrame()
        try:
            df = pd.read_csv(self.path, parse_dates=['Date'])
        except:
            return pd.DataFrame()
        numeric = ['Quantity', 'Price', 'Total', 'Fee', 'FX_Rate', 'Result']
        for c in numeric:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
        return df

    def save(self, df: pd.DataFrame):
        df.to_csv(self.path, index=False, date_format='%Y-%m-%d %H:%M:%S')

    @staticmethod
    def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
        key = ['Date', 'Type', 'Ticker', 'Total', 'Reference']
        key = [c for c in key if c in df.columns]
        if key:
            return df.drop_duplicates(subset=key, keep='last').sort_values('Date').reset_index(drop=True)
        return df

# ────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────
def safe_float(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except:
        return 0.0

def round_money(val: float) -> float:
    return round(val, 2)

def format_price(price: float) -> str:
    if price <= 0:
        return "£0.00"
    if price < 1:
        s = f"£{price:,.4f}"
        return s.rstrip('0').rstrip('.') if '.' in s else s
    return f"£{round_money(price):,.2f}"

# ────────────────────────────────────────────────
# TRADING212 SERVICE
# ────────────────────────────────────────────────
class Trading212Service:
    def __init__(self, creds: ApiCredentials):
        self.creds = creds
        self.session = requests.Session()
        self.session.headers.update(self._headers())

    def _headers(self):
        if not self.creds.key or not self.creds.secret:
            return {}
        token = base64.b64encode(f"{self.creds.key}:{self.creds.secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def fetch_positions(self) -> List[Position]:
        cache = Cache.load()
        if Cache.is_valid(cache):
            return [Position(**p) for p in cache['positions']]
        try:
            r = self.session.get(f"{BASE_URL}/equity/positions", timeout=12)
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            raise RuntimeError(f"Positions API failed: {str(e)}")
        positions = []
        total_value = 0.0
        for pos in items:
            try:
                instr = pos.get('instrument', {})
                ticker = instr.get('ticker', '').split('_')[0].upper().rstrip('L')
                qty = safe_float(pos.get('quantity'))
                avg_price = safe_float(pos.get('averagePricePaid'))
                current_price = safe_float(pos.get('currentPrice'))
                w = pos.get('walletImpact', {}) or {}
                est_value = safe_float(w.get('currentValue'))
                api_pl = safe_float(w.get('unrealizedProfitLoss'))
                total_cost = safe_float(w.get('totalCost'))
                if api_pl == 0 and qty != 0 and abs(current_price - avg_price) > 0.001:
                    api_pl = (current_price - avg_price) * qty
                unrealised_pl = 0.0 if abs(api_pl) < ZERO_PL_THRESHOLD else round_money(api_pl)
                est_value = 0.0 if abs(est_value) < ZERO_PL_THRESHOLD else round_money(est_value)
                total_cost = round_money(total_cost)
                positions.append(Position(
                    ticker=ticker, quantity=qty, avg_price=avg_price,
                    current_price=current_price, est_value=est_value,
                    unrealised_pl=unrealised_pl, total_cost=total_cost
                ))
                total_value += est_value
            except:
                continue
        for p in positions:
            p.portfolio_pct = (p.est_value / total_value * 100) if total_value > 0 else 0
        Cache.save([p.__dict__ for p in positions])
        return positions

    def fetch_cash_balance(self) -> float:
        try:
            r = self.session.get(f"{BASE_URL}/equity/account/cash", timeout=8)
            r.raise_for_status()
            data = r.json()
            for k in ['free', 'freeCash', 'cash', 'available']:
                if (val := data.get(k)) is not None:
                    return round_money(safe_float(val))
            return 0.0
        except:
            return 0.0

    def request_history_export(self, time_from: str, time_to: str) -> int:
        payload = {
            "dataIncluded": {
                "includeDividends": True,
                "includeInterest": True,
                "includeOrders": True,
                "includeTransactions": True
            },
            "timeFrom": time_from,
            "timeTo": time_to
        }
        try:
            r = self.session.post(
                f"{BASE_URL}/equity/history/exports",
                json=payload,
                timeout=15
            )
            r.raise_for_status()
            return r.json()["reportId"]
        except Exception as e:
            raise RuntimeError(f"Failed to request export: {str(e)}")

    def get_export_status(self) -> List[Dict]:
        try:
            r = self.session.get(f"{BASE_URL}/equity/history/exports", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise RuntimeError(f"Failed to get export status: {str(e)}")

    def download_export_csv(self, download_link: str) -> bytes:
        try:
            r = requests.get(download_link, timeout=30)
            r.raise_for_status()
            return r.content
        except Exception as e:
            raise RuntimeError(f"Failed to download CSV: {str(e)}")

# ────────────────────────────────────────────────
# ANALYTICS
# ────────────────────────────────────────────────
class Analytics:
    @staticmethod
    def calculate(df: pd.DataFrame, positions: List[Position], cash: float) -> Dict:
        if df.empty:
            hv = sum(p.est_value for p in positions)
            return {
                'total_assets': hv + cash, 'holdings_value': hv, 'net_gain': 0.0,
                'total_return_pct': 0.0, 'realised_pl': 0.0, 'fees': 0.0,
                'deposits': 0.0, 'deposit_count': 0, 'ttm_dividends': 0.0
            }
        fees = float(df['Fee'].sum()) if 'Fee' in df.columns else 0.0
        realised = float(df['Result'].sum()) if 'Result' in df.columns else 0.0
        deposit_mask = df['Type'].str.contains('deposit', case=False, na=False)
        deposits = float(df.loc[deposit_mask, 'Total'].sum())
        dep_count = int(deposit_mask.sum())
        hv = sum(p.est_value for p in positions)
        ta = hv + cash
        ng = ta - deposits
        tr_pct = (ng / deposits * 100) if deposits > 0 else 0.0
        ttm_div = 0.0
        if 'Date' in df.columns and 'Type' in df.columns and 'Result' in df.columns:
            one_yr_ago = datetime.now() - timedelta(days=365)
            ttm_div = float(df[
                (df['Date'] >= one_yr_ago) &
                df['Type'].str.contains('dividend', case=False, na=False) &
                (df['Result'] > 0)
            ]['Result'].sum())
        return {
            'total_assets': ta, 'holdings_value': hv, 'net_gain': ng,
            'total_return_pct': tr_pct, 'realised_pl': realised, 'fees': fees,
            'deposits': deposits, 'deposit_count': dep_count, 'ttm_dividends': ttm_div
        }

# ────────────────────────────────────────────────
# MAIN APPLICATION
# ────────────────────────────────────────────────
class Trading212App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.state('zoomed')
        self.repo = TransactionsRepo()
        self.df = self.repo.load()
        self.creds = Secrets.load()
        self.service = Trading212Service(self.creds)
        self.positions: List[Position] = []
        self.cash_balance: float = 0.0
        self.last_refresh_str = "Never"
        self.last_successful_refresh = 0.0
        self.last_total_assets = 0.0
        self.MIN_REFRESH_GAP = 60
        self.cooldown_end_time = 0.0
        self.countdown_after_id = None
        self.next_auto_refresh_time = 0.0
        self.netgain_period_var = tk.StringVar(value="1d")
        self._setup_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh(async_fetch=True)
        self.update_countdown()
        self.root.bind_all("<Control-s>", lambda event: self.save_notes() if hasattr(self, 'notes_text') else None)

        def auto_refresh_loop():
            self.refresh(async_fetch=True)
            self.root.after(AUTO_REFRESH_INTERVAL_SEC * 1000, auto_refresh_loop)
        self.root.after(10000, auto_refresh_loop)

    def _setup_style(self):
        if BOOTSTRAP:
            self.style = tb.Style(theme="darkly")
            self.style.configure("Treeview", rowheight=28, font=('Segoe UI', 10))
            self.style.configure("Treeview.Heading", font=('Segoe UI', 11, 'bold'))
        else:
            ttk.Style().theme_use('clam')

    def _build_ui(self):
        self.sidebar = ttk.Frame(self.root, width=220)
        self.sidebar.pack(side=LEFT, fill=Y, padx=(10, 0), pady=10)
        self.content = ttk.Frame(self.root)
        self.content.pack(side=LEFT, fill=BOTH, expand=True, padx=10, pady=10)

        self.tabs = {}
        self.tab_dashboard = ttk.Frame(self.content); self.tabs["Dashboard"] = self.tab_dashboard
        self.tab_netgain = ttk.Frame(self.content); self.tabs["Net Gain History"] = self.tab_netgain
        self.tab_transactions = ttk.Frame(self.content); self.tabs["Transactions"] = self.tab_transactions
        self.tab_positions = ttk.Frame(self.content); self.tabs["Positions"] = self.tab_positions
        self.tab_minmax = ttk.Frame(self.content); self.tabs["Historical Highs & Lows"] = self.tab_minmax
        self.tab_notes = ttk.Frame(self.content); self.tabs["Notes"] = self.tab_notes
        self.tab_settings = ttk.Frame(self.content); self.tabs["Settings"] = self.tab_settings

        self._build_sidebar()
        self._build_dashboard()
        self._build_netgain_chart()
        self._build_transactions()
        self._build_positions()
        self._build_minmax()
        self._build_notes()
        self._build_settings()
        self.switch_tab("Dashboard")

    def switch_tab(self, tab_name):
        for tab in self.tabs.values():
            tab.pack_forget()
        self.tabs[tab_name].pack(fill=BOTH, expand=True)
        if BOOTSTRAP:
            for btn in self.menu_btns.values():
                btn.configure(bootstyle="secondary")
            self.menu_btns[tab_name].configure(bootstyle="primary")

    def _build_sidebar(self):
        ttk.Label(self.sidebar, text=APP_NAME, font=('Segoe UI', 14, 'bold')).pack(pady=20, padx=10)
        menu_items = [
            "Dashboard", "Positions", "Transactions",
            "Net Gain History", "Historical Highs & Lows",
            "Notes", "Settings"
        ]
        self.menu_btns = {}
        for item in menu_items:
            bootstyle = "secondary" if BOOTSTRAP else ""
            btn = ttk.Button(self.sidebar, text=item, command=lambda t=item: self.switch_tab(t), bootstyle=bootstyle)
            btn.pack(fill=X, pady=4, padx=8)
            self.menu_btns[item] = btn
        ttk.Separator(self.sidebar).pack(fill=X, pady=15, padx=10)
        ttk.Button(self.sidebar, text="Import CSV", bootstyle="primary", command=self.fetch_and_import_history).pack(fill=X, pady=4, padx=8)
        ttk.Separator(self.sidebar).pack(fill=X, pady=15, padx=10)

        stats_grid = ttk.Frame(self.sidebar)
        stats_grid.pack(fill=X, padx=10, pady=8)
        stats_grid.columnconfigure((0,1), weight=1)
        stats = [
            ("# Positions", "📊", "—"), ("Avg Position", "💰", "—"),
            ("Cash %", "💸", "—"), ("Total Deposits", "🏦", "—"),
            ("Deposits Count", "🔢", "—"), ("Market Buys", "🛒", "—"),
            ("Market Sells", "💵", "—"), ("Net Gain £", "💰", "—"),
        ]
        self.stats_vars = {}
        self.stats_labels = {}
        for i, (label, icon, default) in enumerate(stats):
            tile = tb.Frame(stats_grid, bootstyle="dark", padding=10) if BOOTSTRAP else ttk.Frame(stats_grid)
            tile.grid(row=i//2, column=i%2, padx=6, pady=6, sticky='ew')
            header = ttk.Frame(tile)
            header.pack(fill=X, pady=(2,0))
            ttk.Label(header, text=icon, font=('Segoe UI', 13)).pack(side=LEFT, padx=(8,6))
            ttk.Label(header, text=label, font=('Segoe UI', 10)).pack(side=LEFT)
            var = tk.StringVar(value=default)
            lbl = ttk.Label(tile, textvariable=var, font=('Segoe UI', 12, 'bold'), anchor='center')
            lbl.pack(pady=(2,6))
            self.stats_vars[label] = var
            self.stats_labels[label] = lbl

        ttk.Separator(self.sidebar).pack(fill=X, pady=15, padx=10)
        ttk.Label(self.sidebar, text="Additional Features:", font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10)
        ttk.Button(self.sidebar, text="Help / About", bootstyle="outline", command=self.show_help).pack(fill=X, pady=4, padx=8)

        self.refresh_label = ttk.Label(self.sidebar, text="Status: Waiting...", foreground='gray')
        self.refresh_label.pack(pady=10, padx=10, anchor='s')
        self.countdown_var = tk.StringVar(value="Next auto-refresh: calculating...")
        self.countdown_label = ttk.Label(self.sidebar, textvariable=self.countdown_var, foreground='yellow', font=('Segoe UI', 10, 'bold'))
        self.countdown_label.pack(pady=(0,10), padx=10, anchor='s')

    def show_help(self):
        messagebox.showinfo("Help / About", f"{APP_NAME}\n\nPersonal Trading212 portfolio tracker.\nDouble-click tickers in Historical Highs & Lows for price chart.\nSudden changes logged to anomaly_log.json.\nRich-text Notes tab added.\nNet gain chart smoothed.\nUse 'Fetch & Import History CSV' in Settings to download full history (split into 1-year chunks).")

    def update_countdown(self):
        now = time.time()
        if self.next_auto_refresh_time > now:
            remaining = max(0, int(self.next_auto_refresh_time - now))
            minutes = remaining // 60
            seconds = remaining % 60
            if minutes > 0:
                text = f"Next auto-refresh in {minutes} min {seconds:02d} sec"
            else:
                text = f"Next auto-refresh in {seconds} sec"
            self.countdown_var.set(text)
            self.root.after(1000, self.update_countdown)
        else:
            self.countdown_var.set("Auto-refreshing...")

    def _set_total_return_text(self, text: str):
        if "Total Return" in self.card_vars:
            self.card_vars["Total Return"].set(text)

    def start_cooldown_countdown(self, seconds_left: int):
        if self.countdown_after_id:
            self.root.after_cancel(self.countdown_after_id)
        if seconds_left <= 0:
            self._set_total_return_text("Refreshing...")
            self.refresh(async_fetch=True)
            return
        self.countdown_after_id = self.root.after(1000, lambda: self.start_cooldown_countdown(seconds_left - 1))

    def refresh(self, async_fetch: bool = False, is_auto_retry: bool = False):
        def _task():
            try:
                self.root.after(0, lambda: self._set_total_return_text("Refreshing..."))
                self.positions = self.service.fetch_positions()
                min_max = load_min_max()
                price_hist = load_price_history()
                now_ts = time.time()
                now_str = datetime.now().isoformat()
                for p in self.positions:
                    t = p.ticker
                    c = p.current_price
                    if c <= 0: continue
                    if t not in min_max:
                        min_max[t] = {'min':c, 'max':c, 'first_seen':now_str, 'last_updated':now_str, 'count':1, 'last_price':c}
                    else:
                        d = min_max[t]
                        d['min'] = min(d['min'], c)
                        d['max'] = max(d['max'], c)
                        d['last_updated'] = now_str
                        d['count'] += 1
                        d['last_price'] = c
                    if t not in price_hist:
                        price_hist[t] = []
                    price_hist[t].append({"ts": now_ts, "price": round_money(c)})
                save_min_max(min_max)
                save_price_history(price_hist)
                self.cash_balance = self.service.fetch_cash_balance()
                self.last_successful_refresh = time.time()
                self.cooldown_end_time = time.time() + self.MIN_REFRESH_GAP
                self.next_auto_refresh_time = time.time() + AUTO_REFRESH_INTERVAL_SEC
                summary = Analytics.calculate(self.df, self.positions, self.cash_balance)
                net_gain_value = summary['net_gain']
                tv = summary['total_assets']
                if self.last_total_assets > 0:
                    change = tv - self.last_total_assets
                    reason = ""
                    if abs(change) >= ANOMALY_THRESHOLD_ABS:
                        reason = f"absolute change >= £{ANOMALY_THRESHOLD_ABS}"
                    elif abs(change / self.last_total_assets) >= ANOMALY_THRESHOLD_PCT:
                        reason = f"percent change >= {ANOMALY_THRESHOLD_PCT*100}%"
                    if reason:
                        log_anomaly(now_ts, self.last_total_assets, tv, net_gain_value, reason)
                self.last_total_assets = tv
                hist = load_net_gain_history()
                hist.append({"ts": now_ts, "net_gain": round(net_gain_value, 2), "total_assets": round(tv, 2)})
                save_net_gain_history(hist)
                num_pos = len([p for p in self.positions if p.quantity > 0])
                avg_pos = tv / num_pos if num_pos > 0 else 0
                cash_pct = (self.cash_balance / tv * 100) if tv > 0 else 0
                buy_count = sell_count = 0
                if not self.df.empty:
                    buy_count = int(self.df['Type'].str.contains('buy', case=False, na=False).sum())
                    sell_count = int(self.df['Type'].str.contains('sell', case=False, na=False).sum())
                session_change_str = ""
                if self.last_total_assets > 0:
                    ch_pct = ((tv - self.last_total_assets) / self.last_total_assets) * 100
                    arrow = "↑" if ch_pct >= 0 else "↓"
                    session_change_str = f" {arrow} {ch_pct:+.2f}%"
                self.last_refresh_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.root.after(0, lambda: self._render_dashboard(
                    summary, num_pos, avg_pos, cash_pct, session_change_str,
                    buy_count=buy_count, sell_count=sell_count, net_gain=net_gain_value
                ))
                self.root.after(0, lambda: self.refresh_label.config(
                    text=f"Last refresh: {self.last_refresh_str}",
                    foreground='lime'
                ))
                self.root.after(0, self._render_positions)
                self.root.after(0, self._render_minmax)
                self.root.after(0, self.render_transactions)
                self.root.after(0, self._render_netgain_chart)
                self.root.after(0, self.update_countdown)
            except Exception as e:
                self.root.after(0, lambda: self._set_total_return_text(f"Error: {str(e)}"))
                self.root.after(0, lambda: self.refresh_label.config(text=f"Error: {str(e)}", foreground='red'))

        now = time.time()
        if not is_auto_retry and self.cooldown_end_time > now:
            rem = int(self.cooldown_end_time - now) + 1
            self.start_cooldown_countdown(rem)
            return
        if async_fetch:
            threading.Thread(target=_task, daemon=True).start()
        else:
            _task()

    def _build_dashboard(self):
        main = ttk.Frame(self.tab_dashboard, padding=20)
        main.pack(fill=BOTH, expand=True)
        cards_frame = ttk.Frame(main)
        cards_frame.pack(fill=X, pady=(0,20))
        self.card_vars = {}
        self.card_frames = {}
        cards = [
            ("Portfolio Value", "💰", "#4CAF50"),
            ("Cash Available", "💸", "#9C27B0"),
            ("Total Return", "📈", "#2196F3"),
            ("TTM Dividends", "📅", "#FFEB3B"),
            ("Realised P/L", "🏦", "#FF9800"),
            ("Fees Paid", "⚠️", "#F44336"),
        ]
        for i, (title, emoji, color) in enumerate(cards):
            card = tb.Frame(cards_frame, bootstyle="dark", padding=14) if BOOTSTRAP else ttk.Frame(cards_frame)
            card.grid(row=i//3, column=i%3, padx=12, pady=10, sticky=EW)
            ttk.Label(card, text=f"{emoji} {title}", font=('Segoe UI', 13, 'bold')).pack(anchor='w')
            var = tk.StringVar(value="—")
            ttk.Label(card, textvariable=var, font=('Segoe UI', 26, 'bold'), foreground=color).pack(anchor='center', pady=8)
            self.card_vars[title] = var
            self.card_frames[title] = card
        cards_frame.columnconfigure((0,1,2), weight=1)
        self.warning_var = tk.StringVar(value="")
        ttk.Label(main, textvariable=self.warning_var, font=('Segoe UI', 10), foreground='#FF9800').pack(pady=(0,10))
        if MATPLOTLIB:
            chart_frame = tb.Frame(main, bootstyle="dark") if BOOTSTRAP else ttk.Frame(main)
            chart_frame.pack(fill=BOTH, expand=True, pady=10)
            self.fig = Figure(figsize=(14, 6.5), facecolor='#1e1e2f')
            self.ax1 = self.fig.add_subplot(121)
            self.ax2 = self.fig.add_subplot(122)
            for ax in (self.ax1, self.ax2):
                ax.set_facecolor('#252535')
                ax.tick_params(colors='white', labelsize=10)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                ax.spines['left'].set_color('gray')
                ax.spines['bottom'].set_color('gray')
                ax.grid(True, axis='y', alpha=0.15, color='gray', linestyle='--')
            self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
            self.canvas.get_tk_widget().pack(fill=BOTH, expand=True, padx=6, pady=6)

    def _render_dashboard(self, s: Dict, num_pos: int, avg_pos: float, cash_pct: float,
                          session_change_str: str = "", buy_count: int = 0, sell_count: int = 0,
                          net_gain: float = 0.0):
        self.card_vars["Portfolio Value"].set(f"£{round_money(s['total_assets']):,.2f}")
        self.card_vars["Cash Available"].set(f"£{round_money(self.cash_balance):,.2f} ({cash_pct:.1f}%)")
        gain = s['net_gain']
        pct = s['total_return_pct']
        sign_g = "+" if gain >= 0 else ""
        sign_p = "+" if pct >= 0 else ""
        ret_text = f"{sign_g}£{round_money(gain):,.2f} ({sign_p}{pct:.2f}%){session_change_str}"
        self.card_vars["Total Return"].set(ret_text)
        if BOOTSTRAP and "Total Return" in self.card_frames:
            card = self.card_frames["Total Return"]
            if pct > 10: card.configure(bootstyle="success")
            elif pct > 3: card.configure(bootstyle="info")
            elif pct > -3: card.configure(bootstyle="secondary")
            elif pct > -10: card.configure(bootstyle="warning")
            else: card.configure(bootstyle="danger")
        self.card_vars["TTM Dividends"].set(f"£{round_money(s['ttm_dividends']):,.2f}")
        self.card_vars["Realised P/L"].set(f"£{round_money(s['realised_pl']):+,.2f}")
        self.card_vars["Fees Paid"].set(f"£{round_money(s['fees']):,.2f}")
        self.stats_vars["# Positions"].set(f"{num_pos}")
        self.stats_vars["Avg Position"].set(f"£{round_money(avg_pos):,.0f}")
        self.stats_vars["Cash %"].set(f"{cash_pct:.1f}%")
        self.stats_vars["Total Deposits"].set(f"£{round_money(s['deposits']):,.0f}")
        self.stats_vars["Deposits Count"].set(f"{s.get('deposit_count', 0):,d}")
        self.stats_vars["Market Buys"].set(f"{buy_count:,d}")
        self.stats_vars["Market Sells"].set(f"{sell_count:,d}")
        sign = "+" if net_gain >= 0 else ""
        self.stats_vars["Net Gain £"].set(f"{sign}£{round_money(net_gain):,.2f}")
        if "Net Gain £" in self.stats_labels:
            lbl = self.stats_labels["Net Gain £"]
            lbl.configure(foreground="#90EE90" if net_gain > 0 else "#FF9999" if net_gain < 0 else "white")
        warnings = []
        min_ago = (time.time() - self.last_successful_refresh) / 60 if self.last_successful_refresh else 999
        if min_ago > STALE_THRESHOLD_MIN:
            warnings.append(f"Data stale ({int(min_ago)} min ago)")
        max_pct = max((p.portfolio_pct for p in self.positions if p.quantity > 0), default=0)
        if max_pct > CONCENTRATION_THRESHOLD_PCT:
            warnings.append(f"High concentration: {max_pct:.1f}% in one position")
        self.warning_var.set(" • ".join(warnings) if warnings else "")
        if MATPLOTLIB and self.positions:
            self.ax1.clear()
            self.ax2.clear()
            active = [p for p in self.positions if p.est_value > 0]
            sorted_active = sorted(active, key=lambda x: -x.est_value)
            n = len(sorted_active)
            if n <= MAX_BAR_TICKERS:
                tickers = [p.ticker for p in sorted_active]
                values = [p.est_value for p in sorted_active]
                colors = ['#66BB6A' if p.unrealised_pl >= 0 else '#EF5350' for p in sorted_active]
                self.ax1.bar(tickers, values, color=colors, edgecolor='gray', linewidth=0.8)
                self.ax1.tick_params(axis='x', rotation=60, labelsize=9)
            else:
                top_n = min(35, n)
                tickers = [p.ticker for p in sorted_active[:top_n]]
                values = [p.est_value for p in sorted_active[:top_n]]
                colors = ['#66BB6A' if p.unrealised_pl >= 0 else '#EF5350' for p in sorted_active[:top_n]]
                self.ax1.barh(tickers[::-1], values[::-1], color=colors[::-1], height=0.65)
                self.ax1.set_title(f"Top {top_n} Positions", fontsize=13)
                self.ax1.invert_yaxis()
            self.ax1.set_title("Position Values" if n <= MAX_BAR_TICKERS else "", fontsize=14)
            pie_vals, pie_lbls = [], []
            if n > 15:
                top_vals = [p.est_value for p in sorted_active[:15]]
                top_lbls = [p.ticker for p in sorted_active[:15]]
                other = sum(p.est_value for p in sorted_active[15:])
                pie_vals = top_vals + [other]
                pie_lbls = top_lbls + ['Other']
            else:
                pie_vals = [p.est_value for p in sorted_active]
                pie_lbls = [p.ticker for p in sorted_active]
            self.ax2.pie(pie_vals, labels=pie_lbls, autopct='%1.1f%%',
                         colors=plt.cm.tab20.colors[:len(pie_vals)], textprops={'color':'white', 'fontsize':9})
            self.ax2.set_title("Allocation", fontsize=14)
            self.fig.tight_layout(pad=1.5)
            self.canvas.draw()

    def _build_netgain_chart(self):
        frame = ttk.Frame(self.tab_netgain, padding=20)
        frame.pack(fill=BOTH, expand=True)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, pady=(0,12))
        periods = [("1hr","1hr"), ("4hr","4hr"), ("8hr","16hr"),
                   ("1d","1d"), ("1w","1w"), ("1m","1m"), ("3m","3m"),
                   ("YTD","YTD"), ("All Time","All Time")]
        self.period_buttons = {}
        for label, value in periods:
            btn = ttk.Button(btn_frame, text=label, command=lambda v=value: self.set_netgain_period(v), width=8)
            btn.pack(side=LEFT, padx=4, pady=3)
            self.period_buttons[value] = btn
        self.set_netgain_period("1d", update_buttons_only=True)
        if not MATPLOTLIB:
            ttk.Label(frame, text="Matplotlib not available", foreground="orange").pack(pady=40)
            return
        fig = Figure(figsize=(12,6), facecolor='#1e1e2f')
        ax = fig.add_subplot(111)
        ax.set_facecolor('#252535')
        ax.tick_params(colors='white')
        ax.grid(True, axis='y', alpha=0.12, color='gray', linestyle='--')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('gray')
        ax.spines['bottom'].set_color('gray')
        ax.set_title("Net Gain Over Time (£)", color='white', fontsize=14, pad=15)
        ax.set_xlabel("Date", color='white')
        ax.set_ylabel("Net Gain (£)", color='white')
        self.netgain_fig = fig
        self.netgain_ax = ax
        self.netgain_canvas = FigureCanvasTkAgg(fig, master=frame)
        self.netgain_canvas.get_tk_widget().pack(fill=BOTH, expand=True, padx=5, pady=5)

    def set_netgain_period(self, period: str, update_buttons_only=False):
        self.netgain_period_var.set(period)
        if BOOTSTRAP:
            for v, b in self.period_buttons.items():
                b.configure(bootstyle="primary" if v == period else "secondary")
        if not update_buttons_only:
            self._render_netgain_chart()

    def get_netgain_date_cutoff(self) -> Optional[datetime]:
        p = self.netgain_period_var.get()
        now = datetime.now()
        if p in ("All Time", "All"): return None
        if p == "YTD": return datetime(now.year, 1, 1)
        if p == "3m": return now - timedelta(days=90)
        if p == "1m": return datetime(now.year, now.month, 1)
        if p == "1w": return now - timedelta(days=now.weekday())
        if p == "1d": return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if p in ("1hr","4hr","8hr","16hr"):
            h = int(p[:-2])
            cutoff = now - timedelta(hours=h)
            return max(cutoff, now.replace(hour=0, minute=0, second=0, microsecond=0))
        return now - timedelta(days=30)

    def _render_netgain_chart(self):
        if not MATPLOTLIB or not hasattr(self, 'netgain_ax'):
            return
        hist = load_net_gain_history()
        if not hist:
            self.netgain_ax.clear()
            self.netgain_ax.text(0.5, 0.5, "No history yet", ha='center', va='center', color='gray')
            self.netgain_canvas.draw()
            return
        df_hist = pd.DataFrame(hist)
        cutoff = self.get_netgain_date_cutoff()
        if cutoff:
            df_hist = df_hist[df_hist['ts'].apply(lambda t: datetime.fromtimestamp(t) >= cutoff)]
        if len(df_hist) >= NETGAIN_SMOOTH_WINDOW:
            df_hist['net_gain'] = (
                df_hist['net_gain']
                .rolling(window=NETGAIN_SMOOTH_WINDOW, center=True, min_periods=1)
                .median()
            )
        times = [datetime.fromtimestamp(t) for t in df_hist['ts']]
        gains = df_hist['net_gain'].tolist()
        if not times:
            self.netgain_ax.clear()
            self.netgain_ax.text(0.5, 0.5, "No data in period", ha='center', va='center', color='gray')
            self.netgain_canvas.draw()
            return
        p = self.netgain_period_var.get()
        if p in ("All Time", "All") and len(times) > CHART_DOWNSAMPLE_THRESHOLD:
            step = max(1, len(times) // 2000)
            times = times[::step]
            gains = gains[::step]
        for a in list(self.netgain_fig.get_children()) + list(self.netgain_ax.get_children()):
            if isinstance(a, mtext.Annotation):
                try: a.remove()
                except: pass
        self.netgain_ax.clear()
        if gains:
            min_g, max_g = min(gains), max(gains)
            span = max_g - min_g
            center = (max_g + min_g) / 2
            half = max(span * 0.7, 5, abs(max_g)*1.2, abs(min_g)*1.2)
            self.netgain_ax.set_ylim(center - half, center + half)
        line, = self.netgain_ax.plot(
            times, gains,
            color='#BB86FC', lw=1.8, marker='o', ms=3, alpha=0.9
        )
        self.netgain_ax.axhline(0, color='gray', lw=0.8, ls='--', alpha=0.5)
        self.netgain_ax.fill_between(times, gains, 0,
                                     where=np.array(gains)>=0,
                                     color='#4CAF50', alpha=0.08)
        self.netgain_ax.fill_between(times, gains, 0,
                                     where=np.array(gains)<0,
                                     color='#EF5350', alpha=0.08)
        self.netgain_ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
        self.netgain_ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
        plt.setp(self.netgain_ax.get_xticklabels(), rotation=35, ha='right')
        self.netgain_ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda y, _: f'£{y:,.0f}' if abs(y)>=10 else f'£{y:,.2f}')
        )
        if MPLCURSORS_AVAILABLE:
            cursor = mplcursors.cursor(line, hover=True)
            @cursor.connect("add")
            def _(sel):
                dt = mdates.num2date(sel.target[0]).strftime("%Y-%m-%d %H:%M")
                val = sel.target[1]
                sel.annotation.set_text(f"{dt}\n£{val:,.2f}")
                sel.annotation.get_bbox_patch().set_facecolor("#2d2d44")
                sel.annotation.get_bbox_patch().set_edgecolor("#bb86fc")
                sel.annotation.set_color("white")
        self.netgain_fig.tight_layout()
        self.netgain_canvas.draw()

    def _build_transactions(self):
        filter_bar = ttk.Frame(self.tab_transactions)
        filter_bar.pack(fill=X, pady=(0,8))
        ttk.Label(filter_bar, text="Filter:").pack(side=LEFT, padx=8)
        self.tx_filter_var = StringVar()
        ttk.Entry(filter_bar, textvariable=self.tx_filter_var, width=45).pack(side=LEFT, padx=6)
        self.tx_filter_var.trace('w', lambda *a: self.render_transactions())
        tree_frame = ttk.Frame(self.tab_transactions)
        tree_frame.pack(fill=BOTH, expand=True, padx=5, pady=5)
        cols = ["Date", "Type", "Ticker", "Quantity", "Price", "Total", "Fee", "Result", "Note"]
        self.tree_tx = ttk.Treeview(tree_frame, columns=cols, show='headings')
        for c in cols:
            self.tree_tx.heading(c, text=c, command=lambda col=c: self._sort_tree(self.tree_tx, col, False))
            anchor = 'w' if c in ["Date","Type","Ticker","Note"] else 'e'
            width = 160 if c in ["Date","Note"] else 110
            self.tree_tx.column(c, width=width, anchor=anchor, stretch=True)
        vsb = ttk.Scrollbar(tree_frame, orient=VERTICAL, command=self.tree_tx.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=HORIZONTAL, command=self.tree_tx.xview)
        self.tree_tx.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_tx.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree_tx.tag_configure('even', background='#222233')
        self.tree_tx.tag_configure('odd', background='#1a1a2a')
        self.tree_tx.tag_configure('buy', foreground='#66BB6A')
        self.tree_tx.tag_configure('sell', foreground='#EF5350')
        self.tree_tx.tag_configure('dividend', foreground='#FFCA28')
        self.tree_tx.tag_configure('total', font=('Segoe UI', 10, 'bold'), foreground='#BB86FC')
        self.render_transactions()

    def render_transactions(self):
        self.tree_tx.delete(*self.tree_tx.get_children())
        filter_text = self.tx_filter_var.get().lower().strip()
        if filter_text:
            rows = [
                (i, r) for i, r in self.df.iterrows()
                if filter_text in ' '.join(str(v).lower() for v in r)
            ]
        else:
            rows = list(self.df.iterrows())
        for idx, (_, row) in enumerate(rows):
            values = [row.get(c, '') for c in self.tree_tx['columns']]
            tags = ['even' if idx % 2 == 0 else 'odd']
            t = str(row.get('Type','')).lower()
            if 'buy' in t: tags.append('buy')
            elif 'sell' in t: tags.append('sell')
            elif 'dividend' in t: tags.append('dividend')
            self.tree_tx.insert('', 'end', values=values, tags=tags)
        if not self.df.empty:
            totals = ["TOTAL", "", "", self.df['Quantity'].sum(), "", self.df['Total'].sum(),
                      self.df['Fee'].sum(), self.df['Result'].sum(), ""]
            formatted = [f"{v:,.2f}" if isinstance(v,(int,float)) and i not in [0,1,2,4,8] else v
                         for i,v in enumerate(totals)]
            self.tree_tx.insert('', 'end', values=formatted, tags=('total',))

    def _build_positions(self):
        frame = ttk.Frame(self.tab_positions, padding=12)
        frame.pack(fill=BOTH, expand=True)
        cols = ["Ticker", "Qty", "Avg Price", "Current", "Value", "Unreal. P/L", "Cost", "% Portfolio"]
        self.tree_pos = ttk.Treeview(frame, columns=cols, show='headings')
        for c in cols:
            self.tree_pos.heading(c, text=c, command=lambda col=c: self._sort_tree(self.tree_pos, col, False))
            anchor = 'w' if c == "Ticker" else 'e'
            width = 140 if c in ["Value","Unreal. P/L","Cost"] else 100
            self.tree_pos.column(c, width=width, anchor=anchor)
        vsb = ttk.Scrollbar(frame, orient=VERTICAL, command=self.tree_pos.yview)
        hsb = ttk.Scrollbar(frame, orient=HORIZONTAL, command=self.tree_pos.xview)
        self.tree_pos.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_pos.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree_pos.tag_configure('profit', foreground='#66BB6A')
        self.tree_pos.tag_configure('loss', foreground='#EF5350')
        self.tree_pos.tag_configure('total', font=('Segoe UI', 10, 'bold'), foreground='#BB86FC')
        self._render_positions()

    def _render_positions(self):
        self.tree_pos.delete(*self.tree_pos.get_children())
        sorted_pos = sorted(self.positions, key=lambda x: -x.est_value if x.quantity > 0 else 0)
        tv = sum(p.est_value for p in sorted_pos)
        tpl = sum(p.unrealised_pl for p in sorted_pos)
        tc = sum(p.total_cost for p in sorted_pos)
        for idx, p in enumerate(sorted_pos):
            if p.quantity <= 0: continue
            tags = ['profit' if p.unrealised_pl >= 0 else 'loss', 'even' if idx%2==0 else 'odd']
            curr_price_str = format_price(p.current_price)
            avg_price_str = format_price(p.avg_price)
            vals = (
                p.ticker,
                f"{p.quantity:,.4f}",
                avg_price_str,
                curr_price_str,
                f"£{round_money(p.est_value):,.2f}",
                f"£{round_money(p.unrealised_pl):+,.2f}",
                f"£{round_money(p.total_cost):,.2f}",
                f"{p.portfolio_pct:.1f}%"
            )
            self.tree_pos.insert('', 'end', values=vals, tags=tags)
        footer = ("TOTAL", "", "", "", f"£{round_money(tv):,.2f}",
                  f"£{round_money(tpl):+,.2f}", f"£{round_money(tc):,.2f}", "100.0%")
        self.tree_pos.insert('', 'end', values=footer, tags=('total',))

    def _build_minmax(self):
        frame = ttk.Frame(self.tab_minmax, padding=12)
        frame.pack(fill=BOTH, expand=True)
        cols = ["Ticker", "Status", "Current", "Min Price", "Max Price",
                "% from Min", "% from Max", "Updates", "First Seen", "Last Updated"]
        self.tree_minmax = ttk.Treeview(frame, columns=cols, show='headings')
        for c in cols:
            self.tree_minmax.heading(c, text=c, command=lambda col=c: self._sort_tree(self.tree_minmax, col, False))
            anchor = 'w' if c in ["Ticker","Status","First Seen","Last Updated"] else 'e'
            width = 140 if "Price" in c else 100
            self.tree_minmax.column(c, width=width, anchor=anchor)
        vsb = ttk.Scrollbar(frame, orient=VERTICAL, command=self.tree_minmax.yview)
        hsb = ttk.Scrollbar(frame, orient=HORIZONTAL, command=self.tree_minmax.xview)
        self.tree_minmax.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_minmax.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree_minmax.tag_configure('closed', foreground='#FF5555')
        self.tree_minmax.bind("<Double-1>", self.on_minmax_double_click)
        self._render_minmax()

    def on_minmax_double_click(self, event):
        item = self.tree_minmax.identify_row(event.y)
        if not item: return
        values = self.tree_minmax.item(item, "values")
        if not values: return
        ticker = values[0]
        self.show_price_history_chart(ticker)

    def show_price_history_chart(self, ticker: str):
        if not MATPLOTLIB:
            messagebox.showinfo("Chart", "Matplotlib not available.")
            return
        win = tk.Toplevel(self.root)
        win.title(f"{ticker} Price History")
        win.geometry("1000x600")
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill=BOTH, expand=True)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, pady=(0,12))
        periods = [("1d","1d"), ("1wk","1wk"), ("1mth","1mth"), ("1yr","1yr"), ("Max","Max")]
        period_var = tk.StringVar(value="1d")
        period_btns = {}
        for lbl, val in periods:
            b = ttk.Button(btn_frame, text=lbl,
                           command=lambda v=val: set_p(v),
                           width=8)
            b.pack(side=LEFT, padx=4, pady=3)
            period_btns[val] = b
        def get_cutoff(p):
            n = datetime.now()
            if p == "Max": return None
            if p == "1yr": return n - timedelta(days=365)
            if p == "1mth": return n - timedelta(days=30)
            if p == "1wk": return n - timedelta(days=7)
            if p == "1d": return n - timedelta(days=1)
            return None
        def render():
            hist = load_price_history().get(ticker, [])
            if not hist:
                ax.clear()
                ax.text(0.5, 0.5, "No price history yet", ha='center', va='center', color='gray', fontsize=12)
                canvas.draw()
                return
            ts_list = [datetime.fromtimestamp(d['ts']) for d in hist]
            prices = [d['price'] for d in hist]
            cutoff = get_cutoff(period_var.get())
            if cutoff is not None:
                mask = [t >= cutoff for t in ts_list]
                ts_list = [t for t, keep in zip(ts_list, mask) if keep]
                prices = [p for p, keep in zip(prices, mask) if keep]
            if not ts_list:
                ax.clear()
                ax.text(0.5, 0.5, f"No data in selected period ({period_var.get()})",
                        ha='center', va='center', color='gray', fontsize=12)
                canvas.draw()
                return
            for artist in list(fig.get_children()) + list(ax.get_children()):
                if isinstance(artist, mtext.Annotation):
                    try: artist.remove()
                    except: pass
            ax.clear()
            ax.set_facecolor('#252535')
            ax.tick_params(colors='white')
            ax.grid(True, axis='y', alpha=0.12, color='gray', linestyle='--')
            for spine in ['top', 'right']:
                ax.spines[spine].set_visible(False)
            ax.spines['left'].set_color('gray')
            ax.spines['bottom'].set_color('gray')
            ax.set_title(f"{ticker} Price History (£)", color='white', fontsize=14, pad=15)
            ax.set_xlabel("Date", color='white')
            ax.set_ylabel("Price (£)", color='white')
            line, = ax.plot(
                ts_list, prices,
                color='#BB86FC',
                linewidth=1.8,
                marker='o',
                markersize=3,
                alpha=0.9
            )
            if prices:
                min_p = min(prices)
                max_p = max(prices)
                span = max_p - min_p
                padding = max(span * 0.10, 0.05)
                ax.set_ylim(min_p - padding, max_p + padding)
                ax.axhline(min_p, color='#EF5350', lw=0.8, ls='--', alpha=0.5,
                           label=f"Min: £{min_p:,.2f}")
                ax.axhline(max_p, color='#4CAF50', lw=0.8, ls='--', alpha=0.5,
                           label=f"Max: £{max_p:,.2f}")
                ax.legend(loc='upper left', frameon=True,
                          facecolor='#252535', edgecolor='gray', labelcolor='white')
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b %Y'))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
            plt.setp(ax.get_xticklabels(), rotation=35, ha='right')
            ax.yaxis.set_major_formatter(
                plt.FuncFormatter(lambda y, _: f'£{y:,.2f}' if abs(y) < 100 else f'£{int(y):,}')
            )
            if MPLCURSORS_AVAILABLE and ts_list and prices:
                cursor = mplcursors.cursor(line, hover=True, highlight=True)
                @cursor.connect("add")
                def on_hover(sel):
                    dt_str = mdates.num2date(sel.target[0]).strftime("%Y-%m-%d %H:%M")
                    val = sel.target[1]
                    sel.annotation.set_text(f"{dt_str}\n£{val:,.2f}")
                    sel.annotation.get_bbox_patch().set_alpha(0.92)
                    sel.annotation.get_bbox_patch().set_facecolor("#2d2d44")
                    sel.annotation.get_bbox_patch().set_edgecolor("#bb86fc")
                    sel.annotation.set_color("white")
                    sel.annotation.xy = sel.target
            fig.tight_layout()
            canvas.draw()
        def set_p(period: str):
            period_var.set(period)
            if BOOTSTRAP:
                for v, btn in period_btns.items():
                    btn.configure(bootstyle="primary" if v == period else "secondary")
            render()
        fig = Figure(figsize=(12, 6), facecolor='#1e1e2f')
        ax = fig.add_subplot(111)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        canvas.get_tk_widget().pack(fill=BOTH, expand=True, padx=5, pady=5)
        set_p("1d")

    def _render_minmax(self):
        self.tree_minmax.delete(*self.tree_minmax.get_children())
        mm = load_min_max()
        curr = {p.ticker: p for p in self.positions}
        for idx, t in enumerate(sorted(mm)):
            d = mm[t]
            status = "Open" if t in curr else "Closed"
            price = curr[t].current_price if t in curr else d.get('last_price', 0)
            mn = d['min']
            mx = d['max']
            fm = (price - mn) / mn * 100 if mn > 0 else 0
            fM = (price - mx) / mx * 100 if mx > 0 else 0
            vals = (
                t, status,
                format_price(price),
                format_price(mn),
                format_price(mx),
                f"{fm:+.1f}%", f"{fM:+.1f}%",
                d['count'],
                d['first_seen'][:19],
                d['last_updated'][:19]
            )
            tags = ['even' if idx%2==0 else 'odd']
            if status == "Closed": tags.append('closed')
            self.tree_minmax.insert('', 'end', values=vals, tags=tags)

    def _build_notes(self):
        frame = ttk.Frame(self.tab_notes, padding=12)
        frame.pack(fill=BOTH, expand=True)

        # Toolbar (unchanged - your original toolbar code)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=X, pady=(0, 8))

        fonts = ['Arial', 'Helvetica', 'Times New Roman', 'Courier New', 'Verdana', 'Georgia']
        font_var = tk.StringVar(value='Arial')
        font_menu = ttk.OptionMenu(toolbar, font_var, 'Arial', *fonts, command=lambda f: self.change_font_family(f))
        font_menu.pack(side=LEFT, padx=4)

        sizes = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 72]
        size_var = tk.StringVar(value='12')
        size_menu = ttk.OptionMenu(toolbar, size_var, '12', *[str(s) for s in sizes], command=lambda s: self.change_font_size(int(s)))
        size_menu.pack(side=LEFT, padx=4)

        bold_btn = ttk.Button(toolbar, text="B", width=4, command=lambda: self.toggle_tag('bold'))
        bold_btn.pack(side=LEFT, padx=2)

        italic_btn = ttk.Button(toolbar, text="I", width=4, command=lambda: self.toggle_tag('italic'))
        italic_btn.pack(side=LEFT, padx=2)

        underline_btn = ttk.Button(toolbar, text="U", width=4, command=lambda: self.toggle_tag('underline'))
        underline_btn.pack(side=LEFT, padx=2)

        color_btn = ttk.Button(toolbar, text="Color", width=6, command=self.choose_color)
        color_btn.pack(side=LEFT, padx=4)

        align_left = ttk.Button(toolbar, text="Left", width=6, command=lambda: self.set_alignment('left'))
        align_left.pack(side=LEFT, padx=2)

        align_center = ttk.Button(toolbar, text="Center", width=6, command=lambda: self.set_alignment('center'))
        align_center.pack(side=LEFT, padx=2)

        align_right = ttk.Button(toolbar, text="Right", width=6, command=lambda: self.set_alignment('right'))
        align_right.pack(side=LEFT, padx=2)

        bullet_btn = ttk.Button(toolbar, text="• Bullet", width=8, command=self.insert_bullet)
        bullet_btn.pack(side=LEFT, padx=4)

        save_btn = ttk.Button(toolbar, text="Save Notes", bootstyle="success", command=self.save_notes)
        save_btn.pack(side=RIGHT, padx=4)

        load_btn = ttk.Button(toolbar, text="Load Notes", bootstyle="info", command=self.load_notes)
        load_btn.pack(side=RIGHT, padx=4)

        # ──── Text area + vertical scrollbar ────
        text_container = ttk.Frame(frame)
        text_container.pack(fill=BOTH, expand=True)

        # Make column 0 (text) expand, column 1 (scrollbar) stay narrow
        text_container.columnconfigure(0, weight=1)
        text_container.rowconfigure(0, weight=1)

        # Text widget
        self.notes_text = tk.Text(
            text_container,
            wrap='word',
            font=('Arial', 12),
            undo=True,
            bg='#1e1e2f',
            fg='white',
            insertbackground='white'
        )
        self.notes_text.grid(row=0, column=0, sticky='nsew', padx=(0, 4))

        # Vertical scrollbar on the right
        scroll_y = ttk.Scrollbar(text_container, orient='vertical', command=self.notes_text.yview)
        scroll_y.grid(row=0, column=1, sticky='ns')

        # Connect scrollbar ↔ text widget
        self.notes_text.configure(yscrollcommand=scroll_y.set)

        # Optional: if you ever want horizontal scrolling (e.g. disable word wrap)
        # scroll_x = ttk.Scrollbar(frame, orient='horizontal', command=self.notes_text.xview)
        # scroll_x.pack(side=BOTTOM, fill=X)
        # self.notes_text.configure(xscrollcommand=scroll_x.set)
        # self.notes_text.config(wrap='none')

        # Load any saved notes
        self.load_notes(silent=True)
        
    def change_font_family(self, family):
        try:
            current_font = tkfont.Font(font=self.notes_text.cget("font"))
            new_font = tkfont.Font(family=family, size=current_font.cget("size"), weight=current_font.cget("weight"))
            self.notes_text.configure(font=new_font)
        except:
            pass

    def change_font_size(self, size):
        try:
            current_font = tkfont.Font(font=self.notes_text.cget("font"))
            new_font = tkfont.Font(family=current_font.cget("family"), size=size, weight=current_font.cget("weight"))
            self.notes_text.configure(font=new_font)
        except:
            pass

    def toggle_tag(self, tag_name):
        try:
            current_tags = self.notes_text.tag_names("sel.first")
            if tag_name in current_tags:
                self.notes_text.tag_remove(tag_name, "sel.first", "sel.last")
            else:
                self.notes_text.tag_add(tag_name, "sel.first", "sel.last")
        except tk.TclError:
            pass

    def choose_color(self):
        color = colorchooser.askcolor(title="Choose Text Color")
        if color[1]:
            tag_name = f"color_{color[1].replace('#','')}"
            try:
                self.notes_text.tag_add(tag_name, "sel.first", "sel.last")
                self.notes_text.tag_configure(tag_name, foreground=color[1])
            except tk.TclError:
                pass

    def set_alignment(self, align):
        try:
            for a in ['left','center','right']:
                self.notes_text.tag_remove(a, "sel.first", "sel.last")
            self.notes_text.tag_add(align, "sel.first", "sel.last")
            self.notes_text.tag_configure(align, justify=align, lmargin1=0, lmargin2=20 if align != 'left' else 0)
        except tk.TclError:
            pass

    def insert_bullet(self):
        try:
            self.notes_text.insert("insert", "• ")
            self.notes_text.tag_add("bullet_indent", "insert linestart", "insert lineend")
            self.notes_text.tag_configure("bullet_indent", lmargin1=20, lmargin2=40)
            self.notes_text.mark_set("insert", "insert + 3c")
            self.notes_text.focus_set()
        except:
            pass

    def save_notes(self):
        content = self.notes_text.get("1.0", tk.END).rstrip()
        tags_data = {}
        for tag in self.notes_text.tag_names():
            if tag in ('sel',): continue
            ranges = self.notes_text.tag_ranges(tag)
            if not ranges:
                continue
            tag_ranges = []
            for i in range(0, len(ranges), 2):
                start = str(ranges[i])
                end = str(ranges[i+1])
                tag_ranges.append((start, end))
            if tag_ranges:
                cfg = {}
                if tag == 'bold':
                    cfg['font'] = 'bold'
                elif tag == 'italic':
                    cfg['font'] = 'italic'
                elif tag == 'underline':
                    cfg['underline'] = True
                elif tag.startswith('color_'):
                    cfg['foreground'] = self.notes_text.tag_cget(tag, 'foreground')
                elif tag in ('left', 'center', 'right'):
                    cfg['justify'] = tag
                elif tag == "bullet_indent":
                    cfg['lmargin1'] = 20
                    cfg['lmargin2'] = 40
                tags_data[tag] = {
                    'ranges': tag_ranges,
                    'config': cfg
                }
        data = {
            "content": content,
            "tags": tags_data,
            "saved_at": datetime.now().isoformat()
        }
        try:
            with open(NOTES_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            messagebox.showinfo("Notes", "Notes (with formatting) saved successfully.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{str(e)}")

    def on_closing(self):
        if not hasattr(self, 'notes_text'):
            self.root.destroy()
            return
        current_content = self.notes_text.get("1.0", tk.END).strip()
        if current_content:
            self.save_notes()
        self.root.destroy()

    def load_notes(self, silent=False):
        if not os.path.exists(NOTES_FILE):
            if not silent:
                messagebox.showinfo("Notes", "No saved notes found.")
            return
        try:
            with open(NOTES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.notes_text.delete("1.0", tk.END)
            content = data.get("content", "")
            self.notes_text.insert(tk.END, content)
            tags_data = data.get("tags", {})
            for tag, info in tags_data.items():
                if 'config' in info:
                    cfg = info['config']
                    if 'font' in cfg:
                        if cfg['font'] == 'bold':
                            self.notes_text.tag_configure(tag, font=('Arial', 12, 'bold'))
                        elif cfg['font'] == 'italic':
                            self.notes_text.tag_configure(tag, font=('Arial', 12, 'italic'))
                    if 'underline' in cfg:
                        self.notes_text.tag_configure(tag, underline=True)
                    if 'foreground' in cfg:
                        self.notes_text.tag_configure(tag, foreground=cfg['foreground'])
                    if 'justify' in cfg:
                        self.notes_text.tag_configure(tag, justify=cfg['justify'])
                    if 'lmargin1' in cfg and 'lmargin2' in cfg:
                        self.notes_text.tag_configure(tag, lmargin1=cfg['lmargin1'], lmargin2=cfg['lmargin2'])
                for start, end in info.get('ranges', []):
                    self.notes_text.tag_add(tag, start, end)
            if not silent:
                messagebox.showinfo("Notes", "Notes (with formatting) loaded successfully.")
        except Exception as e:
            if not silent:
                messagebox.showerror("Error", f"Failed to load:\n{str(e)}")

    def _build_settings(self):
        f = ttk.Frame(self.tab_settings, padding=30)
        f.pack(fill=BOTH, expand=True)
        ttk.Label(f, text="API Key").grid(row=0, column=0, sticky='e', pady=8, padx=10)
        self.api_key_var = tk.StringVar(value=self.creds.key)
        ttk.Entry(f, textvariable=self.api_key_var, width=55).grid(row=0, column=1, pady=8)
        ttk.Label(f, text="API Secret").grid(row=1, column=0, sticky='e', pady=8, padx=10)
        self.api_secret_var = tk.StringVar(value=self.creds.secret)
        ttk.Entry(f, textvariable=self.api_secret_var, width=55, show="*").grid(row=1, column=1, pady=8)
        btn_frame = ttk.Frame(f)
        btn_frame.grid(row=2, column=1, pady=20, sticky='e')
        ttk.Button(btn_frame, text="Save Credentials", bootstyle="success", command=self.save_credentials).pack(side=LEFT, padx=8)
        ttk.Button(btn_frame, text="Clear Cache", bootstyle="warning", command=self.clear_cache).pack(side=LEFT, padx=8)
        ttk.Button(btn_frame, text="Clear Transactions", bootstyle="danger", command=self.clear_transactions).pack(side=LEFT, padx=8)
        ttk.Button(btn_frame, text="Fetch & Import History CSV", bootstyle="info",
                   command=self.fetch_and_import_history).pack(side=LEFT, padx=8)

    def save_credentials(self):
        creds = ApiCredentials(self.api_key_var.get().strip(), self.api_secret_var.get().strip())
        Secrets.save(creds)
        self.creds = creds
        self.service = Trading212Service(creds)
        messagebox.showinfo("Saved", "Credentials updated. Restart recommended.")

    def clear_cache(self):
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        messagebox.showinfo("Cache", "Cache cleared.")

    def clear_transactions(self):
        if messagebox.askyesno("Confirm", "Delete all transactions?"):
            self.df = pd.DataFrame()
            self.repo.save(self.df)
            self.render_transactions()
            self.refresh()

    def import_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if not path: return
        self._import_csv_from_path(path)
        self.render_transactions()
        messagebox.showinfo("Imported", f"Imported from file.\nTotal transactions: {len(self.df)}")
        self.refresh(async_fetch=True)

    def fetch_and_import_history(self):
        if not self.creds.key or not self.creds.secret:
            messagebox.showerror("Error", "API credentials not set in Settings.")
            return

        # Single chunk: last 365 days only (avoids rate limit & splitting complexity)
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=365)

        progress_win = tk.Toplevel(self.root)
        progress_win.title("Fetching Recent Trading 212 History")
        progress_win.geometry("450x180")
        progress_win.transient(self.root)
        progress_win.grab_set()

        ttk.Label(progress_win, text="Requesting last 12 months (single API call)...", font=('Segoe UI', 10)).pack(pady=10)
        status_var = tk.StringVar(value="Requesting report...")
        ttk.Label(progress_win, textvariable=status_var, wraplength=420).pack(pady=5)

        def run_fetch():
            try:
                time_from = start_date.strftime("%Y-%m-%dT00:00:00Z")
                time_to   = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")

                status_var.set("Sending export request...")
                progress_win.update()

                report_id = self.service.request_history_export(time_from, time_to)

                status_var.set(f"Report requested (ID: {report_id})\nWaiting for generation...")
                progress_win.update()

                # Poll (with longer initial delay)
                time.sleep(10)
                max_attempts = 40  # ~10 minutes max
                for attempt in range(max_attempts):
                    time.sleep(15)
                    reports = self.service.get_export_status()
                    for rep in reports:
                        if rep.get("reportId") == report_id:
                            status = rep.get("status")
                            if status == "Finished":
                                dl_link = rep.get("downloadLink")
                                if not dl_link:
                                    raise RuntimeError("No download link found")
                                status_var.set("Downloading CSV...")
                                progress_win.update()

                                csv_bytes = self.service.download_export_csv(dl_link)

                                temp_path = os.path.join(DATA_DIR, f"temp_recent_export_{int(time.time())}.csv")
                                with open(temp_path, "wb") as f:
                                    f.write(csv_bytes)

                                status_var.set("Importing recent transactions...")
                                progress_win.update()

                                before_count = len(self.df)
                                self._import_csv_from_path(temp_path)
                                added = len(self.df) - before_count

                                # Keep file for now (comment out when stable)
                                # try: os.remove(temp_path)
                                # except: pass

                                status_var.set("Done!")
                                progress_win.update()
                                progress_win.after(2000, progress_win.destroy)

                                if added > 0:
                                    messagebox.showinfo("Success", f"Imported {added} new rows from the last 12 months.")
                                else:
                                    messagebox.showinfo("Done", "No new transactions found in the last 12 months.")
                                self.refresh(async_fetch=True)
                                return
                            elif status in ("Failed", "Canceled"):
                                raise RuntimeError(f"Report generation {status.lower()}")
                            else:
                                status_var.set(f"Status: {status} (attempt {attempt+1}/{max_attempts})")
                                progress_win.update()
                                break

                raise RuntimeError("Report generation timed out after ~10 minutes.")

            except Exception as e:
                progress_win.after(0, lambda: messagebox.showerror("Error", f"Failed:\n{str(e)}"))
                progress_win.after(0, progress_win.destroy)

        threading.Thread(target=run_fetch, daemon=True).start()

    def _import_csv_from_path(self, path: str):
        try:
            # Try multiple encodings
            encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'iso-8859-1']
            df_new = None
            for enc in encodings:
                try:
                    raw = pd.read_csv(path, encoding=enc, on_bad_lines='skip')
                    if not raw.empty:
                        df_new = raw
                        print(f"DEBUG: Read CSV with encoding: {enc}")
                        break
                except:
                    continue

            if df_new is None:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    content_preview = f.read(500)
                raise ValueError(f"Cannot parse CSV.\nFile preview:\n{content_preview}")

            # Normalize columns
            df_new.columns = df_new.columns.str.strip().str.lower()

            # Debug: show actual columns
            print("DEBUG: Columns in downloaded CSV:", list(df_new.columns))

            # Your existing mapping logic (expanded a bit)
            mapping = {
                'time': 'Date', 'date': 'Date',
                'action': 'Type', 'type': 'Type',
                'ticker': 'Ticker', 'symbol': 'Ticker',
                'no. of shares': 'Quantity', 'quantity': 'Quantity',
                'price / share': 'Price', 'price': 'Price',
                'total': 'Total', 'amount': 'Total',
                'result': 'Result', 'p/l': 'Result',
                'exchange rate': 'FX_Rate', 'fx rate': 'FX_Rate',
                'currency': 'Currency',
                'notes': 'Note', 'note': 'Note',
                'id': 'Reference'
            }

            processed = pd.DataFrame()
            for old, new in mapping.items():
                matches = [c for c in df_new.columns if old.lower() in c.lower()]
                if matches:
                    processed[new] = df_new[matches[0]]

            # Fee columns
            fee_cols = [c for c in df_new.columns if any(word in c.lower() for word in ['fee', 'tax', 'stamp', 'commission'])]
            processed['Fee'] = df_new[fee_cols].sum(axis=1, numeric_only=True).fillna(0) if fee_cols else 0.0

            # Type normalization
            if 'Type' in processed.columns:
                processed['Type'] = processed['Type'].astype(str).str.lower().replace({
                    r'(?i)buy|market buy': 'Buy',
                    r'(?i)sell|market sell': 'Sell',
                    r'(?i)deposit': 'Deposit',
                    r'(?i)withdrawal': 'Withdrawal',
                    r'(?i)dividend': 'Dividend'
                }, regex=True)

            processed['Date'] = pd.to_datetime(processed.get('Date'), errors='coerce')

            # Numeric columns
            for col in ['Quantity', 'Price', 'Total', 'Fee', 'Result', 'FX_Rate']:
                if col in processed.columns:
                    processed[col] = pd.to_numeric(processed[col], errors='coerce').fillna(0)

            # Dedup & append (your existing logic)
            dedup_cols = ['Date', 'Type', 'Ticker', 'Total', 'Reference']
            dedup_cols = [c for c in dedup_cols if c in processed.columns]

            existing = self.df.copy()
            if 'Date' in existing.columns:
                existing['Date'] = pd.to_datetime(existing['Date'], errors='coerce')

            if not existing.empty and dedup_cols:
                merged = pd.merge(processed, existing[dedup_cols], how='left', on=dedup_cols, indicator=True)
                new_rows = merged[merged['_merge'] == 'left_only'].drop(columns='_merge')
            else:
                new_rows = processed.copy()

            if new_rows.empty:
                print("DEBUG: No new rows after deduplication")
                return

            self.df = pd.concat([self.df, new_rows], ignore_index=True)
            self.df = self.repo.deduplicate(self.df.fillna({'Ticker':'-', 'Note':''}))
            self.df = self.df.sort_values('Date', ascending=False).reset_index(drop=True)
            self.repo.save(self.df)

            print(f"DEBUG: Added {len(new_rows)} new rows from recent history")

        except Exception as e:
            print(f"ERROR during import: {str(e)}")
            raise

    def _sort_tree(self, tree, col, reverse):
        data = [(tree.set(k, col), k) for k in tree.get_children('')]
        data.sort(reverse=reverse, key=lambda t: (t[0] is not None, t[0]))
        for i, (_, k) in enumerate(data):
            tree.move(k, '', i)
        tree.heading(col, command=lambda: self._sort_tree(tree, col, not reverse))

if __name__ == '__main__':
    root = tb.Window(themename="darkly") if BOOTSTRAP else tk.Tk()
    app = Trading212App(root)
    root.mainloop()
