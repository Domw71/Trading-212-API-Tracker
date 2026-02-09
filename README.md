🚀 Quick Start

Obtain Trading 212 API credentials
Log in at https://www.trading212.com (web or mobile app)
Go to Settings → API (Beta)
Accept the mandatory risk warning
Click Generate API key
Give it a name (e.g. "Portfolio Pro")
Set IP restriction (recommended for security) or leave unrestricted for testing
Copy API Key and API Secret (secret is shown only once!)
Security reminder: Never share the secret. You can revoke or regenerate keys anytime in the same settings page.
Run the applicationBashgit clone https://github.com/YOUR_USERNAME/trading212-portfolio-pro.git
cd trading212-portfolio-pro
pip install -r requirements.txt          # create this file if needed
python trading212_portfolio_pro.py
Configure API credentials
Open the Settings tab
Paste your API Key and API Secret
Click Save Credentials
Restart the app (strongly recommended after changing credentials)

Import your transaction history (required for accurate net gain & total return)
In Trading 212: Go to Account → History → Export → download CSV
(Note: usually limited to ~1 year per export; download multiple files for full history)
In the app sidebar: click Import CSV
Select your file(s) → the app auto-maps columns, removes duplicates, sorts by date
Repeat for older periods if needed (merges automatically)
After import + refresh → Net Gain and Total Return become meaningful

Start tracking
The dashboard auto-refreshes live data
The net gain history chart starts collecting points with each successful refresh


📁 Folder Structure (after first run)
texttrading212-portfolio-pro/
├── data/
│   ├── transactions.csv          # your imported trades (do not edit manually)
│   ├── positions_cache.json      # temporary cache (short TTL)
│   ├── settings.json             # stored API credentials (keep private!)
│   ├── min_max.json              # per-ticker historical min/max prices
│   └── net_gain_history.json     # persistent net gain time-series data
├── trading212_portfolio_pro.py   # main application script
├── README.md
└── .gitignore                    # should contain: data/
Security tip: Add data/ to .gitignore before pushing to any public repository.
⚠️ Important Notes & Limitations

This tool is read-only — it never places orders or modifies your account
Works best with Invest and Stocks & Shares ISA accounts (CFD accounts may have partial/limited API support)
CSV import is essential — without deposits/withdrawals history, net gain shows £0 or incorrect values
Maximum 500 history points stored (oldest dropped automatically)
Enforces ~60-second gap between API calls to respect rate limits
High clone counts with very low visitors on GitHub? → Completely normal (mostly automated bots/scanners)

🛠️ Troubleshooting
ProblemLikely Solution"Refreshing..." stuck foreverCheck internet, credentials, rate limit — wait 60–120 secondsNet Gain / Total Return shows £0Import CSV containing your depositsNo hover tooltips appear on chartRun pip install mplcursors and restart the appInterface looks plain/basicInstall pip install ttkbootstrap for the dark modern themeAPI returns 403 / Cloudflare errorRegenerate key pair, verify IP restriction settings, try without VPNSome positions show unrealised P/L = 0Known API behavior — app automatically applies fallback calculation when needed

🙌 Contributing
Pull requests are welcome!
Popular improvement ideas:

Support for automatic CSV download (if Trading 212 ever exposes it)
Chart export to PNG/PDF
Multi-currency handling/display
Additional performance metrics & visualizations

📜 License
MIT License
Free to use, modify, and share.
Built with ❤️ for the Trading 212 community.
Questions, bugs or feature requests? Feel free to open an issue.
Happy investing! 📈
text
