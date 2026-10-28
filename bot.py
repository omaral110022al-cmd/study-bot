import asyncio
import html
import io
import json
import logging
import os
import random
import re
import time

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

TOKEN = os.environ["TELEGRAM_TOKEN"]
MODEL = os.getenv("MODEL", "gemini-flash-lite-latest")
NUM_Q = int(os.getenv("NUM_QUESTIONS", "8"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
PORT = int(os.getenv("PORT", "10000"))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
sessions: dict[int, dict] = {}
history: dict[int, list] = {}
venting: set[int] = set()
vent_hist: dict[int, list] = {}
busy: set[int] = set()

LETTERS = {"en": ["A", "B", "C", "D"], "ar": ["أ", "ب", "ج", "د"]}

LIMIT_MSG = "وصلنا للحد المجاني لجيمناي، جرّب بعد شوي ⏳\nFree limit reached, please try again in a bit."
ERR_MSG = "صار خطأ، جرّب مرة ثانية.\nSomething went wrong, please try again."
NODOC_MSG = "ابعت الملف أو الصور أولاً 📄"

CREDIT = (
    "👨‍💻 تم تطوير البوت بواسطة @mbxll0\n"
    "🎓 صمّم البوت: عمر\n\n"
    "Developed by @mbxll0\n"
    "Designed by Omar"
)

HELP_TEXT = (
    "ℹ️ شو بقدر أعمل؟\n\n"
    "1) ابعت ملف PDF أو صور أو نص، وبعدين اطلب اللي بدك:\n"
    "• اشرح صفحة 5\n"
    "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
    "• لخص صفحة 2\n"
    "• حل السؤال 3 صفحة 4\n"
    "• بطاقات مراجعة لصفحة 7\n"
    "• أو اسألني أي سؤال عن المادة\n\n"
    "2) تقدر تستخدم الأزرار بدل الكتابة.\n"
    "3) رقم الصفحة = ترتيبها داخل الملف، مش رقمها المطبوع.\n"
    "4) صور متتالية خلال دقيقة بتنضم لنفس الملف.\n"
    "5) /new ملف جديد  /vent فضفضة  /about عن البوت"
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

EXPLAIN_SYS = """You are an expert university tutor with a friendly, motivating style. The student sends part of a lecture, textbook, slides, or problem sheet and wants it explained.

DETECT the content language and kind:
- If the content is mainly Arabic: explain in Arabic only (put the English technical term in parentheses on first mention).
- Otherwise: explain each section in clear English first, then the same idea in simple Arabic (Jordanian-friendly Modern Arabic), keeping technical terms in English.
- If the student's request clearly asks for a specific language, follow it.
- Kind "theory": cover EVERYTHING in the given content: every concept, definition, formula, term, example, table, and figure. Do not skip details. Organize into short numbered sections, add a short real-world example or intuition where it helps, explain formulas step by step and say what each symbol means.
- Kind "problems": start with a short "Method and formulas" section, then solve EVERY problem step by step with all calculations and units, double-check the arithmetic, and end each with "Answer: ..." (or "الجواب: ...").
- Follow the details of the student's request (more detail, simpler, focus on a topic, and so on) exactly.
- End with a short list "Key Takeaways" (or "أهم النقاط" when the explanation is Arabic).
- If something is unreadable, say so instead of guessing.
Plain text only: no markdown symbols (no *, #, backticks) and NEVER LaTeX or dollar signs. Write math in Unicode: x², √, π, ×, ÷, ≤, ≥, ∫, Σ, x₁. Use numbering, line breaks and a few emojis for headings."""

REQUEST_SYS = """You are an expert, careful tutor. The student sends content (image, PDF, or text) together with a request. Do exactly what the request asks: solve, explain, translate, check answers, and so on.

When the task is to solve:
- If the request names specific questions (for example "solve question 3"), solve ONLY those. Otherwise solve every question and problem in the content, in order.
- For each one: restate it in one short line, then show numbered step-by-step working with all calculations and units, double-check the arithmetic, and end with a clear line "Answer: ..." (or "الجواب: ..." in Arabic).
- For multiple-choice questions: state the correct option and explain why. For written or theory questions: give a complete model answer.
- If part of the content is unreadable or ambiguous, say clearly what you could not read and state your assumption instead of guessing silently.

Language: if the student's request is written in Arabic, answer in Arabic (keep technical terms in English). Otherwise follow the content: Arabic content gets an Arabic answer; English content gets an English answer followed by a brief Arabic summary of the final answers.
Plain text only: no markdown symbols (no *, #, or backticks) and NEVER LaTeX or dollar signs. Write math in Unicode: x², √, π, ×, ÷, ≤, ≥, ∫, Σ, x₁. Use numbering, line breaks and a few emojis for headings."""

QUESTIONS_SYS = """You write practice questions from the given content. Return ONLY valid JSON, no code fences:
{"language": "en", "questions": [{"q": "...", "options": ["...", "...", "...", "..."], "answer": 0, "why": "...", "hint": "..."}]}
- "language": "ar" if the content is mainly Arabic, otherwise "en". ALL text of the questions, options, hints and explanations must be in that language only.
- Write exactly the number of questions requested. Each has 4 options, exactly one correct; "answer" is the zero-based index; vary its position.
- Cover ALL parts of the given content, from easy to challenging, testing understanding and application, not just memorization.
- If the content is math, physics, or engineering problems: write new numeric problems similar to the content with different numbers; options are numbers or short expressions with plausible wrong options (typical mistakes); "why" is the complete step-by-step solution; double-check every calculation.
- "why" explains why the answer is correct and briefly why the common wrong choices are wrong. "hint" nudges toward the answer without revealing it.
- Follow the details of the student's request (difficulty, topic) if given.
- Plain text only, Unicode math (x², √, π, ×, ÷), NEVER LaTeX. Use \\n for line breaks inside JSON strings."""

SUMMARY_SYS = """Write a compact cheat sheet of the given content: key ideas, definitions, formulas, solving patterns for math, and common mistakes. Language: Arabic content gets Arabic only; English content gets English followed by a short Arabic version. Follow the details of the student's request if given. Plain text only: no markdown symbols (no *, #, backticks), NEVER LaTeX; use Unicode math. Use numbering, line breaks and a few emojis."""

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


# ---------------------------------------------------------------- basics

def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


_ids = [int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()]
ADMIN_ID = int(os.getenv("ADMIN_ID") or (_ids[0] if _ids else 0))
seen: dict[int, dict] = {}


def who(u) -> str:
    name = u.full_name or "?"
    tag = f"@{u.username}" if u.username else "(no username)"
    return f"{name} {tag} | id: {u.id}"


async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message:
        return
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


async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    if not seen:
        await update.message.reply_text("No users yet since last restart.")
        return
    lines = [f"{i}) {r['info']} — {r['count']} msgs" for i, r in enumerate(seen.values(), 1)]
    await update.message.reply_text("\n".join(lines)[:4000])


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


async def send_long(ctx, chat: int, text: str):
    for part in chunks((text or "").strip() or "..."):
        await ctx.bot.send_message(chat, part)


async def call_model(parts, instruction, system, as_json=True, max_tokens=16000) -> str:
    contents = list(parts) + [types.Part.from_text(text=instruction)]
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json" if as_json else "text/plain",
        max_output_tokens=max_tokens,
    )
    last = None
    for attempt in range(3):
        try:
            resp = await client.aio.models.generate_content(
                model=MODEL, contents=contents, config=cfg
            )
            return resp.text or ""
        except Exception as e:
            last = e
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise last


def fail_text(e: Exception) -> str:
    msg = str(e)
    return LIMIT_MSG if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) else ERR_MSG


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

    if pages:
        bad = [p for p in pages if p > n]
        if bad:
            raise DocError(f"الملف فيه {n} صفحة بس، وما في صفحة {bad[0]} 🤔")
        if len(pages) > MAX_PAGES:
            raise DocError(f"حدد {MAX_PAGES} صفحة على الأكثر بالمرة 🙏")
        label = f"page {pages[0]}" if len(pages) == 1 else f"pages {fmt_pages(pages)}"
    else:
        if n > MAX_PAGES:
            raise DocError("toobig")
        label = "all pages"

    if kind == "images":
        imgs = doc["images"]
        chosen = imgs if not pages else [imgs[p - 1] for p in pages]
        return [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in chosen], label

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
        head = (
            "📥 استلمت الصورة (صفحة 1)\n"
            "(ابعت صور ثانية خلال دقيقة وبنضمها لنفس الملف)\n"
        )
    else:
        head = "📥 استلمت النص\n"
    tail = "\nشو بدك أعمل فيه؟ اختار من الأزرار 👇"
    if doc["kind"] != "text":
        tail += (
            "\nأو اكتب طلبك مباشرة، مثلاً:\n"
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
        [b("📖 اشرح", "explain"), b("✅ حل الأسئلة", "solve")],
        [b("📝 أسئلة اختبار", "quiz"), b("📋 ملخص", "sum")],
        [b("🃏 بطاقات", "cards"), b("❓ اسأل", "ask")],
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
        return
    await update.message.reply_text(
        "أهلاً! 👋 أنا مساعدك الدراسي 📚\n\n"
        "📄 ابعتلي ملف PDF أو صور أو نص، وبعدين اطلب اللي بدك:\n"
        "• اشرح صفحة 5\n"
        "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
        "• لخص صفحة 2\n"
        "• حل السؤال 3 صفحة 4\n\n"
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
        text = await call_model([], f"{convo}Student says: {m.text}", VENT_SYS, False, 4000)
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
            parts, f"{ctxnote}{convo}Student question: {text}", ASK_SYS, False, 8000
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
    doc = s["doc"]
    busy.add(uid)
    wait = None
    try:
        try:
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
        note = f"[Content: {label}]"
        req = extra.strip()

        if action == "explain":
            instr = f"{note}\nStudent request: {req or 'Explain this content comprehensively.'}"
            text = await call_model(parts, instr, EXPLAIN_SYS, False, 16000)
            await send_long(ctx, chat, text)
            await send_menu(ctx, chat, s)

        elif action == "solve":
            instr = f"{note}\nStudent request: {req or 'Solve every question and problem in the content.'}"
            text = await call_model(parts, instr, REQUEST_SYS, False, 16000)
            await send_long(ctx, chat, text)
            await ctx.bot.send_message(chat, cheer())
            await send_menu(ctx, chat, s)

        elif action == "sum":
            instr = f"{note}\nStudent request: {req or 'Write the cheat sheet now.'}"
            text = await call_model(parts, instr, SUMMARY_SYS, False, 8000)
            await send_long(ctx, chat, text)
            await send_menu(ctx, chat, s)

        elif action == "cards":
            instr = f"{note}\nStudent request: {req or 'Create the flashcards now.'}"
            raw = await call_model(parts, instr, CARDS_SYS, True, 8000)
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
            raw = await call_model(parts, instr, QUESTIONS_SYS, True, 16000)
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

async def store_document(m, ctx: ContextTypes.DEFAULT_TYPE):
    uid = m.from_user.id
    s = get_session(uid)
    caption = (m.caption or "").strip()
    group = m.media_group_id
    now = time.time()
    old = s.get("doc")
    doc = None

    if m.photo:
        f = await m.photo[-1].get_file()
        data = bytes(await f.download_as_bytearray())
        same_group = bool(group and old and old.get("group") == group)
        recent = bool(old and old["kind"] == "images" and now - old["t"] < 90 and not caption)
        if old and old["kind"] == "images" and (same_group or recent):
            old["images"].append(data)
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
        doc = {"kind": "images", "name": "صور", "images": [data], "n": 1, "t": now, "group": group}

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

    s["doc"] = doc
    s["qz"] = None
    s["pending"] = None
    s["last"] = None
    s["cap"] = caption if (group and caption) else None
    history.pop(uid, None)

    # A caption on a single file is a request: do it right away.
    if caption and not group:
        await handle_text(ctx, m.chat_id, uid, caption)
        return

    await send_menu(ctx, m.chat_id, s, text=doc_intro(doc))


async def on_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    uid = m.from_user.id
    if not allowed(uid):
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

    if uid in busy:
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

    qz["pos"] += 1
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
    if action in ("explain", "solve", "quiz", "sum", "cards"):
        await run_action(ctx, chat, uid, action)


async def health(_request):
    return web.Response(text="ok")


async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("vent", vent_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(TypeHandler(Update, track), group=-1)
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_hint, pattern=r"^h:"))
    app.add_handler(CallbackQueryHandler(on_retry, pattern=r"^retry$"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.PDF,
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
    await app.updater.start_polling(drop_pending_updates=False)
    log.info("Bot is running")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
