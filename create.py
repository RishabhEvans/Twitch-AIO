import os
import sys
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
sys.dont_write_bytecode = True
import time
import re
import random
import string
import socket
import requests
import threading
import calendar
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from patchright.sync_api import sync_playwright
from config import (
    Config, BLOCKED_DOMAINS, fetch_kopechka_domains,
    PROXY_SPEED_CHECK_TIMEOUT, PROXY_NAV_TIMEOUT_FLOOR, PROXY_NAV_TIMEOUT_CEILING,
    PROXY_WAIT_TIMEOUT_FLOOR, PROXY_WAIT_TIMEOUT_CEILING,
    PROXY_QUARANTINE_COOLDOWN, PROXY_USE_MAX, PROXY_NAV_MULTIPLIER, PROXY_WAIT_MULTIPLIER,
    OTP_WAIT_BEFORE_PIVOT, OTP_FETCH_TIMEOUT, BIRTHDAY_RETRY_MAX, BIRTHDAY_RETRY_DELAY_MS,
    BLOCKED_USERNAME_SUBS,
)
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
ACCOUNTS = os.path.join(HERE, "results", "accounts.txt")
TOKENS = os.path.join(HERE, "results", "tokens.txt")
LOGFILE = os.path.join(HERE, "logs.txt")
PROXIES_FILE = os.path.join(HERE, "results", "proxies.txt")

HEADLESS = os.getenv("HEADLESS", "0") == "1"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL")
if not BROWSER_CHANNEL:
    BROWSER_CHANNEL = "chrome" if os.name == "nt" else "chromium"
DELETE_INBOX_AFTER = os.getenv("KEEP_INBOX", "0") != "1"

_ANSI = re.compile(r"\033\[[0-9;]*m")
file_lock = threading.Lock()

cfg = Config
CT_BASE = "https://api.kopechka.com/api/v1"
CT_KEY = cfg.KopechkaApiKey
CT_HEADERS = {"Accept": "application/json", "x-api-key": cfg.KopechkaApiKey}
CT_DOMAINS = []
CT_RPS = 50
_ct_gap = 1.0 / max(0.5, CT_RPS)
_ct_lock = threading.Lock()
_ct_last = [0.0]
_domain_idx = 0
_domain_idx_lock = threading.Lock()

PRE_CREATED_INBOXES = Queue()
proxy_pool = None

_CONFIG_TEXT = ""
try:
    with open(os.path.join(HERE, "config.py"), encoding="utf-8") as _cf:
        _CONFIG_TEXT = _cf.read()
except Exception:
    pass


def save_blocked_domains():
    global _CONFIG_TEXT
    try:
        new_line = f"BLOCKED_DOMAINS = {BLOCKED_DOMAINS}"
        new_text = re.sub(r"^BLOCKED_DOMAINS\s*=\s*\[.*?\]", new_line, _CONFIG_TEXT, flags=re.MULTILINE)
        if new_text != _CONFIG_TEXT:
            with open(os.path.join(HERE, "config.py"), "w", encoding="utf-8") as f:
                f.write(new_text)
            _CONFIG_TEXT = new_text
    except Exception:
        pass



def ct_throttle():
    with _ct_lock:
        wait = _ct_gap - (time.time() - _ct_last[0])
        if wait > 0:
            time.sleep(wait)
        _ct_last[0] = time.time()


def get_ct_domain():
    global _domain_idx
    with _domain_idx_lock:
        for _ in range(len(CT_DOMAINS)):
            d = CT_DOMAINS[_domain_idx % len(CT_DOMAINS)]
            _domain_idx += 1
            if d not in BLOCKED_DOMAINS:
                return d
        return CT_DOMAINS[0]



def create_inbox():
    try:
        rq = __import__('requests')
        import uuid
        r = rq.post(
            f"{CT_BASE}/orders",
            json={"site": "twitch.tv", "quantity": 1},
            headers=dict(CT_HEADERS, **{"Content-Type": "application/json", "idempotency-key": str(uuid.uuid4())}),
            timeout=15,
        )
        if r.status_code in (200, 201):
            data = r.json().get("data", r.json())
            email = data.get("email")
            order_id = data.get("orderId") or data.get("id")
            if email and order_id:
                domain = email.split("@")[1] if "@" in email else ""
                if domain not in BLOCKED_DOMAINS:
                    try:
                        rq.post(
                            f"{CT_BASE}/orders/{order_id}/start-polling",
                            json={},
                            headers=dict(CT_HEADERS, **{"Content-Type": "application/json"}),
                            timeout=10,
                        )
                    except Exception:
                        pass
                    Logger.write(f"create_inbox: order {order_id} -> {email}")
                    return (email, order_id)
    except Exception:
        pass
    Logger.write("create_inbox: API call failed, returning None")
    return None


def pre_create_inboxes(count):
    created = 0
    for _ in range(count):
        inbox = create_inbox()
        if inbox:
            PRE_CREATED_INBOXES.put(inbox)
            created += 1
            print(f"\r{YELLOW}[@] ({created}/{count}) inboxes pre-created{X}", end="", flush=True)
    print()
    return created


def get_inbox():
    return create_inbox()


def delete_inbox(addr_or_id):
    try:
        if isinstance(addr_or_id, tuple):
            order_id = addr_or_id[1]
        elif isinstance(addr_or_id, str) and "@" in addr_or_id:
            Logger.write(f"delete_inbox: legacy email addr {addr_or_id}, skipping")
            return False
        else:
            order_id = str(addr_or_id)
        r = __import__('requests').delete(
            f"{CT_BASE}/orders/{order_id}",
            headers=CT_HEADERS,
            timeout=10,
        )
        Logger.write(f"delete_inbox: order {order_id} -> HTTP {r.status_code}")
        return r.status_code in (200, 204)
    except Exception:
        Logger.write(f"delete_inbox: {addr_or_id} -> request failed")
        return False


def cleanup_stale_inboxes():
    try:
        removed = 0
        Logger.write(f"cleanup_stale_inboxes: removed {removed}")
        return removed
    except Exception:
        pass
    return 0



def gen_username():
    for _ in range(100):
        length = random.randint(8, 14)
        chars = string.ascii_lowercase + string.digits
        name = "".join(random.choice(chars) for _ in range(length))
        nl = name.lower()
        if not any(bad in nl for bad in BLOCKED_USERNAME_SUBS):
            return name
    return "".join(random.choice(string.ascii_lowercase) for _ in range(12))


