import os
import platform
import random
import subprocess
import re
import time
import threading
import statistics
import requests
from typing import Optional, Dict, List

_re_proto = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_re_ansi = re.compile(r"\033\[[0-9;]*m")

RED = "\033[91m"
GREEN = "\033[92m"
PURPLE = "\033[95m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
X = "\033[0m"
C_ORANGE = "\033[38;5;208m"



def speed_color(ms):
    if ms <= 500:
        return GREEN
    if ms <= 800:
        return CYAN
    if ms <= 1200:
        return YELLOW
    if ms <= 2000:
        return C_ORANGE
    return RED



def normalize_proxy(proxy):
    if not proxy:
        return None
    p = proxy.strip()
    if p in ("", "your_proxy"):
        return None
    if _re_proto.match(p):
        return p
    if "@" in p:
        return f"http://{p}"
    parts = p.split(":")
    if len(parts) >= 2:
        port = parts[-1]
        host = parts[-2]
        creds = ":".join(parts[:-2]) if len(parts) > 2 else ""
        if creds:
            return f"http://{creds}@{host}:{port}"
        else:
            return f"http://{host}:{port}"
    return f"http://{p}"


def parse_proxy_for_pw(proxy_line, sid=""):
    line = proxy_line.strip()
    if not line:
        return None
    if "://" in line:
        line = line.split("://", 1)[1]
    user = None
    pw = None
    if "@" in line:
        creds, hp = line.rsplit("@", 1)
        if ":" in creds:
            user, pw = creds.split(":", 1)
    else:
        parts = line.split(":")
        if len(parts) >= 4:
            hp = f"{parts[0]}:{parts[1]}"
            user = parts[2]
            pw = parts[3]
        else:
            hp = line
    if user and sid:
        user = user + sid
    if user is not None:
        return {"server": f"http://{hp}", "username": user, "password": pw}
    return {"server": f"http://{hp}"}



def build_proxy_pool(proxies_file):
    try:
        with open(proxies_file, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    except Exception:
        return []


class ProxyPool:

    def __init__(self, proxies_list, quarantine_cooldown=30):
        self.lock = threading.Lock()
        self.quarantine_lock = threading.Lock()
        self.pool = list(proxies_list)
        self.quarantined = {}
        self.cooldown = quarantine_cooldown

    def acquire(self):
        now = time.time()
        with self.lock:
            with self.quarantine_lock:
                for p, release_time in list(self.quarantined.items()):
                    if now >= release_time:
                        self.pool.append(p)
                        del self.quarantined[p]
            if not self.pool:
                with self.quarantine_lock:
                    if self.quarantined:
                        for p, release_time in list(self.quarantined.items()):
                            if now >= release_time:
                                self.pool.append(p)
                                del self.quarantined[p]
                if not self.pool:
                    return None
            idx = random.randrange(len(self.pool))
            return self.pool.pop(idx)

    def release(self, proxy_line):
        if not proxy_line:
            return
        with self.lock:
            with self.quarantine_lock:
                if proxy_line not in self.quarantined and proxy_line not in self.pool:
                    self.pool.append(proxy_line)

    def quarantine(self, bad_proxy, cooldown_seconds=None):
        if not bad_proxy:
            return
        cd = cooldown_seconds if cooldown_seconds is not None else self.cooldown
        with self.lock:
            if bad_proxy in self.pool:
                self.pool.remove(bad_proxy)
        with self.quarantine_lock:
            self.quarantined[bad_proxy] = time.time() + cd

    def __len__(self):
        with self.lock:
            return len(self.pool)



def check_proxy_speed(proxy_line, timeout=3.0):
    try:
        pw_proxy = parse_proxy_for_pw(proxy_line)
        if not pw_proxy:
            return False, 0
        server = pw_proxy.get("server", "")
        user = pw_proxy.get("username")
        pwd = pw_proxy.get("password")
        if user and pwd:
            proxy_url = f"http://{user}:{pwd}@{server.replace('http://', '')}"
        else:
            proxy_url = server
        t0 = time.time()
        r = requests.head("http://clients3.google.com/generate_204", proxies={"http": proxy_url, "https": proxy_url}, timeout=timeout)
        elapsed = time.time() - t0
        r.close()
        return True, elapsed
    except Exception:
        return False, 0



def dynamic_timeout(proxy_ms, multiplier=10, floor=15000, ceiling=90000):
    return min(max(int(proxy_ms * multiplier), floor), ceiling)



PROXY_USES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "info", "proxy-uses.txt")
PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "proxies.txt")
_proxy_uses_lock = threading.RLock()


