"""ActiveSG gym capacity alerts on Telegram.

Every few minutes during your alert hours, reads ActiveSG's live gym capacity
and messages you when a tracked gym drops to or below your threshold. The
Telegram bot is also the control panel: /on, /off, /threshold, /gyms, /hours.

ActiveSG's capacity API sits behind Cloudflare, which rejects plain HTTP
clients, so the data is read through a headless Microsoft Edge (already on
every Windows PC). Only the Python standard library is used.

Run:        python gymbot.py
Test fetch: python gymbot.py --check
"""

import ctypes
import html
import json
import logging
import logging.handlers
import msvcrt
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "state.json"
LOCK_FILE = BASE_DIR / ".gymbot.lock"
LOG_FILE = BASE_DIR / "gymbot.log"
EDGE_PROFILE = BASE_DIR / "edge-profile"

CAPACITY_URL = (
    "https://activesg.gov.sg/api/trpc/pass.getFacilityCapacities"
    "?input=%7B%22json%22%3Anull%2C%22meta%22%3A%7B%22values%22%3A%5B%22undefined%22%5D%7D%7D"
)
CROWD_PAGE_URL = "https://activesg.gov.sg/gym-pool-crowd"

POLL_MINUTES = 5
# After alerting, a gym must climb this many points above the threshold before
# it can alert again, so readings hovering around the threshold don't spam you.
HYSTERESIS = 5
# Start fresh (re-alert gyms that are still low) after a gap this long, e.g.
# the next morning or after the PC was off.
REARM_AFTER_GAP_MINUTES = 30
FAIL_ALERT_AFTER = 3
LONG_POLL_SECONDS = 25

SGT = timezone(timedelta(hours=8))

DEFAULT_STATE = {
    "owner_chat_id": None,
    "enabled": True,
    "threshold": 30,
    "tracked": [],
    "hours": {"weekday": ["07:00", "22:00"], "weekend": ["07:00", "22:00"]},
    "alerted": [],
    "last_poll_at": None,
    "fail_count": 0,
    "gym_names": {},
    "update_offset": 0,
}

HELP_TEXT = """\
I watch ActiveSG gym crowds and message you when a gym you track is at or below your threshold.

/gyms - tap gyms to track or untrack them
/status - your settings and live capacity
/threshold 30 - alert at or below 30%
/on, /off - turn alerts on or off
/hours - show alert hours
/hours 07:00-22:00 - set hours for every day
/hours weekday 18:00-22:00 - set weekday hours (Mon-Fri)
/hours weekend 08:00-22:00 - set weekend hours (Sat-Sun)"""

BOT_COMMANDS = [
    {"command": "gyms", "description": "Choose gyms to track"},
    {"command": "status", "description": "Settings and live capacity"},
    {"command": "threshold", "description": "Set alert threshold, e.g. /threshold 30"},
    {"command": "on", "description": "Turn alerts on"},
    {"command": "off", "description": "Turn alerts off"},
    {"command": "hours", "description": "Show or set alert hours"},
    {"command": "help", "description": "How to use this bot"},
]

log = logging.getLogger("gymbot")


class FetchError(Exception):
    pass


class TelegramError(Exception):
    pass


# --- Pure logic (unit tested) -------------------------------------------------


def check_thresholds(gyms, tracked, threshold, alerted):
    """Decide which tracked gyms to alert on.

    Returns (gyms_to_alert, new_alerted_ids). A gym alerts once when it is at or
    below the threshold, then stays quiet until it rises above
    threshold + HYSTERESIS. Closed gyms are skipped.
    """
    by_id = {g["id"]: g for g in gyms}
    still_alerted = set(alerted)
    to_alert = []
    for gym_id in tracked:
        gym = by_id.get(gym_id)
        if gym is None or gym.get("isClosed"):
            continue
        pct = gym["capacityPercentage"]
        if pct <= threshold:
            if gym_id not in still_alerted:
                to_alert.append(gym)
                still_alerted.add(gym_id)
        elif pct > threshold + HYSTERESIS:
            still_alerted.discard(gym_id)
    return to_alert, still_alerted


