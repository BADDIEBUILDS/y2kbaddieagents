# y2kbaddie reseller bot 🛍️

A private Telegram bot (@y2kbaddieagentbot) that helps run the y2kbaddie reselling brand.
Send it photos of an item → it identifies it, checks authenticity honestly, prices it for the
UK resale market (Vinted/Depop/eBay) using live web search, and writes a ready-to-post listing.
Also answers reselling questions. Powered by Claude (Anthropic API).

Phase 1 (now): research + draft listings.
Phase 2 (later): auto-post to eBay via the official API — always with your approval first.

## Deploy (Railway)

1. Push this repo to GitHub.
2. Railway → New Project → Deploy from GitHub → pick this repo.
3. Add variables:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `ANTHROPIC_API_KEY` — from console.anthropic.com
   - `OWNER_ID` — your Telegram user id (see below) — **this makes it private**
   - `CLAUDE_MODEL` *(optional)* — defaults to `claude-opus-4-8`; set `claude-sonnet-4-6` to spend less
4. Deploy.

## Make it private

The bot ignores everyone except the ids in `OWNER_ID` (comma-separated).
Message the bot `/id` to get your user id, then paste it into the `OWNER_ID` variable in
Railway and redeploy. Until `OWNER_ID` is set, anyone who finds the bot can use it (and spend
your API credit) — so set it before sharing the bot's name anywhere.

## Use

- Send photos (a few angles + any markings) with an optional caption → get ID + price + listing.
- Or just type a question → it answers, searching the web for current prices.
