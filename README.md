# ActiveSG gym alerts

A Telegram bot that messages you when an ActiveSG gym you track is at or below a crowd level you choose (e.g. 30%). It runs on this PC and costs nothing.

- Checks every 5 minutes, only during your alert hours (default: every day 07:00–22:00 SGT).
- Sends one alert when a gym drops to or below your threshold. It won't alert again for that gym until it goes 5 points above the threshold.
- While it's checking and the laptop is plugged in, it keeps Windows from idle-sleeping. The screen still turns off, and closing the lid still sleeps the laptop.
- If it can't read ActiveSG 3 times in a row, it tells you once.

## One-time setup

1. In Telegram, open **@BotFather**, send `/newbot`, and follow the prompts. It gives you a token like `123456:ABC-...`.
2. Copy `.env.example` to `.env` and paste the token after `TELEGRAM_BOT_TOKEN=`.
3. Double-click `start.cmd`.
4. Open your new bot in Telegram and send `/start`. The first chat to do this becomes the only one the bot listens to.
5. Send `/gyms` and tap the gyms you want. Then set `/threshold 30`.

## Everyday use

| Command | What it does |
|---|---|
| `/on`, `/off` | Turn alerts on or off |
| `/gyms` | Tap to track or untrack gyms (✅ = tracked), with live % |
| `/threshold 30` | Alert at or below 30% |
| `/status` | Your settings and live % for tracked gyms |
| `/hours 07:00-22:00` | Alert hours for every day |
| `/hours weekday 18:00-22:00` | Alert hours Mon–Fri (`weekend` for Sat–Sun) |

To stop the bot, close the minimized "ActiveSG gym bot" window. To check that ActiveSG can be read without Telegram, run `py gymbot.py --check`.

## How it works

ActiveSG has no public API. The [Gym & Pool Crowd page](https://activesg.gov.sg/gym-pool-crowd) loads its data from an undocumented endpoint that sits behind Cloudflare. Cloudflare blocks plain HTTP clients such as curl and Python's urllib, even from a Singapore home connection. So the bot reads the data through a headless copy of Microsoft Edge, using a normal desktop user-agent and its own browser profile in `edge-profile/`. If ActiveSG changes the endpoint, the bot's failure alert will tell you.

Files the bot creates (they're in `.gitignore`):
- `.env`: your token
- `state.json`: your settings
- `gymbot.log`: activity log
- `edge-profile/`: the headless browser profile

Run the tests with `py -m unittest test_gymbot`.

## Troubleshooting

- **`CERTIFICATE_VERIFY_FAILED` in `gymbot.log`:** Windows downloads some root certificates only the first time it needs them, and Python only reads the ones already downloaded. Open https://api.telegram.org once in Edge, and the bot will connect on its next retry.
- **"gymbot is already running":** A copy is already open. Look for the minimized "ActiveSG gym bot" window.
