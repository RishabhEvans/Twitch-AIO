#!/usr/bin/env python3
import os
import sys
import time
import re
import random
import threading
import tempfile
import shutil
import requests
from concurrent.futures import ThreadPoolExecutor
from patchright.sync_api import sync_playwright
from utils import (
    parse_proxy_for_pw, record_proxy_use, purge_proxy_file,
    StageTimer, ProxyScore, get_proxy_score, speed_color,
    ProxyPool, Logger, Progress, set_status_lines_func, fmt_elapsed,
    dynamic_timeout, check_proxy_speed, build_proxy_pool,
    emit_raw, ok, err, step, info, warn, spin_info,
    progress_printer, pause_exit, flush_stdin, score_report,
    RED, GREEN, PURPLE, CYAN, YELLOW, X, C_ORANGE,
)

if os.name == "nt":
    os.system("")

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(HERE, "results", "tokens.txt")
LOGFILE = os.path.join(HERE, "logs.txt")
TEXTS_DIR = os.path.join(HERE, "texts")
PROXIES_FILE = os.path.join(HERE, "results", "proxies.txt")

HEADLESS = os.getenv("HEADLESS", "0") == "1"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL")
if not BROWSER_CHANNEL:
    BROWSER_CHANNEL = "chrome" if os.name == "nt" else "chromium"

PROXY_SPEED_CHECK_TIMEOUT = float(os.getenv("PROXY_SPEED_TIMEOUT", "8.0"))
PROXY_NAV_TIMEOUT_FLOOR = 15000
PROXY_NAV_TIMEOUT_CEILING = 90000
PROXY_QUARANTINE_COOLDOWN = 300

FAST_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-field-trial-config",
    "--disable-hang-monitor",
    "--disable-component-update",
    "--disable-sync",
    "--disable-extensions",
    "--mute-audio",
    "--metrics-recording-only",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-logging",
    "--disable-breakpad",
    "--disable-infobars",
    "--disable-features=AutomationControlled,Translate,OptimizationHints,MediaRouter,IsolateOrigins,site-per-process",
]

proxy_pool = None  # set in main()


def load_tokens():
    try:
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


def load_messages(filename):
    path = os.path.join(TEXTS_DIR, filename)
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        return lines
    except Exception:
        return []


class ChatProgress:
    """Chat-specific progress tracking (shared with UI via set_status_lines_func)."""
    sent = 0
    start = 0.0
    session_duration = 0.0
    session_end = 0.0
    done_event = threading.Event()
    chat_started = threading.Event()
    messages_filename = ""
    lock = threading.Lock()

    @classmethod
    def inc(cls):
        with cls.lock:
            if not cls.chat_started.is_set():
                cls.chat_started.set()
                cls.start = time.time()
                cls.session_end = cls.start + cls.session_duration
            cls.sent += 1

    @classmethod
    def snapshot(cls):
        with cls.lock:
            return cls.sent, cls.session_duration


def fmt_time_remaining(remaining):
    r = max(0, int(remaining))
    m = r // 60
    s = r % 60
    return f"{m}:{s:02d}"


def _chat_status_lines():
    sent, duration = ChatProgress.snapshot()
    elapsed = time.time() - ChatProgress.start if ChatProgress.chat_started.is_set() else 0
    remaining = max(0, duration - elapsed)
    spm = elapsed / sent if sent > 0 else 0.0
    return [
        f"---------- {sent} messages sent ----------",
        f"---------- time remaining: {fmt_time_remaining(remaining)} ----------",
        f"---------- average: {spm:.2f} secs/message ----------",
    ]


_ALLOW = ("twitch.tv", "twitchcdn.net", "ttvnw.net", "jtvnw.net",
          "passport.twitch.tv", "gql.twitch.tv", "k.twitchcdn.net",
          "static.twitchcdn.net", "assets.twitch.tv")


