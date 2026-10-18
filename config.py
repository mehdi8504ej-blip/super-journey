import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# ---------- Basic settings ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_NAME = os.getenv("BOT_NAME", "GrowGram")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}
DB_PATH = os.getenv("DB_PATH", "data/growgram.db")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "your_support").lstrip("@")

# ---------- Payment (card to card) ----------
CARD_NUMBER = os.getenv("CARD_NUMBER", "6037-0000-0000-0000")
CARD_OWNER = os.getenv("CARD_OWNER", "نام صاحب کارت")

# ---------- Credit economy ----------
# 1 member delivered to an advertiser costs COST_PER_MEMBER credits.
# A participant earns JOIN_REWARD credits per verified join.
# With 2 / 1: two joins by participants = one free member for yourself.
COST_PER_MEMBER = _int("COST_PER_MEMBER", 2)
JOIN_REWARD = _int("JOIN_REWARD", 1)
WELCOME_BONUS = _int("WELCOME_BONUS", 4)
REFERRAL_BONUS = _int("REFERRAL_BONUS", 3)  # paid to the inviter after the invitee's first verified join

# ---------- Campaign limits ----------
MIN_CAMPAIGN = _int("MIN_CAMPAIGN", 20)
MAX_CAMPAIGN = _int("MAX_CAMPAIGN", 10000)
CAMPAIGN_PRESETS = [50, 100, 250, 500, 1000]
MAX_CHANNELS_PER_USER = _int("MAX_CHANNELS_PER_USER", 10)

# ---------- Anti abuse ----------
DAILY_TASK_LIMIT = _int("DAILY_TASK_LIMIT", 60)  # max verified joins per user per UTC day
TASK_COOLDOWN = _int("TASK_COOLDOWN", 4)  # seconds between verified joins
RECHECK_DAYS = _int("RECHECK_DAYS", 3)  # members who leave before this get penalized
REPORT_LIMIT = _int("REPORT_LIMIT", 5)  # reports needed to auto-pause a campaign

# ---------- Price list (in Toman) ----------
# Credits delivered = members * COST_PER_MEMBER
PACKAGES = [
    {"members": 100, "price": 80_000},
    {"members": 500, "price": 300_000},
    {"members": 1000, "price": 500_000},
    {"members": 5000, "price": 2_000_000},
]
