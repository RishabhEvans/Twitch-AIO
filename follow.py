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

from config import Config

cfg = Config
if os.name == "nt":
    os.system("")

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(HERE, "results", "tokens.txt")
LOGFILE = os.path.join(HERE, "logs.txt")
FOLLOWS_FILE = os.path.join(HERE, "info", "follows.txt")
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


def load_follows_cache():
    cache = {}
    try:
        os.makedirs(os.path.dirname(FOLLOWS_FILE), exist_ok=True)
        if os.path.exists(FOLLOWS_FILE):
            with open(FOLLOWS_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or " | " not in line:
                        continue
                    parts = line.split(" | ")
                    if len(parts) >= 2:
                        token = parts[0].strip()
                        channels = set(c.strip().lower() for c in parts[1].split(",") if c.strip())
                        cache[token] = channels
    except Exception:
        pass
    return cache


_follows_file_lock = threading.Lock()


def save_follow(token, channel):
    channel = channel.lower()
    try:
        with _follows_file_lock:
            os.makedirs(os.path.dirname(FOLLOWS_FILE), exist_ok=True)
            lines = []
            found = False
            if os.path.exists(FOLLOWS_FILE):
                with open(FOLLOWS_FILE, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            for i, line in enumerate(lines):
                if line.startswith(token + " | "):
                    existing = set(c.strip().lower() for c in line.split(" | ", 1)[1].split(",") if c.strip())
                    existing.add(channel)
                    lines[i] = f"{token} | {', '.join(sorted(existing))}"
                    found = True
                    break
            if not found:
                lines.append(f"{token} | {channel}")
            with open(FOLLOWS_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except Exception:
        pass


_ledger_lock = threading.Lock()
ACCOUNTS_FILE = os.path.join(HERE, "results", "accounts.txt")


def purge_dead_token(token_to_nuke):
    with _ledger_lock:
        try:
            if os.path.exists(TOKENS_FILE):
                lines = []
                with open(TOKENS_FILE, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                new_lines = [l for l in lines if l.strip() != token_to_nuke.strip()]
                if len(new_lines) < len(lines):
                    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
                        f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
        except Exception:
            pass
        try:
            if os.path.exists(ACCOUNTS_FILE):
                lines = []
                with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                new_lines = [l for l in lines if not l.strip().endswith(":" + token_to_nuke.strip())]
                if len(new_lines) < len(lines):
                    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                        f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
        except Exception:
            pass
    Logger.write(f"purged dead token & account from ledgers: {token_to_nuke[:10]}...")


def visible(page, sel):
    try:
        b = page.locator(sel).first
        return bool(b.count() and b.is_visible())
    except Exception:
        return False


_ALLOW = ("twitch.tv", "twitchcdn.net", "ttvnw.net", "jtvnw.net",
          "passport.twitch.tv", "gql.twitch.tv", "k.twitchcdn.net")


def _route(route):
    try:
        r = route.request
        if r.resource_type in ("image", "media"):
            return route.abort()
        url = r.url
        if any(t in url for t in (".m3u8", ".ts", ".mp4", "usher.ttvnw.net", "video-weaver", "jtvnw.net/v1",
                                   "scorecardresearch", "amazon-adsystem", "google-analytics", "sentry",
                                   "gql.twitch.tv/spade", "doubleclick", "googletagmanager", "facebook.com/tr")):
            return route.abort()
        if not any(d in url for d in _ALLOW):
            return route.abort()
        return route.continue_()
    except Exception:
        try:
            return route.continue_()
        except Exception:
            return


def follow_one(slot, token, channel):
    tag = f"#{slot}"
    short = token[:8]
    timing = {}
    attempt = 1
    last_reason = ""
    while True:
        if attempt > 15:
            Logger.write(f"{tag}: max retries (15) exhausted, giving up")
            err(f"{tag} failed after 15 attempts")
            return False
        attempt_tag = f"{tag} (attempt {attempt})" if attempt > 1 else tag
        if attempt > 1 and last_reason:
            step(f"{attempt_tag} retrying - {last_reason}")
        try:
            user_dir = tempfile.mkdtemp(prefix="pwf_")
        except Exception:
            Logger.write(f"{attempt_tag}: FATAL - tempfile.mkdtemp failed, retrying in 2s")
            time.sleep(2.0)
            attempt += 1
            continue
        p_line = proxy_pool.acquire()
        if not p_line:
            Logger.write(f"{attempt_tag}: no proxy available, waiting...")
            shutil.rmtree(user_dir, ignore_errors=True)
            time.sleep(1.0)
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
        done = False
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(**lk)
                try:
                    ctx.route("**/*", _route)
                    ctx.add_cookies([
                        {"name": "auth-token", "value": token, "domain": ".twitch.tv", "path": "/",
                         "secure": True, "httpOnly": False, "sameSite": "None"}
                    ])
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    console_severe = []
                    console_connections = []
                    slow_network_flag = [False]

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
                            slow_network_flag[0] = True
                    page.on("console", _handle_console)
                    page.on("pageerror", lambda exc: Logger.write(f"PAGEERROR {attempt_tag}: {exc}"))
                    network_rejected = []

                    def _handle_response(resp):
                        if resp.status in (400, 422, 429) and ("signup" in resp.url.lower() or "register" in resp.url.lower() or "gql" in resp.url.lower()):
                            Logger.write(f"NETWORK {attempt_tag}: HTTP {resp.status} on {resp.url[:80]}")
                            network_rejected.append(True)
                            if resp.status == 400:
                                proxy_score.http_422 += 1
                            elif resp.status == 422:
                                proxy_score.http_422 += 1
                            elif resp.status == 429:
                                proxy_score.http_429 += 1
                    page.on("response", _handle_response)

                    step(f"{attempt_tag} loading https://www.twitch.tv/{channel}...")
                    with StageTimer("follow_navigate", tag=attempt_tag, extra=timing):
                        page.goto(f"https://www.twitch.tv/{channel}", wait_until="domcontentloaded", timeout=gotoms)
                    Logger.write(f"{attempt_tag}: step_load | page ready, dismissing overlays...")

                    with StageTimer("follow_overlay", tag=attempt_tag, extra=timing):
                        for sel in [
                            'button[data-a-target="content-classification-gate-overlay-start-watching-button"]',
                            'button[data-a-target="consent-banner-accept"]',
                        ]:
                            try:
                                b = page.locator(sel).first
                                if b.count() and b.is_visible():
                                    b.click(timeout=3000)
                                    Logger.write(f"{attempt_tag}: dismissed overlay {sel}")
                            except Exception:
                                pass
                        page.wait_for_timeout(random.randint(200, 800))

                    Logger.write(f"{attempt_tag}: step_buttons | waiting for auth buttons to appear...")
                    with StageTimer("follow_button_wait", tag=attempt_tag, extra=timing):
                        button_found = False
                        button_sel = 'button[data-a-target="login-button"], button[data-a-target="unfollow-button"], button[data-a-target="follow-button"]'
                        elapsed = 0
                        poll_ms = 500
                        early_deadline = min(6000, gotoms)
                        while elapsed < gotoms:
                            try:
                                btn = page.locator(button_sel).first
                                if btn.count() and btn.is_visible():
                                    Logger.write(f"{attempt_tag}: step_buttons | button found on page")
                                    button_found = True
                                    break
                            except Exception:
                                pass
                            page.wait_for_timeout(poll_ms)
                            elapsed += poll_ms
                            if elapsed >= early_deadline and (console_severe or console_connections):
                                last_reason = f"reason: proxy flagged by Twitch at {elapsed}ms"
                                Logger.write(f"{attempt_tag}: early bailout at {elapsed}ms - proxy flagged (severe={bool(console_severe)} conn_err={bool(console_connections)}), abandoning")
                                err(f"{attempt_tag} {short}.. proxy blocked/flagged by Twitch ({elapsed}ms) - fast pivot")
                                try:
                                    ctx.close()
                                except Exception:
                                    pass
                                shutil.rmtree(user_dir, ignore_errors=True)
                                attempt += 1
                                continue
                        if not button_found:
                            last_reason = f"reason: no button found after {gotoms}ms"
                            Logger.write(f"{attempt_tag}: Timeout after {gotoms}ms waiting for Follow/Unfollow button at {page.url} - proxy too slow to render JS")
                            err(f"{attempt_tag} {short}.. no button found on page")
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            shutil.rmtree(user_dir, ignore_errors=True)
                            attempt += 1
                            continue

                    if console_severe or console_connections:
                        proxy_score.http_429 += 1
                        Logger.write(f"{attempt_tag}: proxy flagged via console (severe={bool(console_severe)} conn_err={bool(console_connections)}) - ProxyScore={proxy_score}")
                        err(f"{attempt_tag} {short}.. proxy flagged by Twitch")
                        if proxy_score.should_quarantine:
                            Logger.write(f"{attempt_tag}: ProxyScore QUARANTINE - {proxy_score}")
                            proxy_pool.quarantine(p_line)
                            purge_proxy_file(p_line)
                        shutil.rmtree(user_dir, ignore_errors=True)
                        attempt += 1
                        continue

                    with StageTimer("follow_click", tag=attempt_tag, extra=timing):
                        if visible(page, 'button[data-a-target="login-button"]'):
                            Logger.write(f"{attempt_tag}: step_state | login button visible = token expired")
                            purge_dead_token(token)
                            err(f"{attempt_tag} invalid/expired token removed, retrying with next account...")
                            return False
                        elif visible(page, 'button[data-a-target="unfollow-button"]'):
                            Logger.write(f"{attempt_tag}: step_state | unfollow button visible = already following")
                            save_follow(token, channel)
                            if console_severe or console_connections:
                                proxy_score.http_429 += 1
                            proxy_score.success += 1
                            Progress.inc()
                            ok(f"{attempt_tag} {short}.. already following {channel}")
                            done = True
                            time.sleep(random.uniform(1.0, 1.5))
                        elif visible(page, 'button[data-a-target="follow-button"]'):
                            Logger.write(f"{attempt_tag}: step_state | follow button visible = ready to follow")
                            page.wait_for_timeout(random.randint(300, 600))
                            confirmed = False
                            for click_attempt in range(1, 4):
                                try:
                                    btn = page.locator('button[data-a-target="follow-button"]').first
                                    btn.click(delay=random.randint(50, 150), timeout=5000)
                                    Logger.write(f"{attempt_tag}: clicked follow button (native click #{click_attempt})")
                                except Exception:
                                    Logger.write(f"{attempt_tag}: follow button click #{click_attempt} failed")
                                    continue
                                sub_check_end = time.time() + 3
                                while time.time() < sub_check_end:
                                    if visible(page, 'button[data-a-target="unfollow-button"]'):
                                        confirmed = True
                                        break
                                    page.wait_for_timeout(random.randint(100, 300))
                                if confirmed:
                                    break
                                else:
                                    Logger.write(f"{attempt_tag}: click #{click_attempt} not confirmed in 3s")
                            if not confirmed:
                                err(f"{attempt_tag} {short}.. clicked but not confirmed (3 attempts)")
                                try:
                                    page.screenshot(path=os.path.join(HERE, f"dbg_follow_{slot}.png"))
                                except Exception:
                                    pass
                            else:
                                save_follow(token, channel)
                                record_proxy_use(p_line)
                                if console_severe or console_connections:
                                    proxy_score.http_429 += 1
                                proxy_score.success += 1
                                Progress.inc()
                                ok(f"{attempt_tag} {short}.. followed {channel}")
                                done = True
                                time.sleep(random.uniform(1.0, 1.5))
                        else:
                            Logger.write(f"{attempt_tag}: step_state | no button matched - unknown state")
                            err(f"{attempt_tag} {short}.. follow button not found")
                            try:
                                page.screenshot(path=os.path.join(HERE, f"dbg_follow_{slot}.png"))
                            except Exception:
                                pass
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            ename = type(e).__name__
            proxy_score.timeout += 1
            last_reason = f"reason: {ename}"
            Logger.write(f"{attempt_tag}: {ename} | ProxyScore now={proxy_score}")
            if proxy_score.should_quarantine:
                Logger.write(f"{attempt_tag}: ProxyScore QUARANTINE - {proxy_score}")
                proxy_pool.quarantine(p_line)
                purge_proxy_file(p_line)
        finally:
            shutil.rmtree(user_dir, ignore_errors=True)
            if p_line:
                proxy_pool.release(p_line)
        if done:
            return True
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

    def _follow_status_lines():
        done, goal = Progress.snapshot()
        if Progress.end_time > 0:
            el = Progress.end_time - Progress.start
        else:
            el = time.time() - Progress.start
        spf = el / done if done > 0 else 0.0
        return [
            f"---------- ({done}/{goal}) follows done | time taken: {fmt_elapsed(el)} ----------",
            f"---------- average: {spf:.2f} secs/follow ----------",
        ]
    set_status_lines_func(_follow_status_lines)

    tokens = load_tokens()
    if not tokens:
        print(f"{RED}no tokens in results/tokens.txt{X}")
        return

    channel = input(f"{CYAN}channel to follow > {X}").strip().lstrip("@").lower()
    if not channel:
        return

    try:
        count = int(
            input(f"{CYAN}how many follows? (1-{len(tokens)}, blank=all) {X}").strip() or str(len(tokens))
        )
    except Exception:
        count = len(tokens)
    count = max(1, min(count, len(tokens)))

    try:
        workers = int(input(f"{CYAN}how many chrome windows in parallel? {X}").strip() or "1")
    except Exception:
        workers = 1

    follows_cache = load_follows_cache()
    full_tokens = load_tokens()
    available_tokens = []
    for t in full_tokens:
        if t in follows_cache and channel.lower() in follows_cache[t]:
            continue
        available_tokens.append(t)

    skipped_count = len(full_tokens) - len(available_tokens)
    if skipped_count:
        print(f"{CYAN}{skipped_count} tokens already follow @{channel}, skipped{X}")

    if not available_tokens:
        print(f"{RED}all tokens already follow @{channel}{X}")
        return

    count = min(count, len(available_tokens))
    workers = max(1, min(workers, count))

    n = count
    init_proxies = build_proxy_pool(PROXIES_FILE)
    proxy_pool = ProxyPool(init_proxies, quarantine_cooldown=PROXY_QUARANTINE_COOLDOWN)
    print(f"{CYAN}{len(init_proxies)} proxies loaded | {n} tokens -> following '@{channel}'{X}")

    Progress.reset()
    Progress.goal = n
    Progress.start = time.time()
    Progress.done_event.clear()
    Logger.write(f"===== follow run | channel={channel} | tokens={n} | windows={workers} =====")

    pt = threading.Thread(target=progress_printer, daemon=True)
    pt.start()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        from concurrent.futures import as_completed
        futures = [ex.submit(follow_one, i + 1, available_tokens[i], channel) for i in range(n)]
        for f in as_completed(futures):
            f.result()

    Progress.done_event.set()
    pt.join(timeout=0.15)
    Logger.write(f"===== follow finished | {' | '.join(_follow_status_lines())} =====")
    Logger.write("Stage Timing Report:\n" + StageTimer.report())
    Logger.write("Proxy Score Report:\n" + score_report())
    Logger.flush_and_stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    pause_exit()