def _route(route):
    try:
        r = route.request
        if r.resource_type in ("image", "media", "font"):
            return route.abort()
        url = r.url
        if any(t in url for t in (".m3u8", ".ts", ".mp4", "usher.ttvnw.net", "video-weaver", "jtvnw.net/v1",
                                   "scorecardresearch", "amazon-adsystem", "google-analytics", "sentry",
                                   "gql.twitch.tv/spade", "doubleclick", "googletagmanager", "facebook.com/tr",
                                   ".jpg", ".png", ".gif", ".webp", ".avif", "static-cdn.jtvnw.net")):
            return route.abort()
        if not any(d in url for d in _ALLOW):
            return route.abort()
        return route.continue_()
    except Exception:
        try:
            return route.continue_()
        except Exception:
            return


def chat_one(slot, token, channel):
    tag = f"#{slot}"
    short = token[:8]
    attempt = 1
    messages = load_messages(ChatProgress.messages_filename)
    if not messages:
        err(f"{tag} no messages loaded, exiting")
        return

    while True:
        if attempt > 15:
            Logger.write(f"{tag}: max retries (15) exhausted, giving up")
            err(f"{tag} failed after 15 attempts")
            return
        attempt_tag = f"{tag} (attempt {attempt})" if attempt > 1 else tag
        try:
            user_dir = tempfile.mkdtemp(prefix="pwc_")
        except Exception:
            Logger.write(f"{attempt_tag}: FATAL - tempfile.mkdtemp failed, retrying in 2s")
            time.sleep(2.0)
            attempt += 1
            continue
        p_line = proxy_pool.acquire()
        if not p_line:
            Logger.write(f"{attempt_tag}: no proxy available, waiting...")
            shutil.rmtree(user_dir, ignore_errors=True)
            time.sleep(1)
            attempt += 1
            continue
        proxy_score = get_proxy_score(p_line)
        proxy_ok, speed = check_proxy_speed(p_line, timeout=PROXY_SPEED_CHECK_TIMEOUT)
        if proxy_ok:
            ms = speed * 1000
            proxy_score.nav_ms = (proxy_score.nav_ms or 0) or ms
        else:
            ms = 2000
            proxy_score.timeout += 1
            proxy_pool.quarantine(p_line)
        col = speed_color(ms)
        gotoms = dynamic_timeout(ms, multiplier=10, floor=PROXY_NAV_TIMEOUT_FLOOR, ceiling=PROXY_NAV_TIMEOUT_CEILING)
        info(f"{attempt_tag} new proxy = {col}{ms:.0f}ms{X} | token={short}..")
        lk = dict(
            user_data_dir=user_dir,
            headless=HEADLESS,
            no_viewport=True,
            args=list(FAST_ARGS),
            chromium_sandbox=True,
            ignore_default_args=["--enable-automation"],
        )
        if BROWSER_CHANNEL:
            lk["channel"] = BROWSER_CHANNEL
        if p_line:
            lk["proxy"] = parse_proxy_for_pw(p_line)
            Logger.write(f"{attempt_tag}: proxy={p_line[:50]}")

        page = None
        ctx = None
        try:
            p = sync_playwright().start()
            ctx = p.chromium.launch_persistent_context(**lk)
            ctx.route("**/*", _route)
            ctx.add_cookies([
                {"name": "auth-token", "value": token, "domain": ".twitch.tv", "path": "/",
                 "secure": True, "httpOnly": False, "sameSite": "None"}
            ])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            console_severe = []
            console_connections = []

            def _handle_console(msg):
                if "irc-ws" in msg.text:
                    return
                Logger.write(f"CONSOLE {attempt_tag}: [{msg.type}] {msg.text}")
                if any(kw in msg.text.lower() for kw in ("suspicious", "kasada", "ip flagged", "access denied", "try again later")):
                    console_severe.append(True)
                if any(kw in msg.text.lower() for kw in ("err_timed_out", "err_tunnel_connection_failed", "err_proxy_connection_failed", "err_connection_closed")):
                    proxy_score.console_err += 1
                    console_connections.append(True)
                if "slow network" in msg.text.lower() and "font" not in msg.text.lower():
                    proxy_score.slow_network += 1
            page.on("console", _handle_console)

            step(f"{attempt_tag} loading https://www.twitch.tv/{channel}...")
            page.goto(f"https://www.twitch.tv/{channel}", wait_until="domcontentloaded", timeout=gotoms)
            Logger.write(f"{attempt_tag}: step_load | page ready")
            page.wait_for_timeout(random.randint(2000, 4000))

            try:
                body_text = (page.locator("body").inner_text() or "").lower()
                if "verified accounts only chat" in body_text:
                    err(f"{attempt_tag} {short}.. account not verified - skipping")
                    attempt += 1
                    continue
                if "followers-only" in body_text or "subscribers only" in body_text:
                    err(f"{attempt_tag} {short}.. chat access restricted - skipping")
                    attempt += 1
                    continue
            except Exception:
                pass

            chat_input = None
            expand_clicked = False
            poll_elapsed = 0
            poll_gap = 500
            found = False
            while poll_elapsed < gotoms:
                page.wait_for_timeout(poll_gap)
                poll_elapsed += poll_gap
                chat_input = page.locator('div[data-a-target="chat-input"]').first
                if chat_input.count() and chat_input.is_visible():
                    found = True
                    break
                try:
                    expand = page.locator('button[data-a-target="right-column__toggle-collapse-btn"]').first
                    if expand.count() and expand.is_visible():
                        if not expand_clicked and poll_elapsed >= 10000:
                            expand.click(timeout=3000)
                            expand_clicked = True
                            Logger.write(f"{attempt_tag}: clicked chat expand button after {poll_elapsed}ms")
                        page.wait_for_timeout(1000)
                        continue
                except Exception:
                    pass
                Logger.write(f"{attempt_tag}: chat still loading... ({poll_elapsed}ms elapsed)")

            if not found:
                err(f"{attempt_tag} {short}.. chat input not found after {gotoms}ms - fast pivot")
                attempt += 1
                continue

            Logger.write(f"{attempt_tag}: chat input visible after {poll_elapsed}ms")
            Logger.write(f"{attempt_tag}: chat input ready, starting session loop")
            info(f"{attempt_tag} {short}.. chat connected, typing begins")

            chat_input_sel = 'div[data-a-target="chat-input"]'
            while time.time() < ChatProgress.session_end:
                message = random.choice(messages)

                for _ in range(30):
                    chat_input = page.locator(chat_input_sel).first
                    if chat_input.count() and chat_input.is_visible():
                        break
                    page.wait_for_timeout(200)
                else:
                    Logger.write(f"{attempt_tag}: chat input vanished mid-session - attempting reconnect")
                    page.goto(f"https://www.twitch.tv/{channel}", wait_until="domcontentloaded", timeout=gotoms)
                    page.wait_for_timeout(random.randint(2000, 4000))
                    continue

                page.wait_for_timeout(random.randint(500, 1000))
                chat_input.click()
                page.wait_for_timeout(random.randint(200, 500))
                for ch in message:
                    chat_input.press(ch)
                    page.wait_for_timeout(random.randint(30, 80))
                Logger.write(f"{attempt_tag}: typed: {message[:50]}...")

                page.wait_for_timeout(random.randint(300, 600))
                try:
                    send_btn = page.locator('button[data-a-target="chat-send-button"]').first
                    send_btn.click(timeout=5000)
                    Logger.write(f"{attempt_tag}: clicked send")
                except Exception:
                    chat_input.press("Enter")
                    Logger.write(f"{attempt_tag}: pressed Enter to send")

                ChatProgress.inc()
                ok(f"{attempt_tag} {short}.. sent: {message[:30]}")

                page.wait_for_timeout(random.randint(2000, 5000))
                delay = random.uniform(3, 8)
                info(f"{attempt_tag} waiting {delay:.1f}s until next message...")
                page.wait_for_timeout(int(delay * 1000))

                if console_severe or console_connections:
                    Logger.write(f"{attempt_tag}: console flagged mid-session - fast pivot")
                    break

            return  # Success - session time expired

        except Exception as e:
            ename = type(e).__name__
            proxy_score.timeout += 1
            Logger.write(f"{attempt_tag}: {ename} | ProxyScore now={proxy_score}")
            err(f"{attempt_tag} {short}.. {ename} - retrying ({attempt}/15)")
            if proxy_score.should_quarantine:
                proxy_pool.quarantine(p_line)
        finally:
            try:
                if ctx:
                    ctx.close()
            except Exception:
                pass
            try:
                shutil.rmtree(user_dir, ignore_errors=True)
            except Exception:
                pass
            if p_line:
                proxy_pool.release(p_line)
            try:
                p.stop()
            except Exception:
                pass
        attempt += 1


