import asyncio
import html
import io
import json
import logging
import os
import random
import re
import time
from collections import Counter

from aiohttp import web
from google import genai
from google.genai import types
from pypdf import PdfReader, PdfWriter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("study-bot")

# ------------------------------------------------------------------ config
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
MODEL = os.getenv("MODEL", "gemini-flash-latest").strip()
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gemini-flash-lite-latest").strip()
NUM_Q = int(os.getenv("NUM_QUESTIONS", "8"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
BATCH = max(1, int(os.getenv("BATCH_PAGES", "3")))
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "20"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "60"))  # requests per user per day, 0 = unlimited
PORT = int(os.getenv("PORT", "10000"))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
sessions: dict[int, dict] = {}
history: dict[int, list] = {}
venting: set[int] = set()
vent_hist: dict[int, list] = {}
busy: set[int] = set()
locks: dict[int, asyncio.Lock] = {}
usage: dict[int, list] = {}
bg_tasks: set = set()
BOOT = time.time()

LETTERS = {"en": ["A", "B", "C", "D"], "ar": ["أ", "ب", "ج", "د"]}
IMG_MIMES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

LIMIT_MSG = "وصلنا للحد المجاني لجيمناي، جرّب بعد شوي ⏳\nFree limit reached, please try again in a bit."
ERR_MSG = "صار خطأ، جرّب مرة ثانية.\nSomething went wrong, please try again."
NODOC_MSG = "ابعت الملف أو الصور أولاً 📄"
QUOTA_MSG = "وصلت للحد اليومي من الطلبات 🙏 جرّب بكرة بإذن الله.\nDaily limit reached, please try again tomorrow."

CREDIT = (
    "👨‍💻 تم تطوير البوت بواسطة @mbxll0\n"
    "🎓 صمّم البوت: عمر\n\n"
    "Developed by @mbxll0\n"
    "Designed by Omar"
)

HELP_TEXT = (
    "ℹ️ شو بقدر أعمل؟\n\n"
    "1) ابعت ملف PDF أو صور (صورة أو عدة صور) أو نص.\n"
    "2) اضغط 🧠 «تحليل ذكي»: بقرأ كل صفحة، إذا فيها أسئلة بحلها بالتفصيل، وإذا فيها شرح "
    "بشرحه شرح وافي (عربي ← عربي، إنجليزي ← إنجليزي + عربي).\n"
    "3) أو اكتب طلبك مباشرة:\n"
    "• اشرح صفحة 5\n"
    "• حل السؤال 3 صفحة 4\n"
    "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
    "• لخص صفحة 2\n"
    "• مصطلحات صفحة 7\n"
    "• توقعات امتحان صفحة 1-4\n"
    "• بطاقات مراجعة لصفحة 7\n"
    "• أو اسألني أي سؤال عن المادة\n\n"
    "4) نصيحة: ابعت الصور «كملف» (بدون ضغط) عشان تطلع أوضح، وصوّر الصفحة كاملة وبإضاءة كويسة.\n"
    "5) رقم الصفحة = ترتيبها داخل الملف، مش رقمها المطبوع.\n"
    "6) صور متتالية خلال دقيقة بتنضم لنفس الملف.\n"
    "7) /new ملف جديد  /vent فضفضة  /about عن البوت"
)

CHEER_ANY = [
    "ما شاء الله عليك! كمّل هيك 💪",
    "كل خطوة بتقربك من هدفك، فخورين فيك 🌟",
    "إنت قدها وقدود! استمر 🔥",
    "تعبك مش رح يروح هدر، الله يوفقك 🤍",
    "شاطر والله! خذ استراحة صغيرة وكمّل 😉",
    "الدراسة مش سهلة، بس إنت أقوى منها 💪",
    "حلو كتير! ولا يهمك، بتوصل بإذن الله 🌸",
]
CHEER_HIGH = [
    "🏆 أداء رهيب! إنت فعلاً متمكّن، كمّل هيك",
    "🔥 ما شاء الله عليك! النتيجة بتحكي عنك",
    "🌟 عبقري! فخورين فيك، كمّل عالمستوى هاد",
]
CHEER_LOW = [
    "ولا يهمك، الغلط أسرع طريقة للتعلّم 💪 جرّب مرة ثانية وراح تتحسن",
    "إنت مش ضعيف، بس لسا بتتعلّم، وكل محاولة بتفرق 🌱 كمّل",
    "النتيجة مش هي اللي بتحدد قيمتك، المهم إنك حاولت 🤍 يلا مرة ثانية",
]


def cheer(pct=None) -> str:
    if pct is None:
        pool = CHEER_ANY
    elif pct >= 70:
        pool = CHEER_HIGH
    else:
        pool = CHEER_LOW
    return random.choice(pool)


VENT_INTRO = (
    "💬 وضع الفضفضة\n"
    "احكي اللي براسك، أنا بسمعك وما رح أحاضرك 🤍\n\n"
    "⚠️ أنا بوت ذكاء اصطناعي، مش بديل عن أهلك وصحابك أو أخصائي. "
    "وكلامك بيتعالج عبر جيمناي، فما تكتب معلومات حساسة جداً.\n\n"
    "لما تخلص اضغط الزر تحت أو ابعت /stop"
)
VENT_END = "تمام 🤍 أنا هون أي وقت بدك تفضفض. وقت ما تكون جاهز ابعت ملف أو سؤال وكمّلنا."
CRISIS = (
    "انتحر", "أنتحر", "اقتل حالي", "أقتل حالي", "ما بدي اعيش", "ما بدي أعيش",
    "اذي حالي", "أذي حالي", "اؤذي نفسي", "أؤذي نفسي",
    "kill myself", "suicide", "end my life", "hurt myself",
)
CRISIS_NOTE = (
    "🤍 كلامك مهم عندي. إذا حاسس إنك بخطر أو بتفكر تأذي حالك، احكي مع حدا قريب منك هسا "
    "(أهلك أو صديق بتثق فيه) أو اتصل بأرقام الطوارئ بدولتك. مش لازم تمر بهالشي لحالك."
)

T = {
    "en": {
        "b_hint": "💡 Hint",
        "ready": "Ready for the quiz? Let's go 🎯",
        "q": "Question",
        "correct": "✅ Correct!",
        "wrong": "❌ Wrong. Correct answer: {r}",
        "done": "Score: {s}/{t} ({p}%)",
        "stats": "🔥 Best streak: {b}   💡 Hints used: {h}",
        "missed": "You missed {n} question(s).",
        "retry": "🔁 Retry missed questions",
        "nohint": "No hint for this one.",
        "titles": [
            (90, "🏆 Genius!"),
            (70, "🥇 Excellent!"),
            (50, "🥈 Good job!"),
            (0, "🥉 Keep practicing, you'll get there!"),
        ],
    },
    "ar": {
        "b_hint": "💡 تلميح",
        "ready": "جاهز للاختبار؟ يلا نبدأ 🎯",
        "q": "سؤال",
        "correct": "✅ صح!",
        "wrong": "❌ غلط. الجواب الصحيح: {r}",
        "done": "نتيجتك: {s}/{t} ({p}%)",
        "stats": "🔥 أطول سلسلة: {b}   💡 تلميحات: {h}",
        "missed": "غلطت في {n} سؤال.",
        "retry": "🔁 أعد الأسئلة الغلط",
        "nohint": "ما في تلميح لهالسؤال.",
        "titles": [
            (90, "🏆 عبقري!"),
            (70, "🥇 ممتاز!"),
            (50, "🥈 جيد!"),
            (0, "🥉 كمّل تمرين وراح توصل!"),
        ],
    },
}

