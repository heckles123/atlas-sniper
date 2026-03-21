"""
AtlasFinder Slot Sniper
========================
Monitors slot availability and purchases the instant a slot opens.

Plans:
  UNIVERSAL        — 1 slot,  $30/h, min 1h
  SWAN             — 3 slots, $15/h, min 1h
  ATLAS            — 6 slots, $12/h, min 6h
  UNIVERSAL FARMER — 6 slots, $6/h,  min 4h
  ATLAS AUCTION    — 2 slots, bid-based

Railway environment variables:
  TOKEN            — JWT from localStorage.getItem("token") on atlasfinder.org
  TARGET_PLAN      — ATLAS, SWAN, UNIVERSAL, UNIVERSAL FARMER, ATLAS AUCTION
  HOURS            — hours to purchase (must meet plan minimum)
  AUCTION_BID      — starting bid for auction mode (default: 12.0)
  AUCTION_SLOT     — slot number to target in auction, 0 = first available
  DISCORD_WEBHOOK  — optional Discord webhook URL
  DRY_RUN          — true/false (default: false)

USAGE:
    python atlasfinder_sniper.py
"""

import asyncio
import aiohttp
import json
import sys
import os
import time
import threading
from datetime import datetime, timezone

# ══════════════════════════════════════════════════════════════
#  ★  CONFIG — all from environment variables  ★
# ══════════════════════════════════════════════════════════════

TOKEN           = os.environ.get("TOKEN", "")
TARGET_PLAN     = os.environ.get("TARGET_PLAN", "ATLAS")
HOURS           = int(os.environ.get("HOURS", "6"))
AUCTION_BID     = float(os.environ.get("AUCTION_BID", "12.0"))
AUCTION_SLOT    = int(os.environ.get("AUCTION_SLOT", "0"))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
DRY_RUN         = os.environ.get("DRY_RUN", "false").lower() == "true"
BOT_ID          = os.environ.get("BOT_ID", "atlas-sniper")
INGEST_URL      = os.environ.get("INGEST_URL", "")

# ══════════════════════════════════════════════════════════════
#  API config
# ══════════════════════════════════════════════════════════════
BASE_URL           = "https://atlasfinder.org/app-api"
TURNSTILE_SITE_URL = "https://atlasfinder.org/purchase"
TURNSTILE_SITEKEY  = "0x4AAAAAACG-iFJo6qcy3NlA"

TURNSTILE_SOLVER_URL = os.environ.get("TURNSTILE_SOLVER_URL", "http://localhost:5000/turnstile")
TURNSTILE_RESULT_URL = os.environ.get("TURNSTILE_RESULT_URL", "http://localhost:5000/result")

# Plan metadata (from API probe)
PLAN_META = {
    "UNIVERSAL":        {"id": "b4cab330-dad2-4f31-a07f-47e90c611c17", "minHours": 1,  "costPerHour": 30, "type": "purchase"},
    "SWAN":             {"id": "72638660-0b34-46b3-a1da-6a08385206b1", "minHours": 1,  "costPerHour": 15, "type": "purchase"},
    "ATLAS":            {"id": "89cc0c54-0f70-4db9-835f-5c37e5503968", "minHours": 6,  "costPerHour": 12, "type": "purchase"},
    "UNIVERSAL FARMER": {"id": "e43458a9-88bf-4679-8617-78c0a2809a1a", "minHours": 4,  "costPerHour": 6,  "type": "purchase"},
    "ATLAS AUCTION":    {"id": "7117c8fa-16c4-4597-baf1-fb28ca21104c", "minHours": 2,  "costPerHour": 0,  "type": "auction"},
}

# ══════════════════════════════════════════════════════════════
#  Colorama
# ══════════════════════════════════════════════════════════════
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    class Fore:
        RED = GREEN = YELLOW = CYAN = MAGENTA = WHITE = ""
    class Style:
        RESET_ALL = ""