def main():
    global proxy_pool
    StageTimer.reset()
    Logger.init(LOGFILE)
    if os.path.exists(LOGFILE):
        try:
            with open(LOGFILE, "w", encoding="utf-8") as f:
                pass
        except Exception:
            pass

    set_status_lines_func(_chat_status_lines)

    tokens = load_tokens()
    if not tokens:
        print(f"{RED}no tokens in results/tokens.txt{X}")
        return

    channel = input(f"{CYAN}channel to chat in > {X}").strip().lstrip("@").lower()
    if not channel:
        return

    try:
        duration = int(input(f"{CYAN}session duration (seconds) > {X}").strip())
    except Exception:
        print("invalid duration")
        return
    if duration <= 0:
        return

    available_tokens = tokens[:]
    print(f"{CYAN}{len(available_tokens)} tokens available{X}")
    try:
        accounts = int(input(f"{CYAN}how many accounts? (1-{len(available_tokens)}) {X}").strip())
    except Exception:
        accounts = 1
    accounts = max(1, min(accounts, len(available_tokens)))

    print(f"{CYAN}available text files:{X}")
    try:
        txt_files = [f for f in os.listdir(TEXTS_DIR) if f.endswith(".txt")]
        for i, f in enumerate(txt_files, 1):
            print(f"  {i}. {f}")
    except Exception:
        txt_files = []
    if not txt_files:
        print(f"{RED}no .txt files in texts/ folder{X}")
        return
    try:
        choice = input(f"{CYAN}pick file (enter name) > {X}").strip()
        filename = choice if choice in txt_files else txt_files[0]
    except Exception:
        filename = txt_files[0]

    messages = load_messages(filename)
    if not messages:
        print(f"{RED}no messages in texts/{filename}{X}")
        return
    print(f"{CYAN}{len(messages)} messages loaded from texts/{filename}{X}")

    ChatProgress.messages_filename = filename

    n = accounts
    init_proxies = build_proxy_pool(PROXIES_FILE)
    proxy_pool = ProxyPool(init_proxies, quarantine_cooldown=PROXY_QUARANTINE_COOLDOWN)
    print(f"{CYAN}{len(init_proxies)} proxies loaded | {n} chatters | {duration}s session -> '@{channel}'{X}")

    ChatProgress.session_duration = duration
    ChatProgress.session_end = time.time() + duration
    ChatProgress.start = time.time()
    ChatProgress.done_event.clear()
    Logger.write(f"===== chat session | channel={channel} | chatters={n} | duration={duration}s =====")

    pt = threading.Thread(target=progress_printer, daemon=True)
    pt.start()

    with ThreadPoolExecutor(max_workers=n) as ex:
        from concurrent.futures import as_completed
        futures = [ex.submit(chat_one, i + 1, available_tokens[i], channel) for i in range(n)]
        for f in as_completed(futures):
            f.result()

    ChatProgress.done_event.set()
    pt.join(timeout=0.15)
    Logger.write(f"===== chat session finished | total messages: {ChatProgress.sent} =====")
    Logger.flush_and_stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    pause_exit()