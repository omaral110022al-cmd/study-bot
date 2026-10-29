import asyncio
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import tempfile
import time
from collections import Counter, OrderedDict

import aiohttp
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
VOICE_AR = os.getenv("VOICE_AR", "ar-JO-TaimNeural").strip()
VOICE_EN = os.getenv("VOICE_EN", "en-US-AndrewNeural").strip()
UPLOAD_MIN = int(os.getenv("UPLOAD_MIN_KB", "300")) * 1024  # bigger files go through the Gemini Files API
MAX_VOICE_SEC = int(os.getenv("MAX_VOICE_SEC", "180"))
CACHE_MAX_CHARS = int(os.getenv("CACHE_MAX_CHARS", "6000000"))
FONT_DIR = "/tmp/studybot_fonts"

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
    "3) أو اكتب (أو سجّل فويس) طلبك:\n"
    "• اشرح صفحة 5\n"
    "• حل السؤال 3 صفحة 4\n"
    "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
    "• لخص صفحة 2\n"
    "• مصطلحات صفحة 7\n"
    "• توقعات امتحان صفحة 1-4\n"
    "• ملخص صوتي لصفحة 2-5 🎧\n"
    "• حلل أسئلة سنوات (لما ترفع امتحانات سابقة) 📊\n"
    "• /find قانون أوم  (يدلك أي صفحة فيها الموضوع) 🔍\n"
    "• أو اسألني أي سؤال عن المادة\n\n"
    "4) 🎙️ تقدر تبعت فويس بدل الكتابة، وبفهمه وبرد عليك.\n"
    "5) 📄 تحت كل شرح في زر «تحميل كـ PDF» (أو ملف HTML إذا ما اشتغل الـ PDF).\n"
    "6) نصيحة: ابعت الصور «كملف» (بدون ضغط) عشان تطلع أوضح.\n"
    "7) رقم الصفحة = ترتيبها داخل الملف، مش رقمها المطبوع.\n"
    "8) /new ملف جديد  /vent فضفضة  /about عن البوت"
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

PAPERS_SYS = (
    """You are an expert exam analyst. The student uploaded PAST EXAM PAPERS (one paper or several years). Do NOT solve the questions. Analyze them and report:

§ 📊 نظرة عامة: how many papers and questions, the exam structure, question types (multiple choice, true/false, short answer, essay, numeric), the distribution of marks, and how heavy the exam is.
§ 🔁 أكثر المواضيع تكراراً: a ranked list of topics or chapters with how many questions touch each one, their approximate share of the marks, and in which paper or year they appear.
§ 🧮 القوانين والمفاهيم اللي بيركز عليها الدكتور: the laws, formulas, definitions, derivations and diagrams that the examiner keeps asking about.
§ 🧩 أنماط الأسئلة المتكررة: repeated or near-identical questions and the usual twists.
§ 🎯 توقعات: the topics most likely to appear next, ranked, with reasons grounded ONLY in what is visible in the papers.
§ 🗓️ خطة مذاكرة مقترحة: priorities (لازم / مهم / إذا في وقت) with a short suggested order.

Be honest about the limits: if there is only one paper, say that the patterns are weak. Never invent questions, years or numbers that are not visible in the files. Read every page carefully. Write in Arabic and keep the technical terms in English, unless the student asks for another language."""
    + VISION_RULES
    + FORMAT_RULES
)

AUDIO_SYS = """You are a friendly teacher recording a short audio lesson for a student who will listen while walking to university. Turn the given content into a spoken-style summary script: one clear opening sentence (what this part is about), then the key ideas in a logical order, the important definitions and laws explained in words, one or two simple examples, and a short closing recap of the most important points.
Rules:
- Length: about 400 to 900 words depending on the content.
- The text will be read aloud by a text-to-speech voice, so write plain flowing sentences only: no emojis, no symbols, no bullet points, no headings, no markdown, no parentheses lists. Say formulas in words (for example: force equals mass times acceleration) and write units in words.
- Language: Arabic content gets an Arabic script (simple and friendly Modern Standard Arabic). English content gets an English script. If the student's request asks for a language, follow it.
- Short sentences and a natural rhythm."""

FIND_SYS = """You are a precise study-search assistant. The student gives a topic or a question, and a document whose pages are in order. Find where the topic appears. Return ONLY valid JSON, no code fences:
{"explained": [page numbers where the topic is taught or explained, best first],
 "questions": [page numbers with exercises or questions about the topic],
 "mentioned": [other pages that mention it],
 "summary": "one or two short sentences, in the student's language, saying where it is and what each page contains"}
Page numbers are the position of the page inside the file, starting at 1 (not the printed numbers). Understand meaning, synonyms and Arabic/English translations, not only exact words. If the topic is not in the document, return empty lists and say so in "summary"."""