def proxy_key(proxy_line):
    try:
        p = proxy_line.strip()
        if not p:
            return None
        if "://" in p:
            p = p.split("://", 1)[1]
        p = p.strip()
        if not p:
            return None
        return p
    except Exception:
        return None


def record_proxy_use(proxy_line, max_uses=10):
    key = proxy_key(proxy_line)
    if not key:
        return False
    with _proxy_uses_lock:
        os.makedirs(os.path.dirname(PROXY_USES_FILE), exist_ok=True)
        entries = {}
        if os.path.exists(PROXY_USES_FILE):
            with open(PROXY_USES_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if " | " in line:
                        k, v = line.split(" | ", 1)
                        entries[k] = int(v)
        count = entries.get(key, 0) + 1
        entries[key] = count
        with open(PROXY_USES_FILE, "w", encoding="utf-8") as f:
            for k, v in entries.items():
                f.write(f"{k} | {v}\n")
        if count >= max_uses:
            purge_proxy_file(proxy_line, key)
            return True
    return False


def purge_proxy_file(proxy_line, key=None):
    if key is None:
        key = proxy_key(proxy_line)
    if not key:
        return
    with _proxy_uses_lock:
        for path in (PROXY_USES_FILE, PROXIES_FILE):
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                if path.endswith("proxy-uses.txt"):
                    new_lines = [l for l in lines if not l.startswith(key + " | ")]
                else:
                    new_lines = [l for l in lines if key not in l]
                if len(new_lines) < len(lines):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
            except Exception:
                pass



_stage_data = {}
_stage_lock = threading.Lock()


class StageTimer:
    def __init__(self, stage_name, tag="", extra=None):
        self.stage = stage_name
        self.tag = tag
        self.extra = extra
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *args):
        elapsed_ms = (time.perf_counter() - self._t0) * 1000.0
        with _stage_lock:
            _stage_data.setdefault(self.stage, []).append(elapsed_ms)
        if self.extra is not None:
            self.extra[self.stage] = elapsed_ms

    @classmethod
    def report(cls):
        with _stage_lock:
            if not _stage_data:
                return "  (no timing data collected)"
            lines = []
            ranked = sorted(_stage_data.items(), key=lambda kv: statistics.mean(kv[1]), reverse=True)
            for name, vals in ranked:
                if len(vals) >= 2:
                    avg = statistics.mean(vals)
                    med = statistics.median(vals)
                    try:
                        p95 = sorted(vals)[int(len(vals) * 0.95)] if len(vals) >= 20 else max(vals)
                    except Exception:
                        p95 = max(vals)
                    lines.append(
                        f"  {name:22s} avg={avg:7.0f}ms  med={med:7.0f}ms  p95={p95:7.0f}ms  "
                        f"n={len(vals):3d}  min={min(vals):7.0f}ms  max={max(vals):7.0f}ms"
                    )
                else:
                    lines.append(f"  {name:22s} avg={vals[0]:7.0f}ms  n={len(vals):3d}")
            return "\n".join(lines)

    @classmethod
    def reset(cls):
        with _stage_lock:
            _stage_data.clear()



SCORE_NAV_SLOW_THRESHOLD = 8000
SCORE_CONSECUTIVE_ERR_THRESHOLD = 3
SCORE_HTTP_422_THRESHOLD = 2
SCORE_HTTP_429_THRESHOLD = 2


