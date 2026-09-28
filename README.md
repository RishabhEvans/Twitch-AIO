# twitch-aio

An all-in-one Twitch automation toolkit. Create accounts, follow channels and send chat messages from a pool of accounts, with built-in proxy management and a stealth browser engine.

Three small scripts, one shared foundation, no external services beyond a mail provider and your own proxies.

## What's inside

| Script | What it does |
| --- | --- |
| `create.py` | Creates Twitch accounts in parallel. Handles disposable inboxes and OTP codes automatically, and saves every working login. |
| `follow.py` | Follows a channel from any number of saved accounts, paced and proxy-aware, with a cache so nothing is followed twice. |
| `chat.py` | Sends chat messages from saved accounts, cycling through the message files in `texts/`. |

All three share the same foundation from `utils.py`:

- **Proxy pool** with speed checks, per-proxy usage limits, scoring and automatic quarantine of slow or failing proxies.
- **Parallel workers** with a configurable number of browser windows.
- **Live progress** with per-stage timing, and a full log written to `logs.txt`.
- **Headless mode** for servers (`HEADLESS=1`).

The browser automation runs on [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright), a stealth-patched build of Playwright. Everything else is plain `requests` and the standard library.

## Requirements

- Python 3.9 or newer
- Google Chrome installed (or the bundled Chromium build, see below)
- A Kopechka API key for inbox creation (`create.py` only)

## Install

```bash
git clone https://github.com/RishabhEvans/twitch-aio.git
cd twitch-aio
pip install -r requirements.txt
patchright install chrome
```

The code defaults to the installed Chrome on both Windows and Linux. To use the bundled Chromium instead, run `patchright install chromium` and set `BROWSER_CHANNEL=chromium`.

## Configure

**1. Mail provider.** Open `config.py` and set your Kopechka API key:

```python
self.KopechkaApiKey = "YOUR_KOPECHKA_API_KEY_HERE"
```

The key is used to create disposable inboxes and fetch OTP codes during account creation. The list of blocked email domains in the same file is pre-tuned; edit it if you want to allow or exclude specific providers.

**2. Proxies.** Put one proxy per line in `results/proxies.txt`. All of these formats work:

```
host:port
host:port:user:pass
user:pass@host:port
http://user:pass@host:port
```

Blank lines and `#` comments are ignored. Proxies are speed-checked on start, scored during use, and quarantined automatically when they start failing. Per-proxy usage is tracked in `info/proxy-uses.txt`.

**3. Chat messages.** Drop one or more `.txt` files into `texts/`. `chat.py` asks which file to send from. One message per line.

## Usage

### Create accounts

```bash
python create.py
```

You will be asked how many accounts to create and how many browser windows to run in parallel. Each account is created end to end: signup, inbox, OTP, and the finished login is appended to:

- `results/accounts.txt` in `email:username:password:token` format
- `results/tokens.txt` with the auth tokens only

### Follow a channel

```bash
python follow.py
```

You will be asked for the channel name, how many follows to send, and the parallel window count. Pairs that already followed a channel are cached in `info/follows.txt` (`token | channels`) so the same account is never used twice for the same channel.

### Chat

```bash
python chat.py
```

Pick a channel, a session duration, how many accounts to use, and which message file to send from. Messages are cycled across accounts for the length of the session.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HEADLESS` | `0` | Set to `1` to run browsers headless. |
| `BROWSER_CHANNEL` | `chrome` on Windows, `chromium` elsewhere | Browser build to drive. |
| `KEEP_INBOX` | `0` | Set to `1` to keep inboxes after account creation. |
| `PROXY_SPEED_TIMEOUT` | `8.0` | Per-proxy speed check timeout, in seconds. |

## Project layout

```
twitch-aio/
├── create.py           Account creation
├── follow.py           Follow automation
├── chat.py             Chat automation
├── config.py           Settings and tuning values
├── utils.py            Shared helpers: proxies, logging, progress
├── requirements.txt
├── info/
│   ├── follows.txt     Follow cache (token | channels)
│   └── proxy-uses.txt  Per-proxy usage counter
├── results/
│   ├── accounts.txt    Created accounts (output)
│   ├── tokens.txt      Auth tokens (output)
│   └── proxies.txt     Proxy list (input)
├── texts/              Chat message files
└── logs.txt            Run log
```

## Notes

- Run the scripts from the project root; all paths are resolved relative to the scripts themselves.
- Timeouts, proxy limits, scoring thresholds and username blocklists are all adjustable at the top of `config.py`.
- Account creation depends on the mail provider being up. The OTP step retries a few times before pivoting to a fresh inbox.
- If a run is interrupted, progress is not lost: finished accounts stay in `results/accounts.txt` and finished follows stay in `info/follows.txt`.

## Disclaimer

Provided as-is, for automation research and testing on accounts and infrastructure you control. You are responsible for how you use it.
