BLOCKED_DOMAINS = ['ditapi.info', 'ditplay.info', 'ditgame.info', 'areueally.info', 'junkstopper.info', 'ditcloud.info', 'attachmy.site', 'ditube.info', 'addmy.space', 'ditdrive.info', 'cybertemp.xyz', 'tempymail.com', '10minute.email', 'michaeladmin.store', 'michaeladmin.org', 'yummymeow.org', 'michaeladmin.eu', 'yummymeow.store', 'michaeladmin.fr', 'yummymeow.eu', 'yummymeow.fr', 'picturehostel.fr', 'andrewcluh.top', 'test1byali.cfd', 'lazerbade.org', 'hotelpiesi.pro', 'picturehostel.org', 'hotelplazer.com', '6974.click', 'mm2flip.pw', 'ionolenonos.fr', 'hazmat.help', 'funnylolcap.org', 'xeno.gay', 'nicusaurelus.online', 'selltub.com', 'siperer.online', 'loganister.com', 'pivotylabs.pro', 'hotelplazer.org', 'atombomb.online', 'fazka.ovh', 'sexymeowers.store', 'oshensantaeulalia.com', 'noinspect.top', 'lazerbade.store', 'sexfrancecute.tattoo', 'soniot.online', 'xpromail.xyz', 'barnrestaurant.pro', 'sexfrancecute.lol', 'sexymeowers.site', 'funnylolcap.fr', 'lazerbade.fr', 'arrabyte.com', 'caprasolver.xyz', 'ionolenonos.store', 'sexymeowers.space', 'memoniclife.online', 'sexfrancecute.xyz', 'sexymeowers.xyz', 'xen-bypass.com', 'funnylolcap.store', 'evomiserver.com', 'waterpalace.pro', 'blakeagency.buzz', 'rgbeastcomputers.site', 'securitybot.online', 'ytoof.app', 'diddyricky.fr', 'rexabot.com', 'diddyricky.com', 'dwiworka.pro', 'hotelplazer.store', 'selltub.me', 'fruitservice.xyz', 'sevopay.com', 'planworker.pro', 'hotelplazer.fr', 'diddyricky.info', 'picturehostel.com', 'soniot.store', 'picturehostel.store', 'emziegz.com', 'cybertemp.cfd', 'diddyricky.store', 'ionolenonos.com', 'funnylolcap.com', 'ionolenonos.info', 'lovertboy.online', 'whitemistral.pro', 'parthekio.live', 'kmail123.com', 'xoxo3.website', 'soniot.fr', 'noshiper.pro', 'lazerbade.com', 'neverover.life', 'ogoplace.pro', 'ashnkarsh.life', 'atelierdelune.shop', 'naniniva.com', 'crazy.hcap.ai', 'jamyjmkqdsc.com', 'foothilltoy.site', 'nexus-streams.online', 'soniot.info', 'acidservices.xyz', 'reusecomputers.site']

PROXY_SPEED_CHECK_TIMEOUT = 3.0
PROXY_NAV_TIMEOUT_FLOOR = 20000
PROXY_NAV_TIMEOUT_CEILING = 60000
PROXY_WAIT_TIMEOUT_FLOOR = 25000
PROXY_WAIT_TIMEOUT_CEILING = 60000
PROXY_QUARANTINE_COOLDOWN = 30

PROXY_USE_MAX = 5
PROXY_NAV_MULTIPLIER = 30
PROXY_WAIT_MULTIPLIER = 15

SCORE_NAV_SLOW_THRESHOLD = 8000
SCORE_CONSECUTIVE_ERR_THRESHOLD = 3
SCORE_HTTP_422_THRESHOLD = 2
SCORE_HTTP_429_THRESHOLD = 2

OTP_WAIT_BEFORE_PIVOT = 15
OTP_FETCH_TIMEOUT = 60

BIRTHDAY_RETRY_MAX = 5
BIRTHDAY_RETRY_DELAY_MS = 500

BLOCKED_USERNAME_SUBS = (
    "pus", "dick", "dic", "fuk", "fuc", "coc", "cok", "sht", "tit", "fag",
    "nig", "kum", "cum", "ass", "sex", "xxx", "fap", "jiz", "wank", "porn",
    "slut", "bich", "dild", "vagin", "penis", "puss", "cock", "shit",
    "fuck", "cnut", "cunt", "twat", "paki", "spic", "kike", "rape",
    "nazi", "hitle", "stalin", "anus", "butt", "boob", "tits",
    "phuk", "phuc", "phuq", "fck", "fkc", "fukk", "fcu", "fcu",
    "sht", "shyt", "sh1t", "5hit",
    "n1g", "n1gg", "nigg", "niga", "n1ga", "ngga",
    "k1ke", "kyke",
    "c0ck", "c0k", "d1ck", "d1c",
    "p0rn", "pr0n", "p0rn",
    "5ex", "s3x", "seks", "s4x",
    "b00b", "b00bs", "t1ts",
    "w4nk", "w4nker",
    "mastur", "masterb",
    "puss1", "pussii",
    "pen1s", "pen1", "p3nis",
    "v4g", "v4gina", "vulva",
    "a55", "a5s", "4ss",
    "azz", "ahole", "a55hole",
    "1488", "wpww", "wpwd", "148",
    "heil",
    "kkk", "kuklux",
    "crack", "hero1n", "heroin", "c0caine", "cocain",
    "meth", "mdma",
    "admin", "moderator", "tw1tch", "twitchstaff", "staff",
    "support", "official",
    "verify", "verified",
    "security", "secure",
    "giveaway", "freegift", "freenitro",
)


class Configuration:
    def __init__(self):
        self.CybertempApiBase = "https://api.cybertemp.xyz"
        self.CybertempApiKey = "tk_76198a6f2f873438a6bb68158b2c4ff43da3523d8cf6f8f0578a42f1c861947e"
        self.CybertempDomainsCache = []
        # Scoring thresholds exposed for utils.py ProxyScore
        self.SCORE_NAV_SLOW_THRESHOLD = SCORE_NAV_SLOW_THRESHOLD
        self.SCORE_CONSECUTIVE_ERR_THRESHOLD = SCORE_CONSECUTIVE_ERR_THRESHOLD
        self.SCORE_HTTP_422_THRESHOLD = SCORE_HTTP_422_THRESHOLD
        self.SCORE_HTTP_429_THRESHOLD = SCORE_HTTP_429_THRESHOLD

    def validate(self):
        warnings = []
        if not self.CybertempApiKey:
            warnings.append("CYBERTEMP_API_KEY is empty - inbox creation will fail")
        return warnings


import random as _random


def fetch_cybertemp_domains():
    try:
        r = __import__('requests').get(
            f"{Config.CybertempApiBase}/getDomains",
            headers={"X-API-KEY": Config.CybertempApiKey},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            domains = []
            for item in data:
                if isinstance(item, dict):
                    d = item.get("domain") or item.get("name") or ""
                    status = str(item.get("status", "")).lower()
                    if d and ("active" in status or "online" in status) and d not in BLOCKED_DOMAINS:
                        domains.append(d)
                elif isinstance(item, str):
                    if item not in BLOCKED_DOMAINS:
                        domains.append(item)
            if domains:
                _random.shuffle(domains)
                Config.CybertempDomainsCache = domains
                return domains
    except Exception:
        pass
    return Config.CybertempDomainsCache or ["cybertemp.xyz"]


Config = Configuration()