def _load_score_config():
    global SCORE_NAV_SLOW_THRESHOLD, SCORE_CONSECUTIVE_ERR_THRESHOLD
    global SCORE_HTTP_422_THRESHOLD, SCORE_HTTP_429_THRESHOLD
    try:
        from config import Config
        SCORE_NAV_SLOW_THRESHOLD = getattr(Config, "SCORE_NAV_SLOW_THRESHOLD", SCORE_NAV_SLOW_THRESHOLD)
        SCORE_CONSECUTIVE_ERR_THRESHOLD = getattr(Config, "SCORE_CONSECUTIVE_ERR_THRESHOLD", SCORE_CONSECUTIVE_ERR_THRESHOLD)
        SCORE_HTTP_422_THRESHOLD = getattr(Config, "SCORE_HTTP_422_THRESHOLD", SCORE_HTTP_422_THRESHOLD)
        SCORE_HTTP_429_THRESHOLD = getattr(Config, "SCORE_HTTP_429_THRESHOLD", SCORE_HTTP_429_THRESHOLD)
    except Exception:
        pass


_load_score_config()


class ProxyScore:
    __slots__ = ("nav_ms", "console_err", "slow_network", "http_422",
                 "http_429", "timeout", "success", "quarantined_at")

    def __init__(self):
        self.nav_ms = None
        self.console_err = 0
        self.slow_network = 0
        self.http_422 = 0
        self.http_429 = 0
        self.timeout = 0
        self.success = 0
        self.quarantined_at = None

    @property
    def negative_score(self):
        score = 0
        if self.nav_ms is not None and self.nav_ms > SCORE_NAV_SLOW_THRESHOLD:
            score += 1
        if self.console_err >= SCORE_CONSECUTIVE_ERR_THRESHOLD:
            score += 1
        if self.slow_network >= SCORE_CONSECUTIVE_ERR_THRESHOLD:
            score += 1
        if self.http_422 >= SCORE_HTTP_422_THRESHOLD:
            score += 1
        if self.http_429 >= SCORE_HTTP_429_THRESHOLD:
            score += 1
        if self.timeout >= 1:
            score += 1
        return score

    @property
    def should_quarantine(self):
        return self.negative_score >= 2 and self.success == 0

    @property
    def is_healthy(self):
        return self.success > 0 and self.negative_score <= 1

    def __repr__(self):
        return (f"ProxyScore(nav={self.nav_ms}ms, err={self.console_err}, "
                f"422={self.http_422}, 429={self.http_429}, to={self.timeout}, "
                f"ok={self.success}, neg={self.negative_score}, "
                f"quar={'YES' if self.should_quarantine else 'no'})")


_score_registry = {}
_score_lock = threading.Lock()


def get_proxy_score(proxy_line):
    raw = proxy_line.split("://", 1)[-1] if "://" in proxy_line else proxy_line
    key = raw
    with _score_lock:
        if key not in _score_registry:
            _score_registry[key] = ProxyScore()
        return _score_registry[key]


def score_report():
    with _score_lock:
        if not _score_registry:
            return "  (no proxy scores recorded)"
        entries = sorted(_score_registry.items(), key=lambda kv: kv[1].negative_score, reverse=True)
        lines = []
        for key, sc in entries:
            lines.append(f"  {key[:50]:50s}  {sc}")
        return "\n".join(lines)