# ------------------------------------------------------------------ prompts
FORMAT_RULES = """

FORMAT RULES (strict):
- Plain text only: no markdown symbols (no *, #, backticks, no tables with |) and NEVER LaTeX or dollar signs.
- Write math in Unicode: x², x₁, √, π, ×, ÷, ≤, ≥, ≠, ≈, ∞, ∫, ∑, Δ, α, β, θ, λ, ħ, ψ, →, ⇒. Write fractions as (a)/(b).
- Put every section heading on its own line starting with "§ " followed by an emoji and the title, for example "§ 📌 المفاهيم الأساسية". The app turns these lines into bold headings.
- Use numbering, short lines and blank lines between sections so it is easy to read on a phone.
- Every final answer of a problem goes on its own line that starts with "Answer:" (or "الجواب:" when writing Arabic)."""

LANG_RULES = """

LANGUAGE RULES (decide separately for each page / question):
- If the page is mainly Arabic: write the explanation in clear Arabic only, and put the English technical term in parentheses on first mention.
- If the page is mainly English (or another Latin-script language): for every section write the explanation in English first, then the same explanation in simple, friendly Arabic right after it. Keep technical terms in English inside the Arabic text.
- If the student's request clearly asks for a specific language, follow the request."""

VISION_RULES = """

READING THE INPUT: look very carefully at every image / page. Read all text, numbers, units, symbols, subscripts, axes, labels, circuit and free-body diagrams, tables, and handwriting. If any part is blurry, cut off or ambiguous, say exactly what you could not read and state your assumption; never invent content."""

EXPLAIN_SYS = (
    """You are an exceptional university professor and private tutor. The student sends pages from a lecture, textbook, slides or notes and needs a COMPLETE, deep explanation so that they can study ONLY from your answer and pass the exam.

Rules:
- Cover EVERYTHING on the pages: every concept, definition, law, theorem, formula, derivation step, table, chart, figure or diagram (describe what it shows and why it matters), example, footnote and highlighted term. Never skip or merge details to save space. No long introduction.
- Go section by section in the order of the page. For each section: (1) the idea in simple words, (2) the precise details and definitions, (3) the intuition or why it matters, with a short real-life example or analogy when it helps.
- Formulas: write the formula, say what each symbol means with its unit, explain where it comes from or when to use it, and give a tiny numeric example when useful.
- Science and engineering (physics, quantum, nanotechnology, materials, chemistry, math): explain the physical meaning, the assumptions and limits, and how the concepts connect to each other.
- Humanities, history and religion content: give the context (who, when, where, why), causes and effects, how events relate, and the significance. Only state names and dates that appear in the content or that you are certain of, and say so if you are not sure.
- If a page mainly contains exercises or problems, solve them completely instead: restate the question, Given, Required, method, numbered steps with units, and a final "Answer:" line.
- Where something is hard add a short "💡 تبسيط" note. Where students often make mistakes add a "⚠️ خطأ شائع" note.
- End each page with "§ ✅ أهم النقاط" (a short list). At the very end of your whole answer add "§ 📝 اختبر نفسك" with 3 quick self-check questions and brief answers.
- Follow the details of the student's request (more detail, simpler, focus on a topic, and so on) exactly."""
    + LANG_RULES
    + VISION_RULES
    + FORMAT_RULES
)

REQUEST_SYS = (
    """You are an expert, careful tutor. The student sends content (image, PDF, or text) together with a request. Do exactly what the request asks: solve, explain, translate, check answers, and so on.

When the task is to solve:
- If the request names specific questions (for example "solve question 3"), solve ONLY those. Otherwise solve every question and problem in the content, in order.
- For each one: restate it in one short line; for calculations write Given, Required, Method and formulas, then numbered step-by-step working with all calculations and units; double-check the arithmetic; end with a clear "Answer:" line.
- Multiple-choice and true/false: state the correct option, explain why it is right and briefly why each other option is wrong.
- Written or theory questions: give a complete model answer that would earn full marks.
- If a question depends on a figure, read the figure carefully and say what you read from it.
- Language: Arabic question gets an Arabic solution. English question gets the full solution in English followed by a clear Arabic explanation of the steps and the final answer. If the student's request is written in Arabic, make the explanations Arabic and keep technical terms in English."""
    + VISION_RULES
    + FORMAT_RULES
)

SMART_SYS = (
    """You are an expert university tutor with a sharp eye. The student sends pages or photos (lecture slides, textbook pages, handwritten notes, exam sheets, problem sets). Look at EACH page, decide what it is, then act:

A) The page contains QUESTIONS or PROBLEMS (exercises, exam questions, multiple choice, true/false, fill-in-the-blank, calculations, proofs, written questions) -> solve EVERY one completely:
   - restate the question in one short line;
   - calculations: Given, Required, Method and formulas, numbered steps with all calculations and units, double-check the arithmetic, then an "Answer:" line;
   - multiple choice and true/false: say the correct option clearly, explain why it is right and briefly why each other option is wrong;
   - theory or essay questions: a complete model answer that would earn full marks;
   - if a question depends on a figure, read it carefully and say what you read.
B) The page is EXPLANATORY (theory, slides, definitions, notes) -> explain it in full depth: every concept, definition, law, formula (with the meaning of each symbol and its unit), figure, table and example, section by section, with intuition and a short example where helpful. End the page with "§ ✅ أهم النقاط".
C) The page mixes both -> explain the theory first, then solve the questions.
D) Poor quality or handwriting -> read as best you can and say clearly which part is unclear and what you assumed.

For several pages, handle each page separately under its own heading. Never skip details. Be accurate: double-check every number and fact. Where something is hard add a "💡 تبسيط" note, and where students often err add a "⚠️ خطأ شائع" note.
Language: Arabic pages get Arabic only. English pages get the full English explanation or solution followed by a clear Arabic version (Arabic explanation of the steps, reasoning and final answer)."""
    + VISION_RULES
    + FORMAT_RULES
)

QUESTIONS_SYS = """You write practice questions from the given content. Return ONLY valid JSON, no code fences:
{"language": "en", "questions": [{"q": "...", "options": ["...", "...", "...", "..."], "answer": 0, "why": "...", "hint": "..."}]}
- "language": "ar" if the content is mainly Arabic, otherwise "en". ALL text of the questions, options, hints and explanations must be in that language only.
- Write exactly the number of questions requested. Each has 4 options, exactly one correct; "answer" is the zero-based index; vary its position.
- Cover ALL parts of the given content, from easy to challenging, testing understanding and application, not just memorization.
- If the content is math, physics, or engineering problems: write new numeric problems similar to the content with different numbers; options are numbers or short expressions with plausible wrong options (typical mistakes); "why" is the complete step-by-step solution; double-check every calculation.
- "why" explains why the answer is correct and briefly why the common wrong choices are wrong. "hint" nudges toward the answer without revealing it.
- Follow the details of the student's request (difficulty, topic) if given.
- Plain text only, Unicode math (x², √, π, ×, ÷), NEVER LaTeX. Use \\n for line breaks inside JSON strings."""

SUMMARY_SYS = (
    """Write a complete but compact study cheat sheet of the given content: key ideas, definitions, laws, formulas (with symbol meanings), solving patterns for math and physics, important dates and names for history, and common mistakes. Follow the details of the student's request if given."""
    + LANG_RULES
    + FORMAT_RULES
)

TERMS_SYS = (
    """Extract EVERY important term from the given content and build a glossary. For each term write one block:
- the term in English and in Arabic (Arabic content: the Arabic term with its English equivalent in parentheses),
- a clear one or two line definition in simple words,
- when useful, a formula, unit, or tiny example.
Order the terms as they appear in the content. Follow the details of the student's request if given. Write the definitions in Arabic (keep English terms as they are)."""
    + FORMAT_RULES
)