def gen_password(n=12):
    lo = random.choice(string.ascii_lowercase)
    up = random.choice(string.ascii_uppercase)
    dg = random.choice(string.digits)
    rest = [random.choice(string.ascii_letters + string.digits + "!@#$%&*?") for _ in range(n - 4)]
    chars = [lo, up, dg, "?"] + rest
    random.shuffle(chars)
    return "".join(chars)


def random_birthday():
    year = random.randint(1970, 1999)
    month = random.randint(1, 12)
    day = random.randint(1, calendar.monthrange(year, month)[1])
    return {"month": calendar.month_name[month], "day": str(day), "year": str(year)}



_OTP_PATTERN = re.compile(r"\b(\d{4,8})\b")


def _extract_otp_from_messages(messages):
    if not isinstance(messages, list):
        return None
    for msg in messages:
        subj = (msg.get("subject") or msg.get("title") or "").lower()
        text = msg.get("text") or msg.get("body") or msg.get("content") or msg.get("plain") or ""
        html = msg.get("html") or msg.get("htmlBody") or ""
        blob = subj + " " + text + " " + re.sub("<[^>]+>", " ", html)
        mobj = re.search(r"\b(\d{6})\b", blob)
        if mobj:
            return mobj.group(1)
        mobj = re.search(r"\b(\d{6})\b", text)
        if mobj:
            return mobj.group(1)
        if "verif" in subj or "code" in subj or "verif" in blob:
            mobj = _OTP_PATTERN.search(blob)
            if mobj:
                return mobj.group(1)
        mobj = _OTP_PATTERN.search(text)
        if mobj:
            return mobj.group(1)
    return None


def fetch_otp(order_id, timeout=OTP_FETCH_TIMEOUT):
    deadline = time.time() + timeout
    burst = 3
    while time.time() < deadline:
        try:
            r = requests.get(f"{CT_BASE}/orders/{order_id}/messages", headers=CT_HEADERS, timeout=8)
            if r.status_code not in (200, 201):
                time.sleep(0.3)
                continue
            data = r.json()
            messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
            if messages and burst == 3:
                Logger.write(f"fetch_otp: order {order_id} has {len(messages)} messages, sample keys: {list(messages[0].keys()) if isinstance(messages[0], dict) else 'not dict'}")
            code = _extract_otp_from_messages(messages)
            if code:
                return code
        except requests.Timeout:
            pass
        except Exception:
            pass
        if burst > 0:
            burst -= 1
            time.sleep(0.05)
        else:
            time.sleep(0.1)
    Logger.write(f"fetch_otp: order {order_id} timed out after {timeout}s")
    return None


def start_otp_prewarm(order_id, timeout=OTP_WAIT_BEFORE_PIVOT):
    result = []

    def _poll():
        deadline = time.time() + max(1, min(timeout, timeout - 2))
        while time.time() < deadline and not result:
            try:
                r = requests.get(f"{CT_BASE}/orders/{order_id}/messages", headers=CT_HEADERS, timeout=8)
                if r.status_code not in (200, 201):
                    time.sleep(0.3)
                    continue
                data = r.json()
                messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
                if messages and not result:
                    Logger.write(f"otp_prewarm: order {order_id} has {len(messages)} messages")
                code = _extract_otp_from_messages(messages)
                if code:
                    result.append(code)
                    return
            except Exception:
                pass
            time.sleep(0.08)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return result, t


def find_verification_code(order_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = fetch_otp(order_id, timeout=min(10, timeout))
        if code:
            return code
        time.sleep(0.5)
    return None



def append_line(path, line):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().rstrip("\n\r")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content + "\n" + line + "\n")
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def save(email, username, password, token):
    with file_lock:
        append_line(ACCOUNTS, f"{email}:{username}:{password}:{token or ''}")
        if token:
            append_line(TOKENS, token)



_ROUTE_ALLOW = (
    "twitch.tv", "twitchcdn.net", "ttvnw.net", "jtvnw.net",
    "assets.twitch.tv", "static.twitchcdn.net", "passport.twitch.tv",
    "gql.twitch.tv", "k.twitchcdn.net",
)


def _route(route):
    try:
        r = route.request
        if r.resource_type in ("media",):
            return route.abort()
        url = r.url
        if any(t in url for t in (
            "scorecardresearch", "amazon-adsystem", "google-analytics", "sentry",
            "gql.twitch.tv/spade", "doubleclick", "googletagmanager", "facebook.com/tr",
            "/beacon.js",
        )):
            return route.abort()
        url_host = url.split("/")[2] if "://" in url else ""
        if not any(allowed in url_host for allowed in _ROUTE_ALLOW):
            return route.abort()
        return route.continue_()
    except Exception:
        try:
            return route.continue_()
        except Exception:
            return



def set_birthday(page, birth):
    for _ in range(3):
        try:
            count = page.locator("select").count()
            if count >= 2:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    done = set()
    for attempt in range(BIRTHDAY_RETRY_MAX):
        selects = page.locator("select")
        count = selects.count()
        if count == 0:
            if attempt >= 2:
                return False
            page.wait_for_timeout(BIRTHDAY_RETRY_DELAY_MS * (attempt + 1))
            continue
        for i in range(count):
            page.wait_for_timeout(random.randint(100, 250))
            s = selects.nth(i)
            try:
                opts = s.locator("option").all_inner_texts()
            except Exception:
                opts = []
            try:
                if "month" not in done:
                    if birth["month"] in opts:
                        s.select_option(label=birth["month"])
                        done.add("month")
                    else:
                        month_idx = list(calendar.month_name).index(birth["month"]) if birth["month"] in calendar.month_name else 0
                        month_abbr = calendar.month_abbr[month_idx] if 0 < month_idx < 13 else ""
                        if month_abbr and month_abbr in opts:
                            s.select_option(label=month_abbr)
                            done.add("month")
                elif "year" not in done and birth["year"] in opts:
                    s.select_option(label=birth["year"])
                    done.add("year")
                elif "day" not in done and birth["day"] in opts:
                    s.select_option(label=birth["day"])
                    done.add("day")
            except Exception:
                pass
        if {"month", "day", "year"} <= done:
            return True
        page.wait_for_timeout(BIRTHDAY_RETRY_DELAY_MS * (attempt + 1))
    Logger.write(f"set_birthday: failed after {BIRTHDAY_RETRY_MAX} attempts | done={done}")
    return False