TRANSCRIBE_SYS = """You are an accurate speech-to-text transcriber. Transcribe exactly what the speaker says, in the language spoken. Arabic dialects are written in plain standard Arabic spelling; keep English technical terms in English letters. Write every number as digits (for example: صفحة 5, not صفحة خمسة). Output ONLY the transcript, with no quotes or comments. If there is no intelligible speech, output exactly: [unclear]"""

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
# action -> (system, default request, max tokens, temperature); one call for the whole selection
TEXT_ACTIONS = {
    "sum": (SUMMARY_SYS, "Write the cheat sheet now.", 16000, 0.3),
    "terms": (TERMS_SYS, "Build the full glossary now.", 16000, 0.3),
    "exam": (EXAM_SYS, "Write the likely exam questions with full model answers.", 32000, 0.4),
    "papers": (PAPERS_SYS, "Analyze these past exam papers now.", 24000, 0.3),
    "audio": (AUDIO_SYS, "Write the spoken summary script now.", 8000, 0.5),
}
OUT_TITLES = {
    "smart": "تحليل ذكي",
    "explain": "شرح",
    "solve": "حلول",
    "sum": "ملخص",
    "terms": "مصطلحات",
    "exam": "توقعات امتحان",
    "papers": "تحليل أسئلة سنوات",
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


# ---- result cache (same file + same request = instant answer, no Gemini call) ----
cache: "OrderedDict[str, str]" = OrderedDict()
cache_chars = 0
audio_cache: dict[str, str] = {}  # key -> Telegram file_id of the finished audio
file_refs: dict[str, dict] = {}  # sha256 -> uploaded Gemini file


def cache_get(key: str):
    v = cache.get(key)
    if v is None:
        bump("miss")
        return None
    cache.move_to_end(key)
    bump("hits")
    return v


def cache_put(key: str, text: str):
    global cache_chars
    if not text or not text.strip():
        return
    if key in cache:
        cache_chars -= len(cache[key])
    cache[key] = text
    cache.move_to_end(key)
    cache_chars += len(text)
    while cache and (cache_chars > CACHE_MAX_CHARS or len(cache) > 800):
        _, old = cache.popitem(last=False)
        cache_chars -= len(old)


def ckey(dh: str, action: str, pages, core: str) -> str:
    pg = ",".join(str(p) for p in pages) if pages else "*"
    return f"{dh}|{action}|{pg}|{core}|{MODEL}"


def doc_hash(doc: dict) -> str:
    kind = doc["kind"]
    if kind == "pdf":
        if not doc.get("hash"):
            doc["hash"] = hashlib.sha256(doc["pdf"]).hexdigest()
        return doc["hash"]
    if kind == "text":
        return hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()
    h = hashlib.sha256()
    for i in doc["images"]:
        h.update(hashlib.sha256(i["data"]).digest())
    return h.hexdigest()


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
    total = stats["hits"] + stats["miss"]
    rate = f"{stats['hits'] * 100 // total}%" if total else "-"
    acts = "، ".join(f"{k} {v}" for k, v in stats["actions"].most_common()) or "-"
    top = []
    for i, (uid, c) in enumerate(stats["users"].most_common(5), 1):
        info = seen.get(uid, {}).get("info", f"id: {uid}")
        top.append(f"{i}) {info} — {c}")
    text = (
        f"📊 لوحة الإدارة ({stats['day']})\n\n"
        f"🔹 طلبات اليوم: {stats['req']}\n"
        f"🔹 حسب النوع: {acts}\n"
        f"⚡ الكاش: {stats['hits']} جاهز / {stats['miss']} جديد (نسبة النجاح {rate}) — محفوظ {len(cache)} نتيجة\n"
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


_FILE_ERR = re.compile(r"(?i)(permission|not.?found|404|403|file|uri|expired|invalid.?argument)")


async def call_model(parts, instruction, system, as_json=True, max_tokens=16000,
                     temperature=0.4, retry_parts=None) -> str:
    def mk(ps):
        return list(ps) + [types.Part.from_text(text=instruction)]

    contents = mk(parts)
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
                    if retry_parts is not None and _FILE_ERR.search(msg):
                        # an uploaded file may have expired: send the content inline instead
                        try:
                            alt, _ = await retry_parts()
                            contents = mk(alt)
                            retry_parts = None
                            continue
                        except Exception:
                            retry_parts = None
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
    "terms": ["مصطلحات", "مفردات", "glossary"],
    "exam": ["امتحان", "امتحانات", "توقعات", "exam", "exams"],
    "audio": ["صوتي", "صوتيه", "فويس", "بودكاست", "audio", "podcast", "voice"],
    "papers": ["papers", "pastpapers"],
    "papers_weak": ["سنوات", "سنين", "دورات", "سابقه", "سابقات"],
    "find": ["ابحث", "ابحثلي", "دورلي"],
}
WORD2INTENT = {w: k for k, ws in INTENTS.items() for w in ws}
_STOP = {
    "لي", "ال", "صفحه", "صفحات", "الصفحه", "الصفحات", "الي", "من", "حتي", "لحد", "رقم", "ص",
    "page", "pages", "to", "please", "لو", "سمحت", "بليز", "هاي", "هذه", "هاد", "عن", "في", "بس",
    "كمان", "p", "pg", "ممكن", "بدي", "اريد", "لهاي", "هالصفحه",
}


def detect_intent(text: str):
    found = []
    for tok in re.findall(r"\w+", norm(text)):
        cands = [tok]
        if tok.startswith("و") and len(tok) > 2:
            cands.append(tok[1:])
        if tok.startswith("ال") and len(tok) > 3:
            cands.append(tok[2:])
        for c in cands:
            if c in WORD2INTENT:
                found.append(WORD2INTENT[c])
                break
    # "years / previous" alone is too common in normal questions (history!): it only means
    # past-papers when it comes together with exam / question words.
    if "papers_weak" in found:
        if "exam" in found or "quiz" in found:
            found.append("papers")
        found = [f for f in found if f != "papers_weak"]
    if not found:
        return None
    for special in ("audio", "papers", "find"):
        if special in found:
            return special
    return found[0]


def req_core(extra: str) -> str:
    """The 'content' of a request without intent words, page numbers and filler, so that
    'اشرح صفحة 3' and 'اشرحلي ص3' share one cache entry."""
    keep = []
    t0 = norm(extra or "")
    for rx in (RANGE1, RANGE2, SINGLE):
        t0 = rx.sub(" ", t0)
    for t in re.findall(r"\w+", t0):
        base = t[1:] if t.startswith("و") and len(t) > 2 else t
        if t in WORD2INTENT or base in WORD2INTENT or t.isdigit() or t in _STOP or base in _STOP:
            continue
        keep.append(t)
    return " ".join(keep)[:200] or "default"


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


def check_pages(doc: dict, pages, allow_big: bool = False):
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
        if len(pages) > MAX_PAGES and not allow_big:
            raise DocError(f"حدد {MAX_PAGES} صفحة على الأكثر بالمرة 🙏")
    elif n > MAX_PAGES and not allow_big:
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


async def uploaded_part(data: bytes, mime: str):
    """Upload once to the Gemini Files API (free, kept 48h) and reuse it for every request and every student."""
    h = hashlib.sha256(data).hexdigest()
    ref = file_refs.get(h)
    if not ref or time.time() - ref["t"] > 40 * 3600:
        f = await client.aio.files.upload(file=io.BytesIO(data), config={"mime_type": mime})
        for _ in range(15):
            state = str(getattr(getattr(f, "state", None), "name", None) or getattr(f, "state", ""))
            if "PROCESSING" not in state.upper():
                break
            await asyncio.sleep(1)
            f = await client.aio.files.get(name=f.name)
        ref = {"uri": f.uri, "t": time.time()}
        file_refs[h] = ref
    return types.Part.from_uri(file_uri=ref["uri"], mime_type=mime)


async def make_part(data: bytes, mime: str, inline: bool = False):
    if not inline and len(data) >= UPLOAD_MIN:
        try:
            return await uploaded_part(data, mime)
        except Exception:
            log.warning("files upload failed, sending inline", exc_info=True)
    return types.Part.from_bytes(data=data, mime_type=mime)


async def build_parts(doc: dict, pages, inline: bool = False, allow_big: bool = False):
    """Return (parts, label). Raises DocError with a user-facing message."""
    kind = doc["kind"]
    if kind == "text":
        return [types.Part.from_text(text=doc["text"])], "the pasted text"

    n = doc["n"]

    if kind == "pdf" and not n:
        label = (
            f"the file; focus ONLY on file pages {fmt_pages(pages)}" if pages else "the whole file"
        )
        return [await make_part(doc["pdf"], "application/pdf", inline)], label

    check_pages(doc, pages, allow_big)
    if pages:
        label = f"page {pages[0]}" if len(pages) == 1 else f"pages {fmt_pages(pages)}"
    else:
        label = "all pages"

    if kind == "images":
        imgs = doc["images"]
        chosen = imgs if not pages else [imgs[p - 1] for p in pages]
        parts = [await make_part(i["data"], i["mime"], inline) for i in chosen]
        return parts, label

    # pdf
    if not pages or len(pages) >= n:
        return [await make_part(doc["pdf"], "application/pdf", inline)], label
    try:
        data = await asyncio.to_thread(slice_pdf, doc["pdf"], pages)
        return [types.Part.from_bytes(data=data, mime_type="application/pdf")], label
    except Exception:
        log.exception("pdf slice failed")
        label = f"the file; focus ONLY on file pages {fmt_pages(pages)}"
        return [await make_part(doc["pdf"], "application/pdf", inline)], label


def get_session(uid: int) -> dict:
    return sessions.setdefault(
        uid, {"doc": None, "qz": None, "pending": None, "cap": None, "last": None,
              "out": None, "found": None}
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
            "\n\nأو اكتب (أو سجّل فويس) طلبك مباشرة، مثلاً:\n"
            "• اشرح صفحة 5\n"
            "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
            "• لخص صفحة 2\n"
            "• حل السؤال 3 صفحة 4\n"
            "• /find قانون أوم\n\n"
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
        [b("🎯 توقعات امتحان", "exam"), b("🎧 ملخص صوتي", "audio")],
        [b("🔍 ابحث بالملف", "find"), b("📊 تحليل أسئلة سنوات", "papers")],
        [b("❓ اسأل", "ask")],
    ]
    if s and s.get("out"):
        rows.insert(0, [b("📄 تحميل آخر نتيجة PDF", "pdf")])
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
        "أو اكتب طلبك (أو سجّل فويس 🎙️):\n"
        "• اشرح صفحة 5\n"
        "• اعمل 10 أسئلة من صفحة 3 الى 6\n"
        "• لخص صفحة 2\n"
        "• حل السؤال 3 صفحة 4\n"
        "• مصطلحات صفحة 7\n"
        "• توقعات امتحان صفحة 1-4\n"
        "• ملخص صوتي لصفحة 2-5 🎧\n"
        "• /find قانون أوم 🔍\n"
        "• ارفع أسئلة سنوات سابقة واكتب: حلل أسئلة سنوات 📊\n\n"
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


async def vent_reply(m, ctx: ContextTypes.DEFAULT_TYPE, said: str = None):
    uid = m.from_user.id
    said = said or m.text or ""
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
        text = await call_model([], f"{convo}Student says: {said}", VENT_SYS, False, 4000, 0.8)
        text = text.strip() or "🤍 أنا هون، كمّل احكي."
        hist.append((said, text))
        vent_hist[uid] = hist[-8:]
        low = said.lower()
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
        parts, label, note_txt = [], "", ""
        if doc:
            try:
                parts, label = await build_parts(doc, pages)
            except DocError as e:
                if str(e) == "toobig":
                    note_txt = "\n\n(الملف كبير فما استخدمته بجوابي. اذكر رقم الصفحة لو بدك أجاوب منه، مثلاً: شو معنى X بصفحة 5)"
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

        async def retry():
            return await build_parts(doc, pages, inline=True)

        answer = await call_model(
            parts, f"{ctxnote}{convo}Student question: {text}", ASK_SYS, False, 8000, 0.4,
            retry_parts=retry if parts else None,
        )
        answer = answer.strip() or "..."
        hist.append((text, answer))
        history[uid] = hist[-6:]
        await send_long(ctx, chat, answer + note_txt)
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
    if intent == "find":
        await run_find(ctx, chat, uid, text)
    elif intent:
        await run_action(ctx, chat, uid, intent, pages, extract_count(text), text)
    else:
        await answer_question(ctx, chat, uid, text, pages)


# ------------------------------------------------------------- audio (TTS)

_EMOJI_RE = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d\u20e3]+")


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def clean_script(text: str) -> str:
    t = strip_emoji(text)
    t = re.sub(r"[*#`_~§•▪►■]+", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def pick_voice(script: str) -> str:
    ar = len(re.findall(r"[\u0600-\u06FF]", script))
    la = len(re.findall(r"[A-Za-z]", script))
    return VOICE_AR if ar >= la else VOICE_EN


async def tts_file(text: str, voice: str) -> str:
    import edge_tts  # optional dependency: ImportError is handled by the caller

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        await asyncio.wait_for(edge_tts.Communicate(text[:12000], voice).save(path), timeout=150)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        raise
    return path


async def send_audio_summary(ctx, chat: int, key: str, script: str, wait):
    akey = key + "|mp3"
    fid = audio_cache.get(akey)
    if fid:
        try:
            await ctx.bot.send_audio(chat, audio=fid, title="ملخص صوتي", performer="Study Bot")
            return True
        except Exception:
            audio_cache.pop(akey, None)
    script = clean_script(script)
    if not script:
        return False
    try:
        if wait:
            try:
                await wait.edit_text("🎙️ عم أسجّل الصوت...")
            except Exception:
                pass
        path = await tts_file(script, pick_voice(script))
    except Exception:
        log.warning("tts failed", exc_info=True)
        await ctx.bot.send_message(chat, "ما قدرت أحوّل الملخص لصوت هسا 😕 هاد النص بدالو:")
        await send_long(ctx, chat, script)
        return False
    try:
        with open(path, "rb") as f:
            msg = await ctx.bot.send_audio(chat, audio=f, title="ملخص صوتي", performer="Study Bot")
        if msg and msg.audio:
            audio_cache[akey] = msg.audio.file_id
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    return True


# ------------------------------------------------------------ PDF / HTML export

FONT_FILES = {"reg": None, "bold": None}
_FONT_SOURCES = {
    "reg": [
        ("DejaVuSans.ttf", "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf"),
        ("Amiri-Regular.ttf", "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf"),
    ],
    "bold": [
        ("DejaVuSans-Bold.ttf", "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans-Bold.ttf"),
        ("Amiri-Bold.ttf", "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Bold.ttf"),
    ],
}
_SYSTEM_FONTS = {
    "reg": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


async def ensure_fonts() -> bool:
    if FONT_FILES["reg"]:
        return True
    os.makedirs(FONT_DIR, exist_ok=True)
    for key in ("reg", "bold"):
        found = None
        local = [os.path.join("fonts", n) for n, _ in _FONT_SOURCES[key]]
        cached = [os.path.join(FONT_DIR, n) for n, _ in _FONT_SOURCES[key]]
        for p in local + _SYSTEM_FONTS[key] + cached:
            if os.path.exists(p) and os.path.getsize(p) > 50000:
                found = p
                break
        if not found:
            for name, url in _FONT_SOURCES[key]:
                try:
                    timeout = aiohttp.ClientTimeout(total=45)
                    async with aiohttp.ClientSession(timeout=timeout) as sess:
                        async with sess.get(url) as r:
                            if r.status != 200:
                                continue
                            data = await r.read()
                    if len(data) > 50000:
                        p = os.path.join(FONT_DIR, name)
                        with open(p, "wb") as f:
                            f.write(data)
                        found = p
                        break
                except Exception:
                    log.warning("font download failed: %s", url, exc_info=True)
        FONT_FILES[key] = found
    if FONT_FILES["reg"] and not FONT_FILES["bold"]:
        FONT_FILES["bold"] = FONT_FILES["reg"]
    return bool(FONT_FILES["reg"])


def build_pdf(title: str, text: str, reg_path: str, bold_path: str) -> bytes:
    """A4 PDF with correct Arabic shaping and right-to-left lines (needs reportlab, arabic-reshaper, python-bidi)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    import arabic_reshaper

    try:
        from bidi.algorithm import get_display
    except ImportError:
        from bidi import get_display

    pdfmetrics.registerFont(TTFont("SB-Reg", reg_path))
    pdfmetrics.registerFont(TTFont("SB-Bold", bold_path))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    M = 42
    width = W - 2 * M
    state = {"y": H - M}

    def has_arabic(s):
        return re.search(r"[\u0600-\u06FF]", s) is not None

    def shown(s, base):
        sh = arabic_reshaper.reshape(s)
        try:
            return get_display(sh, base_dir=base)
        except TypeError:
            return get_display(sh)

    def measure(s, font, size):
        return pdfmetrics.stringWidth(arabic_reshaper.reshape(s) if has_arabic(s) else s, font, size)

    def wrap(s, font, size):
        lines, cur = [], ""
        for w in s.split():
            t = (cur + " " + w).strip()
            if cur and measure(t, font, size) > width:
                lines.append(cur)
                cur = w
            else:
                cur = t
        if cur:
            lines.append(cur)
        return lines

    def need(h):
        if state["y"] - h < M:
            c.showPage()
            state["y"] = H - M

    def para(s, font="SB-Reg", size=11, gap=1.55, space_before=0, center=False):
        ar = len(re.findall(r"[\u0600-\u06FF]", s))
        la = len(re.findall(r"[A-Za-z]", s))
        rtl = ar > 0 and ar >= la * 0.5
        state["y"] -= space_before
        for ln in wrap(s, font, size):
            need(size * gap)
            c.setFont(font, size)
            state["y"] -= size * gap
            if has_arabic(ln):
                out = shown(ln, "R" if rtl else "L")
            else:
                out = ln
            if center:
                c.drawCentredString(W / 2, state["y"], out)
            elif rtl:
                c.drawRightString(W - M, state["y"], out)
            else:
                c.drawString(M, state["y"], out)

    para(strip_emoji(title), "SB-Bold", 18, center=True)
    state["y"] -= 8
    c.setLineWidth(0.8)
    c.line(M, state["y"], W - M, state["y"])
    state["y"] -= 6

    for raw in text.split("\n"):
        line = strip_emoji(raw).rstrip()
        if not line.strip():
            state["y"] -= 5
            continue
        s = line.strip()
        if raw.strip().startswith("§"):
            para(s.lstrip("§ ").strip(), "SB-Bold", 14, space_before=9)
        elif _ANS_RE.match(s):
            para(s, "SB-Bold", 11.5)
        else:
            para(s, "SB-Reg", 11)

    c.save()
    return buf.getvalue()


def build_html(title: str, text: str) -> bytes:
    body = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("§"):
            body.append(f"<h3 dir='auto'>{html.escape(s.lstrip('§ ').strip())}</h3>")
        elif _ANS_RE.match(s):
            body.append(f"<p dir='auto'><b>{html.escape(s)}</b></p>")
        else:
            body.append(f"<p dir='auto'>{html.escape(s)}</p>")
    css = (
        "body{font-family:'Segoe UI',Tahoma,Arial,sans-serif;max-width:820px;margin:auto;padding:18px;"
        "line-height:1.9;font-size:17px;color:#111}h2{text-align:center}"
        "h3{margin-top:26px;border-bottom:2px solid #ddd;padding-bottom:4px}p{margin:6px 0}"
    )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>"
        f"<h2 dir='auto'>{html.escape(title)}</h2>" + "\n".join(body) + "</body></html>"
    )
    return doc.encode("utf-8")


async def send_export(ctx, chat: int, uid: int):
    s = sessions.get(uid)
    out = s.get("out") if s else None
    if not out:
        await ctx.bot.send_message(chat, "ما في نتيجة أصدّرها هسا. اطلب شرح أو حل أو ملخص أول 📄")
        return
    wait = await ctx.bot.send_message(chat, "📄 عم أجهّز الملف...")
    try:
        try:
            if not await ensure_fonts():
                raise RuntimeError("no font")
            data = await asyncio.to_thread(
                build_pdf, out["title"], out["text"], FONT_FILES["reg"], FONT_FILES["bold"]
            )
            await ctx.bot.send_document(
                chat, document=data, filename="study_notes.pdf", caption="📄 " + out["title"]
            )
        except Exception:
            log.warning("pdf export failed, sending html", exc_info=True)
            data = build_html(out["title"], out["text"])
            await ctx.bot.send_document(
                chat, document=data, filename="study_notes.html",
                caption="📄 " + out["title"] + "\n(افتحه بالمتصفح، ومن القائمة اختار طباعة ← حفظ كـ PDF)",
            )
    except Exception as e:
        log.exception("export failed")
        await ctx.bot.send_message(chat, fail_text(e))
    finally:
        try:
            await wait.delete()
        except Exception:
            pass


# ------------------------------------------------------------- smart search

async def find_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    query = " ".join(ctx.args or []).strip()
    await run_find(ctx, update.effective_chat.id, uid, query)


async def run_find(ctx, chat: int, uid: int, query: str):
    s = sessions.get(uid)
    doc = s.get("doc") if s else None
    if not doc:
        await ctx.bot.send_message(chat, NODOC_MSG)
        return
    query = (query or "").strip()
    if not query:
        await ctx.bot.send_message(chat, "اكتب الموضوع بعد الأمر، مثلاً:\n/find قانون أوم\n/find تجربة رذرفورد")
        return
    if uid in busy:
        await ctx.bot.send_message(chat, "⏳ استنى لحظة، لسا بشتغل على طلبك السابق")
        return
    doc["seen"] = time.time()
    dh = await asyncio.to_thread(doc_hash, doc)
    key = ckey(dh, "find", None, norm(query)[:200])
    raw = cache_get(key)
    if raw is None:
        if not quota_ok(uid):
            await ctx.bot.send_message(chat, QUOTA_MSG)
            return
        note(uid, "find")
        busy.add(uid)
        wait = None
        try:
            await ctx.bot.send_chat_action(chat, "typing")
            wait = await ctx.bot.send_message(chat, "🔍 عم أدور بالملف...")
            parts, label = await build_parts(doc, None, allow_big=True)

            async def retry():
                return await build_parts(doc, None, inline=True, allow_big=True)

            raw = await call_model(
                parts, f"[Content: {label}]\nStudent topic: {query}", FIND_SYS, True, 4000, 0.1, retry
            )
            cache_put(key, raw)
        except Exception as e:
            log.exception("find failed")
            await ctx.bot.send_message(chat, fail_text(e))
            return
        finally:
            busy.discard(uid)
            if wait:
                try:
                    await wait.delete()
                except Exception:
                    pass
    try:
        data = load_json(raw)
    except Exception:
        await ctx.bot.send_message(chat, ERR_MSG)
        return

    n = doc.get("n") or 0

    def clean(lst):
        out = []
        for x in lst or []:
            try:
                p = int(x)
            except Exception:
                continue
            if p >= 1 and (not n or p <= n) and p not in out:
                out.append(p)
        return out

    expl, qs, men = clean(data.get("explained")), clean(data.get("questions")), clean(data.get("mentioned"))
    allp = []
    for p in expl + qs:
        if p not in allp:
            allp.append(p)
    lines = [f"🔍 «{query}»", ""]
    summary = str(data.get("summary", "")).strip()
    if summary:
        lines.append(summary)
        lines.append("")
    if expl:
        lines.append(f"📖 مشروح بصفحة: {fmt_pages(sorted(expl))}")
    if qs:
        lines.append(f"❓ أسئلة عليه بصفحة: {fmt_pages(sorted(qs))}")
    if men:
        lines.append(f"📌 مذكور كمان بصفحة: {fmt_pages(sorted(men))}")
    if not (expl or qs or men) and not summary:
        lines.append("ما لقيت هالموضوع بالملف 🤔")
    s["found"] = {
        "e": sorted(expl)[:MAX_PAGES],
        "q": sorted(qs)[:MAX_PAGES],
        "a": sorted(allp)[:MAX_PAGES],
        "query": query,
    }
    rows = []
    if expl:
        rows.append([InlineKeyboardButton("📖 اشرح صفحات الشرح", callback_data="f:e")])
    if qs:
        rows.append([InlineKeyboardButton("✅ حل الأسئلة عليه", callback_data="f:q")])
    if len(allp) > 1:
        rows.append([InlineKeyboardButton("🧠 تحليل ذكي لكل هالصفحات", callback_data="f:a")])
    await ctx.bot.send_message(
        chat, "\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(rows) if rows else None
    )


async def on_find_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    uid = cq.from_user.id
    if not allowed(uid):
        return
    s = sessions.get(uid)
    found = s.get("found") if s else None
    if not found or not s.get("doc"):
        await ctx.bot.send_message(cq.message.chat_id, NODOC_MSG)
        return
    kind = cq.data.split(":")[1]
    pages = found.get(kind) or None
    if not pages:
        return
    action = {"e": "explain", "q": "solve", "a": "smart"}[kind]
    extra = f"Focus on the topic: {found.get('query', '')}" if kind == "e" else ""
    await run_action(ctx, cq.message.chat_id, uid, action, pages, None, extra)


# ------------------------------------------------------------- actions

async def run_action(ctx, chat: int, uid: int, action: str, pages=None, nq=None, extra: str = "", harder: bool = False):
    s = sessions.get(uid)
    if not s or not s.get("doc"):
        await ctx.bot.send_message(chat, NODOC_MSG)
        return
    if uid in busy:
        await ctx.bot.send_message(chat, "⏳ استنى لحظة، لسا بشتغل على طلبك السابق")
        return
    busy.add(uid)  # claimed before any await, so a double tap cannot start two jobs
    wait = None
    try:
        doc = s["doc"]
        try:
            check_pages(doc, pages)
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

        doc["seen"] = time.time()
        dh = await asyncio.to_thread(doc_hash, doc)
        core = req_core(extra)
        batched = action in BATCHED
        cacheable = batched or action in TEXT_ACTIONS
        batches = make_batches(doc, pages) if batched else None
        keys = [ckey(dh, action, b, core) for b in batches] if batched else [ckey(dh, action, pages, core)]
        all_cached = cacheable and all(k in cache for k in keys)
        if not all_cached and not quota_ok(uid):
            await ctx.bot.send_message(chat, QUOTA_MSG)
            return
        note(uid, action)

        await ctx.bot.send_chat_action(chat, "typing")
        wait = await ctx.bot.send_message(chat, "⏳ عم أشتغل على طلبك...")
        if all_cached:
            await ctx.bot.send_message(chat, "⚡ هالطلب انحلّ قبل، هاي النتيجة فوراً:")
        req = extra.strip()

        if batched:
            system, default_req = BATCHED[action]
            total = len(batches)
            finished = True
            outs = []
            for i, b in enumerate(batches, 1):
                key = keys[i - 1]
                text = cache_get(key)
                from_cache = text is not None
                try:
                    if text is None:
                        if total > 1:
                            try:
                                await wait.edit_text(f"⏳ عم أشتغل على طلبك... ({i}/{total})")
                            except Exception:
                                pass
                        bparts, blabel = await build_parts(doc, b)
                        note_txt = f"[Content: {blabel}]"
                        if b and doc["kind"] != "text":
                            plist = ", ".join(str(p) for p in b)
                            note_txt += (
                                f"\n[This batch contains ONLY these original page numbers, in this order: {plist}. "
                                "Refer to them by these original numbers. Begin each page with a heading line "
                                "'§ 📄 صفحة N | Page N' using these numbers.]"
                            )
                        instr = f"{note_txt}\nStudent request: {req or default_req}"

                        async def retry(b=b):
                            return await build_parts(doc, b, inline=True)

                        text = await call_model(bparts, instr, system, False, 32000, 0.3, retry)
                        cache_put(key, text)
                    outs.append(text)
                    await send_long(ctx, chat, text)
                except Exception as e:
                    log.exception("batch failed")
                    msg = fail_text(e)
                    if total > 1:
                        msg += f"\n(تم إنجاز {i - 1} من {total} أجزاء، كمّل بعدين من الصفحات الباقية)"
                    await ctx.bot.send_message(chat, msg)
                    finished = False
                    break
                if i < total and not from_cache:
                    await asyncio.sleep(3)
            if outs:
                s["out"] = {"title": f"{OUT_TITLES[action]} - {doc['name']}", "text": "\n\n".join(outs)[:300000]}
            if finished and action in ("solve", "smart"):
                await ctx.bot.send_message(chat, cheer())
            await send_menu(ctx, chat, s)

        elif action in TEXT_ACTIONS:
            system, default_req, max_tok, temp = TEXT_ACTIONS[action]
            key = keys[0]
            text = cache_get(key)
            if text is None:
                parts, label = await build_parts(doc, pages)
                instr = f"[Content: {label}]\nStudent request: {req or default_req}"

                async def retry():
                    return await build_parts(doc, pages, inline=True)

                text = await call_model(parts, instr, system, False, max_tok, temp, retry)
                cache_put(key, text)
            if action == "audio":
                await send_audio_summary(ctx, chat, key, text, wait)
            else:
                await send_long(ctx, chat, text)
                s["out"] = {"title": f"{OUT_TITLES[action]} - {doc['name']}", "text": text[:300000]}
            await send_menu(ctx, chat, s)

        elif action == "cards":
            parts, label = await build_parts(doc, pages)
            instr = f"[Content: {label}]\nStudent request: {req or 'Create the flashcards now.'}"

            async def retry():
                return await build_parts(doc, pages, inline=True)

            raw = await call_model(parts, instr, CARDS_SYS, True, 8000, 0.5, retry)
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
            parts, label = await build_parts(doc, pages)
            instr = f"[Content: {label}]\nWrite exactly {n} {flavor}questions. Student request: {req or 'none'}."
            old = s.get("qz")
            if harder and old:
                prev = "\n".join(q["q"] for q in old["questions"])
                instr += f"\nDo not repeat these questions:\n{prev}"

            async def retry():
                return await build_parts(doc, pages, inline=True)

            raw = await call_model(parts, instr, QUESTIONS_SYS, True, 16000, 0.7, retry)
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
            s["out"] = None
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
    s["out"] = None
    s["found"] = None
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


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A voice note is transcribed, then handled exactly like typed text."""
    m = update.message
    uid = m.from_user.id
    if not allowed(uid):
        await deny(update)
        return
    media = m.voice or m.audio
    if not media:
        return
    if (media.duration or 0) > MAX_VOICE_SEC:
        await m.reply_text(f"الفويس طويل 🙏 خليه أقل من {MAX_VOICE_SEC // 60} دقايق.")
        return
    if (media.file_size or 0) > 10 * 1024 * 1024:
        await m.reply_text("ملف الصوت كبير، ابعت فويس أقصر.")
        return
    try:
        await ctx.bot.send_chat_action(m.chat_id, "typing")
        f = await media.get_file()
        data = bytes(await f.download_as_bytearray())
        mime = getattr(media, "mime_type", None) or "audio/ogg"
        text = await call_model(
            [types.Part.from_bytes(data=data, mime_type=mime)],
            "Transcribe this audio.", TRANSCRIBE_SYS, False, 2000, 0.0,
        )
    except Exception as e:
        log.exception("transcription failed")
        await m.reply_text(fail_text(e))
        return
    text = (text or "").strip()
    if not text or text == "[unclear]":
        await m.reply_text("ما قدرت أسمعك منيح 🎙️ جرّب تعيد بصوت أوضح.")
        return
    await m.reply_text(f"🎙️ سمعتك: «{text[:500]}»")
    if uid in venting:
        await vent_reply(m, ctx, text)
        return
    await handle_text(ctx, m.chat_id, uid, text)


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
            "اكتب سؤالك (أو سجّل فويس 🎙️) وبجاوبك ✍️\n(وإذا بدك من صفحة معينة اذكر رقمها، مثلاً: شو معنى X بصفحة 5)",
        )
        return
    if action == "find":
        await ctx.bot.send_message(
            chat,
            "اكتب الموضوع اللي بدك تدور عليه بالملف 🔍\nمثلاً:\n/find قانون أوم\n/find تجربة رذرفورد",
        )
        return
    if action == "pdf":
        await send_export(ctx, chat, uid)
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
    if action in ("smart", "explain", "solve", "quiz", "sum", "cards", "terms", "exam", "audio", "papers"):
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
    app.add_handler(CommandHandler(["find", "search"], find_cmd))
    app.add_handler(TypeHandler(Update, track), group=-1)
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_hint, pattern=r"^h:"))
    app.add_handler(CallbackQueryHandler(on_retry, pattern=r"^retry$"))
    app.add_handler(CallbackQueryHandler(on_find_btn, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(on_bcast, pattern=r"^b:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
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