EXAM_SYS = (
    """You are a university examiner. From the given content, write the questions most likely to appear in the exam, and answer each one fully with a model answer that would earn full marks.
- Write 10 to 15 questions: definitions, short answers, explain / compare / why questions, numeric problems with complete solutions, and one or two essay questions.
- Order them from most to least likely, mark each with its type, and add a suggested mark.
- After each model answer add "⚠️ خطأ شائع" or "💡 نصيحة للامتحان" when relevant.
- Follow the details of the student's request (difficulty, topic) if given."""
    + LANG_RULES
    + FORMAT_RULES
)

CARDS_SYS = """Create 10 flashcards from the given content for active recall. Return ONLY valid JSON, no code fences:
{"cards": [{"front": "...", "back": "..."}]}
Each front and back is ONE short line of plain text (no line breaks, no markdown, no LaTeX; use Unicode math). For math content: front = a problem type or formula question, back = the method or formula. Language: Arabic content gets Arabic cards; otherwise English cards."""

ASK_SYS = """You are a friendly, knowledgeable tutor and assistant. The student asks you a question.
- If file content is provided, use it as context when the question relates to it. If the question is not about the content, answer from your general knowledge.
- If there is a previous conversation, use it to understand follow-up questions.
- Reply in the same language as the student's question (Arabic question: Arabic answer; English question: English answer).
- For math, physics, or any calculation: show clear step-by-step work, double-check the arithmetic, and state the final answer.
- Be clear, accurate, and complete but not wordy. If you are not sure about something, say so honestly instead of guessing.
- Plain text only: no markdown symbols (no *, #, or backticks) and NEVER LaTeX or dollar signs. Write math in Unicode: x², √, π, ×, ÷, ≤, ≥. Use numbering, line breaks, and a few emojis when helpful."""

VENT_SYS = """You are a warm, caring friend for a university student who needs to vent. Speak natural, friendly Jordanian-flavored Arabic by default (reply in the student's language if they write in another one).
- Listen first. Reflect and validate what they feel in your own words. Keep replies short: 2 to 5 sentences. Ask at most one gentle question, and only if it helps them keep talking.
- Do NOT lecture, moralize, diagnose, or give long advice lists. Offer suggestions only if the student asks for them.
- Light humor is fine only if the student is joking first. Never make fun of their feelings.
- Never claim to be a human or a therapist. If it comes up, be honest that you are an AI bot who is happy to listen, and that talking to people close to them matters too.
- If the student mentions wanting to hurt themselves, not wanting to live, or being in danger, take it seriously: respond with warmth, tell them they matter, encourage them to talk to someone they trust right now (family, a friend) and to contact local emergency services or a mental health professional if they are in danger. Stay with them, do not change the subject, and never give harmful details.
- Plain text only, no markdown symbols, no lists. A few gentle emojis are fine."""

# action -> (system prompt, default request); these run page-batch by page-batch
BATCHED = {
    "smart": (SMART_SYS, "Look at every page. Solve all the questions and explain all the theory, in full detail."),
    "explain": (EXPLAIN_SYS, "Explain this content comprehensively."),
    "solve": (REQUEST_SYS, "Solve every question and problem in the content."),
}


# ---------------------------------------------------------------- basics

def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


_ids = [int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()]
ADMIN_ID = int(os.getenv("ADMIN_ID") or (_ids[0] if _ids else 0))
seen: dict[int, dict] = {}


known_users: set[int] = set()

# ---- statistics (for /admin) ----
stats = {
    "day": "", "req": 0, "actions": Counter(), "fallback": 0,
    "limited": 0, "errors": 0, "hits": 0, "miss": 0, "users": Counter(),
}


def roll_stats():
    d = time.strftime("%Y-%m-%d")
    if stats["day"] != d:
        stats.update(day=d, req=0, fallback=0, limited=0, errors=0, hits=0, miss=0)
        stats["actions"] = Counter()
        stats["users"] = Counter()


def bump(name: str, n: int = 1):
    roll_stats()
    stats[name] += n


def note(uid: int, action: str):
    roll_stats()
    stats["req"] += 1
    stats["actions"][action] += 1
    stats["users"][uid] += 1


def quota_ok(uid: int) -> bool:
    if DAILY_LIMIT <= 0 or uid == ADMIN_ID:
        return True
    today = time.strftime("%Y-%m-%d")
    rec = usage.get(uid)
    if not rec or rec[0] != today:
        rec = [today, 0]
        usage[uid] = rec
    if rec[1] >= DAILY_LIMIT:
        return False
    rec[1] += 1
    return True


# ---- user registry (kept in a pinned message in the admin's chat so it survives redeploys) ----
DB_TAG = "#studybot_users"
db = {"msg_id": None, "task": None}


async def load_users(bot):
    if not ADMIN_ID:
        return
    try:
        chat = await bot.get_chat(ADMIN_ID)
        pm = getattr(chat, "pinned_message", None)
        if pm and pm.text and pm.text.startswith(DB_TAG):
            data = json.loads(pm.text.split("\n", 1)[1])
            known_users.update(int(x) for x in data.get("u", []))
            db["msg_id"] = pm.message_id
            log.info("loaded %d users", len(known_users))
    except Exception:
        log.warning("could not load users registry", exc_info=True)


async def save_users(bot):
    if not ADMIN_ID:
        return
    ids = sorted(known_users)
    text = DB_TAG + "\n" + json.dumps({"u": ids}, separators=(",", ":"))
    while len(text) > 3900 and ids:
        ids = ids[1:]
        text = DB_TAG + "\n" + json.dumps({"u": ids}, separators=(",", ":"))
    try:
        if db["msg_id"]:
            try:
                await bot.edit_message_text(text, chat_id=ADMIN_ID, message_id=db["msg_id"])
                return
            except Exception as e:
                if "not modified" in str(e).lower():
                    return
        m = await bot.send_message(ADMIN_ID, text, disable_notification=True)
        db["msg_id"] = m.message_id
        await bot.pin_chat_message(ADMIN_ID, m.message_id, disable_notification=True)
    except Exception:
        log.warning("could not save users registry", exc_info=True)


async def _save_later(bot):
    await asyncio.sleep(20)
    await save_users(bot)


def schedule_save(bot):
    t = db.get("task")
    if t and not t.done():
        return
    t = asyncio.create_task(_save_later(bot))
    db["task"] = t
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


def who(u) -> str:
    name = u.full_name or "?"
    tag = f"@{u.username}" if u.username else "(no username)"
    return f"{name} {tag} | id: {u.id}"


async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message:
        return
    if u.id not in known_users and allowed(u.id):
        known_users.add(u.id)
        schedule_save(ctx.bot)
    rec = seen.get(u.id)
    if rec:
        rec["count"] += 1
        return
    seen[u.id] = {"info": who(u), "count": 1}
    if ADMIN_ID and u.id != ADMIN_ID:
        status = "✅ allowed" if allowed(u.id) else "⛔ blocked (not in ALLOWED_IDS)"
        try:
            await ctx.bot.send_message(ADMIN_ID, f"🆕 New user\n{who(u)}\n{status}")
        except Exception:
            log.exception("admin notify failed")


async def deny(update: Update):
    u = update.effective_user
    try:
        await update.effective_message.reply_text(
            f"🔒 هالبوت خاص ومش مفعّل لحسابك.\nرقمك: {u.id}\nابعت الرقم لصاحب البوت عشان يفعّلك.\n\n"
            f"This bot is private. Send your ID ({u.id}) to the owner to get access."
        )
    except Exception:
        pass


async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    if not seen:
        await update.message.reply_text("No users yet since last restart.")
        return
    lines = [f"{i}) {r['info']} — {r['count']} msgs" for i, r in enumerate(seen.values(), 1)]
    await update.message.reply_text("\n".join(lines)[:4000])


