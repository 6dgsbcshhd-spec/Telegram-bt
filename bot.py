import asyncio, base64, io, logging, os
from datetime import date, datetime, timedelta
from urllib.parse import quote

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, ChatAction,
                            LabeledPrice, Message, PreCheckoutQuery)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI
from aiohttp import web

# ============ КОНФИГ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "qwen/qwen-2.5-72b-instruct:free")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_IDS = [ADMIN_ID]
PAYMENT_CONTACT = "https://t.me/okselov"
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
DB = "bot.db"

PRICES = {
    "month":   {"label": "1 месяц",  "price": "149 ₽", "stars": 100, "days": 30},
    "quarter": {"label": "3 месяца", "price": "349 ₽", "stars": 250, "days": 90},
    "year":    {"label": "1 год",    "price": "990 ₽", "stars": 800, "days": 365},
}
FREE_LIMITS = {"solve_photo": 5, "ask_ai": 5, "shop": 3}
SUBJECTS = {
    "math": "Математика", "rus": "Русский", "eng": "Английский",
    "phys": "Физика", "chem": "Химия", "bio": "Биология",
    "hist": "История", "geo": "География", "inf": "Информатика",
    "lit": "Литература", "other": "Другое",
}
LANGUAGES = {
    "en": "🇬🇧 Английский", "de": "🇩🇪 Немецкий", "es": "🇪🇸 Испанский",
    "fr": "🇫🇷 Французский", "it": "🇮🇹 Итальянский", "zh": "🇨🇳 Китайский",
}
GRADES = {"1-4": "1–4 класс", "5-9": "5–9 класс", "10-11": "10–11 класс", "student": "Студент"}
REFERRAL_GOAL = 20
REFERRAL_REWARD_DAYS = 7
REFERRAL_MILESTONES = [5, 10, 15, 20]
REFERRAL_SHARE_TEXT = "🎓 Нашёл крутого AI-репетитора — решает задания по фото, объясняет темы, учит языкам. Попробуй бесплатно!"

# ============ AI ============
ai_client = AsyncOpenAI(api_key=OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")
SYSTEM_TUTOR = ("Ты — дружелюбный репетитор для школьников и студентов. Отвечай структурированно, "
                "по шагам, с примерами. Используй Markdown.")

async def ai_chat(messages, max_tokens=1200):
    r = await ai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=messages, max_tokens=max_tokens
    )
    return r.choices[0].message.content