def advanced(page):
    try:
        if page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"]').first.is_visible():
            return True
    except Exception:
        pass
    try:
        if not page.locator('input#signup-username, input[autocomplete="username"]').first.is_visible():
            return True
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text()
        if body and ("verification code" in body.lower() or "enter your verification" in body.lower()):
            return True
    except Exception:
        pass
    return False


def dismiss_cookie_wall(page):
    try:
        body = (page.locator("body").inner_text() or "").lower()
        if not body or ("cookie" not in body and "advertising" not in body):
            return False
    except Exception:
        return False
    Logger.write(f"dismiss_cookie_wall: body[:100]={body[:100]}")
    try:
        page.evaluate("""() => { try { if(window.Cookies) Cookies.set('twitch-consent', 'all:1'); return 'eval'; } catch(e) {} try { localStorage.setItem('twitch-consent-accepted', '1'); return 'ls'; } catch(e) {} return 'none'; }""")
        page.wait_for_timeout(200)
        b0 = (page.locator("body").inner_text() or "").lower()
        if "cookie" not in b0 and "advertising" not in b0:
            Logger.write("dismiss_cookie_wall: consent injected via JS")
            return True
    except Exception:
        pass
    result = page.evaluate("""() => {
        const words = ['accept all', 'agree to all', 'allow all', 'accept all cookies',
                       'accept', 'agree', 'allow', 'consent', 'ok', 'yes', 'proceed', 'got it'];
        for (const el of document.querySelectorAll('button, a[href], [role="button"]')) {
            if (!el.offsetParent) continue;
            const text = (el.textContent || '').trim().toLowerCase();
            if (words.some(w => text.includes(w))) {
                el.click();
                return {clicked: true, tag: (el.tagName||'').toLowerCase(), text: text.slice(0, 80)};
            }
        }
        return {clicked: false};
    }""")
    if result.get("clicked"):
        Logger.write(f"dismiss_cookie_wall: JS-CLICKED <{result['tag']}> '{result['text']}'")
        page.wait_for_timeout(400)
        try:
            b2 = (page.locator("body").inner_text() or "").lower()
            if "cookie" not in b2 and "advertising" not in b2:
                return True
        except Exception:
            return True
    try:
        body_txt = (page.locator("body").inner_text() or "").lower()
        if "targeted advertising" in body_txt or "opt out" in body_txt or "trusted partners" in body_txt:
            dismissed = page.evaluate("""() => {
                try {
                    const close = document.querySelector('[data-a-target="close-button"], button[aria-label="Close"], svg[aria-label="Close"]');
                    if (close) { (close.closest('button')||close).click(); return 'close'; }
                    const accept = [...document.querySelectorAll('button')].find(b => /accept|agree|ok|got it/i.test(b.textContent) && b.offsetParent);
                    if (accept) { accept.click(); return 'accept'; }
                    const overlay = document.querySelector('[class*="modal"], [class*="overlay"], [class*="dialog"]');
                    if (overlay) { overlay.remove(); return 'removed'; }
                } catch(e) {}
                return 'none';
            }""")
            Logger.write(f"dismiss_cookie_wall: privacy modal handled ({dismissed})")
            page.wait_for_timeout(400)
            bf2 = (page.locator("body").inner_text() or "").lower()
            if "cookie" not in bf2 and "advertising" not in bf2 and "opt out" not in bf2:
                return True
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        Logger.write("dismiss_cookie_wall: pressed Escape")
        b3 = (page.locator("body").inner_text() or "").lower()
        if "cookie" not in b3 and "advertising" not in b3:
            return True
    except Exception:
        pass
    Logger.write("dismiss_cookie_wall: FAILED to dismiss")
    return False


def handle_email_block(page, email, attempt_tag, email_sel):
    try:
        body_check = page.locator("body").inner_text() or ""
        bad = None
        if "not allowed" in body_check.lower():
            dm = re.search(r"""domain ['\"]([^'\"]+)['\"]""", body_check)
            if dm:
                bad = dm.group(1)
        if not bad:
            try:
                border_color = page.evaluate(f"""(sel) => {{
                    const el = document.querySelector(sel);
                    if (!el) return '';
                    const style = window.getComputedStyle(el);
                    return style.borderColor || '';
                }}""", email_sel)
                if border_color and "rgb" in border_color.lower():
                    rgb = re.findall(r'\d+', border_color)
                    if len(rgb) >= 3 and int(rgb[0]) > 180 and int(rgb[1]) < 80 and int(rgb[2]) < 80:
                        domain_part = email.split("@")[1] if "@" in email else ""
                        if domain_part:
                            bad = domain_part
                            Logger.write(f"{attempt_tag}: red border detected on email field (border-color: {border_color})")
            except Exception:
                pass
        if not bad:
            return email, False
        if bad not in BLOCKED_DOMAINS:
            BLOCKED_DOMAINS.append(bad)
            save_blocked_domains()
        err(f"{attempt_tag} flagged email domain: {bad} - replacing...")
        Logger.write(f"{attempt_tag}: blocked domain detected: {bad}")
        if email and DELETE_INBOX_AFTER:
            delete_inbox(email)
        for _ in range(20):
            new_email = get_inbox()
            if not new_email:
                err(f"{attempt_tag} no inbox after domain block - aborting")
                return email, True
            new_domain = new_email.split("@")[1]
            if new_domain not in BLOCKED_DOMAINS:
                break
            Logger.write(f"{attempt_tag}: new email {new_domain} also blocked, retrying...")
        else:
            err(f"{attempt_tag} all domains blocked - aborting")
            return email, True
        Logger.write(f"{attempt_tag}: replacing with new email: {new_email}")
        step(f"{attempt_tag} replaced email: {new_email}")
        page.locator(email_sel).first.fill("")
        page.locator(email_sel).first.fill(new_email)
        page.locator(email_sel).first.focus()
        page.wait_for_timeout(random.randint(50, 150))
        page.locator('button[type="submit"], button[data-a-target="signup-button"]').first.click(timeout=3000)
        page.wait_for_timeout(random.randint(200, 400))
        return new_email, False
    except Exception:
        return email, False