def _fmt_uptime(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    return f"{h // 24}d {h % 24}h {rem // 60}m" if h >= 24 else f"{h}h {rem // 60}m"


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    roll_stats()
    acts = "، ".join(f"{k} {v}" for k, v in stats["actions"].most_common()) or "-"
    top = []
    for i, (uid, c) in enumerate(stats["users"].most_common(5), 1):
        info = seen.get(uid, {}).get("info", f"id: {uid}")
        top.append(f"{i}) {info} — {c}")
    text = (
        f"📊 لوحة الإدارة ({stats['day']})\n\n"
        f"🔹 طلبات اليوم: {stats['req']}\n"
        f"🔹 حسب النوع: {acts}\n"
        f"🔁 تحويل للموديل الاحتياطي: {stats['fallback']}\n"
        f"🚦 وصول لحد جيمناي (429): {stats['limited']}\n"
        f"⚠️ أخطاء: {stats['errors']}\n\n"
        f"👥 مستخدمين مسجّلين: {len(known_users)} | بهالجلسة: {len(seen)} | جلسات ملفات فعالة: {len(sessions)}\n"
        f"🕒 شغّال من: {_fmt_uptime(time.time() - BOOT)}\n"
        f"🤖 الموديل: {MODEL} (احتياطي: {FALLBACK_MODEL})\n"
        f"📈 حد يومي لكل شخص: {DAILY_LIMIT or 'بدون'}\n\n"
        f"🏆 أكثر المستخدمين اليوم:\n" + ("\n".join(top) if top else "-")
    )
    await update.message.reply_text(text[:4000])


async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("اكتب الرسالة بعد الأمر، مثلاً:\n/broadcast تم تحديث البوت 🎉")
        return
    targets = [u for u in known_users if allowed(u) and u != ADMIN_ID]
    ctx.application.bot_data["bcast"] = {"text": parts[1].strip(), "targets": targets}
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"✅ أرسل لـ {len(targets)}", callback_data="b:ok"),
            InlineKeyboardButton("❌ إلغاء", callback_data="b:no"),
        ]]
    )
    await update.message.reply_text(f"📢 معاينة الرسالة:\n\n{parts[1].strip()}", reply_markup=kb)


async def on_bcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not ADMIN_ID or cq.from_user.id != ADMIN_ID:
        return
    pend = ctx.application.bot_data.pop("bcast", None)
    if cq.data.endswith("no") or not pend:
        await cq.edit_message_reply_markup(None)
        await ctx.bot.send_message(ADMIN_ID, "تم الإلغاء.")
        return
    await cq.edit_message_reply_markup(None)
    ok = fail = 0
    for uid in pend["targets"]:
        try:
            await ctx.bot.send_message(uid, "📢 " + pend["text"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await ctx.bot.send_message(ADMIN_ID, f"تم الإرسال ✅ {ok}\nفشل (حظروا البوت أو غير موجودين): {fail}")


def load_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1], strict=False)


def clean_questions(raw) -> list[dict]:
    good = []
    for q in raw or []:
        opts = q.get("options", [])
        ans = q.get("answer")
        if q.get("q") and 2 <= len(opts) <= 4 and isinstance(ans, int) and 0 <= ans < len(opts):
            q["options"] = [str(o) for o in opts]
            q["why"] = str(q.get("why", ""))
            q["hint"] = str(q.get("hint", ""))
            good.append(q)
    return good


def chunks(text: str, n: int = 4000) -> list[str]:
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        out.append(text)
    return out


def one_line(x) -> str:
    return " ".join(str(x).split())


_ANS_RE = re.compile(r"^(\W*)((?:Final answer|Answer|الجواب(?: النهائي)?)\s*[:：].*)$", re.I)