# ══════════════════════════════════════════════════════════════
#  Ingest — fire-and-forget reporting to dashboard
# ══════════════════════════════════════════════════════════════
def _ingest_fire(payload: dict):
    """Send payload to ingest endpoint in background thread."""
    if not INGEST_URL:
        return
    import urllib.request
    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            INGEST_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def ingest_log(msg: str, level: str = "info"):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    threading.Thread(target=_ingest_fire, args=({
        "type":   "log",
        "bot_id": BOT_ID,
        "line":   line,
        "level":  level,
    },), daemon=True).start()

def ingest_status(plans: list, check_count: int, avg_ms: float):
    threading.Thread(target=_ingest_fire, args=({
        "type":        "status",
        "bot_id":      BOT_ID,
        "plans":       plans,
        "check_count": check_count,
        "avg_ms":      avg_ms,
    },), daemon=True).start()

def ingest_purchase(plan_name: str, result: str, resp_ms: float, bid_amount: float = 0):
    threading.Thread(target=_ingest_fire, args=({
        "type":       "purchase",
        "bot_id":     BOT_ID,
        "plan_name":  plan_name,
        "result":     result,
        "resp_ms":    resp_ms,
        "bid_amount": bid_amount,
    },), daemon=True).start()

def ingest_heartbeat():
    threading.Thread(target=_ingest_fire, args=({
        "type":   "heartbeat",
        "bot_id": BOT_ID,
    },), daemon=True).start()


ALARM_PATH = os.path.normpath(r"C:/Users/ebeze/Downloads/alarmemail/alarm.wav")

def play_alarm():
    if not os.path.exists(ALARM_PATH):
        return
    try:
        import winsound
        def _loop():
            for _ in range(10):
                try:
                    winsound.PlaySound(ALARM_PATH, winsound.SND_FILENAME)
                except Exception:
                    break
        threading.Thread(target=_loop, daemon=True).start()
    except ImportError:
        pass

# ══════════════════════════════════════════════════════════════
#  Logging
# ══════════════════════════════════════════════════════════════
def log(msg, color=Fore.WHITE, level="info"):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{Fore.CYAN}[{ts}]{Style.RESET_ALL} {color}{msg}{Style.RESET_ALL}", flush=True)
    ingest_log(msg, level)

# ══════════════════════════════════════════════════════════════
#  HTTP helpers
# ══════════════════════════════════════════════════════════════
def api_headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin":        "https://atlasfinder.org",
        "Referer":       "https://atlasfinder.org/purchase",
    }