def check_failure_state(page):
    try:
        body_text = (page.locator("body").inner_text() or "").lower()
        if any(kw in body_text for kw in ("suspicious activity", "ip flagged", "access denied", "kasada", "try again later")):
            Logger.write("FAILURE STATE: proxy/ip flagged in body text")
            return "proxy_flagged"
    except Exception:
        pass
    try:
        border = page.evaluate("""() => {
            const el = document.querySelector('input[type="email"]');
            if (!el) return '';
            const s = window.getComputedStyle(el);
            return s.borderColor || '';
        }""")
        if border and "rgb" in border:
            rgb = re.findall(r'\d+', border)
            if len(rgb) >= 3 and int(rgb[0]) > 150 and int(rgb[1]) < 100 and int(rgb[2]) < 100:
                Logger.write(f"FAILURE STATE: email red border detected ({border})")
                return "blocked"
        aria_invalid = page.evaluate("""() => {
            const el = document.querySelector('input[type="email"]');
            return el ? el.getAttribute('aria-invalid') : '';
        }""")
        if aria_invalid == "true":
            Logger.write("FAILURE STATE: email aria-invalid='true'")
            return "blocked"
    except Exception:
        pass
    try:
        error_selectors = [
            'div[data-a-target="form-error"]',
            '[class*="error"]',
            '[class*="Error"]',
            'div[class*="ScInFeatureNotificationContent"]',
            '[class*="notification"]',
            '[class*="Notification"]',
        ]
        for sel in error_selectors:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                text = el.inner_text()
                if text:
                    Logger.write(f"FAILURE STATE: {text[:200]}")
                    if "already" in text.lower() and ("taken" in text.lower() or "linked" in text.lower() or "associated" in text.lower()):
                        return "already_taken"
                    if "not allowed" in text.lower() or "domain" in text.lower() or "invalid" in text.lower() or "blocked" in text.lower():
                        return "blocked"
                    if "suspicious" in text.lower() or "kasada" in text.lower() or "ip flagged" in text.lower() or "access denied" in text.lower():
                        return "proxy_flagged"
                    if "network connection" in text.lower() or "internet connection" in text.lower() or "try again" in text.lower():
                        return "proxy_flagged"
                    return f"error: {text[:80]}"
    except Exception:
        pass
    return None


def is_otp_page(page):
    try:
        body = (page.locator("body").inner_text() or "").lower()
        if "enter your verification code" in body:
            Logger.write("OTP_DETECT: found 'enter your verification code' in body")
            return True
        if "verification code" in body:
            Logger.write("OTP_DETECT: found 'verification code' in body")
            return True
        if "check your email" in body or "email sent" in body:
            Logger.write("OTP_DETECT: found 'check your email' in body")
            return True
    except Exception:
        pass
    try:
        url = page.url.lower()
        if "/verify" in url or "otp" in url or "auth" in url:
            Logger.write(f"OTP_DETECT: OTP URL detected ({page.url})")
            return True
    except Exception:
        pass
    try:
        heading = page.locator("h2, h1, h3, [class*='header'], [class*='title'], p[class*='Verification'], div[class*='verify']").first
        if heading.count() and heading.is_visible():
            htext = (heading.inner_text() or "").lower()
            if "verification" in htext or "code" in htext or "enter the" in htext:
                Logger.write(f"OTP_DETECT: heading text = '{htext[:60]}'")
                return True
    except Exception:
        pass
    try:
        otp_input = page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"], input[type="number"]').first
        if otp_input.count() and otp_input.is_visible():
            Logger.write("OTP_DETECT: OTP numeric input field visible")
            return True
    except Exception:
        pass
    try:
        body_text = page.evaluate("() => document.body.innerText.toLowerCase()") or ""
        if "enter your verification code" in body_text or "verification code" in body_text or "check your email" in body_text:
            Logger.write("OTP_DETECT: body evaluate matched OTP text")
            return True
    except Exception:
        pass
    return False