async def ai_solve_photo(image_bytes, grade=None):
    b64 = base64.b64encode(image_bytes).decode()
    prompt = "Распознай задание на фото, определи предмет и реши пошагово. Структура: 1) Условие. 2) Решение. 3) Ответ. 4) Объяснение."
    if grade: prompt += f" Уровень: {GRADES.get(grade, '')}."
    return await ai_chat([
        {"role": "system", "content": SYSTEM_TUTOR},
        {"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]},
    ], max_tokens=1500)

async def ai_ask(question, grade=None, mode="normal"):
    hint = {"simpler": "Объясни просто.", "deeper": "Углублённо.",
            "example": "Дай 2-3 примера.", "quiz": "Задай 3 вопроса и жди ответа."}.get(mode, "")
    p = f"Вопрос: {question}\n{hint}"
    if grade: p += f"\nУровень: {GRADES.get(grade, '')}."
    return await ai_chat([{"role": "system", "content": SYSTEM_TUTOR}, {"role": "user", "content": p}])

async def ai_tutor(subject, topic, grade=None):
    subj = SUBJECTS.get(subject, subject)
    p = f"Мини-урок по «{subj}» на тему «{topic}». Структура: Теория → Пример → Задание → Разбор. Уровень: {GRADES.get(grade, 'средний') if grade else 'средний'}."
    return await ai_chat([{"role": "system", "content": SYSTEM_TUTOR}, {"role": "user", "content": p}], 1500)

async def ai_lang(lang, mode, user_text=None):
    ln = LANGUAGES.get(lang, lang)
    md = {"talk": "Разговорная практика.", "words": "10 слов с переводом.",
          "gram": "Грамматика с примерами.", "read": "Текст + 3 вопроса.",
          "test": "Тест из 5 вопросов."}.get(mode, "")
    p = f"Язык: {ln}. Режим: {md}"
    if user_text: p += f"\nОтвет ученика: {user_text}\nИсправь ошибки и продолжи."
    return await ai_chat([{"role": "system", "content": SYSTEM_TUTOR}, {"role": "user", "content": p}])

# ============ БД ============
async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, grade TEXT,
            language TEXT DEFAULT 'ru', level TEXT DEFAULT 'normal',
            notifications INTEGER DEFAULT 1, premium INTEGER DEFAULT 0,
            premium_until TEXT, day TEXT, solve_photo INTEGER DEFAULT 0,
            ask_ai INTEGER DEFAULT 0, shop INTEGER DEFAULT 0,
            referrer_id INTEGER, ref_reward_claimed INTEGER DEFAULT 0,
            ref_milestones_hit TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS subjects (user_id INTEGER, subject TEXT, PRIMARY KEY(user_id, subject));
        CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, amount TEXT, status TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS referrals (referred_id INTEGER PRIMARY KEY, referrer_id INTEGER, created_at TEXT);
        """)
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
        if row is None:
            await db.execute("INSERT INTO users(user_id, day) VALUES(?,?)", (uid, date.today().isoformat()))
            await db.commit()
            async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
                row = await c.fetchone()
        d = dict(row)
        if d["premium"] and d["premium_until"]:
            try:
                if datetime.fromisoformat(d["premium_until"]) < datetime.now():
                    await update_user(uid, premium=0); d["premium"] = 0
            except Exception: pass
        return d

async def update_user(uid, **f):
    if not f: return
    cols = ", ".join(f"{k}=?" for k in f)
    async with aiosqlite.connect(DB) as db:
        await db.execute(f"UPDATE users SET {cols} WHERE user_id=?", list(f.values()) + [uid])
        await db.commit()

async def grant_premium(uid, days):
    until = (datetime.now() + timedelta(days=days)).isoformat()
    await update_user(uid, premium=1, premium_until=until)

async def extend_premium(uid, days):
    u = await get_user(uid); now = datetime.now()
    if u.get("premium") and u.get("premium_until"):
        try:
            cur = datetime.fromisoformat(u["premium_until"]); base = cur if cur > now else now
        except Exception: base = now
    else: base = now
    await update_user(uid, premium=1, premium_until=(base + timedelta(days=days)).isoformat())

async def revoke_premium(uid): await update_user(uid, premium=0, premium_until=None)

async def user_exists(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)) as c:
            return await c.fetchone() is not None

async def add_referral(ref_id, new_id):
    if ref_id == new_id or await user_exists(new_id): return False
    async with aiosqlite.connect(DB) as db:
        try:
            await db.execute("INSERT INTO referrals(referred_id, referrer_id, created_at) VALUES(?,?,?)",
                             (new_id, ref_id, datetime.now().isoformat()))
            await db.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (ref_id, new_id))
            await db.commit(); return True
        except aiosqlite.IntegrityError: return False

async def count_refs(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)) as c:
            return (await c.fetchone())[0]

async def refs_list(uid, lim=10):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT referred_id, created_at FROM referrals WHERE referrer_id=? ORDER BY created_at DESC LIMIT ?", (uid, lim)) as c:
            return [dict(r) for r in await c.fetchall()]

async def refs_top(lim=10):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT r.referrer_id, COUNT(*) as cnt, u.username FROM referrals r LEFT JOIN users u ON u.user_id = r.referrer_id GROUP BY r.referrer_id ORDER BY cnt DESC LIMIT ?", (lim,)) as c:
            return [dict(r) for r in await c.fetchall()]

async def mark_milestone(uid, m):
    u = await get_user(uid); hit = [h for h in (u.get("ref_milestones_hit") or "").split(",") if h]
    if str(m) not in hit: hit.append(str(m)); await update_user(uid, ref_milestones_hit=",".join(hit))

async def milestone_hit(uid, m):
    u = await get_user(uid); return str(m) in (u.get("ref_milestones_hit") or "").split(",")

async def claim_ref_reward(uid): await update_user(uid, ref_reward_claimed=1)

async def add_subj(uid, s):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO subjects(user_id, subject) VALUES(?,?)", (uid, s)); await db.commit()

async def del_subj(uid, s):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM subjects WHERE user_id=? AND subject=?", (uid, s)); await db.commit()

async def get_subjs(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT subject FROM subjects WHERE user_id=?", (uid,)) as c:
            return [r[0] for r in await c.fetchall()]

async def log_pay(uid, plan, amount):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO payments(user_id, plan, amount, status, created_at) VALUES(?,?,?,?,?)",
                         (uid, plan, amount, "done", datetime.now().isoformat())); await db.commit()

async def check_limit(uid, field, lim):
    u = await get_user(uid); today = date.today().isoformat()
    if u["day"] != today:
        await update_user(uid, day=today, solve_photo=0, ask_ai=0, shop=0); u = await get_user(uid)
    if u["premium"]: return True
    if u[field] >= lim: return False
    await update_user(uid, **{field: u[field] + 1}); return True

async def get_all_users(lim=50):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, username, premium, premium_until FROM users ORDER BY user_id DESC LIMIT ?", (lim,)) as c:
            return [dict(r) for r in await c.fetchall()]

async def get_user_by_id(uid):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
            r = await c.fetchone(); return dict(r) if r else None

async def count_users():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE premium=1") as c: prem = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE day=?", (date.today().isoformat(),)) as c: act = (await c.fetchone())[0]
    return {"total": total, "premium": prem, "active_today": act}

# ============ KEYBOARDS ============
def main_menu(uid):
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Решить по фото", callback_data="menu:photo")
    kb.button(text="✍️ Задать вопрос", callback_data="menu:ask")
    kb.button(text="🎓 Репетитор", callback_data="menu:tutor")
    kb.button(text="🌍 Изучить язык", callback_data="menu:lang")
    kb.button(text="🛍️ Найти дешевле", callback_data="menu:shop")
    kb.button(text="📚 Мои предметы", callback_data="menu:subjects")
    kb.button(text="🎁 Пригласи друзей", callback_data="menu:ref")
    kb.button(text="⚙️ Настройки", callback_data="menu:settings")
    kb.button(text="💎 Premium", callback_data="menu:premium")
    kb.button(text="💳 Оплата Premium", callback_data="menu:pay")
    if uid in ADMIN_IDS:
        kb.button(text="🛠️ Админ-панель", callback_data="admin:panel")
        kb.adjust(2, 2, 2, 1, 1, 1, 1, 1)
    else:
        kb.adjust(2, 2, 2, 1, 1, 1, 1)
    return kb.as_markup()

def pay_kb():
    kb = InlineKeyboardBuilder()
    for k, p in PRICES.items():
        kb.button(text=f"⭐ {p['label']} — {p['stars']} Stars", callback_data=f"pay_stars:{k}")
    for k, p in PRICES.items():
        kb.button(text=f"💳 {p['label']} — {p['price']}", callback_data=f"pay_rub:{k}")
    kb.button(text="ℹ️ Как оплатить звёздами?", callback_data="pay:help_stars")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(1, 1, 1, 1, 1, 1, 1, 1)
    return kb.as_markup()

def rub_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Перейти в профиль для оплаты", url=PAYMENT_CONTACT)
    kb.button(text="✅ Я оплатил — отправить чек", callback_data="pay:confirm")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()

def premium_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", callback_data="menu:pay")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()

def after_answer():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Решить ещё", callback_data="menu:photo")
    kb.button(text="💡 Объяснить проще", callback_data="after:simpler")
    kb.button(text="📚 Показать теорию", callback_data="after:theory")
    kb.button(text="➡️ Следующее задание", callback_data="menu:photo")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def after_ask():
    kb = InlineKeyboardBuilder()
    kb.button(text="💡 Проще", callback_data="ask:simpler")
    kb.button(text="📖 Подробнее", callback_data="ask:deeper")
    kb.button(text="📝 Дай пример", callback_data="ask:example")
    kb.button(text="🧪 Проверь меня", callback_data="ask:quiz")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def subjects_kb(sel):
    kb = InlineKeyboardBuilder()
    for k, n in SUBJECTS.items():
        kb.button(text=("✅ " if k in sel else "") + n, callback_data=f"subj:{k}")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2)
    return kb.as_markup()

def tutor_subs():
    kb = InlineKeyboardBuilder()
    for k, n in SUBJECTS.items():
        kb.button(text=n, callback_data=f"tutor_s:{k}")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2)
    return kb.as_markup()

def grades_kb():
    kb = InlineKeyboardBuilder()
    for k, n in GRADES.items():
        kb.button(text=n, callback_data=f"grade:{k}")
    kb.adjust(2)
    return kb.as_markup()

def langs_kb():
    kb = InlineKeyboardBuilder()
    for k, n in LANGUAGES.items():
        kb.button(text=n, callback_data=f"lang:{k}")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2)
    return kb.as_markup()

def lang_modes(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗣️ Разговорная практика", callback_data=f"langmode:talk:{lang}")
    kb.button(text="📚 Слова", callback_data=f"langmode:words:{lang}")
    kb.button(text="✍️ Грамматика", callback_data=f"langmode:gram:{lang}")
    kb.button(text="🎧 Понимание текста", callback_data=f"langmode:read:{lang}")
    kb.button(text="📝 Тест", callback_data=f"langmode:test:{lang}")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()

def settings_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎓 Мой класс", callback_data="set:grade")
    kb.button(text="📚 Мои предметы", callback_data="menu:subjects")
    kb.button(text="🌍 Язык интерфейса", callback_data="set:lang")
    kb.button(text="🧠 Уровень объяснений", callback_data="set:level")
    kb.button(text="🔔 Уведомления", callback_data="set:notify")
    kb.button(text="💎 Premium", callback_data="menu:premium")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()

def shop_after():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Найти ещё", callback_data="menu:shop")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2)
    return kb.as_markup()

def back_menu():
    kb = InlineKeyboardBuilder(); kb.button(text="🏠 В меню", callback_data="menu:home")
    return kb.as_markup()

def ref_kb(link):
    kb = InlineKeyboardBuilder()
    share = f"https://t.me/share/url?url={quote(link)}&text={quote(REFERRAL_SHARE_TEXT)}"
    kb.button(text="📤 Поделиться ссылкой", url=share)
    kb.button(text="📋 Скопировать ссылку", callback_data="ref:copy")
    kb.button(text="📊 Мой прогресс", callback_data="ref:progress")
    kb.button(text="🏆 Топ приглашающих", callback_data="ref:top")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(1, 1, 2, 1)
    return kb.as_markup()

def ref_back():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="menu:ref")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2)
    return kb.as_markup()

def admin_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пользователи", callback_data="admin:users")
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="📢 Рассылка", callback_data="admin:broadcast")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2, 1, 1)
    return kb.as_markup()

def admin_back():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:panel")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(2)
    return kb.as_markup()

def admin_users_kb(users):
    kb = InlineKeyboardBuilder()
    for u in users[:20]:
        name = u.get("username") or f"id{u['user_id']}"
        mark = "💎" if u.get("premium") else "🆓"
        kb.button(text=f"{mark} {name}", callback_data=f"admin:user:{u['user_id']}")
    kb.button(text="⬅️ Назад", callback_data="admin:panel")
    kb.adjust(1)
    return kb.as_markup()

def admin_user_act(uid):
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Выдать (30 дн.)", callback_data=f"admin:give:{uid}:30")
    kb.button(text="💎 Выдать (365 дн.)", callback_data=f"admin:give:{uid}:365")
    kb.button(text="➕ Продлить на 30 дн.", callback_data=f"admin:extend:{uid}:30")
    kb.button(text="❌ Снять Premium", callback_data=f"admin:revoke:{uid}")
    kb.button(text="⬅️ Назад", callback_data="admin:users")
    kb.adjust(1)
    return kb.as_markup()

# ============ BOT ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
BUSY = set()

def lock(u):
    if u in BUSY: return False
    BUSY.add(u); return True

def unlock(u): BUSY.discard(u)
def is_admin(u): return u in ADMIN_IDS

class S(StatesGroup):
    photo = State(); ask = State(); topic = State()
    lang = State(); shop = State(); pay = State(); bc = State()

async def send_long(m, text, kb=None):
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        last = i + 4000 >= len(text)
        await m.answer(chunk, reply_markup=kb if last else None)

async def typing(m):
    try: await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    except Exception: pass

MILESTONE_EMOJI = {5: "🔥", 10: "⚡", 15: "🚀", 20: "🏆"}
MILESTONE_TEXT = {5: "Уже 5 друзей! Так держать.", 10: "10 друзей — ты в топе!",
                  15: "15 друзей! Ещё 5 — и Premium твой.", 20: "Цель достигнута!"}

async def handle_new_ref(ref_id):
    cnt = await count_refs(ref_id)
    if cnt in REFERRAL_MILESTONES:
        if not await milestone_hit(ref_id, cnt):
            await mark_milestone(ref_id, cnt)
            try: await bot.send_message(ref_id, f"{MILESTONE_EMOJI[cnt]} *{MILESTONE_TEXT[cnt]}*\n\nПрогресс: *{cnt}/{REFERRAL_GOAL}*")
            except Exception: pass
    if cnt >= REFERRAL_GOAL:
        u = await get_user(ref_id)
        if not u.get("ref_reward_claimed"):
            await extend_premium(ref_id, REFERRAL_REWARD_DAYS)
            await claim_ref_reward(ref_id)
            try: await bot.send_message(ref_id, f"🏆 *Поздравляем!*\n\nТы пригласил *{cnt}* друзей и получаешь *Premium на {REFERRAL_REWARD_DAYS} дней* бесплатно!")
            except Exception: pass
            for a in ADMIN_IDS:
                try: await bot.send_message(a, f"🎁 Реферальная награда: `{ref_id}` пригласил {cnt} друзей")
                except Exception: pass
        return
    if cnt not in REFERRAL_MILESTONES:
        try: await bot.send_message(ref_id, f"🎉 Новый друг! Прогресс: *{cnt}/{REFERRAL_GOAL}*")
        except Exception: pass

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear(); unlock(m.from_user.id)
    args = m.text.split(maxsplit=1)
    ref = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try: ref = int(args[1].replace("ref_", ""))
        except ValueError: pass
    is_new = not await user_exists(m.from_user.id)
    await get_user(m.from_user.id)
    if ref and is_new and await add_referral(ref, m.from_user.id):
        await handle_new_ref(ref)
    await m.answer(
        f"👋 *Привет, {m.from_user.first_name}!*\n\n"
        "Я — твой AI-репетитор. Умею:\n\n"
        "📸 *Решить по фото* — распознаю задание и решу\n"
        "✍️ *Задать вопрос* — отвечу как репетитор\n"
        "🎓 *Репетитор* — мини-урок с теорией и примерами\n"
        "🌍 *Изучить язык* — практика, слова, грамматика\n"
        "🛍️ *Найти дешевле* — поиск лучшей цены\n\n"
        "🎁 *Пригласи 20 друзей* — неделя Premium бесплатно!\n\n"
        "👇 Выбери, что делаем:",
        reply_markup=main_menu(m.from_user.id),
    )

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer("📖 Нажми /start и выбери раздел. Связь: @okselov", reply_markup=back_menu())

@dp.message(Command("premium"))
async def cmd_premium(m: Message):
    await m.answer("💎 *Premium* — безлимит на всё. От 100 ⭐ или 149 ₽.", reply_markup=premium_kb())

@dp.message(Command("grant"))
async def cmd_grant(m: Message):
    if not is_admin(m.from_user.id): return
    try:
        _, uid, days = m.text.split()
        await grant_premium(int(uid), int(days))
        await m.answer(f"✅ Premium выдан {uid} на {days} дн.")
        try: await bot.send_message(int(uid), f"🎉 Premium на {days} дней активирован!")
        except Exception: pass
    except Exception as e:
        await m.answer(f"Формат: /grant <id> <дней>\n{e}")

@dp.callback_query(F.data == "menu:home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear(); unlock(c.from_user.id)
    try: await c.message.edit_text("🏠 Главное меню:", reply_markup=main_menu(c.from_user.id))
    except Exception: await c.message.answer("🏠 Главное меню:", reply_markup=main_menu(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data == "menu:photo")
async def cb_photo(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.photo)
    await c.message.answer("📸 Отправь фотографию задания.", reply_markup=back_menu())
    await c.answer()

@dp.callback_query(F.data == "menu:ask")
async def cb_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.ask)
    await c.message.answer("✍️ Напиши свой вопрос.", reply_markup=back_menu())
    await c.answer()

@dp.callback_query(F.data == "menu:tutor")
async def cb_tutor(c: CallbackQuery):
    await c.message.edit_text("🎓 Выбери предмет:", reply_markup=tutor_subs()); await c.answer()

@dp.callback_query(F.data == "menu:lang")
async def cb_lang(c: CallbackQuery):
    await c.message.edit_text("🌍 Выбери язык:", reply_markup=langs_kb()); await c.answer()

@dp.callback_query(F.data == "menu:shop")
async def cb_shop(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.shop)
    await c.message.answer("🛍️ Отправь фото товара.", reply_markup=back_menu()); await c.answer()

@dp.callback_query(F.data == "menu:subjects")
async def cb_subs(c: CallbackQuery):
    sel = await get_subjs(c.from_user.id)
    await c.message.edit_text("📚 Отметь предметы:", reply_markup=subjects_kb(sel)); await c.answer()

@dp.callback_query(F.data.startswith("subj:"))
async def cb_subj(c: CallbackQuery):
    k = c.data.split(":")[1]
    sel = await get_subjs(c.from_user.id)
    if k in sel: await del_subj(c.from_user.id, k)
    else: await add_subj(c.from_user.id, k)
    sel = await get_subjs(c.from_user.id)
    await c.message.edit_reply_markup(reply_markup=subjects_kb(sel)); await c.answer("Ок")

@dp.callback_query(F.data == "menu:settings")
async def cb_set(c: CallbackQuery):
    await c.message.edit_text("⚙️ Настройки:", reply_markup=settings_kb()); await c.answer()

@dp.callback_query(F.data == "menu:premium")
async def cb_prem(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    st = "💎 Активен" if u["premium"] else "🆓 Free"
    unt = f"\nДо: `{u['premium_until'][:10]}`" if u.get("premium_until") else ""
    await c.message.edit_text(
        f"💎 *Premium* — {st}{unt}\n\n*FREE:*\n• {FREE_LIMITS['solve_photo']} фото/день\n• {FREE_LIMITS['ask_ai']} вопросов/день\n• {FREE_LIMITS['shop']} поисков/день\n\n*PREMIUM:*\n• Безлимит всего\n• История занятий\n• План обучения",
        reply_markup=premium_kb())
    await c.answer()

@dp.callback_query(F.data == "menu:pay")
async def cb_pay(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    st = "💎 Premium" if u["premium"] else "🆓 Free"
    await c.message.edit_text(
        f"💳 *Оплата Premium*\n\nСтатус: {st}\n\n⭐ Stars — мгновенно\n💳 Рубли — через @okselov\n\n👇 Выбери тариф:",
        reply_markup=pay_kb(), disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "pay:help_stars")
async def cb_hs(c: CallbackQuery):
    await c.message.answer("⭐ *Stars* — валюта Telegram. 1. Выбери тариф. 2. Подтверди. 3. Premium активируется сам.", reply_markup=back_menu())
    await c.answer()

@dp.callback_query(F.data.startswith("pay_stars:"))
async def cb_ps(c: CallbackQuery):
    k = c.data.split(":")[1]; p = PRICES.get(k)
    if not p: await c.answer("Нет тарифа", show_alert=True); return
    await bot.send_invoice(chat_id=c.from_user.id, title=f"💎 Premium — {p['label']}",
        description=f"Безлимит на {p['label']}", payload=f"premium_{k}",
        provider_token="", currency="XTR",
        prices=[LabeledPrice(label=p["label"], amount=p["stars"])],
        start_parameter=f"premium_{k}")
    await c.answer()

@dp.pre_checkout_query()
async def pre_co(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_pay(m: Message):
    k = m.successful_payment.invoice_payload.replace("premium_", "")
    p = PRICES.get(k)
    if not p: await m.answer("⚠️ Напишите @okselov"); return
    await grant_premium(m.from_user.id, p["days"])
    await log_pay(m.from_user.id, k, f"{p['stars']} Stars")
    un = f"@{m.from_user.username}" if m.from_user.username else "—"
    for a in ADMIN_IDS:
        try: await bot.send_message(a, f"💎 Оплата звёздами!\n👤 {m.from_user.full_name} ({un})\n🆔 `{m.from_user.id}`\n📦 {p['label']}\n⭐ {p['stars']} Stars")
        except Exception: pass
    u = await get_user(m.from_user.id)
    await m.answer(f"🎉 *Оплата получена!*\n\n💎 Premium на {p['label']}\nДо: `{u['premium_until'][:10]}`", reply_markup=back_menu())

@dp.callback_query(F.data.startswith("pay_rub:"))
async def cb_pr(c: CallbackQuery):
    k = c.data.split(":")[1]; p = PRICES.get(k)
    if not p: await c.answer("Нет тарифа", show_alert=True); return
    await log_pay(c.from_user.id, k, p["price"])
    await c.message.answer(
        f"💳 *Оплата рублями*\n\n📦 {p['label']}\n💰 {p['price']}\n\n1. Напишите @okselov\n2. Переведите {p['price']}\n3. Пришлите чек\n4. Нажмите «✅ Я оплатил»",
        reply_markup=rub_kb(), disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "pay:confirm")
async def cb_pc(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.pay)
    await c.message.answer("📩 Отправьте чек (фото/текст).", reply_markup=back_menu()); await c.answer()

@dp.message(S.pay)
async def msg_pc(m: Message, state: FSMContext):
    un = f"@{m.from_user.username}" if m.from_user.username else "—"
    text = f"🔔 *Заявка на Premium*\n\n👤 {m.from_user.full_name} ({un})\n🆔 `{m.from_user.id}`\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\nВыдать: `/grant {m.from_user.id} 30`"
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
            if m.photo: await bot.send_photo(a, m.photo[-1].file_id, caption="Чек")
            elif m.document: await bot.send_document(a, m.document.file_id, caption="Чек")
        except Exception: pass
    await m.answer("✅ Заявка отправлена! Активация ~10 мин.", reply_markup=back_menu())
    await state.clear()

@dp.callback_query(F.data == "set:grade")
async def cb_sg(c: CallbackQuery):
    await c.message.edit_text("🎓 Класс:", reply_markup=grades_kb()); await c.answer()

@dp.callback_query(F.data.startswith("grade:"))
async def cb_g(c: CallbackQuery):
    g = c.data.split(":")[1]
    await update_user(c.from_user.id, grade=g)
    await c.message.edit_text("✅ Сохранено", reply_markup=back_menu()); await c.answer()

@dp.callback_query(F.data == "set:lang")
async def cb_sl(c: CallbackQuery):
    await c.message.edit_text("🌍 Язык:", reply_markup=langs_kb()); await c.answer()

@dp.callback_query(F.data == "set:notify")
async def cb_sn(c: CallbackQuery):
    u = await get_user(c.from_user.id); nv = 0 if u["notifications"] else 1
    await update_user(c.from_user.id, notifications=nv)
    await c.answer("Уведомления вкл" if nv else "Уведомления выкл", show_alert=True)

@dp.callback_query(F.data == "set:level")
async def cb_slev(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    nv = {"simple": "normal", "normal": "deep", "deep": "simple"}.get(u["level"], "normal")
    await update_user(c.from_user.id, level=nv); await c.answer(f"Уровень: {nv}", show_alert=True)

@dp.callback_query(F.data.startswith("tutor_s:"))
async def cb_ts(c: CallbackQuery, state: FSMContext):
    subj = c.data.split(":")[1]; u = await get_user(c.from_user.id)
    if not u["grade"]:
        await state.update_data(subject=subj)
        await c.message.edit_text("🎓 Сначала класс:", reply_markup=grades_kb()); await c.answer(); return
    await state.update_data(subject=subj); await state.set_state(S.topic)
    await c.message.answer("📖 Тема?", reply_markup=back_menu()); await c.answer()

@dp.message(S.topic)
async def msg_topic(m: Message, state: FSMContext):
    if not lock(m.from_user.id): await m.answer("⏳ Уже обрабатываю"); return
    try:
        d = await state.get_data(); u = await get_user(m.from_user.id)
        await typing(m)
        r = await ai_tutor(d["subject"], m.text, u["grade"])
        await send_long(m, r, after_answer()); await state.clear()
    except Exception as e:
        logging.exception(e); await m.answer("⚠️ Ошибка. Попробуйте ещё.")
    finally: unlock(m.from_user.id)

@dp.message(S.photo, F.photo)
async def msg_ph(m: Message, state: FSMContext):
    if not lock(m.from_user.id): await m.answer("⏳ Обрабатываю"); return
    try:
        if not await check_limit(m.from_user.id, "solve_photo", FREE_LIMITS["solve_photo"]):
            await m.answer("🚫 Лимит. Оформите 💎 Premium.", reply_markup=premium_kb()); return
        u = await get_user(m.from_user.id)
        f = await bot.get_file(m.photo[-1].file_id)
        buf = await bot.download_file(f.file_path)
        await typing(m)
        r = await ai_solve_photo(buf.read(), u["grade"])
        await send_long(m, r, after_answer())
    except Exception as e:
        logging.exception(e); await m.answer("⚠️ Ошибка обработки.")
    finally: unlock(m.from_user.id)

@dp.message(S.photo)
async def msg_ph_w(m: Message): await m.answer("📸 Пришлите фото")

@dp.message(S.ask)
async def msg_ask(m: Message, state: FSMContext):
    if not lock(m.from_user.id): await m.answer("⏳ Отвечаю"); return
    try:
        if not await check_limit(m.from_user.id, "ask_ai", FREE_LIMITS["ask_ai"]):
            await m.answer("🚫 Лимит. Оформите 💎 Premium.", reply_markup=premium_kb()); return
        u = await get_user(m.from_user.id); await typing(m)
        r = await ai_ask(m.text, u["grade"])
        await state.update_data(q=m.text)
        await send_long(m, r, after_ask())
    except Exception as e:
        logging.exception(e); await m.answer("⚠️ Ошибка ИИ.")
    finally: unlock(m.from_user.id)

@dp.callback_query(F.data.startswith("ask:"))
async def cb_aq(c: CallbackQuery, state: FSMContext):
    if not lock(c.from_user.id): await c.answer("⏳"); return
    try:
        mode = c.data.split(":")[1]; d = await state.get_data(); q = d.get("q", "")
        u = await get_user(c.from_user.id); await typing(c.message)
        r = await ai_ask(q, u["grade"], mode)
        await send_long(c.message, r, after_ask()); await c.answer()
    finally: unlock(c.from_user.id)

@dp.callback_query(F.data.startswith("after:"))
async def cb_af(c: CallbackQuery): await c.answer("Скоро", show_alert=True)

@dp.callback_query(F.data.startswith("lang:"))
async def cb_l(c: CallbackQuery):
    lang = c.data.split(":")[1]
    await c.message.edit_text("Режим:", reply_markup=lang_modes(lang)); await c.answer()

@dp.callback_query(F.data.startswith("langmode:"))
async def cb_lm(c: CallbackQuery, state: FSMContext):
    _, mode, lang = c.data.split(":")
    await state.set_state(S.lang); await state.update_data(lang=lang, mode=mode)
    await c.message.edit_text("⏳ Готовлю...")
    r = await ai_lang(lang, mode)
    await send_long(c.message, r, back_menu()); await c.answer()

@dp.message(S.lang)
async def msg_l(m: Message, state: FSMContext):
    if not lock(m.from_user.id): return
    try:
        d = await state.get_data(); await typing(m)
        r = await ai_lang(d["lang"], d["mode"], m.text)
        await send_long(m, r, back_menu())
    finally: unlock(m.from_user.id)

@dp.message(S.shop, F.photo)
async def msg_sh(m: Message, state: FSMContext):
    if not lock(m.from_user.id): return
    try:
        if not await check_limit(m.from_user.id, "shop", FREE_LIMITS["shop"]):
            await m.answer("🚫 Лимит", reply_markup=premium_kb()); return
        f = await bot.get_file(m.photo[-1].file_id)
        buf = await bot.download_file(f.file_path)
        b64 = base64.b64encode(buf.read()).decode()
        r = await ai_chat([{"role": "user", "content": [
            {"type": "text", "text": "Определи товар: название, бренд, модель. Кратко."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}])
        await m.answer(f"🔎 *Распознан товар:*\n{r}\n\n⚠️ Поиск цен — скоро.", reply_markup=shop_after())
        await state.clear()
    except Exception as e:
        logging.exception(e); await m.answer("⚠️ Ошибка")
    finally: unlock(m.from_user.id)

@dp.callback_query(F.data == "menu:ref")
async def cb_r(c: CallbackQuery):
    uid = c.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    cnt = await count_refs(uid); u = await get_user(uid)
    filled = min(int((cnt / REFERRAL_GOAL) * 10), 10)
    bar = "🟩" * filled + "⬜️" * (10 - filled)
    if u.get("ref_reward_claimed"): st = "✅ *Награда получена*"
    elif cnt >= REFERRAL_GOAL: st = "🏆 *Цель достигнута!*"
    else: st = f"🎯 Осталось: *{REFERRAL_GOAL - cnt}*"
    await c.message.edit_text(
        f"🎁 *Пригласи друзей — получи Premium!*\n\n"
        f"Пригласи *{REFERRAL_GOAL}* → *{REFERRAL_REWARD_DAYS} дней Premium* бесплатно.\n\n"
        f"📊 *Прогресс:* {cnt}/{REFERRAL_GOAL}\n{bar}\n\n{st}\n\n"
        f"🔗 *Твоя ссылка:*\n`{link}`",
        reply_markup=ref_kb(link), disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "ref:copy")
async def cb_rc(c: CallbackQuery):
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{c.from_user.id}"
    await c.message.answer(f"📋 Скопируй:\n\n`{link}`\n\n_Зажми для копирования_", disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "ref:progress")
async def cb_rp(c: CallbackQuery):
    uid = c.from_user.id; cnt = await count_refs(uid); refs = await refs_list(uid)
    rem = max(REFERRAL_GOAL - cnt, 0)
    text = f"📊 *Прогресс*\n\n👥 {cnt}/{REFERRAL_GOAL}\n"
    text += f"🎯 Осталось: *{rem}*\n" if rem else "🏆 *Цель достигнута!*\n"
    if refs:
        text += "\n*Последние:*\n"
        for i, r in enumerate(refs, 1):
            text += f"{i}. `{r['referred_id']}` — {r['created_at'][:10]}\n"
    else: text += "\n_Пока пусто_"
    await c.message.answer(text, reply_markup=ref_back()); await c.answer()

@dp.callback_query(F.data == "ref:top")
async def cb_rt(c: CallbackQuery):
    top = await refs_top(10)
    if not top: await c.message.answer("🏆 Пока пусто", reply_markup=ref_back()); await c.answer(); return
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    text = "🏆 *Топ приглашающих*\n\n"
    for i, r in enumerate(top):
        nm = f"@{r['username']}" if r.get("username") else f"id{r['referrer_id']}"
        text += f"{medals[i]} {nm} — *{r['cnt']}*\n"
    await c.message.answer(text, reply_markup=ref_back()); await c.answer()

@dp.callback_query(F.data == "admin:panel")
async def cb_ap(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа", show_alert=True); return
    await state.clear()
    await c.message.edit_text("🛠️ *Админ-панель*", reply_markup=admin_menu()); await c.answer()

@dp.callback_query(F.data == "admin:users")
async def cb_au(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    us = await get_all_users(20)
    if not us: await c.message.edit_text("Пусто", reply_markup=admin_back()); await c.answer(); return
    await c.message.edit_text("👥 Последние 20:", reply_markup=admin_users_kb(us)); await c.answer()

@dp.callback_query(F.data.startswith("admin:user:"))
async def cb_auu(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    uid = int(c.data.split(":")[2]); u = await get_user_by_id(uid)
    if not u: await c.answer("Не найден", show_alert=True); return
    st = "💎 Premium" if u["premium"] else "🆓 Free"
    unt = f"\nДо: `{u['premium_until'][:10]}`" if u.get("premium_until") else ""
    await c.message.edit_text(f"👤 ID: `{uid}`\nИмя: {u.get('username') or '—'}\nСтатус: {st}{unt}", reply_markup=admin_user_act(uid))
    await c.answer()

@dp.callback_query(F.data.startswith("admin:give:"))
async def cb_ag(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _, _, uid, d = c.data.split(":"); uid, d = int(uid), int(d)
    await grant_premium(uid, d); await c.answer(f"✅ {d} дн.", show_alert=True)
    try: await bot.send_message(uid, f"🎉 Premium на {d} дней!")
    except Exception: pass

@dp.callback_query(F.data.startswith("admin:extend:"))
async def cb_ae(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _, _, uid, d = c.data.split(":"); uid, d = int(uid), int(d)
    await extend_premium(uid, d); u = await get_user_by_id(uid)
    await c.answer(f"✅ До {u['premium_until'][:10]}", show_alert=True)
    try: await bot.send_message(uid, f"🎉 Premium продлён на {d} дней!")
    except Exception: pass

@dp.callback_query(F.data.startswith("admin:revoke:"))
async def cb_ar(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    uid = int(c.data.split(":")[2]); await revoke_premium(uid)
    await c.answer("❌ Снято", show_alert=True)

@dp.callback_query(F.data == "admin:stats")
async def cb_as(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    s = await count_users(); top = await refs_top(5)
    tt = "\n".join(f"{i+1}. `{r['referrer_id']}` — {r['cnt']}" for i, r in enumerate(top)) or "_нет_"
    await c.message.edit_text(f"📊 *Статистика*\n\n👥 Всего: {s['total']}\n💎 Premium: {s['premium']}\n🔥 Сегодня: {s['active_today']}\n\n🎁 *Топ:*\n{tt}", reply_markup=admin_back())
    await c.answer()

@dp.callback_query(F.data == "admin:broadcast")
async def cb_ab(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return
    await state.set_state(S.bc)
    await c.message.edit_text("📢 Текст рассылки:", reply_markup=admin_back()); await c.answer()

@dp.message(S.bc)
async def msg_bc(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    us = await get_all_users(10000); sent = 0
    await m.answer(f"📤 Рассылка на {len(us)}...")
    for u in us:
        try: await bot.send_message(u["user_id"], m.text); sent += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Доставлено: {sent}/{len(us)}", reply_markup=admin_back())
    await state.clear()

# ============ WEBHOOK ДЛЯ RENDER ============
async def handle_webhook(request):
    try:
        update = await request.json()
        await dp.feed_update(bot, update)
        return web.Response(status=200)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def health_check(request):
    return web.Response(text="ok")

async def main():
    await db_init()
    logging.info(f"Бот запущен ✅ Admin: {ADMIN_ID}")
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not render_url:
        logging.info("Запуск в режиме polling (локально)")
        await dp.start_polling(bot)
        return
    logging.info(f"Запуск в режиме webhook: {render_url}")
    webhook_url = f"{render_url}/webhook"
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/", health_check)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info(f"Веб-сервер слушает порт {port}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