async def api_get(session, path):
    try:
        async with session.get(
            BASE_URL + path, headers=api_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            try:
                return await r.json(content_type=None), r.status
            except Exception:
                return await r.text(), r.status
    except Exception as e:
        return {"error": str(e)}, None

async def api_post(session, path, body):
    try:
        async with session.post(
            BASE_URL + path, json=body, headers=api_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            try:
                return await r.json(content_type=None), r.status
            except Exception:
                return await r.text(), r.status
    except Exception as e:
        return {"error": str(e)}, None

# ══════════════════════════════════════════════════════════════
#  Discord
# ══════════════════════════════════════════════════════════════
BOT_START_TIME = datetime.now(timezone.utc)
status_msg_id  = None
_wh_parts = DISCORD_WEBHOOK.rstrip("/").split("/") if DISCORD_WEBHOOK else []
_WH_ID    = _wh_parts[-2] if len(_wh_parts) >= 2 else ""
_WH_TOKEN = _wh_parts[-1] if len(_wh_parts) >= 2 else ""
EDIT_URL  = f"https://discord.com/api/webhooks/{_WH_ID}/{_WH_TOKEN}"

def _utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _uptime():
    d = datetime.now(timezone.utc) - BOT_START_TIME
    h, m = int(d.total_seconds()//3600), int((d.total_seconds()%3600)//60)
    return f"{h}h {m}m"

async def discord_post_msg(session, payload, patch_id=None):
    if not DISCORD_WEBHOOK:
        return None
    try:
        if patch_id:
            async with session.patch(f"{EDIT_URL}/messages/{patch_id}", json=payload,
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                return None
        else:
            async with session.post(f"{DISCORD_WEBHOOK}?wait=true", json=payload,
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status in (200, 204):
                    d = await r.json(content_type=None)
                    return str(d.get("id", ""))
    except Exception:
        pass
    return None

async def discord_alert(session, title, description, color, fields=None):
    if not DISCORD_WEBHOOK:
        return
    embed = {"title": title, "description": description, "color": color,
             "timestamp": _utc_iso(), "footer": {"text": "AtlasFinder Sniper"}}
    if fields:
        embed["fields"] = fields
    try:
        async with session.post(DISCORD_WEBHOOK, json={"embeds": [embed]},
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
            pass
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════
#  Turnstile solver (async, task-based)
# ══════════════════════════════════════════════════════════════
async def get_turnstile_token(session: aiohttp.ClientSession) -> str | None:
    log("  Solving Turnstile…", Fore.CYAN)
    t0 = time.monotonic()

    try:
        # Submit task
        async with session.get(
            TURNSTILE_SOLVER_URL,
            params={"url": TURNSTILE_SITE_URL, "sitekey": TURNSTILE_SITEKEY},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                log(f"  ❌ Solver submit failed: {r.status}", Fore.RED)
                return None
            data = await r.json(content_type=None)
            task_id = data.get("task_id")
            if not task_id:
                log(f"  ❌ No task_id: {data}", Fore.RED)
                return None

        # Poll for result
        for i in range(30):  # up to 15 seconds
            await asyncio.sleep(0.5)
            try:
                async with session.get(
                    TURNSTILE_RESULT_URL,
                    params={"id": task_id},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status == 200:
                        result = await r.json(content_type=None)
                        value = result.get("value", "")
                        if value and value != "CAPTCHA_FAIL" and len(value) > 20:
                            elapsed = (time.monotonic() - t0) * 1000
                            log(f"  ✓ Token solved in {elapsed:.0f}ms", Fore.GREEN)
                            return value
                        if value == "CAPTCHA_FAIL":
                            log("  ❌ Solver returned CAPTCHA_FAIL — Cloudflare blocked headless browser", Fore.RED)
                            return None
            except Exception:
                pass

        log("  ❌ Turnstile solve timed out", Fore.RED)
        return None

    except aiohttp.ClientConnectorError:
        log("  ❌ Solver not running! Run: python api_solver.py in Turnstile-Solver folder", Fore.RED)
        return None
    except Exception as e:
        log(f"  ❌ Solver error: {e}", Fore.RED)
        return None

# ══════════════════════════════════════════════════════════════
#  Slot fetching
# ══════════════════════════════════════════════════════════════
async def fetch_plans(session) -> list:
    data, status = await api_get(session, "/plans")
    if status != 200 or not isinstance(data, list):
        return []
    return data

# ══════════════════════════════════════════════════════════════
#  Auction slot fetching
# ══════════════════════════════════════════════════════════════
async def fetch_auction_slots(session, bidding_plan_id: str) -> list:
    data, status = await api_get(session, "/bidding-plans")
    if status != 200 or not isinstance(data, dict):
        return []
    for plan in data.get("biddingPlans", []):
        if plan.get("id") == bidding_plan_id:
            return plan.get("slots", [])
    return []

# ══════════════════════════════════════════════════════════════
#  Place bid (no Turnstile needed)
# ══════════════════════════════════════════════════════════════
async def do_bid(session, plan_id: str, slot_number: int, bid_amount: float) -> bool:
    if DRY_RUN:
        log(f"DRY RUN — skipping bid of ${bid_amount} on slot #{slot_number}.", Fore.MAGENTA)
        return True

    log(f"→ Bidding ${bid_amount} on slot #{slot_number}…", Fore.CYAN)
    data, status = await api_post(
        session,
        f"/bidding-plans/{plan_id}/slot/{slot_number}/bid",
        {"bidAmount": bid_amount},
    )
    log(f"← {status}: {data}", Fore.WHITE)

    if status in (200, 201) and isinstance(data, dict) and not data.get("error"):
        log(f"✅  Bid placed successfully! ${bid_amount} on slot #{slot_number}", Fore.GREEN)
        play_alarm()
        return True

    error = str((data or {}).get("error") or "").lower() if isinstance(data, dict) else str(data).lower()

    if "insufficient" in error or "balance" in error:
        log("❌  Not enough balance.", Fore.RED)
        sys.exit(1)
    if "outbid" in error or "higher" in error:
        log("⚠️  Already outbid — bid higher?", Fore.YELLOW)
        return False
    if status == 401:
        log("❌  Token expired.", Fore.RED)
        sys.exit(1)
    if "not available" in error or "bidding" in error:
        log("⚠️  Slot not open for bidding yet.", Fore.YELLOW)
        return False

    log(f"⚠️  {data}", Fore.YELLOW)
    return False


async def do_purchase(session, plan_id: str, hours: int) -> bool:
    if DRY_RUN:
        log("DRY RUN — skipping purchase.", Fore.MAGENTA)
        return True

    token = await get_turnstile_token(session)
    if not token:
        log("  Could not get Turnstile token", Fore.RED)
        return False

    data, status = await api_post(session, f"/plans/{plan_id}/purchase", {
        "hours":   hours,
        "cfToken": token,
    })
    log(f"← {status}: {data}", Fore.WHITE)

    if status in (200, 201) and isinstance(data, dict) and not data.get("error"):
        log("✅  Purchase confirmed!", Fore.GREEN)
        play_alarm()
        return True

    error = str((data or {}).get("error") or (data or {}).get("message") or "").lower() if isinstance(data, dict) else str(data).lower()

    if "insufficient" in error or "balance" in error:
        log("❌  Not enough balance.", Fore.RED)
        sys.exit(1)
    if "captcha" in error or "token" in error:
        log("⚠️  Captcha failed — retrying with fresh token", Fore.YELLOW)
        return False
    if status == 401:
        log("❌  Token expired — get a fresh token from localStorage.", Fore.RED)
        sys.exit(1)
    if "already" in error or "active" in error:
        log("⚠️  Already subscribed to this plan.", Fore.YELLOW)
        return False
    if "slot" in error and "full" in error:
        log("⚠️  Slot was grabbed before purchase — resuming", Fore.YELLOW)
        return False

    log(f"⚠️  {data}", Fore.YELLOW)
    return False

# ══════════════════════════════════════════════════════════════
#  State tracker
# ══════════════════════════════════════════════════════════════
class PlanState:
    def __init__(self):
        self.last_free: dict[str, int] = {}
        self.initialized = False

    def check(self, plan_name: str, free: int) -> str | None:
        prev = self.last_free.get(plan_name, -1)
        self.last_free[plan_name] = free
        if not self.initialized:
            return None
        if free > 0 and prev <= 0:
            return f"slot opened — {free} free"
        if free > 0:
            return f"freeSlots={free}"
        return None

    def mark_initialized(self):
        self.initialized = True

# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════
async def main():
    if TOKEN == "PASTE_TOKEN_HERE":
        print(f"{Fore.RED}❌  Set TOKEN at the top of the script.")
        print("    Get it from: atlasfinder.org → F12 → Console → localStorage.getItem('token'){Style.RESET_ALL}")
        sys.exit(1)

    if not TOKEN:
        print(f"{Fore.RED}❌  TOKEN environment variable not set.{Style.RESET_ALL}")
        sys.exit(1)

    plan_info = PLAN_META.get(TARGET_PLAN)
    if not plan_info:
        print(f"{Fore.RED}❌  Unknown plan '{TARGET_PLAN}'. Choose from: {list(PLAN_META.keys())}{Style.RESET_ALL}")
        sys.exit(1)

    if plan_info.get("type") != "auction" and HOURS < plan_info["minHours"]:
        print(f"{Fore.RED}❌  Minimum hours for {TARGET_PLAN} is {plan_info['minHours']}.{Style.RESET_ALL}")
        sys.exit(1)

    is_auction = plan_info.get("type") == "auction"

    print(f"\n{Fore.MAGENTA}{'═'*57}")
    print(f"  🔔  AtlasFinder Slot Sniper")
    print(f"{'═'*57}{Style.RESET_ALL}\n")
    if is_auction:
        log(f"Target: {TARGET_PLAN} (AUCTION)  |  Bid: ${AUCTION_BID}  |  Dry run: {DRY_RUN}", Fore.CYAN)
    else:
        log(f"Target: {TARGET_PLAN}  |  Hours: {HOURS}  |  Cost: ${HOURS * plan_info['costPerHour']:.2f}", Fore.CYAN)
        log(f"Dry run: {DRY_RUN}", Fore.CYAN)
    print()

    state         = PlanState()
    attempts      = 0
    purchased     = False
    last_ms       = 0.0
    ms_samples    = []
    global status_msg_id

    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as http:

        # Verify token
        log("Verifying token…", Fore.YELLOW, "info")
        profile, status = await api_get(http, "/user/profile")
        if status != 200:
            log(f"❌  Invalid token (status {status}).", Fore.RED, "error")
            sys.exit(1)
        balance  = profile.get("balance", 0) if isinstance(profile, dict) else 0
        username = profile.get("username", "?") if isinstance(profile, dict) else "?"
        log(f"Logged in as: {username} | Balance: ${balance:.2f} ✓", Fore.GREEN, "success")
        print()
        log("Monitoring…  Ctrl+C to stop\n", Fore.GREEN, "info")

        # Send initial heartbeat immediately on startup
        ingest_heartbeat()

        while not purchased:
            attempts += 1
            t_start = time.monotonic()

            # Heartbeat every 10 checks
            if attempts % 10 == 0:
                ingest_heartbeat()

            # ── AUCTION MODE ─────────────────────────────────
            if is_auction:
                slots = await fetch_auction_slots(http, plan_info["id"])
                last_ms = (time.monotonic() - t_start) * 1000
                ms_samples.append(last_ms)
                if len(ms_samples) > 50:
                    ms_samples.pop(0)

                if not slots:
                    log(f"#{attempts} [{last_ms:.0f}ms]  ⚠ Could not fetch auction slots", Fore.YELLOW)
                    await asyncio.sleep(1)
                    continue

                parts = []
                trigger_slot = None
                for slot in slots:
                    num     = slot.get("slotNumber", "?")
                    status  = slot.get("status", "?")
                    can_bid = slot.get("canBid", False)
                    eta     = slot.get("timeUntilAvailable", 0)
                    min_bid = slot.get("minimumNextBid", AUCTION_BID)
                    occupied_by = slot.get("occupiedBy", "?")

                    if can_bid and (AUCTION_SLOT == 0 or AUCTION_SLOT == num):
                        col = Fore.GREEN
                        trigger_slot = slot
                    else:
                        col = Fore.RED

                    h, rem = divmod(eta, 3600)
                    m, s   = divmod(rem, 60)
                    timer  = f" ⏰{h:02d}:{m:02d}" if h else f" ⏰{m:02d}:{s:02d}" if eta > 0 else " 🟢OPEN"
                    parts.append(f"{col}Slot#{num}({occupied_by[:8]}){timer}${min_bid}{Style.RESET_ALL}")

                log(f"#{attempts} [{last_ms:.0f}ms]  ATLAS AUCTION — " + "   ".join(parts))

                # Ingest status every 3 checks — always include ALL plans
                if attempts % 3 == 0:
                    avg = sum(ms_samples) / len(ms_samples) if ms_samples else last_ms
                    slot_data = [{"name": f"Slot#{s.get('slotNumber')}", "free": 1 if s.get("canBid") else 0, "total": 1} for s in slots]
                    # Also fetch regular plans to include in full status
                    all_plans_data, _ = await api_get(http, "/plans")
                    combined = []
                    if isinstance(all_plans_data, list):
                        for p in all_plans_data:
                            combined.append({"name": p.get("name"), "free": p.get("maxSlots",0) - p.get("currentSlots",0), "total": p.get("maxSlots",0)})
                    # Free only if nobody is occupying the slot
                    auction_free = sum(1 for s in slots if not s.get("occupiedBy") and not s.get("occupiedByUserId"))
                    combined.append({"name": "ATLAS AUCTION", "total": len(slots), "free": auction_free})
                    ingest_status(combined, attempts, avg)

                if not state.initialized:
                    state.mark_initialized()
                    continue

                if trigger_slot:
                    slot_num = trigger_slot.get("slotNumber")
                    min_bid  = trigger_slot.get("minimumNextBid", AUCTION_BID)
                    bid_amt  = max(AUCTION_BID, min_bid)
                    log(f"\n🎉  [ATLAS AUCTION] Slot #{slot_num} open for bidding! Min bid: ${min_bid}\n", Fore.GREEN)
                    for _ in range(3):
                        print("\a", end="", flush=True)

                    asyncio.ensure_future(discord_alert(http,
                        title="🚨  Auction Slot Available!",
                        description=f"ATLAS AUCTION slot #{slot_num} is open for bidding!",
                        color=0xf59e0b,
                        fields=[{"name": "Min Bid",   "value": f"${min_bid}",      "inline": True},
                                {"name": "Your Bid",  "value": f"${bid_amt}",       "inline": True},
                                {"name": "Checks Run","value": f"{attempts:,}",     "inline": True}]
                    ))

                    success = False
                    for attempt_num in range(3):
                        result = await do_bid(http, plan_info["id"], slot_num, bid_amt)
                        if result:
                            success = True
                            break
                        await asyncio.sleep(0.3)

                    if success:
                        log(f"\n✅  Bid placed! Monitoring for outbid...\n", Fore.GREEN, "success")
                        ingest_purchase(TARGET_PLAN, "success", last_ms, bid_amt)
                        await discord_alert(http,
                            title="✅  Bid Placed!",
                            description=f"Bid of **${bid_amt}** placed on ATLAS AUCTION slot #{slot_num}! 🔔",
                            color=0x22c55e,
                        )
                        purchased = True
                    else:
                        log("Bid failed — resuming…\n", Fore.YELLOW, "warning")
                        ingest_purchase(TARGET_PLAN, "failed", last_ms, bid_amt)

            # ── PURCHASE MODE ─────────────────────────────────
            else:
                plans = await fetch_plans(http)
                last_ms = (time.monotonic() - t_start) * 1000

                if not plans:
                    log(f"#{attempts} [{last_ms:.0f}ms]  ⚠ Could not fetch plans", Fore.YELLOW)
                    await asyncio.sleep(1)
                    continue

                parts = []
                target_free = 0
                target_id   = plan_info["id"]

                for plan in plans:
                    name      = plan.get("name", "?")
                    max_slots = plan.get("maxSlots", 0)
                    cur_slots = plan.get("currentSlots", 0)
                    free      = max_slots - cur_slots
                    col       = Fore.GREEN if free > 0 else Fore.RED
                    parts.append(f"{col}{name}:{free}/{max_slots}{Style.RESET_ALL}")
                    if name == TARGET_PLAN:
                        target_free = free

                log(f"#{attempts} [{last_ms:.0f}ms]  " + "   ".join(parts))
                ms_samples.append(last_ms)
                if len(ms_samples) > 50:
                    ms_samples.pop(0)

                # Ingest status every 3 checks — always include ALL plans + auction
                if attempts % 3 == 0:
                    avg = sum(ms_samples) / len(ms_samples) if ms_samples else last_ms
                    plan_data = [{"name": p.get("name"), "free": p.get("maxSlots",0) - p.get("currentSlots",0), "total": p.get("maxSlots",0)} for p in plans]
                    # Also fetch auction slots
                    auction_slots, _ = await api_get(http, "/bidding-plans")
                    if isinstance(auction_slots, dict):
                        for bp in auction_slots.get("biddingPlans", []):
                            bp_slots = bp.get("slots", [])
                            # Free only if nobody is occupying the slot
                            auction_free = sum(1 for s in bp_slots if not s.get("occupiedBy") and not s.get("occupiedByUserId"))
                            plan_data.append({"name": bp.get("name", "ATLAS AUCTION"), "total": len(bp_slots), "free": auction_free})
                    ingest_status(plan_data, attempts, avg)

                # Discord status embed
                if DISCORD_WEBHOOK:
                    fields = []
                    for plan in plans:
                        name  = plan.get("name", "?")
                        mx    = plan.get("maxSlots", 0)
                        cur   = plan.get("currentSlots", 0)
                        free  = mx - cur
                        used  = mx - free
                        bar   = "█" * int((used/mx)*8 if mx else 0) + "░" * (8 - int((used/mx)*8 if mx else 0))
                        fields.append({
                            "name":   f"{'🎯 ' if name == TARGET_PLAN else ''}{name}",
                            "value":  f"`{bar}` {used}/{mx}\n{'🟢 **OPEN**' if free > 0 else '🔴 Full'}",
                            "inline": True,
                        })
                    embed = {"embeds": [{"title": "🔔 AtlasFinder — Live Slot Monitor",
                        "description": f"**Target:** `{TARGET_PLAN}`  •  **Checks:** `{attempts:,}`  •  **Speed:** `{last_ms:.0f}ms`\n**Uptime:** `{_uptime()}`  •  **Dry Run:** `{DRY_RUN}`",
                        "color": 0x22c55e if target_free > 0 else 0x3b82f6,
                        "fields": fields, "footer": {"text": "Last updated"}, "timestamp": _utc_iso()}]}
                    if status_msg_id is None:
                        status_msg_id = await discord_post_msg(http, embed)
                        if status_msg_id:
                            log(f"Discord status message created (id: {status_msg_id})", Fore.CYAN)
                    elif attempts % 10 == 0:
                        asyncio.ensure_future(discord_post_msg(http, embed, status_msg_id))

                if not state.initialized:
                    for plan in plans:
                        name = plan.get("name", "?")
                        free = plan.get("maxSlots", 0) - plan.get("currentSlots", 0)
                        state.last_free[name] = free
                    state.mark_initialized()
                    continue

                trigger = state.check(TARGET_PLAN, target_free)

                for plan in plans:
                    name = plan.get("name", "?")
                    if name != TARGET_PLAN:
                        free = plan.get("maxSlots", 0) - plan.get("currentSlots", 0)
                        state.last_free[name] = free

                if trigger:
                    log(f"\n🎉  [{TARGET_PLAN}] TRIGGERED — {trigger}\n", Fore.GREEN, "trigger")
                    for _ in range(3):
                        print("\a", end="", flush=True)

                    asyncio.ensure_future(discord_alert(http,
                        title=f"🚨  Slot Triggered — {TARGET_PLAN}",
                        description=f"A **{TARGET_PLAN}** slot just opened! Attempting purchase...",
                        color=0x22c55e,
                        fields=[{"name": "Trigger",    "value": trigger,         "inline": False},
                                {"name": "Checks Run", "value": f"{attempts:,}", "inline": True},
                                {"name": "Hours",      "value": str(HOURS),      "inline": True}]
                    ))

                    success = False
                    for attempt_num in range(3):
                        result = await do_purchase(http, target_id, HOURS)
                        if result:
                            success = True
                            break
                        if attempt_num < 2:
                            log(f"  Retrying ({attempt_num+2}/3)…", Fore.YELLOW)
                            await asyncio.sleep(0.5)

                    if success:
                        log(f"\n✅  Done after {attempts} checks!\n", Fore.GREEN, "success")
                        ingest_purchase(TARGET_PLAN, "success", last_ms)
                        await discord_alert(http,
                            title="✅  Purchase Successful!",
                            description=f"**{TARGET_PLAN}** purchased! 🔔",
                            color=0x22c55e,
                            fields=[{"name": "Plan",   "value": TARGET_PLAN,    "inline": True},
                                    {"name": "Hours",  "value": str(HOURS),     "inline": True},
                                    {"name": "Checks", "value": f"{attempts:,}","inline": True}]
                        )
                        purchased = True
                    else:
                        log("All attempts failed — resuming watch…\n", Fore.YELLOW, "warning")
                        ingest_purchase(TARGET_PLAN, "failed", last_ms)
                        await discord_alert(http, title="⚠️  Purchase Failed",
                            description="Slot detected but purchase failed. Resuming watch...",
                            color=0xf59e0b)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Stopped.{Style.RESET_ALL}")