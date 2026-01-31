# Trading212 Portfolio Pro — v3.9 (Clean Charts + Tiny P/L Fix + Dashboard Enhancements)
# ------------------------------------------------------------
# Features & fixes summary:
# • Live positions & cash from Trading212 Public API
# • Accurate total return using imported CSV transactions
# • Rate-limit handling (429) with auto-retry + live cooldown countdown
# • Enhanced dashboard: color-coded Total Return, cash %, session change arrow
# • Warnings: stale data (>10 min), high concentration (>25% in one position)
# • Matplotlib toolbar removed for clean look
# • Charts polished: larger size, no top/right spines, subtle grid
# • Tiny negative P/L (e.g. -0.01) forced to 0.00 via threshold + rounding
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
from tkinter import filedialog, messagebox, BOTH, X, LEFT, RIGHT, Y, BOTTOM, TOP, EW, NS
from tkinter import Text, Scrollbar, StringVar, END

try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import *
    BOOTSTRAP = True
except ImportError:
    BOOTSTRAP = False

try:
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure                  # ← FIXED: this was missing
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB = True
except ImportError:
    MATPLOTLIB = False

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
APP_NAME = "Trading212 Portfolio Pro v3.9"
DATA_DIR = "data"
CSV_FILE = os.path.join(DATA_DIR, "transactions.csv")
CACHE_FILE = os.path.join(DATA_DIR, "positions_cache.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BASE_URL = "https://live.trading212.com/api/v0"
CACHE_TTL = 300
MAX_BAR_TICKERS = 25
STALE_THRESHOLD_MIN = 10
CONCENTRATION_THRESHOLD_PCT = 25
ZERO_PL_THRESHOLD = 0.05  # force |P/L| or value < this to 0.00
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
# SECRETS & CACHE
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
        except Exception:
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
    except (TypeError, ValueError):
        return 0.0

def round_money(val: float) -> float:
    return round(val, 2)

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
            print("[Cache] Using cached positions.")
            print("[Connection] OK")
            return [Position(**p) for p in cache['positions']]

        print(f"[API] Fetching {BASE_URL}/equity/positions ...")
        try:
            r = self.session.get(f"{BASE_URL}/equity/positions", timeout=12)
            r.raise_for_status()
            items = r.json()
            print(f"[API] Got {len(items)} positions.")
            print("[Connection] OK")
        except Exception as e:
            print(f"[Positions API error] {str(e)}")
            raise RuntimeError(f"API failed: {str(e)}")

        positions = []
        total_value = 0.0
        zero_pl_count = 0
        fallback_used = 0
        for pos in items:
            try:
                instr = pos.get('instrument', {})
                ticker_raw = instr.get('ticker', '')
                ticker = ticker_raw.split('_')[0].upper().rstrip('L')
                qty = safe_float(pos.get('quantity'))
                avg_price = safe_float(pos.get('averagePricePaid'))
                current_price = safe_float(pos.get('currentPrice'))
                w = pos.get('walletImpact', {}) or {}
                est_value = safe_float(w.get('currentValue'))
                api_pl = safe_float(w.get('unrealizedProfitLoss'))
                total_cost = safe_float(w.get('totalCost'))

                fallback_pl = (current_price - avg_price) * qty
                if api_pl == 0 and qty != 0 and abs(current_price - avg_price) > 0.001:
                    print(f"[Fallback] {ticker}: API PL=0 → calc {fallback_pl:.2f}")
                    unrealised_pl = fallback_pl
                    fallback_used += 1
                else:
                    unrealised_pl = api_pl

                # Fix tiny rounding artifacts (e.g. -0.01 → 0.00)
                if abs(unrealised_pl) < ZERO_PL_THRESHOLD:
                    unrealised_pl = 0.0
                if abs(est_value) < ZERO_PL_THRESHOLD:
                    est_value = 0.0

                # Round all monetary values
                est_value = round_money(est_value)
                unrealised_pl = round_money(unrealised_pl)
                total_cost = round_money(total_cost)

                if unrealised_pl == 0:
                    zero_pl_count += 1

                positions.append(Position(
                    ticker=ticker,
                    quantity=qty,
                    avg_price=avg_price,
                    current_price=current_price,
                    est_value=est_value,
                    unrealised_pl=unrealised_pl,
                    total_cost=total_cost
                ))
                total_value += est_value
            except Exception as e:
                print(f"[Parse skip] {e}")
                continue

        if fallback_used:
            print(f"[Info] Fallback used on {fallback_used} positions.")
        if zero_pl_count:
            print(f"[Warn] {zero_pl_count}/{len(positions)} positions have P/L = 0")

        for p in positions:
            p.portfolio_pct = (p.est_value / total_value * 100) if total_value > 0 else 0

        Cache.save([p.__dict__ for p in positions])
        return positions

    def fetch_cash_balance(self) -> float:
        print("[API] Fetching cash balance...")
        r = self.session.get(f"{BASE_URL}/equity/account/cash", timeout=8)
        r.raise_for_status()
        data = r.json()
        print("[API Full cash response]", json.dumps(data, indent=2))
        keys = ['free', 'freeCash', 'cash', 'available']
        for k in keys:
            val = data.get(k)
            if val is not None:
                cash = safe_float(val)
                print(f"[API] Parsed {k}: £{cash:.2f}")
                return round_money(cash)
        print("[API] No valid cash key found, defaulting to 0.0")
        return 0.0

# ────────────────────────────────────────────────
# ANALYTICS
# ────────────────────────────────────────────────
class Analytics:
    @staticmethod
    def calculate(df: pd.DataFrame, positions: List[Position], cash: float) -> Dict:
        if df.empty:
            holdings_value = sum(p.est_value for p in positions)
            total_assets = holdings_value + cash
            return {
                'total_assets': total_assets,
                'holdings_value': holdings_value,
                'net_gain': 0.0,
                'total_return_pct': 0.0,
                'realised_pl': 0.0,
                'fees': 0.0,
                'deposits': 0.0,
                'deposit_count': 0,
                'ttm_dividends': 0.0,
            }
        fees = float(df['Fee'].sum()) if 'Fee' in df.columns else 0.0
        realised = float(df['Result'].sum()) if 'Result' in df.columns else 0.0
        deposit_mask = df['Type'].str.contains('deposit', case=False, na=False)
        deposits_sum = float(df.loc[deposit_mask, 'Total'].sum())
        deposit_count = int(deposit_mask.sum())
        holdings_value = sum(p.est_value for p in positions)
        total_assets = holdings_value + cash
        net_gain = total_assets - deposits_sum
        total_return_pct = (net_gain / deposits_sum * 100) if deposits_sum > 0 else 0.0
        ttm_dividends = 0.0
        if not df.empty and 'Date' in df.columns and 'Type' in df.columns and 'Result' in df.columns:
            one_year_ago = datetime.now() - timedelta(days=365)
            recent_div = df[
                (df['Date'] >= one_year_ago) &
                df['Type'].str.contains('dividend', case=False, na=False) &
                (df['Result'] > 0)
            ]['Result'].sum()
            ttm_dividends = float(recent_div)
        return {
            'total_assets': total_assets,
            'holdings_value': holdings_value,
            'net_gain': net_gain,
            'total_return_pct': total_return_pct,
            'realised_pl': realised,
            'fees': fees,
            'deposits': deposits_sum,
            'deposit_count': deposit_count,
            'ttm_dividends': ttm_dividends,
        }

# ────────────────────────────────────────────────
# MAIN APPLICATION
# ────────────────────────────────────────────────
class Trading212App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1700x1000")
        self.repo = TransactionsRepo()
        self.df = self.repo.load()
        self.creds = Secrets.load()
        self.service = Trading212Service(self.creds)
        self.positions: List[Position] = []
        self.cash_balance: float = 0.0
        self.last_refresh_str = "Never"
        self.last_successful_refresh = 0.0
        self.last_total_assets = 0.0
        self.auto_retry_scheduled = False
        self.MIN_REFRESH_GAP = 60
        self.cooldown_end_time = 0.0
        self.countdown_after_id = None
        self._setup_style()
        self._build_ui()
        self.refresh(async_fetch=True)

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
        self.tab_dashboard = ttk.Frame(self.content)
        self.tabs["Dashboard"] = self.tab_dashboard
        self.tab_transactions = ttk.Frame(self.content)
        self.tabs["Transactions"] = self.tab_transactions
        self.tab_positions = ttk.Frame(self.content)
        self.tabs["Positions"] = self.tab_positions
        self.tab_analyst = ttk.Frame(self.content)
        self.tabs["AI Analyst"] = self.tab_analyst
        self.tab_settings = ttk.Frame(self.content)
        self.tabs["Settings"] = self.tab_settings

        self._build_dashboard()
        self._build_transactions()
        self._build_positions()
        self._build_analyst()
        self._build_settings()
        self._build_sidebar()
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
        menu_items = ["Dashboard", "Transactions", "Positions", "AI Analyst", "Settings"]
        self.menu_btns = {}
        for item in menu_items:
            bootstyle = "secondary" if BOOTSTRAP else ""
            btn = ttk.Button(self.sidebar, text=item, command=lambda t=item: self.switch_tab(t), bootstyle=bootstyle)
            btn.pack(fill=X, pady=4, padx=8)
            self.menu_btns[item] = btn
        ttk.Separator(self.sidebar).pack(fill=X, pady=15, padx=10)
        ttk.Button(self.sidebar, text="Refresh", bootstyle="info", command=lambda: self.refresh(True)).pack(fill=X, pady=4, padx=8)
        ttk.Button(self.sidebar, text="Import CSV", bootstyle="primary", command=self.import_csv).pack(fill=X, pady=4, padx=8)
        ttk.Separator(self.sidebar).pack(fill=X, pady=15, padx=10)

        self.stats_frame = ttk.Frame(self.sidebar)
        self.stats_frame.pack(fill=X, padx=10, pady=10)
        stats = [
            ("# Positions", "—"),
            ("Avg Position", "—"),
            ("Cash %", "—"),
            ("Total Deposits", "—"),
            ("Deposits Count", "—"),
            ("Last Refresh", "—"),
        ]
        self.stats_vars = {}
        for label, default in stats:
            f = ttk.Frame(self.stats_frame)
            f.pack(fill=X, pady=4)
            ttk.Label(f, text=label + ":", font=('Segoe UI', 11)).pack(anchor='w')
            var = tk.StringVar(value=default)
            ttk.Label(f, textvariable=var, font=('Segoe UI', 13, 'bold')).pack(anchor='w')
            self.stats_vars[label] = var

        ttk.Separator(self.sidebar).pack(fill=X, pady=15, padx=10)
        ttk.Label(self.sidebar, text="Additional Features:", font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=10)
        ttk.Button(self.sidebar, text="Help / About", bootstyle="outline", command=self.show_help).pack(fill=X, pady=4, padx=8)

        self.refresh_label = ttk.Label(self.sidebar, text="Status: Waiting for refresh...", foreground='gray')
        self.refresh_label.pack(pady=10, padx=10, anchor='s')

    def show_help(self):
        messagebox.showinfo("Help / About", f"{APP_NAME}\n\nA tool for managing Trading212 portfolios.\n\nVersion: 3.9\nBuilt with Tkinter and Pandas.\n\nFor support, check documentation.")

    def _build_dashboard(self):
        main = ttk.Frame(self.tab_dashboard, padding=20)
        main.pack(fill=BOTH, expand=True)

        cards_frame = ttk.Frame(main)
        cards_frame.pack(fill=X, pady=(0, 20))
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
                ax.title.set_color('white')
                ax.title.set_fontsize(14)
                ax.xaxis.label.set_color('white')
                ax.yaxis.label.set_color('white')
                ax.grid(True, axis='y', alpha=0.15, color='gray', linestyle='--', linewidth=0.5)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                ax.spines['left'].set_color('gray')
                ax.spines['bottom'].set_color('gray')

            self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
            self.canvas.get_tk_widget().pack(fill=BOTH, expand=True, padx=6, pady=6)
            # Navigation toolbar intentionally NOT added → hidden

    def _set_total_return_text(self, text: str):
        if "Total Return" in self.card_vars:
            self.card_vars["Total Return"].set(text)

    def start_cooldown_countdown(self, seconds_left: int):
        if self.countdown_after_id:
            self.root.after_cancel(self.countdown_after_id)
            self.countdown_after_id = None

        if seconds_left <= 0:
            self._set_total_return_text("Auto-refreshing...")
            self.refresh(async_fetch=True)
            return

        self._set_total_return_text(f"Wait {seconds_left}s...")
        self.countdown_after_id = self.root.after(
            1000, lambda: self.start_cooldown_countdown(seconds_left - 1)
        )

    def refresh(self, async_fetch: bool = False, is_auto_retry: bool = False):
        def _task():
            try:
                print("\n=== Refresh started ===")
                self.root.after(0, lambda: self._set_total_return_text("Fetching..."))

                self.positions = self.service.fetch_positions()

                cash_ok = True
                try:
                    self.cash_balance = self.service.fetch_cash_balance()
                except Exception as e:
                    cash_ok = False
                    print(f"[Cash fetch failed] {str(e)}")

                if cash_ok:
                    self.last_successful_refresh = time.time()
                    self.cooldown_end_time = time.time() + self.MIN_REFRESH_GAP
                    self.auto_retry_scheduled = False

                    summary = Analytics.calculate(self.df, self.positions, self.cash_balance)
                    tv = summary['total_assets']
                    num_pos = len([p for p in self.positions if p.quantity > 0])
                    avg_pos = tv / num_pos if num_pos > 0 else 0
                    cash_pct = (self.cash_balance / tv * 100) if tv > 0 else 0
                    zero_count = sum(1 for p in self.positions if p.unrealised_pl == 0)
                    status = f"Warning: {zero_count} zero P/L" if zero_count > len(self.positions)//2 else "OK"
                    self.last_refresh_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    session_change_str = ""
                    if self.last_total_assets > 0:
                        change_pct = ((tv - self.last_total_assets) / self.last_total_assets) * 100
                        arrow = "↑" if change_pct >= 0 else "↓"
                        session_change_str = f" {arrow} {change_pct:+.2f}%"
                    self.last_total_assets = tv

                    self.root.after(0, lambda: self._render_dashboard(summary, num_pos, avg_pos, cash_pct, session_change_str))
                    self.root.after(0, lambda: self.refresh_label.config(
                        text=f"Last refresh: {self.last_refresh_str} | {status}",
                        foreground='lime' if "Warning" not in status else 'orange'
                    ))
                    self.root.after(0, self._render_positions)
                    self.root.after(0, self.render_transactions)
                    print("=== Refresh completed successfully ===")
                else:
                    self.auto_retry_scheduled = True
                    self.root.after(0, lambda: self._set_total_return_text("Fetch error – retrying in 60s..."))
                    self.root.after(0, lambda: self.refresh_label.config(
                        text="Cash fetch failed – auto-retrying...",
                        foreground='orange'
                    ))
                    self.root.after(60000, lambda: self.refresh(async_fetch=False, is_auto_retry=True))

            except Exception as e:
                print(f"=== Refresh failed: {str(e)} ===")
                self.root.after(0, lambda: self._set_total_return_text(f"Error: {str(e)}"))
                self.root.after(0, lambda: self.refresh_label.config(text=f"Error: {str(e)}", foreground='red'))

        now = time.time()
        if not is_auto_retry:
            if self.cooldown_end_time > now:
                remaining = int(self.cooldown_end_time - now) + 1
                self.start_cooldown_countdown(remaining)
                return
            else:
                self.cooldown_end_time = now + self.MIN_REFRESH_GAP

        if async_fetch:
            threading.Thread(target=_task, daemon=True).start()
        else:
            _task()

    def _render_dashboard(self, s: Dict, num_pos: int, avg_pos: float, cash_pct: float, session_change_str: str = ""):
        self.card_vars["Portfolio Value"].set(f"£{round_money(s['total_assets']):,.2f}")
        self.card_vars["Cash Available"].set(f"£{round_money(self.cash_balance):,.2f} ({cash_pct:.1f}%)")

        gain = s['net_gain']
        pct = s['total_return_pct']
        sign_gain = "+" if gain >= 0 else ""
        sign_pct = "+" if pct >= 0 else ""
        return_text = f"{sign_gain}£{round_money(gain):,.2f} ({sign_pct}{pct:.2f}%){session_change_str}"
        self.card_vars["Total Return"].set(return_text)

        if BOOTSTRAP and "Total Return" in self.card_frames:
            if pct > 10:
                self.card_frames["Total Return"].configure(bootstyle="success")
            elif pct > 3:
                self.card_frames["Total Return"].configure(bootstyle="info")
            elif pct > -3:
                self.card_frames["Total Return"].configure(bootstyle="secondary")
            elif pct > -10:
                self.card_frames["Total Return"].configure(bootstyle="warning")
            else:
                self.card_frames["Total Return"].configure(bootstyle="danger")

        self.card_vars["TTM Dividends"].set(f"£{round_money(s['ttm_dividends']):,.2f}")
        self.card_vars["Realised P/L"].set(f"£{round_money(s['realised_pl']):+,.2f}")
        self.card_vars["Fees Paid"].set(f"£{round_money(s['fees']):,.2f}")

        self.stats_vars["# Positions"].set(f"{num_pos}")
        self.stats_vars["Avg Position"].set(f"£{round_money(avg_pos):,.0f}")
        self.stats_vars["Cash %"].set(f"{cash_pct:.1f}%")
        self.stats_vars["Total Deposits"].set(f"£{round_money(s['deposits']):,.0f}")
        self.stats_vars["Deposits Count"].set(f"{s.get('deposit_count', 0):,d}")
        self.stats_vars["Last Refresh"].set(self.last_refresh_str)

        warnings = []
        minutes_ago = (time.time() - self.last_successful_refresh) / 60 if self.last_successful_refresh > 0 else 999
        if minutes_ago > STALE_THRESHOLD_MIN:
            warnings.append(f"Data stale ({int(minutes_ago)} min ago)")

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
                self.ax1.set_title(f"Top {top_n} Positions ({n-top_n} more)", fontsize=13)
                self.ax1.invert_yaxis()
                self.ax1.tick_params(axis='y', labelsize=9)

            self.ax1.set_title("Position Values" if n <= MAX_BAR_TICKERS else "", fontsize=14)

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

    def import_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        try:
            raw = pd.read_csv(path)
            raw.columns = raw.columns.str.strip().str.lower()
            mapping = {
                'time': 'Date', 'action': 'Type', 'ticker': 'Ticker',
                'no. of shares': 'Quantity', 'price / share': 'Price',
                'total': 'Total', 'result': 'Result', 'exchange rate': 'FX_Rate',
                'currency (total)': 'Currency', 'notes': 'Note', 'name': 'Instrument Name',
                'id': 'Reference'
            }
            df_new = pd.DataFrame()
            for old, new in mapping.items():
                candidates = [c for c in raw.columns if old.lower() in c.lower()]
                if candidates:
                    df_new[new] = raw[candidates[0]]
            fee_cols = [c for c in raw.columns if any(k in c.lower() for k in ['fee', 'tax', 'stamp', 'conversion'])]
            df_new['Fee'] = raw[fee_cols].sum(axis=1, numeric_only=True).fillna(0) if fee_cols else 0.0
            df_new['Type'] = df_new.get('Type', '').replace({
                'market buy': 'Buy', 'buy': 'Buy',
                'market sell': 'Sell', 'sell': 'Sell',
                'deposit': 'Deposit', 'withdrawal': 'Withdrawal',
                'dividend': 'Dividend'
            }, regex=True)
            df_new['Date'] = pd.to_datetime(df_new.get('Date'), errors='coerce')
            for col in ['Quantity', 'Price', 'Total', 'Fee', 'Result', 'FX_Rate']:
                if col in df_new.columns:
                    df_new[col] = pd.to_numeric(df_new[col], errors='coerce').fillna(0)
            df_new = df_new.reindex(columns=['Date','Type','Ticker','Quantity','Price','Total',
                                             'Fee','Result','FX_Rate','Currency','Note','Reference'])
            for c in ['Date', 'Type', 'Ticker', 'Quantity', 'Total', 'Reference', 'Note']:
                if c not in df_new.columns:
                    df_new[c] = pd.NA
            dedup_cols = ['Date', 'Type', 'Ticker', 'Quantity', 'Price', 'Total', 'Fee', 'Reference']
            dedup_cols = [c for c in dedup_cols if c in df_new.columns]
            existing = self.df.copy()
            if 'Date' in existing.columns:
                existing['Date'] = pd.to_datetime(existing['Date'], errors='coerce')
            if not existing.empty and dedup_cols:
                merged_check = pd.merge(df_new, existing[dedup_cols],
                                       how='left', on=dedup_cols, indicator=True)
                new_rows = merged_check[merged_check['_merge'] == 'left_only'].drop(columns='_merge')
            else:
                new_rows = df_new.copy()
            if new_rows.empty:
                messagebox.showinfo("Import", "No new transactions found.")
                return
            self.df = pd.concat([self.df, new_rows], ignore_index=True)
            self.df = self.repo.deduplicate(self.df.fillna({'Ticker':'-','Note':''}))
            self.df = self.df.sort_values('Date', ascending=False).reset_index(drop=True)
            self.repo.save(self.df)
            self.render_transactions()
            added_count = len(new_rows)
            total_count = len(self.df)
            messagebox.showinfo("Import Complete", f"Added {added_count} new rows.\nTotal transactions now: {total_count}")
            self.refresh(async_fetch=True)
        except Exception as e:
            messagebox.showerror("CSV Error", f"Failed to import:\n{str(e)}")

    def _build_transactions(self):
        filter_bar = ttk.Frame(self.tab_transactions)
        filter_bar.pack(fill=X, pady=(0,8))
        ttk.Label(filter_bar, text="Filter:").pack(side=LEFT, padx=8)
        self.tx_filter_var = StringVar()
        ttk.Entry(filter_bar, textvariable=self.tx_filter_var, width=45).pack(side=LEFT, padx=6)
        self.tx_filter_var.trace('w', lambda *args: self.render_transactions())
        tree_frame = ttk.Frame(self.tab_transactions)
        tree_frame.pack(fill=BOTH, expand=True, padx=5, pady=5)
        cols = ["Date", "Type", "Ticker", "Quantity", "Price", "Total", "Fee", "Result", "Note"]
        self.tree_tx = ttk.Treeview(tree_frame, columns=cols, show='headings')
        for c in cols:
            self.tree_tx.heading(c, text=c, command=lambda col=c: self._sort_tree(self.tree_tx, col, False))
            anchor = 'w' if c in ["Date", "Type", "Ticker", "Note"] else 'e'
            width = 160 if c in ["Date", "Note"] else 110
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
        self.tree_tx.tag_configure('highlight', background='#3a3a55')
        self.tree_tx.tag_configure('buy', foreground='#66BB6A')
        self.tree_tx.tag_configure('sell', foreground='#EF5350')
        self.tree_tx.tag_configure('dividend', foreground='#FFCA28')
        self.tree_tx.tag_configure('total', font=('Segoe UI', 10, 'bold'), foreground='#BB86FC')
        self.render_transactions()

    def render_transactions(self):
        self.tree_tx.delete(*self.tree_tx.get_children(''))
        filter_text = self.tx_filter_var.get().lower().strip()
        if not filter_text:
            rows_to_show = self.df.iterrows()
        else:
            rows_to_show = [
                (idx, row) for idx, row in self.df.iterrows()
                if filter_text in ' '.join(str(v).lower() for v in row)
            ]
        for idx, (_, row) in enumerate(rows_to_show):
            values = [row.get(c, '') for c in self.tree_tx['columns']]
            tags = ['even' if idx % 2 == 0 else 'odd']
            ttype = str(row.get('Type', '')).lower()
            if 'buy' in ttype:
                tags.append('buy')
            elif 'sell' in ttype:
                tags.append('sell')
            elif 'dividend' in ttype:
                tags.append('dividend')
            self.tree_tx.insert('', 'end', values=values, tags=tags)
        if not self.df.empty:
            totals = [
                "TOTAL",
                "",
                "",
                self.df['Quantity'].sum(),
                "",
                self.df['Total'].sum(),
                self.df['Fee'].sum(),
                self.df['Result'].sum(),
                ""
            ]
            formatted = [f"{v:,.2f}" if isinstance(v, (int, float)) and i not in [0,1,2,4,8] else v
                         for i, v in enumerate(totals)]
            self.tree_tx.insert('', 'end', values=formatted, tags=('total',))

    def _sort_tree(self, tree, col, reverse):
        data = [(tree.set(k, col), k) for k in tree.get_children('')]
        data.sort(reverse=reverse, key=lambda t: (t[0] is not None, t[0]))
        for index, (_, k) in enumerate(data):
            tree.move(k, '', index)
        tree.heading(col, command=lambda: self._sort_tree(tree, col, not reverse))

    def _build_positions(self):
        frame = ttk.Frame(self.tab_positions, padding=12)
        frame.pack(fill=BOTH, expand=True)
        cols = ["Ticker", "Qty", "Avg Price", "Current", "Value", "Unreal. P/L", "Cost", "% Portfolio"]
        self.tree_pos = ttk.Treeview(frame, columns=cols, show='headings')
        for c in cols:
            self.tree_pos.heading(c, text=c, command=lambda col=c: self._sort_tree(self.tree_pos, col, False))
            anchor = 'w' if c == "Ticker" else 'e'
            width = 140 if c in ["Value", "Unreal. P/L", "Cost"] else 100
            self.tree_pos.column(c, width=width, anchor=anchor)
        vsb = ttk.Scrollbar(frame, orient=VERTICAL, command=self.tree_pos.yview)
        hsb = ttk.Scrollbar(frame, orient=HORIZONTAL, command=self.tree_pos.xview)
        self.tree_pos.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_pos.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree_pos.tag_configure('even', background='#222233')
        self.tree_pos.tag_configure('odd', background='#1a1a2a')
        self.tree_pos.tag_configure('profit', foreground='#66BB6A')
        self.tree_pos.tag_configure('loss', foreground='#EF5350')
        self.tree_pos.tag_configure('total', font=('Segoe UI', 10, 'bold'), foreground='#BB86FC')
        self._render_positions()

    def _render_positions(self):
        self.tree_pos.delete(*self.tree_pos.get_children(''))
        sorted_pos = sorted(self.positions, key=lambda x: -x.est_value if x.quantity > 0 else 0)
        total_value = sum(p.est_value for p in sorted_pos)
        total_pl = sum(p.unrealised_pl for p in sorted_pos)
        total_cost = sum(p.total_cost for p in sorted_pos)
        for idx, p in enumerate(sorted_pos):
            if p.quantity <= 0:
                continue
            tags = ['profit' if p.unrealised_pl >= 0 else 'loss']
            tags.append('even' if idx % 2 == 0 else 'odd')
            vals = (
                p.ticker,
                f"{p.quantity:,.4f}",
                f"£{round_money(p.avg_price):,.2f}",
                f"£{round_money(p.current_price):,.2f}",
                f"£{round_money(p.est_value):,.2f}",
                f"£{round_money(p.unrealised_pl):+,.2f}",
                f"£{round_money(p.total_cost):,.2f}",
                f"{p.portfolio_pct:.1f}%"
            )
            self.tree_pos.insert('', 'end', values=vals, tags=tags)
        footer = ("TOTAL", "", "", "", f"£{round_money(total_value):,.2f}", f"£{round_money(total_pl):+,.2f}", f"£{round_money(total_cost):,.2f}", "100.0%")
        self.tree_pos.insert('', 'end', values=footer, tags=('total',))

    def _build_analyst(self):
        f = ttk.Frame(self.tab_analyst, padding=20)
        f.pack(fill=BOTH, expand=True)
        ttk.Label(f, text="AI Analyst – Using Trading 212 Live Data", font=('Segoe UI', 14, 'bold')).pack(pady=10)
        ttk.Label(f, text="No external calls → fast & no rate limits", foreground='green').pack()
        ttk.Label(f, text="VERY BASIC insights – NOT financial advice – Educational only", foreground='orange').pack(pady=5)
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill=X, pady=10)
        ttk.Button(btn_frame, text="Analyze My Positions", bootstyle="primary", command=self.run_analyst).pack(side=LEFT, padx=10)
        ttk.Button(btn_frame, text="Clear", bootstyle="secondary", command=lambda: self.analyst_output.delete(1.0, tk.END)).pack(side=LEFT)
        self.analyst_output = tk.Text(f, wrap='word', font=('Consolas', 11), height=25)
        self.analyst_output.pack(fill=BOTH, expand=True, padx=5, pady=5)
        sb = tk.Scrollbar(f, command=self.analyst_output.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.analyst_output.config(yscrollcommand=sb.set)

    def run_analyst(self):
        if not self.positions:
            messagebox.showinfo("No Positions", "No positions loaded. Please refresh first.")
            return
        self.analyst_output.delete(1.0, tk.END)
        self.analyst_output.insert(tk.END, "Quick analysis using your live Trading 212 data...\n\n")
        total_value = sum(p.est_value for p in self.positions)
        total_unreal_pl = sum(p.unrealised_pl for p in self.positions)
        self.analyst_output.insert(tk.END, f"Portfolio snapshot:\n")
        self.analyst_output.insert(tk.END, f" Total value: £{round_money(total_value):,.2f}\n")
        self.analyst_output.insert(tk.END, f" Unrealised P/L: £{round_money(total_unreal_pl):+,.2f}\n\n")
        for p in sorted(self.positions, key=lambda x: -x.est_value):
            if p.quantity <= 0:
                continue
            price_change_pct = ((p.current_price / p.avg_price) - 1) * 100 if p.avg_price > 0 else 0
            pl_pct = (p.unrealised_pl / p.total_cost * 100) if p.total_cost > 0 else 0
            status = "BREAKEVEN" if abs(price_change_pct) < 0.5 else \
                     "IN PROFIT" if price_change_pct > 0 else "IN LOSS"
            comment = ""
            if price_change_pct > 40: comment = "Very strong unrealised gain → many consider taking partial profits"
            elif price_change_pct > 15: comment = "Good profit → often hold, but watch for reversal"
            elif price_change_pct > 0: comment = "Small gain → usually hold"
            elif price_change_pct < -40: comment = "Significant loss → review whether to cut or average down"
            elif price_change_pct < -15: comment = "Notable loss → review thesis"
            else: comment = "Minor movement → typically hold"
            block = f"[{p.ticker.upper()}] {p.quantity:,.3f} shares\n"
            block += f" Current: £{round_money(p.current_price):,.3f} Avg: £{round_money(p.avg_price):,.3f}\n"
            block += f" Change: {price_change_pct:+.1f}% P/L: £{round_money(p.unrealised_pl):+,.2f} ({pl_pct:+.1f}%)\n"
            block += f" Value: £{round_money(p.est_value):,.2f} ({p.portfolio_pct:.1f}%)\n"
            block += f" Status: {status} Thought: {comment}\n"
            block += "─" * 70 + "\n\n"
            self.analyst_output.insert(tk.END, block)
        self.analyst_output.insert(tk.END, "═"*80 + "\nNOT investment advice. Always do your own research.\n")
        self.analyst_output.see(tk.END)

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
        ttk.Button(btn_frame, text="Debug API", bootstyle="secondary", command=self.debug_api).pack(side=LEFT, padx=8)

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
        if messagebox.askyesno("Confirm", "Delete all saved transactions?"):
            self.df = pd.DataFrame()
            self.repo.save(self.df)
            self.render_transactions()
            self.refresh()

    def debug_api(self):
        try:
            r = self.service.session.get(f"{BASE_URL}/equity/positions", timeout=12)
            r.raise_for_status()
            data = r.json()
            with open(os.path.join(DATA_DIR, 'api_debug.json'), 'w') as f:
                json.dump(data, f, indent=2)
            summary = [f"{pos.get('instrument',{}).get('ticker','N/A')}: P/L {pos.get('walletImpact',{}).get('unrealizedProfitLoss','N/A')}" for pos in data[:8]]
            msg = f"Saved to data/api_debug.json\n\nSample:\n" + "\n".join(summary)
            if len(data) > 8: msg += f"\n... +{len(data)-8} more"
            messagebox.showinfo("API Debug", msg)
        except Exception as e:
            messagebox.showerror("Debug Error", str(e))

if __name__ == '__main__':
    root = tb.Window(themename="darkly") if BOOTSTRAP else tk.Tk()
    app = Trading212App(root)
    root.mainloop()
