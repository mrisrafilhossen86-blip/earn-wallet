# Earn Wallet Telegram Mini App

A premium dark-theme mini app for your Earn Wallet bot. It reads and writes the same bot database (bot_data.db), so balances, transfers, withdrawals, bonuses, promo codes and cashback are identical to the bot.

Files
- miniapp_api.py: the API (FastAPI). It also serves the app from the public folder.
- public/index.html, public/style.css, public/app.js: the mini app frontend (no build step).
- requirements.txt, .env.example

What you need
- Python 3.10 or newer on a small host (Render, Railway, any VPS, or your own PC for testing).
- The bot's folder m3bot_payment next to this folder (the API reads its database and receipt_image.py).

Setup (non-coder steps)
1. Copy .env.example to .env and set BOT_TOKEN to the same token as m3bot_payment/.env.
2. Install: pip install -r requirements.txt
3. Run: uvicorn miniapp_api:app --host 0.0.0.0 --port 8000
   Run this in a separate process. Do not stop the bot to do it. The bot keeps running on its own.
4. Put the API on HTTPS. Render or Railway give you an https address for free. Use that address as the Mini App URL.
5. Open http://localhost:8000 to see the app in a normal browser (login only works inside Telegram).

Connect it to the bot (BotFather, in Telegram)
1. Open @BotFather and send /mybots.
2. Tap your bot (for example @KeygenytnYef_bot).
3. Tap Bot Settings, then Menu Button, then Configure menu button.
4. Send the https address from step 4 above (for example https://your-app.onrender.com).
5. Send a button name when asked (for example Open Wallet).
Now the button appears next to the message box when users open the bot.

Notes
- Login is checked with Telegram's signed data (initData) using your bot token. A user can only see and use their own wallet.
- Running the API with the same database as the bot is safe, because both use short locked transactions.
- Withdrawals from the Mini App go to the admin as a request, same as the bot. Approve them in the bot's admin panel.
- If you change the database path, set BOT_DB_PATH in .env.
- The bot's code and database are not changed by this API, except it adds rows to the same tables, just like the bot does.
