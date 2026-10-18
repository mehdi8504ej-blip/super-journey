import asyncio
import html
import logging
import re

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import DB

log = logging.getLogger("growgram")
router = Router()
db: DB = None  # type: ignore  (initialised in main)
BOT_USERNAME = ""
skips: dict[int, set[int]] = {}  # per-user skipped campaigns (in memory)

# ------------------------------------------------------------------ labels
BTN_EARN = "💰 کسب اعتبار"
BTN_PROMO = "📢 تبلیغ کانال/گروه"
BTN_CHANNELS = "📂 کانال‌های من"
BTN_CAMPS = "📊 کمپین‌های من"
BTN_BUY = "💳 خرید اعتبار"
BTN_ACCOUNT = "👤 حساب من"
BTN_REF = "🎁 دعوت دوستان"
BTN_SUPPORT = "☎️ پشتیبانی"
MENU_BUTTONS = {BTN_EARN, BTN_PROMO, BTN_CHANNELS, BTN_CAMPS, BTN_BUY, BTN_ACCOUNT, BTN_REF, BTN_SUPPORT}


# ------------------------------------------------------------------ helpers
def esc(s) -> str:
    return html.escape(str(s or ""))


def money(n: int) -> str:
    return f"{n:,}"


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def normalize_digits(s: str) -> str:
    return s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_EARN), KeyboardButton(text=BTN_PROMO)],
            [KeyboardButton(text=BTN_CHANNELS), KeyboardButton(text=BTN_CAMPS)],
            [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_ACCOUNT)],
            [KeyboardButton(text=BTN_REF), KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
    )


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest as e:
        if "user not found" in str(e).lower() or "participant" in str(e).lower():
            return False
        raise
    if m.status in ("creator", "administrator", "member"):
        return True
    if m.status == "restricted":
        return bool(getattr(m, "is_member", False))
    return False


def user_link(u) -> str:
    name = esc(u.full_name or u.username or u.id)
    return f'<a href="tg://user?id={u.id}">{name}</a>'


async def safe_send(bot: Bot, uid: int, text: str, **kw):
    try:
        await bot.send_message(uid, text, **kw)
    except TelegramAPIError:
        pass