def fmt_html(text: str) -> str:
    """Turn '§ heading' lines and 'Answer:' lines into bold Telegram HTML."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("§"):
            out.append(f"<b>{html.escape(s.lstrip('§ ').strip(), quote=False)}</b>")
            continue
        esc = html.escape(line, quote=False)
        esc = _ANS_RE.sub(r"\1<b>\2</b>", esc)
        out.append(esc)
    return "\n".join(out)


async def send_long(ctx, chat: int, text: str):
    for part in chunks((text or "").strip() or "...", 3500):
        try:
            await ctx.bot.send_message(chat, fmt_html(part), parse_mode="HTML")
        except Exception:
            await ctx.bot.send_message(chat, part.replace("§ ", ""))


async def call_model(parts, instruction, system, as_json=True, max_tokens=16000, temperature=0.4) -> str:
    contents = list(parts) + [types.Part.from_text(text=instruction)]
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json" if as_json else "text/plain",
        max_output_tokens=max_tokens,
        temperature=temperature,
    )
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL and FALLBACK_MODEL != MODEL else [])
    last = None
    for rnd in range(2):
        limited = False
        for mdl in models:
            for attempt in range(2):
                try:
                    resp = await client.aio.models.generate_content(
                        model=mdl, contents=contents, config=cfg
                    )
                    if resp.text:
                        if mdl != MODEL:
                            bump("fallback")
                        return resp.text
                    last = RuntimeError("empty response")
                    break
                except Exception as e:
                    last = e
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        limited = True
                        bump("limited")
                        break  # try the next model
                    await asyncio.sleep(2 * (attempt + 1))
        if not limited:
            break
        if rnd == 0:
            await asyncio.sleep(20)
    raise last or RuntimeError("no response")


def fail_text(e: Exception) -> str:
    msg = str(e)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return LIMIT_MSG
    bump("errors")
    return ERR_MSG


# ------------------------------------------------- understanding requests

_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


def norm(text: str) -> str:
    t = text.translate(_DIG).lower()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        t = t.replace(a, b)
    return t


INTENTS = {
    "solve": ["حل", "حلي", "حلها", "حلهم", "احل", "جاوب", "جاوبلي", "solve"],
    "quiz": ["اسئله", "اساله", "اختبار", "اختبرني", "اسالني", "كويز", "quiz", "questions", "mcq"],
    "cards": ["بطاقات", "فلاش", "flashcards", "flashcard", "cards"],
    "sum": ["لخص", "لخصلي", "ملخص", "تلخيص", "summary", "summarize"],
    "explain": ["اشرح", "اشرحلي", "شرح", "وضح", "فسر", "explain"],
    "smart": ["حلل", "حللي", "حللها", "تحليل", "ذكي", "analyze", "analyse", "smart"],
    "terms": ["مصطلحات", "مفردات", "glossary", "terms", "vocabulary"],
    "exam": ["امتحان", "امتحانات", "توقعات", "exam", "exams"],
}
WORD2INTENT = {w: k for k, ws in INTENTS.items() for w in ws}


def detect_intent(text: str):
    for tok in re.findall(r"\w+", norm(text)):
        cands = [tok]
        if tok.startswith("و") and len(tok) > 2:
            cands.append(tok[1:])
        if tok.startswith("ال") and len(tok) > 3:
            cands.append(tok[2:])
        for c in cands:
            if c in WORD2INTENT:
                return WORD2INTENT[c]
    return None


PG = r"(?:صفحات|صفحه|ص|pages|page|pg|p)"
SINGLE = re.compile(rf"(?<!\w){PG}\s*\.?\s*(?:رقم\s*)?(\d+)")
RANGE1 = re.compile(
    rf"(?<!\w){PG}\s*\.?\s*(?:رقم\s*)?(\d+)\s*(?:-|–|—|الي|حتي|لحد|to|till|until)\s*(?:{PG}\s*\.?\s*)?(\d+)"
)
RANGE2 = re.compile(
    r"من\s+(?:صفحه\s*|ص\s*)?(\d+)\s*(?:الي|حتي|لحد|-|–|to)\s*(?:صفحه\s*|ص\s*)?(\d+)"
)
BARE_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|الي|حتي|لحد|to)\s*(\d+)")
COUNT_RE = re.compile(r"(\d{1,2})\s*(?:اسئله|اساله|سؤال|سوال|questions?|mcq)")


def _add_range(pages: set, a: int, b: int):
    if a > b:
        a, b = b, a
    pages.update(range(a, min(b, a + 200) + 1))


def extract_pages(text: str, bare: bool = False):
    t = norm(text)
    pages: set[int] = set()
    rest = t
    for rx in (RANGE1, RANGE2):
        for m in rx.finditer(rest):
            _add_range(pages, int(m.group(1)), int(m.group(2)))
        rest = rx.sub(" ", rest)
    for m in SINGLE.finditer(rest):
        pages.add(int(m.group(1)))
    if not pages and bare:
        for m in BARE_RANGE.finditer(t):
            _add_range(pages, int(m.group(1)), int(m.group(2)))
        rest = BARE_RANGE.sub(" ", t)
        for m in re.finditer(r"\d+", rest):
            pages.add(int(m.group(0)))
    out = sorted(p for p in pages if p >= 1)
    return out or None


def extract_count(text: str):
    m = COUNT_RE.search(norm(text))
    if not m:
        return None
    return max(3, min(int(m.group(1)), 20))


def fmt_pages(sel: list[int]) -> str:
    out, start, prev = [], None, None
    for p in sel:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


# --------------------------------------------------------- documents

class DocError(Exception):
    pass


def pdf_page_count(data: bytes) -> int:
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return 0


def slice_pdf(data: bytes, pages: list[int]) -> bytes:
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def check_pages(doc: dict, pages):
    """Raise DocError with a user-facing message when the page request is not possible."""
    if doc["kind"] == "text":
        return
    n = doc["n"]
    if doc["kind"] == "pdf" and not n:
        return
    if pages:
        bad = [p for p in pages if p > n]
        if bad:
            raise DocError(f"الملف فيه {n} صفحة بس، وما في صفحة {bad[0]} 🤔")
        if len(pages) > MAX_PAGES:
            raise DocError(f"حدد {MAX_PAGES} صفحة على الأكثر بالمرة 🙏")
    elif n > MAX_PAGES:
        raise DocError("toobig")


def make_batches(doc: dict, pages):
    """Split a page selection into small batches so every page gets a full-depth answer."""
    if doc["kind"] == "text":
        return [None]
    n = doc["n"]
    if not n:
        return [pages]
    sel = pages or list(range(1, n + 1))
    if len(sel) <= BATCH:
        return [sel]
    return [sel[i : i + BATCH] for i in range(0, len(sel), BATCH)]


async def build_parts(doc: dict, pages):
    """Return (parts, label). Raises DocError with a user-facing message."""
    kind = doc["kind"]
    if kind == "text":
        return [types.Part.from_text(text=doc["text"])], "the pasted text"

    n = doc["n"]

    if kind == "pdf" and not n:
        label = (
            f"the file; focus ONLY on file pages {fmt_pages(pages)}" if pages else "the whole file"
        )
        return [types.Part.from_bytes(data=doc["pdf"], mime_type="application/pdf")], label

    check_pages(doc, pages)
    if pages:
        label = f"page {pages[0]}" if len(pages) == 1 else f"pages {fmt_pages(pages)}"
    else:
        label = "all pages"

    if kind == "images":
        imgs = doc["images"]
        chosen = imgs if not pages else [imgs[p - 1] for p in pages]
        return [types.Part.from_bytes(data=i["data"], mime_type=i["mime"]) for i in chosen], label

    # pdf
    if not pages:
        return [types.Part.from_bytes(data=doc["pdf"], mime_type="application/pdf")], label
    try:
        data = await asyncio.to_thread(slice_pdf, doc["pdf"], pages)
        return [types.Part.from_bytes(data=data, mime_type="application/pdf")], label
    except Exception:
        log.exception("pdf slice failed")
        label = f"the file; focus ONLY on file pages {fmt_pages(pages)}"
        return [types.Part.from_bytes(data=doc["pdf"], mime_type="application/pdf")], label


def get_session(uid: int) -> dict:
    return sessions.setdefault(
        uid, {"doc": None, "qz": None, "pending": None, "cap": None, "last": None}
    )


def doc_intro(doc: dict) -> str:
    if doc["kind"] == "pdf":
        pg = f"📚 {doc['n']} صفحة\n" if doc["n"] else ""
        head = f"📥 استلمت الملف: {doc['name']}\n{pg}"
    elif doc["kind"] == "images":
        n = doc["n"]
        head = f"📥 استلمت {n} صفحة/صور\n"
        if n == 1:
            head += "(ابعت صور ثانية خلال دقيقة وبنضمها لنفس الملف)\n"
    else:
        head = "📥 استلمت النص\n"
    tail = "\nشو بدك أعمل فيه؟ اختار من الأزرار 👇"
    if doc["kind"] != "text":
        tail += (
            "\n🧠 «تحليل ذكي»: بقرا كل صفحة، إذا فيها أسئلة بحلها وإذا فيها شرح بشرحه بالتفصيل."
            "\n\nأو اكتب طلبك مباشرة، مثلاً:\n"
            "• اشرح صفحة 5\n"
            "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
            "• لخص صفحة 2\n"
            "• حل السؤال 3 صفحة 4\n\n"
            "(رقم الصفحة = ترتيبها داخل الملف)"
        )
    return head + tail


# --------------------------------------------------------------- menus

def menu_kb(s=None, after_quiz: bool = False) -> InlineKeyboardMarkup:
    def b(label, data):
        return InlineKeyboardButton(label, callback_data=f"m:{data}")

    rows = [
        [b("🧠 تحليل ذكي (حل + شرح تلقائي)", "smart")],
        [b("📖 اشرح", "explain"), b("✅ حل الأسئلة", "solve")],
        [b("📝 أسئلة اختبار", "quiz"), b("📋 ملخص", "sum")],
        [b("🃏 بطاقات", "cards"), b("🔤 مصطلحات", "terms")],
        [b("🎯 توقعات امتحان", "exam"), b("❓ اسأل", "ask")],
    ]
    if after_quiz:
        rows.insert(0, [b("➕ أسئلة جديدة أصعب", "more")])
    if s and s.get("cap"):
        rows.insert(0, [b("▶️ نفّذ طلبك", "runcap")])
    rows.append([b("💬 فضفضة", "vent"), b("ℹ️ مساعدة", "help")])
    return InlineKeyboardMarkup(rows)


async def send_menu(ctx, chat: int, s=None, after_quiz: bool = False, text: str = "شو بدك بعد؟ 👇"):
    await ctx.bot.send_message(chat, text, reply_markup=menu_kb(s, after_quiz))


# ------------------------------------------------------------ commands

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        await deny(update)
        return
    await update.message.reply_text(
        "أهلاً! 👋 أنا مساعدك الدراسي 📚\n\n"
        "📄 ابعتلي ملف PDF أو صور أو نص، وبعدين:\n"
        "🧠 اضغط «تحليل ذكي»: بقرا كل صفحة، إذا فيها أسئلة بحلها بالتفصيل، وإذا شرح بشرحه شرح وافي "
        "(عربي ← عربي، إنجليزي ← إنجليزي + عربي)\n\n"
        "أو اكتب طلبك:\n"
        "• اشرح صفحة 5\n"
        "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
        "• لخص صفحة 2\n"
        "• حل السؤال 3 صفحة 4\n"
        "• مصطلحات صفحة 7\n"
        "• توقعات امتحان صفحة 1-4\n\n"
        "💬 أو اسألني أي سؤال، أو اضغط فضفضة إذا بدك تفضفض.\n\n"
        "/new ملف جديد   /help مساعدة   /about عن البوت\n\n"
        + CREDIT,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("💬 فضفضة", callback_data="m:vent"),
                    InlineKeyboardButton("ℹ️ مساعدة", callback_data="m:help"),
                ]
            ]
        ),
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT)


async def about_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        return
    await update.message.reply_text(CREDIT)


async def new_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    sessions.pop(uid, None)
    history.pop(uid, None)
    venting.discard(uid)
    vent_hist.pop(uid, None)
    await update.message.reply_text("تمام! ابعت ملف أو صور جديدة 📄")


# --------------------------------------------------------------- vent

async def enter_vent(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    venting.add(uid)
    vent_hist.pop(uid, None)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ خلصت فضفضة", callback_data="m:ventend")]]
    )
    await ctx.bot.send_message(chat_id, VENT_INTRO, reply_markup=kb)


async def exit_vent(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    was = uid in venting
    venting.discard(uid)
    vent_hist.pop(uid, None)
    if was:
        await ctx.bot.send_message(chat_id, VENT_END + "\n\n" + cheer())


async def vent_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    await enter_vent(update.effective_chat.id, ctx, uid)


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    await exit_vent(update.effective_chat.id, ctx, uid)


async def vent_reply(m, ctx: ContextTypes.DEFAULT_TYPE):
    uid = m.from_user.id
    if uid in busy:
        await m.reply_text("⏳ ...")
        return
    busy.add(uid)
    try:
        await ctx.bot.send_chat_action(m.chat_id, "typing")
        hist = vent_hist.get(uid, [])
        convo = ""
        if hist:
            convo = (
                "Conversation so far:\n"
                + "\n".join(f"Student: {q}\nYou: {a[:600]}" for q, a in hist[-6:])
                + "\n\n"
            )
        text = await call_model([], f"{convo}Student says: {m.text}", VENT_SYS, False, 4000, 0.8)
        text = text.strip() or "🤍 أنا هون، كمّل احكي."
        hist.append((m.text, text))
        vent_hist[uid] = hist[-8:]
        low = m.text.lower()
        if any(k in low for k in CRISIS):
            text += "\n\n" + CRISIS_NOTE
        for part in chunks(text):
            await m.reply_text(part)
    except Exception as e:
        log.exception("vent failed")
        await m.reply_text(fail_text(e))
    finally:
        busy.discard(uid)


# ------------------------------------------------------ free questions

async def answer_question(ctx, chat: int, uid: int, text: str, pages):
    if uid in busy:
        await ctx.bot.send_message(chat, "⏳ ...")
        return
    if not quota_ok(uid):
        await ctx.bot.send_message(chat, QUOTA_MSG)
        return
    note(uid, "ask")
    busy.add(uid)
    try:
        await ctx.bot.send_chat_action(chat, "typing")
        s = sessions.get(uid)
        doc = s.get("doc") if s else None
        parts, label, note = [], "", ""
        if doc:
            try:
                parts, label = await build_parts(doc, pages)
            except DocError as e:
                if str(e) == "toobig":
                    note = "\n\n(الملف كبير فما استخدمته بجوابي. اذكر رقم الصفحة لو بدك أجاوب منه، مثلاً: شو معنى X بصفحة 5)"
                else:
                    await ctx.bot.send_message(chat, str(e))
                    return
        hist = history.get(uid, [])
        convo = ""
        if hist:
            convo = (
                "Previous conversation:\n"
                + "\n".join(f"Student: {q}\nTutor: {a[:800]}" for q, a in hist[-4:])
                + "\n\n"
            )
        ctxnote = f"[File context: {label}]\n" if parts else ""
        answer = await call_model(
            parts, f"{ctxnote}{convo}Student question: {text}", ASK_SYS, False, 8000, 0.4
        )
        answer = answer.strip() or "..."
        hist.append((text, answer))
        history[uid] = hist[-6:]
        await send_long(ctx, chat, answer + note)
    except Exception as e:
        log.exception("ask failed")
        await ctx.bot.send_message(chat, fail_text(e))
    finally:
        busy.discard(uid)


async def handle_text(ctx, chat: int, uid: int, text: str):
    s = sessions.get(uid)
    doc = s.get("doc") if s else None
    pages = extract_pages(text)

    if not doc:
        if pages:
            await ctx.bot.send_message(chat, NODOC_MSG + "\nوبعدين اكتب رقم الصفحة اللي بدك إياها.")
            return
        await answer_question(ctx, chat, uid, text, None)
        return

    doc["seen"] = time.time()

    # The user is answering "which pages?"
    if s.get("pending"):
        pend = s["pending"]
        pg = extract_pages(text, bare=True)
        s["pending"] = None
        if pg and not detect_intent(text):
            await run_action(ctx, chat, uid, pend["action"], pg, pend["nq"], pend["extra"], pend["harder"])
            return

    intent = detect_intent(text)
    if intent:
        await run_action(ctx, chat, uid, intent, pages, extract_count(text), text)
    else:
        await answer_question(ctx, chat, uid, text, pages)


# ------------------------------------------------------------- actions

async def run_action(ctx, chat: int, uid: int, action: str, pages=None, nq=None, extra: str = "", harder: bool = False):
    s = sessions.get(uid)
    if not s or not s.get("doc"):
        await ctx.bot.send_message(chat, NODOC_MSG)
        return
    if uid in busy:
        await ctx.bot.send_message(chat, "⏳ استنى لحظة، لسا بشتغل على طلبك السابق")
        return
    if not quota_ok(uid):
        await ctx.bot.send_message(chat, QUOTA_MSG)
        return
    note(uid, action)
    doc = s["doc"]
    doc["seen"] = time.time()
    busy.add(uid)
    wait = None
    try:
        parts, label = None, ""
        try:
            check_pages(doc, pages)
            if action not in BATCHED:
                parts, label = await build_parts(doc, pages)
        except DocError as e:
            if str(e) == "toobig":
                s["pending"] = {"action": action, "nq": nq, "extra": extra, "harder": harder}
                await ctx.bot.send_message(
                    chat,
                    f"الملف كبير ({doc['n']} صفحة) 📚\nاكتب أرقام الصفحات اللي بدك إياها، مثلاً: 5 أو 5-8",
                )
            else:
                await ctx.bot.send_message(chat, str(e))
            return

        await ctx.bot.send_chat_action(chat, "typing")
        wait = await ctx.bot.send_message(chat, "⏳ عم أشتغل على طلبك...")
        req = extra.strip()

        if action in BATCHED:
            system, default_req = BATCHED[action]
            batches = make_batches(doc, pages)
            total = len(batches)
            finished = True
            for i, b in enumerate(batches, 1):
                if total > 1:
                    try:
                        await wait.edit_text(f"⏳ عم أشتغل على طلبك... ({i}/{total})")
                    except Exception:
                        pass
                try:
                    bparts, blabel = await build_parts(doc, b)
                    note = f"[Content: {blabel}]"
                    if b and doc["kind"] != "text":
                        plist = ", ".join(str(p) for p in b)
                        note += (
                            f"\n[This batch contains ONLY these original page numbers, in this order: {plist}. "
                            "Refer to them by these original numbers. Begin each page with a heading line "
                            "'§ 📄 صفحة N | Page N' using these numbers.]"
                        )
                    instr = f"{note}\nStudent request: {req or default_req}"
                    text = await call_model(bparts, instr, system, False, 32000, 0.3)
                    await send_long(ctx, chat, text)
                except Exception as e:
                    log.exception("batch failed")
                    msg = fail_text(e)
                    if total > 1:
                        msg += f"\n(تم إنجاز {i - 1} من {total} أجزاء، كمّل بعدين من الصفحات الباقية)"
                    await ctx.bot.send_message(chat, msg)
                    finished = False
                    break
                if i < total:
                    await asyncio.sleep(3)
            if finished and action in ("solve", "smart"):
                await ctx.bot.send_message(chat, cheer())
            await send_menu(ctx, chat, s)

        else:
            note = f"[Content: {label}]"

            if action == "sum":
                instr = f"{note}\nStudent request: {req or 'Write the cheat sheet now.'}"
                text = await call_model(parts, instr, SUMMARY_SYS, False, 16000, 0.3)
                await send_long(ctx, chat, text)
                await send_menu(ctx, chat, s)

            elif action == "terms":
                instr = f"{note}\nStudent request: {req or 'Build the full glossary now.'}"
                text = await call_model(parts, instr, TERMS_SYS, False, 16000, 0.3)
                await send_long(ctx, chat, text)
                await send_menu(ctx, chat, s)

            elif action == "exam":
                instr = f"{note}\nStudent request: {req or 'Write the likely exam questions with full model answers.'}"
                text = await call_model(parts, instr, EXAM_SYS, False, 32000, 0.4)
                await send_long(ctx, chat, text)
                await send_menu(ctx, chat, s)

            elif action == "cards":
                instr = f"{note}\nStudent request: {req or 'Create the flashcards now.'}"
                raw = await call_model(parts, instr, CARDS_SYS, True, 8000, 0.5)
                cards = load_json(raw).get("cards", [])
                lines = []
                for i, c in enumerate(cards, 1):
                    front = html.escape(one_line(c.get("front", "")))
                    back = html.escape(one_line(c.get("back", "")))
                    lines.append(f"{i}) {front}\n<tg-spoiler>{back}</tg-spoiler>")
                body = "🃏 بطاقات مراجعة (اضغط على الشريط الأسود لتظهر الإجابة)\n\n" + "\n\n".join(lines)
                for part in chunks(body):
                    await ctx.bot.send_message(chat, part, parse_mode="HTML")
                await send_menu(ctx, chat, s)

            elif action == "quiz":
                n = nq or NUM_Q
                flavor = "NEW and somewhat harder " if harder else ""
                instr = f"{note}\nWrite exactly {n} {flavor}questions. Student request: {req or 'none'}."
                old = s.get("qz")
                if harder and old:
                    prev = "\n".join(q["q"] for q in old["questions"])
                    instr += f"\nDo not repeat these questions:\n{prev}"
                raw = await call_model(parts, instr, QUESTIONS_SYS, True, 16000, 0.7)
                data = load_json(raw)
                qs = clean_questions(data.get("questions"))
                if not qs:
                    await ctx.bot.send_message(chat, "ما قدرت أطلع أسئلة من هالمحتوى، جرّب صفحات ثانية.")
                    await send_menu(ctx, chat, s)
                else:
                    qlang = "ar" if data.get("language") == "ar" else "en"
                    s["qz"] = new_quiz(qlang, qs)
                    s["last"] = {"pages": pages, "nq": nq, "extra": extra}
                    await ctx.bot.send_message(chat, T[qlang]["ready"])
                    await send_question(chat, ctx, s["qz"])
    except Exception as e:
        log.exception("action failed")
        await ctx.bot.send_message(chat, fail_text(e))
    finally:
        busy.discard(uid)
        if wait:
            try:
                await wait.delete()
            except Exception:
                pass


# ----------------------------------------------------- storing documents

async def delayed_menu(ctx, chat: int, uid: int, doc: dict):
    """After an album arrives, wait a moment so every photo is collected, then continue."""
    await asyncio.sleep(3.5)
    s = sessions.get(uid)
    if not s or s.get("doc") is not doc:
        return
    cap = s.get("cap")
    if cap:
        s["cap"] = None
        await handle_text(ctx, chat, uid, cap)
        return
    await send_menu(ctx, chat, s, text=doc_intro(doc))


async def store_document(m, ctx: ContextTypes.DEFAULT_TYPE):
    uid = m.from_user.id
    lock = locks.setdefault(uid, asyncio.Lock())
    async with lock:
        await _store_document(m, ctx)


async def _store_document(m, ctx: ContextTypes.DEFAULT_TYPE):
    uid = m.from_user.id
    s = get_session(uid)
    caption = (m.caption or "").strip()
    group = m.media_group_id
    now = time.time()
    old = s.get("doc")
    doc = None

    img = None
    if m.photo:
        f = await m.photo[-1].get_file()
        img = {"mid": m.message_id, "data": bytes(await f.download_as_bytearray()), "mime": "image/jpeg"}
    elif m.document and (m.document.mime_type or "") in IMG_MIMES:
        if (m.document.file_size or 0) > 19 * 1024 * 1024:
            await m.reply_text("الصورة كبيرة، ابعتها أصغر من 19MB.")
            return
        f = await m.document.get_file()
        img = {
            "mid": m.message_id,
            "data": bytes(await f.download_as_bytearray()),
            "mime": m.document.mime_type,
        }

    if img:
        same_group = bool(group and old and old.get("group") == group)
        recent = bool(
            old and old["kind"] == "images" and not group and not caption and now - old["t"] < 90
        )
        if old and old["kind"] == "images" and (same_group or recent):
            if len(old["images"]) >= MAX_IMAGES:
                await m.reply_text(f"الحد الأقصى {MAX_IMAGES} صورة بالملف الواحد 🙏")
                return
            old["images"].append(img)
            old["images"].sort(key=lambda x: x["mid"])
            old["n"] = len(old["images"])
            old["t"] = now
            if same_group:
                if caption:
                    s["cap"] = caption
                return  # silent for the rest of an album
            await send_menu(
                ctx, m.chat_id, s,
                text=f"➕ صار عندك {old['n']} صفحات. ابعت كمان أو اختار من الأزرار 👇",
            )
            return
        doc = {"kind": "images", "name": "صور", "images": [img], "n": 1, "t": now, "group": group}

    elif m.document and m.document.mime_type == "application/pdf":
        if (m.document.file_size or 0) > 19 * 1024 * 1024:
            await m.reply_text("الملف كبير، ابعته أصغر من 19MB.")
            return
        f = await m.document.get_file()
        data = bytes(await f.download_as_bytearray())
        n = await asyncio.to_thread(pdf_page_count, data)
        doc = {"kind": "pdf", "name": m.document.file_name or "file.pdf", "pdf": data, "n": n, "t": now}

    elif m.text:
        doc = {"kind": "text", "name": "نص", "text": m.text, "n": 0, "t": now}

    else:
        await m.reply_text("ابعت صورة أو PDF أو نص.")
        return

    doc["seen"] = now
    s["doc"] = doc
    s["qz"] = None
    s["pending"] = None
    s["last"] = None
    s["cap"] = caption if (group and caption) else None
    history.pop(uid, None)

    # An album: wait for the rest of the photos, then continue once.
    if group and doc["kind"] == "images":
        task = asyncio.create_task(delayed_menu(ctx, m.chat_id, uid, doc))
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return

    # A caption on a single file is a request: do it right away.
    if caption and not group:
        await handle_text(ctx, m.chat_id, uid, caption)
        return

    await send_menu(ctx, m.chat_id, s, text=doc_intro(doc))


async def on_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    uid = m.from_user.id
    if not allowed(uid):
        await deny(update)
        return

    # In vent mode every text message is a vent. Sending a file leaves vent mode.
    if uid in venting:
        if m.text:
            await vent_reply(m, ctx)
            return
        venting.discard(uid)
        vent_hist.pop(uid, None)

    # Short text = a request or question. Long text = pasted study material.
    if m.text:
        s = sessions.get(uid)
        limit = 600 if (s and s.get("doc")) else 300
        if len(m.text) <= limit:
            await handle_text(ctx, m.chat_id, uid, m.text)
            return

    if uid in busy and not (m.photo or m.document):
        await m.reply_text("⏳ ...")
        return
    try:
        await store_document(m, ctx)
    except Exception as e:
        log.exception("store failed")
        await m.reply_text(fail_text(e))


# ---------------------------------------------------------------- quiz

def new_quiz(lang: str, questions: list) -> dict:
    return {
        "lang": lang,
        "questions": questions,
        "order": list(range(len(questions))),
        "pos": 0,
        "score": 0,
        "wrong": [],
        "marks": [],
        "streak": 0,
        "best": 0,
        "hinted": set(),
    }


def bar(qz: dict) -> str:
    total = len(qz["order"])
    done = "".join("🟩" if ok else "🟥" for ok in qz["marks"])
    return done + "⬜" * (total - len(qz["marks"]))


def grade(lang: str, pct: int) -> str:
    for lim, txt in T[lang]["titles"]:
        if pct >= lim:
            return txt
    return ""


async def send_question(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, qz: dict):
    lang = qz["lang"]
    t = T[lang]
    letters = LETTERS[lang]
    q = qz["questions"][qz["order"][qz["pos"]]]
    text = (
        f"{bar(qz)}\n\n{t['q']} {qz['pos'] + 1}/{len(qz['order'])}\n\n{q['q']}\n\n"
        + "\n".join(f"{letters[i]}) {o}" for i, o in enumerate(q["options"]))
    )
    rows = [
        [
            InlineKeyboardButton(letters[i], callback_data=f"a:{qz['pos']}:{i}")
            for i in range(len(q["options"]))
        ],
        [InlineKeyboardButton(t["b_hint"], callback_data=f"h:{qz['pos']}")],
    ]
    await ctx.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(rows))


def get_quiz(uid: int):
    s = sessions.get(uid)
    qz = s.get("qz") if s else None
    return s, (qz if qz and qz["order"] else None)


async def on_hint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    s, qz = get_quiz(cq.from_user.id)
    if not qz:
        await cq.answer()
        return
    pos = int(cq.data.split(":")[1])
    if pos != qz["pos"]:
        await cq.answer()
        return
    q = qz["questions"][qz["order"][pos]]
    hint = q.get("hint", "").strip()
    if not hint:
        await cq.answer(T[qz["lang"]]["nohint"], show_alert=True)
        return
    qz["hinted"].add(pos)
    await cq.answer(hint[:190], show_alert=True)


async def on_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    uid = cq.from_user.id
    s, qz = get_quiz(uid)
    if not qz:
        return
    _, pos, choice = cq.data.split(":")
    pos, choice = int(pos), int(choice)
    if pos != qz["pos"]:
        return

    lang = qz["lang"]
    t = T[lang]
    letters = LETTERS[lang]
    qi = qz["order"][pos]
    q = qz["questions"][qi]
    ok = choice == q["answer"]
    qz["pos"] = pos + 1  # advance immediately so a double tap cannot count twice
    qz["marks"].append(ok)
    if ok:
        qz["score"] += 1
        qz["streak"] += 1
        qz["best"] = max(qz["best"], qz["streak"])
    else:
        qz["wrong"].append(qi)
        qz["streak"] = 0

    right = f"{letters[q['answer']]}) {q['options'][q['answer']]}"
    result = t["correct"] if ok else t["wrong"].format(r=right)
    fire = f"  🔥×{qz['streak']}" if qz["streak"] >= 3 else ""
    new_text = f"{cq.message.text}\n\n{result}{fire}\n\n{q['why']}"
    try:
        await cq.edit_message_text(new_text[:4090])
    except Exception:
        await ctx.bot.send_message(cq.message.chat_id, f"{result}{fire}\n\n{q['why']}"[:4090])

    if qz["pos"] < len(qz["order"]):
        await send_question(cq.message.chat_id, ctx, qz)
        return

    total = len(qz["order"])
    pct = qz["score"] * 100 // total
    msg = (
        f"{bar(qz)}\n\n{t['done'].format(s=qz['score'], t=total, p=pct)}\n"
        f"{grade(lang, pct)}\n"
        f"{t['stats'].format(b=qz['best'], h=len(qz['hinted']))}"
    )
    kb = None
    if qz["wrong"]:
        msg += "\n" + t["missed"].format(n=len(qz["wrong"]))
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t["retry"], callback_data="retry")]]
        )
    msg += "\n\n" + cheer(pct)
    await ctx.bot.send_message(cq.message.chat_id, msg, reply_markup=kb)
    await send_menu(ctx, cq.message.chat_id, s, after_quiz=True)


async def on_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    s = sessions.get(cq.from_user.id)
    qz = s.get("qz") if s else None
    if not qz or not qz["wrong"]:
        return
    qz["order"], qz["wrong"], qz["pos"], qz["score"] = qz["wrong"], [], 0, 0
    qz["marks"], qz["streak"], qz["best"], qz["hinted"] = [], 0, 0, set()
    await cq.edit_message_reply_markup(None)
    await send_question(cq.message.chat_id, ctx, qz)


# ----------------------------------------------------------- menu clicks

async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    uid = cq.from_user.id
    if not allowed(uid):
        return
    chat = cq.message.chat_id
    action = cq.data.split(":")[1]

    if action == "vent":
        await enter_vent(chat, ctx, uid)
        return
    if action == "ventend":
        await exit_vent(chat, ctx, uid)
        return
    if action == "help":
        await ctx.bot.send_message(chat, HELP_TEXT)
        return

    s = sessions.get(uid)
    if not s or not s.get("doc"):
        await ctx.bot.send_message(chat, NODOC_MSG)
        return

    if action == "ask":
        await ctx.bot.send_message(
            chat,
            "اكتب سؤالك وبجاوبك ✍️\n(وإذا بدك من صفحة معينة اذكر رقمها، مثلاً: شو معنى X بصفحة 5)",
        )
        return
    if action == "runcap":
        cap = s.get("cap")
        s["cap"] = None
        if cap:
            await handle_text(ctx, chat, uid, cap)
        return
    if action == "more":
        last = s.get("last") or {}
        await run_action(
            ctx, chat, uid, "quiz", last.get("pages"), last.get("nq"), last.get("extra", ""), True
        )
        return
    if action in ("smart", "explain", "solve", "quiz", "sum", "cards", "terms", "exam"):
        await run_action(ctx, chat, uid, action)


# ----------------------------------------------------------------- server

async def health(_request):
    return web.Response(text="ok")


async def janitor():
    """Free memory: forget files nobody touched for 2 hours."""
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - 2 * 3600
        for uid, s in list(sessions.items()):
            d = s.get("doc")
            if d and d.get("seen", d.get("t", 0)) < cutoff and uid not in busy:
                sessions.pop(uid, None)
                history.pop(uid, None)


async def main():
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("vent", vent_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(TypeHandler(Update, track), group=-1)
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_hint, pattern=r"^h:"))
    app.add_handler(CallbackQueryHandler(on_retry, pattern=r"^retry$"))
    app.add_handler(CallbackQueryHandler(on_bcast, pattern=r"^b:"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND)
            | filters.PHOTO
            | filters.Document.PDF
            | filters.Document.IMAGE,
            on_content,
        )
    )

    web_app = web.Application()
    web_app.add_routes([web.get("/", health)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await app.initialize()
    await app.start()
    await load_users(app.bot)
    await app.updater.start_polling(drop_pending_updates=False)
    task = asyncio.create_task(janitor())
    bg_tasks.add(task)
    log.info("Bot is running")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
