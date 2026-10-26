# Earn Wallet Telegram Mini App

A dark-theme mini app for the Earn Wallet bot. It keeps its OWN wallet database (wallet_miniapp.db, or the path in MINIAPP_DB_PATH). It does not read the bot's bot_data.db, so balances start fresh: every new user gets a 50 BDT signup bonus the first time they open the app. Balances in the mini app and in the bot are separate.

Files
- miniapp_api.py: the API (FastAPI). It also serves the app from the public folder.
- public/index.html, public/style.css, public/app.js: the mini app frontend (no build step).
- requirements.txt

What you need
- Python 3.10 or newer (Render, Railway, any VPS, or your own PC for testing).
- The environment variable BOT_TOKEN, set to the same token as your bot (set it in the hosting settings, not in the code).
- Optional: BOT_USERNAME (used for the referral link), MINIAPP_DB_PATH (path of the wallet database; use a persistent disk if your host has one).

Setup
1. Install: pip install -r requirements.txt
2. Run: uvicorn miniapp_api:app --host 0.0.0.0 --port 8000
3. Put the API on HTTPS (Render gives you a free https address). Use that address as the Mini App URL.
4. Open the address in a normal browser to see the page (the wallet only loads inside Telegram).

Connect it to the bot
- Add an Open Wallet button in the bot's /start message with the https address, and/or set it as the menu button in @BotFather (Bot Settings, Menu Button, Configure menu button).

Notes
- Every request is checked with Telegram's signed data (initData) using BOT_TOKEN. A user can only see and use their own wallet.
- If the wallet cannot load, the app stays on screen and shows the reason (for example a missing BOT_TOKEN or a session that must be reopened from the bot).