class Logger:

    _instance = None
    _init_lock = threading.Lock()

    def __init__(self, path):
        self.path = path
        self.queue = []
        self.lock = threading.Lock()
        self.done = threading.Event()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    @classmethod
    def init(cls, path):
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = cls(path)
            return cls._instance

    @classmethod
    def write(cls, line):
        inst = cls._instance
        if inst is None:
            return
        try:
            clean = _re_ansi.sub("", line)
            with inst.lock:
                inst.queue.append(time.strftime("%Y-%m-%d %H:%M:%S ") + clean)
        except Exception:
            pass

    @classmethod
    def flush_and_stop(cls):
        inst = cls._instance
        if inst is None:
            return
        inst.done.set()
        inst._writer.join(timeout=2.0)
        with inst.lock:
            if inst.queue:
                try:
                    with open(inst.path, "a", encoding="utf-8") as f:
                        f.write("\n".join(inst.queue) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:
                    pass
                inst.queue.clear()

    def _write_loop(self):
        while not self.done.is_set() or (self.queue if self.lock else True):
            with self.lock:
                batch = self.queue[:]
                self.queue.clear()
            if batch:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write("\n".join(batch) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:
                    pass
            self.done.wait(timeout=0.5)



class Progress:
    created = 0
    goal = 0
    start = 0.0
    end_time = 0.0
    done_event = threading.Event()
    goal_reached = threading.Event()
    lock = threading.Lock()

    @classmethod
    def inc(cls):
        with cls.lock:
            if cls.created < cls.goal:
                cls.created += 1
                if cls.created >= cls.goal and cls.end_time == 0.0:
                    cls.end_time = time.time()
                    cls.goal_reached.set()

    @classmethod
    def snapshot(cls):
        with cls.lock:
            return cls.created, cls.goal

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.created = 0
            cls.goal = 0
            cls.start = 0.0
            cls.end_time = 0.0
            cls.done_event.clear()
            cls.goal_reached.clear()


_print_lock = threading.Lock()


def fmt_elapsed(sec):
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h}h {m}m {s}s"


def _default_status_lines(label="created"):
    created, goal = Progress.snapshot()
    if Progress.end_time > 0:
        el = Progress.end_time - Progress.start
    else:
        el = time.time() - Progress.start
    spa = el / created if created > 0 else 0.0
    return [
        f"---------- ({created}/{goal}) {label} | time taken: {fmt_elapsed(el)} ----------",
        f"---------- average: {spa:.2f} secs/account ----------",
    ]


_footer_drawn = False
_status_lines_func = _default_status_lines


def set_status_lines_func(fn):
    global _status_lines_func
    _status_lines_func = fn


def _footer_buf(prefix=""):
    global _footer_drawn
    lines = _status_lines_func()
    buf = ""
    if _footer_drawn:
        buf += "\r" + (f"\033[{len(lines) - 1}A" if len(lines) > 1 else "") + "\033[J"
    buf += prefix + os.linesep.join(lines)
    _footer_drawn = True
    return buf


_emit_lock = threading.Lock()


def emit_raw(line):
    with _print_lock:
        print(_footer_buf(line + "\n"), end="", flush=True)
    Logger.write(line)



_spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spinner_idx = 0


def spinner_char():
    global _spinner_idx
    c = _spinner_chars[_spinner_idx % len(_spinner_chars)]
    _spinner_idx += 1
    return c



def ok(m):
    emit_raw(f"{GREEN}[+] {m}{X}")


def err(m):
    emit_raw(f"{RED}[-] {m}{X}")


def step(m):
    emit_raw(f"{PURPLE}[~] {m}{X}")


def info(m):
    emit_raw(f"{CYAN}[*] {m}{X}")


def warn(m):
    emit_raw(f"{YELLOW}[!] {m}{X}")


def spin_info(m):
    emit_raw(f"{CYAN}[{spinner_char()}] {m}{X}")



def progress_printer():
    while not Progress.done_event.is_set():
        with _print_lock:
            print(_footer_buf(), end="", flush=True)
        Progress.done_event.wait(timeout=0.15)



def flush_stdin():
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        else:
            import sys as _s
            import termios
            termios.tcflush(_s.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def pause_exit():
    flush_stdin()
    print(f"\n{GREEN}done! press [ENTER] to exit...{X}")
    import sys
    sys.stdout.flush()
    if os.name == "nt":
        import msvcrt
        while True:
            if msvcrt.kbhit():
                if msvcrt.getch() == b'\r':
                    break
    else:
        import sys as _s
        _s.stdin.readline()
    os._exit(0)



def clear_screen():
    system = platform.system().lower()
    if system in ("linux", "darwin"):
        subprocess.call("clear")
    elif system == "windows":
        subprocess.call("cls", shell=True)
    else:
        print("Unsupported platform")


def generate_random_id(length):
    charset = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(charset) for _ in range(length))


if __name__ == "__main__":
    print("generate_random_id(8):", generate_random_id(8))
    with StageTimer("test_stage", tag="smoke"):
        time.sleep(0.05)
    print("StageTimer report:\n" + StageTimer.report())
    ps = get_proxy_score("http://user:pass@1.2.3.4:8080")
    ps.nav_ms = 3500
    ps.http_429 = 3
    ps.console_err = 2
    print(f"ProxyScore test: {ps}")
    print(f"  should_quarantine={ps.should_quarantine}")
    print("score_report:\n" + score_report())