# ------------------------------------------------------------------ middleware
class GuardMiddleware(BaseMiddleware):
    """Blocks banned users and registers everybody else (except /start, which handles referrals)."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not user.is_bot:
            is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
            if not is_start:
                db.upsert_user(user.id, user.username, user.full_name)
            row = db.get_user(user.id)
            if row and row["banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ دسترسی شما به ربات مسدود شده است.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⛔ دسترسی شما به ربات مسدود شده است.")
                return
        return await handler(event, data)


# ------------------------------------------------------------------ FSM
class AddCh(StatesGroup):
    wait = State()


class CustomN(StatesGroup):
    wait = State()


class Buy(StatesGroup):
    receipt = State()


# ================================================================== START / BASIC
@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ref = None
    if command.args and command.args.startswith("ref_"):
        try:
            ref = int(command.args[4:])
        except ValueError:
            ref = None
    user, is_new = db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name, ref)
    if user["banned"]:
        return await m.answer("⛔ دسترسی شما به ربات مسدود شده است.")
    text = (
        f"👋 به <b>{esc(config.BOT_NAME)}</b> خوش آمدید!\n\n"
        "اینجا می‌توانید کانال یا گروه تلگرامی‌تان را رشد دهید:\n"
        "💰 با عضو شدن در کانال‌های دیگران <b>اعتبار رایگان</b> بگیرید\n"
        "💳 یا اعتبار بخرید و فوراً عضو جذب کنید\n"
        "📢 و با اعتبار خود، کمپین بزنید\n\n"
        f"💎 قانون ساده: هر عضو شدن = {config.JOIN_REWARD} اعتبار | "
        f"هر عضو جدید برای شما = {config.COST_PER_MEMBER} اعتبار"
    )
    if is_new and config.WELCOME_BONUS:
        text += f"\n\n🎁 هدیه‌ی ورود: <b>{config.WELCOME_BONUS}</b> اعتبار به حساب شما اضافه شد."
    await m.answer(text, reply_markup=main_menu())


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"🆔 آیدی عددی شما: <code>{m.from_user.id}</code>")


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("لغو شد.", reply_markup=main_menu())


@router.message(F.text == BTN_ACCOUNT)
async def menu_account(m: Message, state: FSMContext):
    await state.clear()
    u = db.get_user(m.from_user.id)
    s = db.account_stats(m.from_user.id)
    await m.answer(
        "👤 <b>حساب من</b>\n\n"
        f"🆔 شناسه: <code>{m.from_user.id}</code>\n"
        f"💎 موجودی: <b>{u['balance']}</b> اعتبار\n"
        f"✅ عضویت‌های تأییدشده: {s['joins']}\n"
        f"🏆 اعتبار کسب‌شده از عضویت: {s['earned']}\n"
        f"📢 کمپین‌ها: {s['campaigns']} | اعضای جذب‌شده: {s['members_got']}\n"
        f"👥 دعوت‌شده‌ها: {s['referrals']}"
    )


@router.message(F.text == BTN_REF)
async def menu_ref(m: Message, state: FSMContext):
    await state.clear()
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{m.from_user.id}"
    await m.answer(
        "🎁 <b>دعوت دوستان</b>\n\n"
        f"با هر دوستی که با لینک شما وارد شود و اولین عضویتش را انجام دهد، "
        f"<b>{config.REFERRAL_BONUS}</b> اعتبار می‌گیرید.\n\n"
        f"🔗 لینک اختصاصی شما:\n{link}"
    )


@router.message(F.text == BTN_SUPPORT)
async def menu_support(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(f"☎️ پشتیبانی: @{config.SUPPORT_USERNAME}")


# ================================================================== EARN (TASKS)
def task_view(uid: int):
    skip = skips.setdefault(uid, set())
    row = db.next_task(uid, skip)
    if row is None and skip:
        skip.clear()
        row = db.next_task(uid, skip)
    if row is None:
        return None, None
    link = row["invite_link"] or (f"https://t.me/{row['username']}" if row["username"] else None)
    if not link:
        skip.add(row["id"])
        return task_view(uid) if len(skip) < 50 else (None, None)
    kind = "کانال" if row["chat_type"] == "channel" else "گروه"
    text = (
        f"📢 <b>{esc(row['title'])}</b>\n"
        f"نوع: {kind}\n\n"
        "1️⃣ روی «🔗 ورود» بزنید و عضو شوید.\n"
        "2️⃣ برگردید و «✅ عضو شدم» را بزنید.\n\n"
        f"🎁 پاداش: <b>{config.JOIN_REWARD}</b> اعتبار\n"
        f"⚠️ حداقل {config.RECHECK_DAYS} روز عضو بمانید؛ در صورت خروج، پاداش کسر می‌شود."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 ورود", url=link)
    kb.button(text="✅ عضو شدم", callback_data=f"done:{row['id']}")
    kb.button(text="⏭ بعدی", callback_data=f"skip:{row['id']}")
    kb.button(text="🚩 گزارش", callback_data=f"rep:{row['id']}")
    kb.adjust(1, 1, 2)
    return text, kb.as_markup()


NO_TASKS = (
    "😴 فعلاً کمپین فعالی برای شما نیست.\n"
    "کمی بعد دوباره سر بزنید یا خودتان کمپین بزنید تا اعضای بیشتری بیایند."
)


async def show_next(cb: CallbackQuery, uid: int, header: str = ""):
    text, kb = task_view(uid)
    if text is None:
        text, kb = NO_TASKS, None
    try:
        await cb.message.edit_text(header + text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.message(F.text == BTN_EARN)
async def menu_earn(m: Message, state: FSMContext):
    await state.clear()
    text, kb = task_view(m.from_user.id)
    if text is None:
        return await m.answer(NO_TASKS)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery):
    skips.setdefault(cb.from_user.id, set()).add(int(cb.data.split(":")[1]))
    await cb.answer()
    await show_next(cb, cb.from_user.id)


@router.callback_query(F.data.startswith("rep:"))
async def cb_report(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    res = db.report_campaign(cid, cb.from_user.id)
    skips.setdefault(cb.from_user.id, set()).add(cid)
    if res is None:
        await cb.answer("قبلاً این کمپین را گزارش کرده‌اید.")
    else:
        n, paused, owner = res
        await cb.answer("🚩 گزارش شما ثبت شد.")
        if paused:
            for admin in config.ADMIN_IDS:
                await safe_send(
                    cb.bot, admin, f"🚩 کمپین #{cid} با {n} گزارش متوقف شد.\nبرای ادامه: /resume {cid}"
                )
            if owner:
                await safe_send(
                    cb.bot, owner, f"⚠️ کمپین #{cid} شما به‌دلیل گزارش‌های متعدد متوقف و در حال بررسی است."
                )
    await show_next(cb, cb.from_user.id)


@router.callback_query(F.data.startswith("done:"))
async def cb_done(cb: CallbackQuery):
    uid = cb.from_user.id
    cid = int(cb.data.split(":")[1])

    ok, msg = db.task_gate(uid)
    if not ok:
        return await cb.answer(msg, show_alert=True)

    camp = db.campaign_full(cid)
    if not camp or camp["status"] != "active" or camp["delivered"] >= camp["target"]:
        await cb.answer("این کمپین تکمیل یا متوقف شده است.", show_alert=True)
        return await show_next(cb, uid)

    try:
        member = await is_member(cb.bot, camp["chat_id"], uid)
    except (TelegramBadRequest, TelegramForbiddenError):
        # the bot lost admin access: pause the campaign and tell the owner
        db.set_campaign_status(cid, "paused")
        await safe_send(
            cb.bot,
            camp["owner_id"],
            f"⚠️ ربات دیگر در «{esc(camp['title'])}» ادمین نیست و کمپین #{cid} متوقف شد.\n"
            "ربات را دوباره ادمین کنید و با پشتیبانی تماس بگیرید.",
        )
        await cb.answer("خطا در بررسی این کمپین. مورد بعدی را امتحان کنید.", show_alert=True)
        return await show_next(cb, uid)
    except TelegramAPIError:
        return await cb.answer("خطای موقت تلگرام. چند لحظه بعد دوباره تلاش کنید.", show_alert=True)

    if not member:
        return await cb.answer("❌ هنوز عضو نشده‌اید! اول روی «🔗 ورود» بزنید و عضو شوید.", show_alert=True)

    res = db.complete_join(uid, cid)
    if not res["ok"]:
        await cb.answer("این مورد دیگر قابل ثبت نیست.", show_alert=True)
        return await show_next(cb, uid)

    if res["completed"]:
        await safe_send(
            cb.bot,
            res["owner_id"],
            f"🎉 کمپین «{esc(res['title'])}» با {res['target']} عضو تکمیل شد!",
        )
    if res["ref_to"]:
        await safe_send(
            cb.bot, res["ref_to"], f"🎁 دوست دعوت‌شده‌ی شما فعال شد و {config.REFERRAL_BONUS} اعتبار گرفتید!"
        )
    header = f"✅ +{res['reward']} اعتبار | موجودی: {res['balance']}\n\n"
    await cb.answer(f"+{res['reward']} اعتبار")
    await show_next(cb, uid, header)


# ================================================================== CHANNELS
def parse_chat_ref(m: Message):
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "type", None) == "channel":
        return fo.chat.id
    t = normalize_digits((m.text or "").strip())
    for p in ("https://t.me/", "http://t.me/", "t.me/"):
        if t.startswith(p):
            t = t[len(p):]
    if t.startswith("+") or t.startswith("joinchat"):
        return None
    t = t.lstrip("@").split("/")[0].split("?")[0]
    if re.fullmatch(r"-?\d{5,}", t):
        return int(t)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", t):
        return "@" + t
    return None


ADD_HELP = (
    "➕ <b>افزودن کانال یا گروه</b>\n\n"
    "1️⃣ ربات را به کانال/گروه خود اضافه و <b>ادمین</b> کنید\n"
    "   • کانال: دسترسی «Invite Users via Link» لازم است\n"
    "2️⃣ سپس یکی از این‌ها را بفرستید:\n"
    "   • یوزرنیم عمومی، مثل <code>@mychannel</code>\n"
    "   • یا یک پیام از کانال را برای من <b>فوروارد</b> کنید (کانال خصوصی)\n"
    "   • یا آیدی عددی گروه خصوصی (مثل <code>-100123456789</code>)\n\n"
    "برای لغو: /cancel"
)


async def start_add_channel(target, state: FSMContext, uid: int):
    if db.count_channels(uid) >= config.MAX_CHANNELS_PER_USER:
        return await target.answer(f"حداکثر {config.MAX_CHANNELS_PER_USER} کانال/گروه می‌توانید ثبت کنید.")
    await state.set_state(AddCh.wait)
    await target.answer(ADD_HELP)


@router.message(F.text == BTN_CHANNELS)
async def menu_channels(m: Message, state: FSMContext):
    await state.clear()
    chans = db.user_channels(m.from_user.id)
    kb = InlineKeyboardBuilder()
    for ch in chans:
        icon = "📣" if ch["chat_type"] == "channel" else "👥"
        kb.button(text=f"{icon} {ch['title'][:30]}", callback_data=f"ch:{ch['id']}")
    kb.button(text="➕ افزودن کانال/گروه", callback_data="addch")
    kb.adjust(1)
    text = "📂 <b>کانال‌ها و گروه‌های شما</b>" if chans else "هنوز کانال یا گروهی ثبت نکرده‌اید."
    await m.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "addch")
async def cb_addch(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await start_add_channel(cb.message, state, cb.from_user.id)


@router.callback_query(F.data.startswith("ch:"))
async def cb_channel(cb: CallbackQuery):
    ch = db.get_channel(int(cb.data.split(":")[1]), cb.from_user.id)
    if not ch:
        return await cb.answer("پیدا نشد.", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 ثبت کمپین", callback_data=f"newc:{ch['id']}")
    kb.button(text="🗑 حذف", callback_data=f"delch:{ch['id']}")
    kb.adjust(2)
    await cb.answer()
    await cb.message.answer(
        f"<b>{esc(ch['title'])}</b>\n🔗 {esc(ch['invite_link'])}", reply_markup=kb.as_markup()
    )


@router.callback_query(F.data.startswith("delch:"))
async def cb_delch(cb: CallbackQuery):
    refund = db.remove_channel(int(cb.data.split(":")[1]), cb.from_user.id)
    if refund is None:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await cb.answer("حذف شد.")
    extra = f"\n💎 {refund} اعتبار از کمپین‌های نیمه‌تمام به حساب شما برگشت." if refund else ""
    await cb.message.edit_text("🗑 کانال حذف شد." + extra)


@router.message(AddCh.wait)
async def add_channel(m: Message, state: FSMContext):
    ref = parse_chat_ref(m)
    if ref is None:
        return await m.answer("❌ ورودی نامعتبر است.\n\n" + ADD_HELP)
    try:
        chat = await m.bot.get_chat(ref)
    except TelegramAPIError:
        return await m.answer("❌ کانال/گروه پیدا نشد. مطمئن شوید ربات در آن عضو است و دوباره بفرستید.")
    if chat.type not in ("channel", "group", "supergroup"):
        return await m.answer("❌ فقط کانال و گروه قابل ثبت است.")
    try:
        me = await m.bot.get_chat_member(chat.id, m.bot.id)
        um = await m.bot.get_chat_member(chat.id, m.from_user.id)
    except TelegramAPIError:
        return await m.answer("❌ ربات را در کانال/گروه ادمین کنید و دوباره تلاش کنید.")
    if me.status != "administrator":
        return await m.answer("❌ ربات هنوز ادمین نیست. ادمینش کنید و دوباره بفرستید.")
    if um.status not in ("creator", "administrator"):
        return await m.answer("❌ فقط مالک یا ادمین می‌تواند آن را ثبت کند.")
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            link = await m.bot.export_chat_invite_link(chat.id)
        except TelegramAPIError:
            return await m.answer(
                "❌ ربات اجازه‌ی ساخت لینک دعوت ندارد. دسترسی «Invite Users via Link» را به ربات بدهید."
            )
    ch_id = db.add_channel(chat.id, m.from_user.id, chat.title or str(chat.id), chat.username, link, chat.type)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 ثبت کمپین", callback_data=f"newc:{ch_id}")
    await m.answer(f"✅ «{esc(chat.title)}» ثبت شد.", reply_markup=kb.as_markup())


# ================================================================== CAMPAIGNS
def count_keyboard(ch_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in config.CAMPAIGN_PRESETS:
        kb.button(text=f"{n} عضو • {n * config.COST_PER_MEMBER} اعتبار", callback_data=f"cn:{ch_id}:{n}")
    kb.button(text="✏️ تعداد دلخواه", callback_data=f"cn:{ch_id}:0")
    kb.adjust(1)
    return kb.as_markup()


def confirm_text_kb(ch_id: int, n: int, balance: int):
    cost = n * config.COST_PER_MEMBER
    text = (
        f"📢 کمپین <b>{n}</b> عضو\n"
        f"💎 هزینه: <b>{cost}</b> اعتبار\n"
        f"موجودی شما: {balance} اعتبار"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید و شروع", callback_data=f"cok:{ch_id}:{n}")
    kb.button(text="❌ انصراف", callback_data="cancel")
    kb.adjust(2)
    return text, kb.as_markup()


@router.message(F.text == BTN_PROMO)
async def menu_promo(m: Message, state: FSMContext):
    await state.clear()
    chans = db.user_channels(m.from_user.id)
    if not chans:
        return await start_add_channel(m, state, m.from_user.id)
    kb = InlineKeyboardBuilder()
    for ch in chans:
        icon = "📣" if ch["chat_type"] == "channel" else "👥"
        kb.button(text=f"{icon} {ch['title'][:30]}", callback_data=f"newc:{ch['id']}")
    kb.adjust(1)
    await m.answer("کدام کانال/گروه را تبلیغ کنیم؟", reply_markup=kb.as_markup())


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("لغو شد.")
    await cb.message.edit_text("لغو شد.")


@router.callback_query(F.data.startswith("newc:"))
async def cb_newc(cb: CallbackQuery):
    ch = db.get_channel(int(cb.data.split(":")[1]), cb.from_user.id)
    if not ch:
        return await cb.answer("پیدا نشد.", show_alert=True)
    bal = db.get_user(cb.from_user.id)["balance"]
    await cb.answer()
    await cb.message.answer(
        f"📢 <b>{esc(ch['title'])}</b>\n💎 موجودی شما: {bal} اعتبار\n\nتعداد عضو مورد نظر را انتخاب کنید:",
        reply_markup=count_keyboard(ch["id"]),
    )


@router.callback_query(F.data.startswith("cn:"))
async def cb_count(cb: CallbackQuery, state: FSMContext):
    _, ch_id, n = cb.data.split(":")
    ch_id, n = int(ch_id), int(n)
    if not db.get_channel(ch_id, cb.from_user.id):
        return await cb.answer("پیدا نشد.", show_alert=True)
    await cb.answer()
    if n == 0:
        await state.set_state(CustomN.wait)
        await state.update_data(ch_id=ch_id)
        return await cb.message.answer(
            f"تعداد عضو را بفرستید (بین {config.MIN_CAMPAIGN} تا {config.MAX_CAMPAIGN}):\nلغو: /cancel"
        )
    bal = db.get_user(cb.from_user.id)["balance"]
    text, kb = confirm_text_kb(ch_id, n, bal)
    await cb.message.edit_text(text, reply_markup=kb)


@router.message(CustomN.wait)
async def custom_count(m: Message, state: FSMContext):
    t = normalize_digits((m.text or "").strip())
    if not t.isdigit() or not (config.MIN_CAMPAIGN <= int(t) <= config.MAX_CAMPAIGN):
        return await m.answer(f"❌ یک عدد بین {config.MIN_CAMPAIGN} تا {config.MAX_CAMPAIGN} بفرستید.")
    ch_id = (await state.get_data()).get("ch_id")
    await state.clear()
    bal = db.get_user(m.from_user.id)["balance"]
    text, kb = confirm_text_kb(ch_id, int(t), bal)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cok:"))
async def cb_confirm_campaign(cb: CallbackQuery):
    _, ch_id, n = cb.data.split(":")
    n = int(n)
    if not (config.MIN_CAMPAIGN <= n <= config.MAX_CAMPAIGN):
        return await cb.answer("تعداد نامعتبر است.", show_alert=True)
    status, val = db.create_campaign(cb.from_user.id, int(ch_id), n)
    await cb.answer()
    if status == "ok":
        return await cb.message.edit_text(
            f"✅ کمپین #{val} شروع شد!\nپیشرفت را از «{BTN_CAMPS}» ببینید."
        )
    if status == "exists":
        return await cb.message.edit_text("⚠️ برای این کانال/گروه یک کمپین فعال دارید. اول آن را تمام یا لغو کنید.")
    if status == "nobalance":
        return await cb.message.edit_text(
            f"❌ اعتبار کافی نیست. <b>{val}</b> اعتبار کم دارید.\n"
            f"از «{BTN_EARN}» رایگان بگیرید یا از «{BTN_BUY}» بخرید."
        )
    await cb.message.edit_text("❌ کانال پیدا نشد.")


STATUS_LABEL = {"active": "🟢 فعال", "paused": "⏸ متوقف", "done": "✅ تکمیل", "cancelled": "🚫 لغو"}


@router.message(F.text == BTN_CAMPS)
async def menu_camps(m: Message, state: FSMContext):
    await state.clear()
    camps = db.user_campaigns(m.from_user.id)
    if not camps:
        return await m.answer("هنوز کمپینی نزده‌اید.")
    lines = ["📊 <b>کمپین‌های اخیر شما</b>\n"]
    kb = InlineKeyboardBuilder()
    for c in camps:
        lines.append(
            f"#{c['id']} • {esc(c['title'])}\n"
            f"   {c['delivered']}/{c['target']} • {STATUS_LABEL.get(c['status'], c['status'])}"
        )
        if c["status"] in ("active", "paused"):
            kb.button(text=f"🚫 لغو #{c['id']} (بازگشت اعتبار)", callback_data=f"cx:{c['id']}")
    kb.adjust(1)
    await m.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("cx:"))
async def cb_cancel_campaign(cb: CallbackQuery):
    refund = db.cancel_campaign(int(cb.data.split(":")[1]), cb.from_user.id)
    if refund is None:
        return await cb.answer("قابل لغو نیست.", show_alert=True)
    await cb.answer("لغو شد.")
    await cb.message.answer(f"🚫 کمپین لغو شد. 💎 {refund} اعتبار به حساب شما برگشت.")


# ================================================================== BUY
@router.message(F.text == BTN_BUY)
async def menu_buy(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    lines = ["💳 <b>خرید اعتبار</b>\n"]
    for i, p in enumerate(config.PACKAGES):
        per = p["price"] // p["members"]
        credits = p["members"] * config.COST_PER_MEMBER
        lines.append(f"• {money(p['members'])} عضو ({money(credits)} اعتبار) — {money(p['price'])} تومان (هر عضو {money(per)} تومان)")
        kb.button(text=f"{money(p['members'])} عضو • {money(p['price'])} تومان", callback_data=f"buy:{i}")
    kb.adjust(1)
    lines.append("\nبسته‌ی مورد نظر را انتخاب کنید 👇")
    await m.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    if not (0 <= idx < len(config.PACKAGES)):
        return await cb.answer()
    order_id = db.create_order(cb.from_user.id, idx)
    o = db.get_order(order_id)
    await state.set_state(Buy.receipt)
    await state.update_data(order_id=order_id)
    await cb.answer()
    await cb.message.answer(
        f"🧾 <b>سفارش #{order_id}</b>\n"
        f"بسته: {money(o['members'])} عضو ({money(o['credits'])} اعتبار)\n"
        f"💰 مبلغ: <b>{money(o['price'])}</b> تومان\n\n"
        "مبلغ را کارت‌به‌کارت واریز کنید:\n"
        f"💳 <code>{esc(config.CARD_NUMBER)}</code>\n"
        f"👤 به نام: {esc(config.CARD_OWNER)}\n\n"
        "📸 بعد از پرداخت، <b>عکس رسید</b> یا <b>شماره پیگیری</b> را همین‌جا بفرستید.\n"
        "لغو: /cancel"
    )


@router.message(Buy.receipt, F.photo | F.text)
async def receive_receipt(m: Message, state: FSMContext):
    order_id = (await state.get_data()).get("order_id")
    if m.photo:
        receipt = "photo:" + m.photo[-1].file_id
    else:
        receipt = "text:" + (m.text or "")[:500]
    if not order_id or not db.set_order_receipt(order_id, m.from_user.id, receipt):
        await state.clear()
        return await m.answer("این سفارش دیگر معتبر نیست. دوباره از «خرید اعتبار» شروع کنید.")
    await state.clear()
    o = db.get_order(order_id)
    caption = (
        f"🧾 <b>سفارش #{order_id}</b>\n"
        f"کاربر: {user_link(m.from_user)} (<code>{m.from_user.id}</code>)\n"
        f"بسته: {money(o['members'])} عضو → {money(o['credits'])} اعتبار\n"
        f"مبلغ: <b>{money(o['price'])}</b> تومان"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید", callback_data=f"ordok:{order_id}")
    kb.button(text="❌ رد", callback_data=f"ordno:{order_id}")
    kb.adjust(2)
    sent = False
    for admin in config.ADMIN_IDS:
        try:
            if m.photo:
                await m.bot.send_photo(admin, m.photo[-1].file_id, caption=caption, reply_markup=kb.as_markup())
            else:
                await m.bot.send_message(
                    admin, caption + f"\nرسید: {esc(m.text)}", reply_markup=kb.as_markup()
                )
            sent = True
        except TelegramAPIError:
            pass
    if not sent:
        log.warning("Order %s could not be delivered to any admin", order_id)
    await m.answer(
        "✅ رسید دریافت شد. پس از بررسی، اعتبار به حساب شما اضافه می‌شود (معمولاً چند دقیقه).",
        reply_markup=main_menu(),
    )


async def mark_admin_message(cb: CallbackQuery, status_text: str):
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n{status_text}")
        else:
            await cb.message.edit_text((cb.message.html_text or "") + f"\n\n{status_text}")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("ordok:"))
async def cb_order_ok(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    o = db.approve_order(int(cb.data.split(":")[1]))
    if not o:
        return await cb.answer("این سفارش قبلاً بررسی شده.", show_alert=True)
    await cb.answer("تأیید شد.")
    await mark_admin_message(cb, f"✅ تأیید شد توسط {cb.from_user.full_name}")
    await safe_send(
        cb.bot,
        o["user_id"],
        f"✅ پرداخت شما تأیید شد!\n💎 {money(o['credits'])} اعتبار اضافه شد.\nموجودی: <b>{o['balance']}</b>\n"
        f"حالا از «{BTN_PROMO}» کمپین بزنید.",
    )


@router.callback_query(F.data.startswith("ordno:"))
async def cb_order_no(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    o = db.reject_order(int(cb.data.split(":")[1]))
    if not o:
        return await cb.answer("این سفارش قبلاً بررسی شده.", show_alert=True)
    await cb.answer("رد شد.")
    await mark_admin_message(cb, f"❌ رد شد توسط {cb.from_user.full_name}")
    await safe_send(
        cb.bot,
        o["user_id"],
        f"❌ سفارش #{o['id']} تأیید نشد. اگر پرداخت کرده‌اید با @{config.SUPPORT_USERNAME} تماس بگیرید.",
    )


# ================================================================== ADMIN
@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    s = db.stats()
    await m.answer(
        "🛠 <b>پنل مدیریت</b>\n\n"
        f"👥 کاربران: {s['users']}\n"
        f"📣 کانال/گروه: {s['channels']}\n"
        f"🟢 کمپین فعال: {s['active_campaigns']}\n"
        f"✅ عضویت‌ها: {s['joins']} (خروج زودهنگام: {s['left']})\n"
        f"🧾 سفارش تأییدشده: {s['orders']} | در انتظار: {s['pending_orders']}\n"
        f"💰 درآمد: {money(s['revenue'])} تومان\n\n"
        "<b>دستورات</b>\n"
        "/addcredits ID تعداد — افزودن/کسر اعتبار\n"
        "/userinfo ID — اطلاعات کاربر\n"
        "/ban ID | /unban ID\n"
        "/pause ID | /resume ID — کمپین\n"
        "/broadcast متن — ارسال همگانی"
    )


@router.message(Command("addcredits"))
async def cmd_addcredits(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid, amount = (int(x) for x in normalize_digits(command.args or "").split()[:2])
    except ValueError:
        return await m.answer("استفاده: /addcredits USER_ID AMOUNT")
    bal = db.adjust(uid, amount, "admin", ref=m.from_user.id)
    if bal is None:
        return await m.answer("کاربر پیدا نشد.")
    await m.answer(f"✅ موجودی جدید: {bal}")
    if amount > 0:
        await safe_send(m.bot, uid, f"🎁 {amount} اعتبار به حساب شما اضافه شد.")


@router.message(Command("userinfo"))
async def cmd_userinfo(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid = int(normalize_digits(command.args or ""))
    except ValueError:
        return await m.answer("استفاده: /userinfo USER_ID")
    u = db.get_user(uid)
    if not u:
        return await m.answer("کاربر پیدا نشد.")
    s = db.account_stats(uid)
    await m.answer(
        f"👤 {esc(u['full_name'])} @{esc(u['username'])}\n"
        f"موجودی: {u['balance']} | مسدود: {'بله' if u['banned'] else 'خیر'}\n"
        f"عضویت‌ها: {s['joins']} | کمپین‌ها: {s['campaigns']} | دعوت‌ها: {s['referrals']}"
    )


@router.message(Command("ban", "unban"))
async def cmd_ban(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid = int(normalize_digits(command.args or ""))
    except ValueError:
        return await m.answer("استفاده: /ban USER_ID")
    ok = db.set_banned(uid, command.command == "ban")
    await m.answer("✅ انجام شد." if ok else "کاربر پیدا نشد.")


@router.message(Command("pause", "resume"))
async def cmd_pause(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        cid = int(normalize_digits(command.args or ""))
    except ValueError:
        return await m.answer("استفاده: /pause CAMPAIGN_ID")
    ok = db.set_campaign_status(cid, "paused" if command.command == "pause" else "active")
    await m.answer("✅ انجام شد." if ok else "کمپین پیدا نشد یا قابل تغییر نیست.")


@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        return await m.answer("استفاده: /broadcast متن پیام")
    ids = db.all_user_ids()
    await m.answer(f"ارسال برای {len(ids)} کاربر شروع شد...")
    sent = 0
    for uid in ids:
        try:
            await m.bot.send_message(uid, text)
            sent += 1
        except TelegramAPIError:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ ارسال شد: {sent}/{len(ids)}")


# ================================================================== BACKGROUND JOB
async def recheck_loop(bot: Bot):
    """Re-verifies membership after RECHECK_DAYS. Leavers lose their reward and the slot reopens."""
    while True:
        try:
            for j in db.due_joins(100):
                try:
                    still = await is_member(bot, j["chat_id"], j["user_id"])
                except TelegramAPIError:
                    db.postpone_join(j["id"], 6 * 3600)  # bot lost access / temporary error: do not punish
                    continue
                if still:
                    db.mark_kept(j["id"])
                else:
                    res = db.mark_left(j["id"])
                    if res and res["penalty"]:
                        await safe_send(
                            bot,
                            res["user_id"],
                            f"⚠️ از «{esc(res['title'])}» زودتر از موعد خارج شدید؛ {res['penalty']} اعتبار کسر شد.",
                        )
                await asyncio.sleep(0.1)
        except Exception:
            log.exception("recheck loop error")
        await asyncio.sleep(600)


# ================================================================== MAIN
async def main():
    global db, BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: send /myid to the bot, put the number in .env and restart.")
    db = DB(config.DB_PATH)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    router.message.outer_middleware(GuardMiddleware())
    router.callback_query.outer_middleware(GuardMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(recheck_loop(bot))
    log.info("Bot @%s started", BOT_USERNAME)
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