def submit_signup(page, console_integrity=None, network_rejected=None, submitted=None, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            body = page.locator("body").inner_text() or ""
            body_lower = body.lower()
            if "enter your verification code" in body_lower:
                Logger.write("OTP_DETECT: body text 'enter your verification code'")
                return True
            if "verification code" in body_lower and ("enter" in body_lower or "check your email" in body_lower):
                Logger.write("OTP_DETECT: body text 'verification code'")
                return True
            if "check your email" in body_lower:
                Logger.write("OTP_DETECT: body text 'check your email'")
                return True
        except Exception:
            pass
        try:
            aria = page.locator('input[type="email"]').first.get_attribute('aria-invalid')
            if aria == "true":
                Logger.write("submit_signup: email aria-invalid=true (DOM detected)")
                return "blocked"
        except Exception:
            pass
        if page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"]').first.is_visible():
            Logger.write("OTP_DETECT: OTP numeric input field visible")
            return True
        if advanced(page):
            return True
        fail = check_failure_state(page)
        if fail:
            if fail == "already_taken":
                return "already_taken"
            if fail == "proxy_flagged":
                return "proxy_flagged"
            if "phone" in str(fail).lower():
                return "proxy_flagged"
            try:
                body = page.locator("body").inner_text() or ""
                if body and ("not allowed" in body.lower()):
                    return "blocked"
            except Exception:
                pass
            return "blocked"
        if console_integrity:
            Logger.write("submit_signup: console integrity error detected")
            return "integrity_error"
        time.sleep(0.1)
    return False


def wait_for_token(ctx, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        try:
            for c in ctx.cookies():
                if c.get("name") == "auth-token" and c.get("value"):
                    return c["value"]
        except Exception:
            pass
        if Progress.done_event.is_set():
            return None
        time.sleep(0.2)
    return None



FAST_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-extensions",
    "--mute-audio",
    "--metrics-recording-only",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-features=AutomationControlled,Translate,OptimizationHints,MediaRouter,IsolateOrigins,site-per-process",
]



_timing_collectors = {}


def create_one(slot):
    tag = f"#{slot}"
    timing = {}
    _timing_collectors[slot] = timing
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
        p_line = proxy_pool.acquire()
        if not p_line:
            last_reason = "reason: no proxy available"
            Logger.write(f"{attempt_tag}: no proxy available, waiting...")
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
        gotoms = dynamic_timeout(ms, multiplier=PROXY_NAV_MULTIPLIER, floor=PROXY_NAV_TIMEOUT_FLOOR, ceiling=PROXY_NAV_TIMEOUT_CEILING)
        waittoms = dynamic_timeout(ms, multiplier=PROXY_WAIT_MULTIPLIER, floor=PROXY_WAIT_TIMEOUT_FLOOR, ceiling=PROXY_WAIT_TIMEOUT_CEILING)
        if attempt == 1:
            info(f"{tag} new proxy = {col}{ms:.0f}ms{X}")
        else:
            info(f"{attempt_tag} new proxy = {col}{ms:.0f}ms{X}")
        inbox_data = get_inbox()
        if not inbox_data:
            last_reason = "reason: no email created"
            proxy_pool.release(p_line)
            err(f"{tag} no email created, retrying...")
            attempt += 1
            continue
        email, order_id = inbox_data
        username = gen_username()
        password = gen_password()
        birth = random_birthday()
        info(f"{attempt_tag} {email} | {username}")
        try:
            user_dir = tempfile.mkdtemp(prefix="pw_")
        except Exception:
            last_reason = "reason: tempfile.mkdtemp failed"
            Logger.write(f"{attempt_tag}: FATAL - tempfile.mkdtemp failed, retrying in 2s")
            if p_line:
                proxy_pool.release(p_line)
            p_line = None
            time.sleep(2.0)
            attempt += 1
            continue
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
        lk["proxy"] = parse_proxy_for_pw(p_line)
        created = False
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(**lk)
                try:
                    ctx.route("**/*", _route)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    console_integrity = []
                    console_severe = []
                    console_connections = []
                    _422_flag = [False]
                    slow_network_flag = [False]

                    def _handle_console(msg):
                        if "irc-ws" in msg.text:
                            return
                        Logger.write(f"CONSOLE {attempt_tag}: [{msg.type}] {msg.text}")
                        if msg.type == "error" and (
                            "failed to find a valid digest in the 'integrity' attribute" in msg.text.lower()
                            or "integrity mismatch" in msg.text.lower()
                        ):
                            console_integrity.append(True)
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

                    def _handle_response(resp):
                        if resp.status in (400, 422, 429) and ("signup" in resp.url.lower() or "register" in resp.url.lower() or "gql" in resp.url.lower()):
                            Logger.write(f"NETWORK {attempt_tag}: HTTP {resp.status} on {resp.url[:80]}")
                            if resp.status == 400:
                                proxy_score.http_422 += 1
                                _422_flag[0] = True
                            elif resp.status == 422:
                                proxy_score.http_422 += 1
                                _422_flag[0] = True
                            elif resp.status == 429:
                                proxy_score.http_429 += 1
                    page.on("response", _handle_response)

                    def _handle_request(req):
                        if req.method == "POST" and ("signup" in req.url.lower() or "register" in req.url.lower() or "protected_register" in req.url.lower()):
                            Logger.write(f"NETWORK {attempt_tag}: POST detected to {req.url[:80]}")
                    page.on("request", _handle_request)

                    t_nav = time.time()
                    step(f"{attempt_tag} navigating to signup page...")
                    with StageTimer("navigate", tag=attempt_tag, extra=timing):
                        page.goto("https://www.twitch.tv/signup", wait_until="domcontentloaded", timeout=gotoms)
                        page.wait_for_timeout(random.randint(300, 800))
                    Logger.write(f"{attempt_tag}: page loaded in {(time.time()-t_nav)*1000:.0f}ms, url={page.url}")
                    Logger.write(f"{attempt_tag}: step_load | loaded={page.url}")

                    if console_connections and not console_severe:
                        warn(f"{attempt_tag} slow proxy - tunnel connection failures, switching...")
                        Logger.write(f"{attempt_tag}: {len(console_connections)} tunnel failures detected after navigation - fast pivot")
                        last_reason = "reason: proxy tunnel connection failed"
                        proxy_pool.quarantine(p_line)
                        purge_proxy_file(p_line)
                        try:
                            ctx.close()
                        except Exception:
                            pass
                        shutil.rmtree(user_dir, ignore_errors=True)
                        attempt += 1
                        continue

                    record_proxy_use(p_line)

                    with StageTimer("cookie", tag=attempt_tag, extra=timing):
                        dismiss_cookie_wall(page)
                    Logger.write(f"{attempt_tag}: step_cookie | cookies dismissed, looking for email button...")

                    try:
                        b = page.get_by_role("button", name=re.compile("email", re.I))
                        if b.count() and b.first.is_visible():
                            b.first.click(timeout=2000)
                            page.wait_for_timeout(300)
                            Logger.write(f"{attempt_tag}: clicked 'use email' button")
                    except Exception:
                        pass

                    email_sel = 'input[autocomplete="email"], input#signup-email, input[type="email"], input[name="email"]'
                    t_email = time.time()
                    Logger.write(f"{attempt_tag}: step_email | waiting for email field...")
                    with StageTimer("email_load", tag=attempt_tag, extra=timing):
                        page.locator(email_sel).first.wait_for(state="visible", timeout=waittoms)
                    timing["email_visible_ms"] = (time.time() - t_email) * 1000
                    Logger.write(f"{attempt_tag}: email visible in {timing['email_visible_ms']:.0f}ms")
                    with StageTimer("email_fill", tag=attempt_tag, extra=timing):
                        page.locator(email_sel).first.fill(email)
                        Logger.write(f"{attempt_tag}: filled email: {email}")
                        page.locator(email_sel).first.focus()
                        page.locator('button[type="submit"], button[data-a-target="signup-button"]').first.click(timeout=3000)
                    Logger.write(f"{attempt_tag}: step_submit_email | clicked signup after email")

                    email_retries = 0
                    while email_retries < 5:
                        page.wait_for_timeout(500)
                        border = page.evaluate("""() => {
                            const el = document.querySelector('input[type="email"]');
                            if (!el) return '';
                            const s = window.getComputedStyle(el);
                            return s.borderColor || '';
                        }""")
                        if not (border and "rgb" in border):
                            break
                        rgb = re.findall(r'\d+', border)
                        if not (len(rgb) >= 3 and int(rgb[0]) > 150 and int(rgb[1]) < 100 and int(rgb[2]) < 100):
                            break
                        Logger.write(f"{attempt_tag}: RED BORDER on email field ({border}) - domain flagged (retry {email_retries+1})")
                        domain_part = email.split("@")[1] if "@" in email else ""
                        if domain_part and domain_part not in BLOCKED_DOMAINS:
                            BLOCKED_DOMAINS.append(domain_part)
                            save_blocked_domains()
                        err(f"{attempt_tag} FLAGGED EMAIL DOMAIN: {domain_part} - replacing...")
                        new_email = get_inbox()
                        if not new_email:
                            err(f"{attempt_tag} no inbox after email block - skipping attempt, will retry with new proxy")
                            last_reason = "reason: all inboxes exhausted (email block)"
                            shutil.rmtree(user_dir, ignore_errors=True)
                            if p_line:
                                proxy_pool.release(p_line)
                            attempt += 1
                            email = None
                            break
                        email = new_email
                        Logger.write(f"{attempt_tag}: replacement email ({email_retries+1}): {email}")
                        step(f"{attempt_tag} replaced email: {email}")
                        page.locator(email_sel).first.fill("")
                        page.locator(email_sel).first.fill(email)
                        page.locator(email_sel).first.focus()
                        page.wait_for_timeout(random.randint(50, 150))
                        page.locator('button[type="submit"], button[data-a-target="signup-button"]').first.click(timeout=3000)
                        Logger.write(f"{attempt_tag}: clicked submit with replacement email")
                        page.wait_for_timeout(random.randint(200, 400))
                        email_retries += 1
                    if email is None:
                        continue
                    if email_retries >= 5:
                        Logger.write(f"{attempt_tag}: 5 email retries exhausted - all domains flagged, retrying with new proxy")
                        err(f"{attempt_tag} all emails flagged, retrying...")
                        last_reason = "reason: all emails flagged after 5 retries"
                        shutil.rmtree(user_dir, ignore_errors=True)
                        if p_line:
                            proxy_pool.release(p_line)
                        attempt += 1
                        continue

                    email, abort = handle_email_block(page, email, attempt_tag, email_sel)
                    if abort:
                        last_reason = "reason: email domain blocked, retrying"
                        shutil.rmtree(user_dir, ignore_errors=True)
                        if p_line:
                            proxy_pool.release(p_line)
                        attempt += 1
                        continue

                    page.wait_for_selector('input#signup-username', state='attached', timeout=15000)
                    Logger.write(f"{attempt_tag}: email submit confirmed, username field attached")

                    user_sel = 'input#signup-username, input[autocomplete="username"], input[name="username"]'
                    Logger.write(f"{attempt_tag}: step_username | waiting for username field...")
                    page.locator(user_sel).first.wait_for(state="visible", timeout=waittoms)
                    Logger.write(f"{attempt_tag}: username fill url={page.url}")
                    with StageTimer("username", tag=attempt_tag, extra=timing):
                        t_fill = time.time()
                        page.locator(user_sel).first.fill(username)
                        Logger.write(f"{attempt_tag}: username filled in {(time.time()-t_fill)*1000:.0f}ms")
                        Logger.write(f"{attempt_tag}: filled username: {username}")
                        page.wait_for_timeout(random.randint(50, 150))

                    with StageTimer("password", tag=attempt_tag, extra=timing):
                        page.locator('input#signup-password, input[type="password"]').first.fill(password)
                        Logger.write(f"{attempt_tag}: password fill url={page.url}")

                    Logger.write(f"{attempt_tag}: step_birthday | setting birthday...")
                    with StageTimer("birthday", tag=attempt_tag, extra=timing):
                        if not set_birthday(page, birth):
                            Logger.write(f"{attempt_tag}: birthday not fully set")
                        else:
                            Logger.write(f"{attempt_tag}: birthday set: {birth['month']} {birth['day']} {birth['year']}")
                    Logger.write(f"{attempt_tag}: total fill phase {(time.time()-t_fill)*1000:.0f}ms")
                    page.wait_for_timeout(random.randint(15, 40))
                    Logger.write(f"{attempt_tag}: pre-submit url={page.url}")

                    settle_lo = max(800, int(ms * 2))
                    settle_hi = min(4500, max(settle_lo + 1000, int(ms * 6)))
                    settle_delay = random.randint(min(settle_lo, settle_hi), max(settle_lo, settle_hi))
                    Logger.write(f"{attempt_tag}: step_submit_signup | waiting for React to settle ({settle_delay}ms)...")
                    otp_timeout = random.uniform(15.0, 16.0)
                    _otp_prewarm, _otp_prewarm_thr = start_otp_prewarm(order_id, timeout=otp_timeout)
                    page.wait_for_timeout(settle_delay)
                    pw_field = page.locator('input[type="password"]').first
                    try:
                        active = page.evaluate("() => document.activeElement?.type || ''")
                        if active != "password":
                            pw_field.focus()
                            page.wait_for_timeout(100)
                    except Exception:
                        pw_field.focus()
                        page.wait_for_timeout(100)
                    pw_field.press("Enter")
                    Logger.write(f"{attempt_tag}: pressed Enter on password field")
                    had_integrity_before_submit = bool(console_integrity)
                    if had_integrity_before_submit:
                        Logger.write(f"{attempt_tag}: pre-submit integrity events cleared ({sum(1 for _ in console_integrity)}), monitoring post-submit only")
                    console_integrity.clear()
                    t_submit = time.time()
                    submit_timeout = max(15, int(ms * 2 / 1000))
                    if console_severe:
                        Logger.write(f"{attempt_tag}: proxy flagged via console - quarantining")
                        signup_ok = "proxy_flagged"
                    else:
                        with StageTimer("submit", tag=attempt_tag, extra=timing):
                            signup_ok = submit_signup(page, console_integrity, None, None, timeout=submit_timeout)
                    submit_elapsed = (time.time() - t_submit) * 1000
                    Logger.write(f"{attempt_tag}: submit_signup took {submit_elapsed:.0f}ms, result={signup_ok}")

                    if signup_ok == "already_taken" or signup_ok == "integrity_error":
                        last_reason = f"reason: {signup_ok}"
                        Logger.write(f"{attempt_tag}: {signup_ok} - fast pivot")
                        if signup_ok == "integrity_error":
                            Logger.write(f"{attempt_tag}: real SRI integrity error - quarantining proxy")
                            proxy_pool.quarantine(p_line)
                            purge_proxy_file(p_line)
                        else:
                            proxy_pool.release(p_line)
                        shutil.rmtree(user_dir, ignore_errors=True)
                        attempt += 1
                        continue

                    if signup_ok == "proxy_flagged":
                        last_reason = "reason: proxy flagged by Twitch"
                        Logger.write(f"{attempt_tag}: proxy flagged - quarantining")
                        proxy_score.http_429 += 1
                        proxy_pool.quarantine(p_line)
                        purge_proxy_file(p_line)
                        shutil.rmtree(user_dir, ignore_errors=True)
                        p_line = None
                        attempt += 1
                        continue

                    if signup_ok == "blocked":
                        new_email, abort_after_block = handle_email_block(page, email, attempt_tag, email_sel)
                        if abort_after_block:
                            last_reason = "reason: email domain blocked after submit"
                            shutil.rmtree(user_dir, ignore_errors=True)
                            if p_line:
                                proxy_pool.release(p_line)
                            attempt += 1
                            continue
                        if new_email != email:
                            email = new_email
                            Logger.write(f"{attempt_tag}: fresh proxy assigned after domain block")
                            _422_flag[0] = False
                            t_fill = time.time()
                            page.locator(user_sel).first.fill(username)
                            page.locator('input#signup-password, input[type="password"]').first.fill(password)
                            for retry_i in range(BIRTHDAY_RETRY_MAX):
                                if set_birthday(page, birth):
                                    break
                                page.wait_for_timeout(BIRTHDAY_RETRY_DELAY_MS * (retry_i + 1))
                            else:
                                Logger.write(f"{attempt_tag}: birthday not fully set after retries")
                            page.wait_for_timeout(random.randint(50, 150))
                            t_submit = time.time()
                            signup_ok = submit_signup(page, console_integrity, None, None, timeout=submit_timeout)
                            Logger.write(f"{attempt_tag}: retry submit_signup took {(time.time()-t_submit)*1000:.0f}ms")
                        if not signup_ok and not _422_flag[0]:
                            try:
                                body = page.locator("body").inner_text() or ""
                                body_lower = body.lower()
                                if "use phone number" in body_lower or "phone number" in body_lower:
                                    Logger.write(f"{attempt_tag}: phone verification required - proxy flagged, aborting attempt")
                                    proxy_score.http_429 += 1
                                    if proxy_score.should_quarantine:
                                        proxy_pool.quarantine(p_line)
                                    err(f"{attempt_tag} signup did not submit - phone verification triggered")
                                elif "verification code" in body_lower:
                                    signup_ok = True
                                    Logger.write(f"{attempt_tag}: signup actually succeeded (OTP page detected late)")
                                else:
                                    err(f"{attempt_tag} signup did not submit")
                                    Logger.write(f"{attempt_tag}: page body = {body[:300]}")
                            except Exception:
                                err(f"{attempt_tag} signup did not submit")
                        if _422_flag[0]:
                            last_reason = "reason: proxy blocked (422)"
                            Logger.write(f"{attempt_tag}: 422 on blocked - proxy rejected, fast pivot")
                            err(f"{attempt_tag} PROXY BLOCKED (422) - removing proxy + new attempt incoming")
                            if p_line:
                                purge_proxy_file(p_line)
                                proxy_pool.quarantine(p_line)
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            shutil.rmtree(user_dir, ignore_errors=True)
                            attempt += 1
                            continue
                        if new_email == email and not signup_ok:
                            last_reason = "reason: proxy blocked - fresh retry"
                            Logger.write(f"{attempt_tag}: block without domain change - proxy issue, fast pivot")
                            err(f"{attempt_tag} PROXY BLOCKED - removing proxy + new attempt incoming")
                            if p_line:
                                purge_proxy_file(p_line)
                                proxy_pool.quarantine(p_line)
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            shutil.rmtree(user_dir, ignore_errors=True)
                            attempt += 1
                            continue

                    if not signup_ok and not console_severe and not console_connections:
                        last_reason = "reason: submit timeout - proxy/tunnel dead"
                        Logger.write(f"{attempt_tag}: submit_signup returned False - proxy/tunnel likely dead, immediate pivot")
                        proxy_score.timeout += 1
                        err(f"{attempt_tag} submit timeout (proxy dead) - fast pivot")
                        if p_line:
                            proxy_pool.quarantine(p_line)
                            purge_proxy_file(p_line)
                        p_line = None
                        try:
                            ctx.close()
                        except Exception:
                            pass
                        shutil.rmtree(user_dir, ignore_errors=True)
                        attempt += 1
                        continue

                    if signup_ok is True:
                        Logger.write(f"{attempt_tag}: signup submitted, fetching OTP immediately...")
                        step(f"{attempt_tag} OTP detected, fetching code now...")

                        if _otp_prewarm_thr and _otp_prewarm_thr.is_alive():
                            _otp_prewarm_thr.join(timeout=5.0)

                        page.wait_for_selector('input[inputmode="numeric"]', state='visible', timeout=5000)
                        try:
                            with StageTimer("otp_fetch", tag=attempt_tag, extra=timing):
                                if _otp_prewarm:
                                    code = _otp_prewarm[0]
                                else:
                                    code = fetch_otp(order_id)
                            if code:
                                Logger.write(f"{attempt_tag}: OTP received: {code}")
                                step(f"{attempt_tag} entering OTP code...")
                                with StageTimer("otp_fill", tag=attempt_tag, extra=timing):
                                    page.locator(
                                        'input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                                    ).first.wait_for(state="visible", timeout=15000)
                                    filled = page.evaluate(f"""(code) => {{
                                        const boxes = document.querySelectorAll('input[inputmode="numeric"], input[autocomplete="one-time-code"]');
                                        if (boxes.length >= 6) {{
                                            for (let i = 0; i < Math.min(code.length, 6); i++) {{
                                                boxes[i].value = code[i];
                                                boxes[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                                                boxes[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                                            }}
                                            if (boxes.length) boxes[boxes.length - 1].focus();
                                            return boxes.length;
                                        }}
                                        if (boxes.length >= 1) {{
                                            boxes[0].value = code;
                                            boxes[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
                                            boxes[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
                                            return 1;
                                        }}
                                        return 0;
                                    }}""", code)
                                    Logger.write(f"{attempt_tag}: OTP filled into {filled} boxes atomically")
                                    page.wait_for_timeout(200)
                                    page.keyboard.press("Enter")
                                    Logger.write(f"{attempt_tag}: submitted OTP via Enter, waiting for token...")
                            else:
                                Logger.write(f"{attempt_tag}: primary OTP missed, trying fallback...")
                                code = find_verification_code(order_id, timeout=otp_timeout)
                                if code:
                                    Logger.write(f"{attempt_tag}: fallback OTP received: {code}")
                                    step(f"{attempt_tag} entering OTP code (fallback)...")
                                    with StageTimer("otp_fill", tag=attempt_tag, extra=timing):
                                        page.locator(
                                            'input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                                        ).first.wait_for(state="visible", timeout=15000)
                                        filled = page.evaluate(f"""(code) => {{
                                            const boxes = document.querySelectorAll('input[inputmode="numeric"], input[autocomplete="one-time-code"]');
                                            if (boxes.length >= 6) {{
                                                for (let i = 0; i < Math.min(code.length, 6); i++) {{
                                                    boxes[i].value = code[i];
                                                    boxes[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                                                    boxes[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                                                }}
                                                if (boxes.length) boxes[boxes.length - 1].focus();
                                                return boxes.length;
                                            }}
                                            if (boxes.length >= 1) {{
                                                boxes[0].value = code;
                                                boxes[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
                                                boxes[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
                                                return 1;
                                            }}
                                            return 0;
                                        }}""", code)
                                        Logger.write(f"{attempt_tag}: OTP (fallback) filled into {filled} boxes atomically")
                                        page.wait_for_timeout(200)
                                        page.keyboard.press("Enter")
                                        Logger.write(f"{attempt_tag}: submitted fallback OTP via Enter, waiting for token...")
                                else:
                                    last_reason = f"reason: no OTP arrived after {otp_timeout:.1f}s"
                                    Logger.write(f"{attempt_tag}: no OTP arrived after {otp_timeout:.1f}s - fast pivot")
                                    err(f"{attempt_tag} no email code arrived")
                                    try:
                                        ctx.close()
                                    except Exception:
                                        pass
                                    proxy_pool.release(p_line)
                                    shutil.rmtree(user_dir, ignore_errors=True)
                                    attempt += 1
                                    continue
                        except Exception as e:
                            Logger.write(f"{attempt_tag}: OTP exception: {type(e).__name__}: {e}")

                    with StageTimer("token", tag=attempt_tag, extra=timing):
                        token = wait_for_token(ctx)
                    if not token:
                        page.wait_for_timeout(1500)
                        page.locator('button[type="submit"], button[data-a-target="signup-button"]').first.click(timeout=3000)
                        page.wait_for_timeout(2000)
                        token = wait_for_token(ctx, timeout=15)
                    if token:
                        proxy_score.success += 1
                        Logger.write(f"{attempt_tag}: auth-token received: {token[:10]}...")
                        save(email, username, password, token)
                        Progress.inc()
                        ok(f"{tag} successfully created: {email}")
                        if DELETE_INBOX_AFTER:
                            delete_inbox((email, order_id))
                        page.wait_for_timeout(random.randint(500, 800))
                        created = True
                        return True
                    else:
                        Logger.write(f"{attempt_tag}: no auth-token")
                        err(f"{attempt_tag} failed (no auth-token)")
                        if p_line and proxy_score.should_quarantine:
                            Logger.write(f"{attempt_tag}: ProxyScore QUARANTINE after auth-token fail - {proxy_score}")
                            proxy_pool.quarantine(p_line)
                        if os.getenv("DEBUG_SCREENSHOTS", "0") == "1":
                            try:
                                page.screenshot(path=os.path.join(HERE, f"dbg_fail_{slot}_at{attempt}.png"))
                            except Exception:
                                pass
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            ename = type(e).__name__
            if "TimeoutError" in ename or "TargetClosedError" in ename or "ERR_" in str(e) or "AttributeError" in ename:
                proxy_score.timeout += 1
                Logger.write(f"{attempt_tag}: {ename} | ProxyScore now={proxy_score}")
                time.sleep(random.uniform(1.0, 3.0))
                last_reason = f"reason: {ename}"
                err(f"{attempt_tag} browser/proxy died (timeout/closed) - retrying with new proxy")
                if proxy_score.should_quarantine:
                    Logger.write(f"{attempt_tag}: ProxyScore QUARANTINE - {proxy_score}")
                    proxy_pool.quarantine(p_line)
                    purge_proxy_file(p_line)
            else:
                last_reason = f"reason: {ename}"
                Logger.write(f"{attempt_tag}: fatal: {ename}: {e}")
                err(f"{attempt_tag} {ename} - retrying with new proxy")
            if p_line and not proxy_score.should_quarantine:
                proxy_pool.release(p_line)
    return False


def cleanup_temp_dirs():
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if name.startswith("pw_"):
                full = os.path.join(tmp, name)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
    except Exception:
        pass


def main():
    global proxy_pool
    cleanup_temp_dirs()
    StageTimer.reset()
    Logger.init(LOGFILE)
    if os.path.exists(LOGFILE):
        try:
            with open(LOGFILE, "w", encoding="utf-8") as f:
                pass
        except Exception:
            pass

    def _create_status_lines():
        created, goal = Progress.snapshot()
        if Progress.end_time > 0:
            el = Progress.end_time - Progress.start
        else:
            el = time.time() - Progress.start
        spa = el / created if created > 0 else 0.0
        return [
            f"---------- ({created}/{goal}) accounts created | time taken: {fmt_elapsed(el)} ----------",
            f"---------- average: {spa:.2f} secs/account ----------",
        ]
    set_status_lines_func(_create_status_lines)

    Logger.write(f"logs: {LOGFILE}")
    try:
        n = int(input(f"{CYAN}how many accounts do you want to create? {X}").strip())
    except Exception:
        print("invalid number")
        return
    if n <= 0:
        return
    try:
        workers_raw = input(f"{CYAN}how many chrome windows in parallel? {X}").strip() or "1"
        workers = int(workers_raw)
    except Exception:
        workers = 1
    workers = max(1, min(workers, n))

    init_proxies = build_proxy_pool(PROXIES_FILE)
    print(f"{CYAN}preparing proxies, inboxes...{X}")
    if not init_proxies:
        print(f"{RED}warning: no proxies{X}")
        Logger.write("warning: no proxies")
    proxy_pool = ProxyPool(init_proxies, quarantine_cooldown=PROXY_QUARANTINE_COOLDOWN)
    domains = fetch_kopechka_domains()
    CT_DOMAINS[:] = domains
    print(f"{CYAN}{len(init_proxies)} proxies loaded, {len(CT_DOMAINS)} email domains fetched{X}")
    if DELETE_INBOX_AFTER:
        freed = cleanup_stale_inboxes()
        if freed:
            Logger.write(f"cleared {freed} stale inboxes")
    print(f"{GREEN}ready. launching {workers} window(s)...{X}")
    Progress.reset()
    Progress.goal = n
    Progress.start = time.time()
    Progress.done_event.clear()
    Logger.write(f"===== run started | goal={n} | windows={workers} =====")
    pt = threading.Thread(target=progress_printer, daemon=True)
    pt.start()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        from concurrent.futures import as_completed
        futures = [ex.submit(create_one, i + 1) for i in range(n)]
        for f in as_completed(futures):
            f.result()

    Progress.done_event.set()
    pt.join(timeout=0.15)
    Logger.write(f"===== run finished | {' | '.join(_create_status_lines())} =====")
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