def parse_hhmm(text):
    """'7' or '07:30' -> 'HH:MM'; raises ValueError on bad input."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", text.strip())
    if not m:
        raise ValueError(text)
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
        raise ValueError(text)
    return f"{hour:02d}:{minute:02d}"


def parse_range(text):
    """'18:00-22:00' -> ['18:00', '22:00']; raises ValueError on bad input."""
    start, sep, end = text.partition("-")
    if not sep:
        raise ValueError(text)
    start, end = parse_hhmm(start), parse_hhmm(end)
    if start >= end:
        raise ValueError(text)
    return [start, end]


def in_alert_hours(now, hours):
    day_type = "weekend" if now.weekday() >= 5 else "weekday"
    start, end = hours[day_type]
    return start <= now.strftime("%H:%M") < end


def short_name(name):
    for junk in ("ActiveSG Gym @ ", "ActiveSG Sport Park @ ", "ActiveSG Hockey Village @ ", " ActiveSG Gym"):
        name = name.replace(junk, "")
    return name.strip()


def format_alert(gyms, threshold):
    lines = [f"🏋️ At or below {threshold}% now:"]
    for g in sorted(gyms, key=lambda g: g["capacityPercentage"]):
        lines.append(f"• {short_name(g['name'])}: {g['capacityPercentage']}%")
    lines.append(CROWD_PAGE_URL)
    return "\n".join(lines)


# --- ActiveSG fetch -----------------------------------------------------------


def find_browser():
    candidates = [
        os.environ.get("BROWSER_PATH"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise FetchError("Microsoft Edge or Google Chrome not found (set BROWSER_PATH in .env)")


def browser_user_agent(browser):
    """A normal desktop UA matching the installed version.

    Headless mode otherwise announces itself as 'HeadlessChrome', which
    Cloudflare blocks.
    """
    versions = [p.name for p in Path(browser).parent.iterdir() if re.fullmatch(r"\d+(\.\d+){3}", p.name)]
    version = max(versions, key=lambda v: tuple(map(int, v.split("."))), default="140.0.0.0")
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version.split('.')[0]}.0.0.0 Safari/537.36"
    )
    if "msedge" in Path(browser).name.lower():
        ua += f" Edg/{version}"
    return ua


def fetch_capacities():
    """Return the list of gyms: {id, name, capacityPercentage, isClosed, ...}."""
    browser = find_browser()
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={EDGE_PROFILE}",
        f"--user-agent={browser_user_agent(browser)}",
        "--timeout=15000",
        "--dump-dom",
        CAPACITY_URL,
    ]
    last_error = None
    # A retry lets a Cloudflare cookie set on the first attempt take effect.
    for _ in range(2):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            last_error = FetchError("browser timed out")
            continue
        dom = proc.stdout.decode("utf-8", errors="replace")
        if not dom.strip():
            last_error = FetchError(f"browser returned nothing (exit code {proc.returncode})")
            continue
        m = re.search(r"<pre[^>]*>(.*?)</pre>", dom, re.S)
        if not m:
            snippet = re.search(r"<title>(.*?)</title>", dom, re.S)
            last_error = FetchError(f"unexpected page: {snippet.group(1) if snippet else dom[:120]!r}")
            continue
        try:
            payload = json.loads(html.unescape(m.group(1)))
            return payload["result"]["data"]["json"]["gymFacilities"]
        except (ValueError, KeyError, TypeError) as e:
            last_error = FetchError(f"unexpected response shape: {e}")
    raise last_error


# --- Windows power ------------------------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def on_ac_power():
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ACLineStatus == 1


def keep_awake(awake):
    """Stop Windows idle-sleeping (the screen may still turn off). Closing the lid still sleeps."""
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if awake else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


# --- Telegram -----------------------------------------------------------------


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, http_timeout=30, **params):
        req = urllib.request.Request(
            self.base + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                description = json.load(e).get("description", str(e))
            except ValueError:
                description = str(e)
            raise TelegramError(f"{method}: {description}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body["result"]

    def send(self, chat_id, text, **extra):
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            link_preview_options={"is_disabled": True},
            **extra,
        )


# --- Bot ----------------------------------------------------------------------


def load_state():
    state = json.loads(json.dumps(DEFAULT_STATE))
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


class GymBot:
    def __init__(self, tg, state):
        self.tg = tg
        self.state = state
        self.latest_gyms = []
        self.latest_at = None
        self.next_poll_at = 0.0

    # Capacity -----------------------------------------------------------------

    def refresh(self):
        gyms = fetch_capacities()
        self.latest_gyms = gyms
        self.latest_at = datetime.now(SGT)
        self.state["gym_names"] = {g["id"]: g["name"] for g in gyms}
        return gyms

    def poll(self):
        s = self.state
        now = datetime.now(SGT)
        active = bool(s["owner_chat_id"] and s["enabled"] and s["tracked"] and in_alert_hours(now, s["hours"]))
        keep_awake(active and on_ac_power())
        if not active:
            return

        try:
            gyms = self.refresh()
        except FetchError as e:
            s["fail_count"] += 1
            log.warning("Fetch failed (%d in a row): %s", s["fail_count"], e)
            if s["fail_count"] == FAIL_ALERT_AFTER:
                self.notify(f"⚠️ I can't read ActiveSG capacity ({e}). I'll keep trying and tell you when it's back.")
            return
        if s["fail_count"] >= FAIL_ALERT_AFTER:
            self.notify("✅ ActiveSG capacity is readable again.")
        s["fail_count"] = 0

        if s["last_poll_at"] is None or now.timestamp() - s["last_poll_at"] > REARM_AFTER_GAP_MINUTES * 60:
            s["alerted"] = []
        s["last_poll_at"] = now.timestamp()

        to_alert, alerted = check_thresholds(gyms, s["tracked"], s["threshold"], s["alerted"])
        s["alerted"] = sorted(alerted)
        tracked_pcts = {g["id"]: g["capacityPercentage"] for g in gyms if g["id"] in s["tracked"]}
        log.info("Polled: %s", ", ".join(f"{short_name(s['gym_names'].get(i, i))}={p}%" for i, p in tracked_pcts.items()))
        if to_alert:
            self.notify(format_alert(to_alert, s["threshold"]))

    def poll_soon(self):
        self.next_poll_at = 0.0

    # Telegram ----------------------------------------------------------------------

    def notify(self, text):
        if self.state["owner_chat_id"] is None:
            return
        try:
            self.tg.send(self.state["owner_chat_id"], text)
        except (TelegramError, OSError) as e:
            log.error("Couldn't send Telegram message: %s", e)

    def handle_update(self, update):
        if "message" in update:
            msg = update["message"]
            self.handle_message(msg["chat"]["id"], (msg.get("text") or "").strip())
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, chat_id, text):
        s = self.state
        if s["owner_chat_id"] is None:
            if text.startswith("/start"):
                s["owner_chat_id"] = chat_id
                log.info("Owner set to chat %s", chat_id)
                self.tg.send(chat_id, "Hi! This bot now belongs to you.\n\n" + HELP_TEXT)
            return
        if chat_id != s["owner_chat_id"]:
            log.warning("Ignoring message from unknown chat %s", chat_id)
            return

        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()
        arg = arg.strip()

        if command in ("/start", "/help"):
            self.tg.send(chat_id, HELP_TEXT)
        elif command == "/on":
            s["enabled"] = True
            s["alerted"] = []
            self.tg.send(chat_id, f"🔔 Alerts on. {self.settings_summary()}")
            self.poll_soon()
        elif command == "/off":
            s["enabled"] = False
            self.tg.send(chat_id, "🔕 Alerts off. Send /on to turn them back on.")
        elif command == "/threshold":
            self.cmd_threshold(chat_id, arg)
        elif command == "/hours":
            self.cmd_hours(chat_id, arg)
        elif command == "/gyms":
            self.cmd_gyms(chat_id)
        elif command == "/status":
            self.cmd_status(chat_id)
        else:
            self.tg.send(chat_id, "I didn't get that.\n\n" + HELP_TEXT)

    def cmd_threshold(self, chat_id, arg):
        s = self.state
        try:
            value = int(arg.rstrip("%"))
            if not 1 <= value <= 100:
                raise ValueError
        except ValueError:
            self.tg.send(chat_id, f"Your threshold is {s['threshold']}%. To change it, send e.g. /threshold 30")
            return
        s["threshold"] = value
        s["alerted"] = []
        self.tg.send(chat_id, f"👍 I'll alert you when a tracked gym is at or below {value}%.")
        self.poll_soon()

    def cmd_hours(self, chat_id, arg):
        s = self.state
        parts = arg.split()
        try:
            if len(parts) == 1:
                s["hours"]["weekday"] = s["hours"]["weekend"] = parse_range(parts[0])
            elif len(parts) == 2 and parts[0].lower() in ("weekday", "weekend"):
                s["hours"][parts[0].lower()] = parse_range(parts[1])
            elif parts:
                raise ValueError(arg)
        except ValueError:
            self.tg.send(chat_id, "I couldn't read that. Try /hours 07:00-22:00 or /hours weekday 18:00-22:00")
            return
        prefix = "👍 Updated. " if parts else ""
        self.tg.send(chat_id, prefix + self.hours_summary())
        self.poll_soon()

    def cmd_gyms(self, chat_id):
        try:
            self.refresh()
        except FetchError as e:
            log.warning("Fetch for /gyms failed: %s", e)
            if not self.state["gym_names"]:
                self.tg.send(chat_id, f"⚠️ Couldn't read ActiveSG right now ({e}). Try again in a bit.")
                return
        self.tg.send(
            chat_id,
            "Tap a gym to track or untrack it (✅ = tracked).",
            reply_markup=self.gyms_keyboard(),
        )

    def cmd_status(self, chat_id):
        s = self.state
        try:
            self.refresh()
            pcts = {g["id"]: g for g in self.latest_gyms}
        except FetchError as e:
            log.warning("Fetch for /status failed: %s", e)
            pcts = {}
        lines = [
            f"Alerts: {'🔔 on' if s['enabled'] else '🔕 off'}",
            f"Threshold: at or below {s['threshold']}%",
            self.hours_summary(),
            "",
        ]
        if s["tracked"]:
            lines.append("Tracked gyms:")
            for gym_id in s["tracked"]:
                gym = pcts.get(gym_id)
                name = short_name(s["gym_names"].get(gym_id, gym_id))
                if gym is None:
                    reading = "?"
                elif gym.get("isClosed"):
                    reading = "closed"
                else:
                    reading = f"{gym['capacityPercentage']}%"
                lines.append(f"• {name}: {reading}")
            if not pcts:
                lines.append("(couldn't read live capacity just now)")
        else:
            lines.append("You're not tracking any gyms yet. Use /gyms to pick some.")
        self.tg.send(chat_id, "\n".join(lines))

    def handle_callback(self, cq):
        s = self.state
        chat_id = cq["message"]["chat"]["id"] if "message" in cq else None
        if chat_id != s["owner_chat_id"] or not cq.get("data", "").startswith("t:"):
            self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            return
        gym_id = cq["data"][2:]
        name = short_name(s["gym_names"].get(gym_id, gym_id))
        if gym_id in s["tracked"]:
            s["tracked"].remove(gym_id)
            if gym_id in s["alerted"]:
                s["alerted"].remove(gym_id)
            toast = f"Stopped tracking {name}"
        else:
            s["tracked"].append(gym_id)
            toast = f"Tracking {name}"
            self.poll_soon()
        self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
        try:
            self.tg.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=cq["message"]["message_id"],
                reply_markup=self.gyms_keyboard(),
            )
        except TelegramError as e:
            log.warning("Couldn't refresh gym buttons: %s", e)

    def gyms_keyboard(self):
        s = self.state
        pcts = {g["id"]: g for g in self.latest_gyms}
        buttons = []
        for gym_id, full_name in sorted(s["gym_names"].items(), key=lambda kv: short_name(kv[1])):
            label = short_name(full_name)
            gym = pcts.get(gym_id)
            if gym is not None:
                label += " · closed" if gym.get("isClosed") else f" · {gym['capacityPercentage']}%"
            if gym_id in s["tracked"]:
                label = "✅ " + label
            buttons.append({"text": label, "callback_data": f"t:{gym_id}"})
        return {"inline_keyboard": [buttons[i : i + 2] for i in range(0, len(buttons), 2)]}

    def hours_summary(self):
        wd, we = self.state["hours"]["weekday"], self.state["hours"]["weekend"]
        if wd == we:
            return f"Alert hours: every day {wd[0]}-{wd[1]}"
        return f"Alert hours: weekdays {wd[0]}-{wd[1]}, weekends {we[0]}-{we[1]}"

    def settings_summary(self):
        n = len(self.state["tracked"])
        gyms = f"{n} gym{'s' if n != 1 else ''}" if n else "no gyms yet (use /gyms)"
        return f"Tracking {gyms} at or below {self.state['threshold']}%. {self.hours_summary()}."

    # Main loop ---------------------------------------------------------------

    def run(self):
        while True:
            try:
                me = self.tg.call("getMe")
                break
            except TelegramError as e:
                sys.exit(f"Telegram rejected the bot token ({e}). Check TELEGRAM_BOT_TOKEN in {ENV_FILE}.")
            except OSError as e:
                log.warning("Can't reach Telegram yet (%s); retrying in 30s.", e)
                time.sleep(30)
        log.info("Running as @%s", me["username"])
        try:
            self.tg.call("setMyCommands", commands=BOT_COMMANDS)
        except TelegramError as e:
            log.warning("Couldn't set command menu: %s", e)
        if self.state["owner_chat_id"] is None:
            log.info("Open https://t.me/%s and send /start to claim this bot.", me["username"])

        while True:
            wait = max(0, min(LONG_POLL_SECONDS, int(self.next_poll_at - time.time())))
            try:
                updates = self.tg.call(
                    "getUpdates",
                    http_timeout=wait + 15,
                    offset=self.state["update_offset"],
                    timeout=wait,
                    allowed_updates=["message", "callback_query"],
                )
            except (TelegramError, OSError) as e:
                log.warning("Telegram getUpdates failed: %s", e)
                updates = []
                time.sleep(5)
            for update in updates:
                self.state["update_offset"] = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except (TelegramError, OSError) as e:
                    log.error("Failed handling update %s: %s", update["update_id"], e)
            if time.time() >= self.next_poll_at:
                self.next_poll_at = time.time() + POLL_MINUTES * 60
                self.poll()
            save_state(self.state)


# --- Entry point ----------------------------------------------------------------


def load_token():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        hint = " The file is empty on disk. Did you save it?" if ENV_FILE.exists() and not ENV_FILE.stat().st_size else ""
        sys.exit(f"Missing TELEGRAM_BOT_TOKEN. Put it in {ENV_FILE} (see .env.example).{hint}")
    return token


def setup_logging():
    handlers = [logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stdout is not None:  # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def check():
    gyms = fetch_capacities()
    for g in sorted(gyms, key=lambda g: g["name"]):
        status = "closed" if g.get("isClosed") else f"{g['capacityPercentage']:>3}%"
        print(f"{status:>7}  {g['name']}")


def main():
    if "--check" in sys.argv:
        check()
        return
    setup_logging()
    lock = open(LOCK_FILE, "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit("gymbot is already running.")
    bot = GymBot(Telegram(load_token()), load_state())
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        keep_awake(False)
        save_state(bot.state)


if __name__ == "__main__":
    main()
