import os
import logging
import json
import re
import io
import time
import asyncio
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
import httpx
from resume_processor import ResumeProcessor

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Conversation States ─────────────────────────────────────────────────────
(
    WAITING_RESUME,
    WAITING_JD,
    PROCESSING,
    SCAN_WAITING_RESUME,
    KW_WAITING_JD,
    TIPS_WAITING_ROLE,
) = range(6)

# ─── In-memory history store  {user_id: [ {ts, score_before, score_after, filename}, … ]} ──
_history: dict[int, list] = defaultdict(list)
MAX_HISTORY = 5

# ─────────────────────────────────────────────────────────────────────────────
#  Static text blocks - ESCAPED for MarkdownV2
# ─────────────────────────────────────────────────────────────────────────────

WELCOME_TEXT = r"""
👋 *Welcome to Resume ATS Optimizer Bot\!*

I help you tailor your resume to maximise your ATS \(Applicant Tracking System\) score for any job posting\.

*What I can do:*
• 📊 Instant ATS health check on your resume
• 🎯 Tailor your resume to a specific job description
• 🔑 Extract the most important keywords from any JD
• 💡 Give role\-specific resume writing tips
• 📈 Track your tailoring history

*Commands:*
/tailor — Full AI tailoring \(resume \+ JD → tailored DOCX\)
/scan — Quick ATS health check only
/keywords — Extract keywords from a job description
/tips — Role\-specific resume writing tips
/history — View your last tailoring sessions
/help — Usage guide
/cancel — Cancel current operation

Ready? Type /tailor to begin\! 🚀
"""

HELP_TEXT = r"""
*📖 How to Get the Best Results*

1️⃣ *Start with /scan* — Upload your resume for an instant ATS health check before anything else\.

2️⃣ *Use /keywords first \(optional\)* — Paste a JD to see the top keywords you need to target\.

3️⃣ *Run /tailor* — Upload your resume \+ paste the JD\. The AI will:
   • Naturalistically inject missing keywords
   • Rewrite your summary to match the role
   • Strengthen bullet points with action verbs \& numbers
   • Add a Core Competencies section if missing

4️⃣ *Download \& submit* — You receive a formatted DOCX ready to send\.

*💡 Pro Tips:*
• Run /tailor separately for each job — every JD is unique
• Use a clean single\-column layout for best ATS parsing
• Include numbers \& percentages in your bullets
• Type /tips to get role\-specific advice before tailoring
"""

TIPS_PROMPT = r"""
*💡 Resume Tips — Choose Your Role Category:*

Pick the category closest to your target role and I'll send you tailored advice\.
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _escape_markdown(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in special_chars else c for c in text)


def _score_bar(score: int, width: int = 20) -> str:
    """Return a visual progress bar string for a score 0–100."""
    filled = round(score / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"`[{bar}]` {score}%"


def _score_badge(score: int) -> tuple[str, str]:
    if score >= 80:
        return "🟢", "Excellent"
    if score >= 65:
        return "🟢", "Good"
    if score >= 45:
        return "🟡", "Fair"
    if score >= 25:
        return "🟠", "Weak"
    return "🔴", "Critical"


def _fmt_keywords(keywords: list, limit: int = 15) -> str:
    if not keywords:
        return "_None_"
    return "  " + "   •   ".join(f"`{kw}`" for kw in keywords[:limit])


def _record_history(user_id: int, filename: str, score_before: int, score_after: int):
    entry = {
        "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "filename": filename,
        "score_before": score_before,
        "score_after": score_after,
        "gain": score_after - score_before,
    }
    _history[user_id].insert(0, entry)
    _history[user_id] = _history[user_id][:MAX_HISTORY]


async def _animated_progress(msg, steps: list[str], delay: float = 1.2):
    """Cycle through processing status lines with a spinner."""
    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    for i, step in enumerate(steps):
        spinner = spinners[i % len(spinners)]
        try:
            await msg.edit_text(
                f"{spinner} *{_escape_markdown(step)}*\n\n_Please wait…_",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────────
#  Error handler
# ─────────────────────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong\\. Please try again or use /cancel to reset\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  /start  /help
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🎯 Tailor Resume",  callback_data="menu_tailor"),
            InlineKeyboardButton("📊 Quick Scan",     callback_data="menu_scan"),
        ],
        [
            InlineKeyboardButton("🔑 Extract Keywords", callback_data="menu_keywords"),
            InlineKeyboardButton("💡 Resume Tips",      callback_data="menu_tips"),
        ],
        [InlineKeyboardButton("📈 My History", callback_data="menu_history")],
    ]
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses from the /start menu."""
    query = update.callback_query
    await query.answer()
    cmd = query.data

    if cmd == "menu_tailor":
        await query.message.reply_text(
            "📄 *Step 1 of 2 — Upload Your Resume*\n\n"
            "Send your resume as a *PDF* or *DOCX* file\\.",
            parse_mode="MarkdownV2",
        )
        context.user_data.clear()
        context.user_data["_from_menu"] = "tailor"

    elif cmd == "menu_scan":
        await query.message.reply_text(
            "📊 *Quick ATS Scan*\n\nSend me your resume \\(PDF or DOCX\\) and I'll give you an instant health report\\.",
            parse_mode="MarkdownV2",
        )
        context.user_data.clear()
        context.user_data["_from_menu"] = "scan"

    elif cmd == "menu_keywords":
        await query.message.reply_text(
            "🔑 *Keyword Extractor*\n\nPaste the job description as a text message and I'll pull out the top ATS keywords\\.",
            parse_mode="MarkdownV2",
        )
        context.user_data.clear()
        context.user_data["_from_menu"] = "keywords"

    elif cmd == "menu_tips":
        await _send_tips_menu(query.message)

    elif cmd == "menu_history":
        await _send_history(query.message, query.from_user.id)


# ─────────────────────────────────────────────────────────────────────────────
#  /cancel
# ─────────────────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled\\. Type /tailor, /scan, /keywords, or /tips to start\\.",
        parse_mode="MarkdownV2",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
#  /tailor  flow  (resume → JD → tailored DOCX)
# ─────────────────────────────────────────────────────────────────────────────

async def tailor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📄 *Step 1 of 2 — Upload Your Resume*\n\n"
        "Send me your resume as a *PDF* or *DOCX* file\\.\n\n"
        "_/cancel to stop at any time\\._",
        parse_mode="MarkdownV2",
    )
    return WAITING_RESUME


async def receive_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "⚠️ Please send a *file* \\(PDF or DOCX\\), not text or an image\\.",
            parse_mode="MarkdownV2",
        )
        return WAITING_RESUME

    fname = doc.file_name or ""
    if not (fname.lower().endswith(".pdf") or fname.lower().endswith(".docx")):
        await update.message.reply_text(
            "⚠️ Only *PDF* and *DOCX* are supported\\. Please try again\\.",
            parse_mode="MarkdownV2",
        )
        return WAITING_RESUME

    status_msg = await update.message.reply_text(
        "⬇️ Downloading resume… running ATS health check, please wait\\.",
        parse_mode="MarkdownV2",
    )

    tg_file = await doc.get_file()
    file_bytes = bytes(await tg_file.download_as_bytearray())
    resume_ext = Path(fname).suffix.lower()

    context.user_data["resume_bytes"] = file_bytes
    context.user_data["resume_name"]  = fname
    context.user_data["resume_ext"]   = resume_ext

    # ── Instant ATS health check ──────────────────────────────────────────────
    try:
        await status_msg.edit_text(
            "🔍 *Scanning your resume…* \\(AI in progress\\)",
            parse_mode="MarkdownV2",
        )
        processor = ResumeProcessor()
        scan = await processor.quick_scan(file_bytes, resume_ext)
        report = _build_scan_report(fname, scan, footer="tailor")
        await status_msg.edit_text(report, parse_mode="MarkdownV2")

    except Exception as e:
        logger.warning("quick_scan failed: %s", e)
        await status_msg.edit_text(
            "✅ *Resume received\\!*\n\n"
            "📋 *Step 2 — Paste the Job Description*\n\n"
            "Copy and paste the full JD as a text message\\.",
            parse_mode="MarkdownV2",
        )

    return WAITING_JD


async def receive_jd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jd_text = (update.message.text or "").strip()
    if len(jd_text) < 50:
        await update.message.reply_text(
            "⚠️ The job description seems too short\\. Please paste the full JD \\(at least a few sentences\\)\\.",
            parse_mode="MarkdownV2",
        )
        return WAITING_JD

    context.user_data["jd_text"] = jd_text

    # Show a keyword preview so the user knows what we'll optimise for
    processor = ResumeProcessor()
    top_kw = processor._extract_keywords(jd_text)[:12]
    kw_preview = "  " + "   •   ".join(f"`{k}`" for k in top_kw)

    keyboard = [
        [
            InlineKeyboardButton("🚀 Tailor My Resume", callback_data="confirm_tailor"),
            InlineKeyboardButton("❌ Cancel",            callback_data="cancel_tailor"),
        ]
    ]
    preview_text = jd_text[:250] + ("…" if len(jd_text) > 250 else "")
    await update.message.reply_text(
        f"📋 *JD received\\!*\n\n_{_escape_markdown(preview_text)}_\n\n"
        f"*🔑 Top keywords detected:*\n{kw_preview}\n\n"
        "Ready to tailor your resume around these keywords?",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PROCESSING


async def confirm_tailor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_tailor":
        await query.edit_message_text("❌ Cancelled\\. Type /tailor to start again\\.", parse_mode="MarkdownV2")
        context.user_data.clear()
        return ConversationHandler.END

    resume_bytes = context.user_data.get("resume_bytes")
    resume_ext   = context.user_data.get("resume_ext")
    jd_text      = context.user_data.get("jd_text")
    resume_name  = context.user_data.get("resume_name", "resume")
    user_id      = query.from_user.id

    # Animated progress message
    prog_msg = await query.message.reply_text("⚙️ Starting…")
    steps = [
        "Parsing your resume content…",
        "Extracting JD keywords & requirements…",
        "AI is rewriting your professional summary…",
        "Injecting keywords into skills & experience…",
        "Calculating ATS score improvement…",
        "Building your formatted DOCX…",
    ]
    progress_task = asyncio.create_task(_animated_progress(prog_msg, steps))

    try:
        processor = ResumeProcessor()
        result = await processor.process(resume_bytes, resume_ext, jd_text)
        progress_task.cancel()

        score_before = result["score_before"]
        score_after  = result["score_after"]
        gain         = score_after - score_before
        _record_history(user_id, resume_name, score_before, score_after)

        e_before, l_before = _score_badge(score_before)
        e_after,  l_after  = _score_badge(score_after)

        score_report = (
            f"✅ *Resume Tailored Successfully\\!*\n\n"
            f"📊 *ATS Score Comparison*\n"
            f"{'─' * 30}\n"
            f"Before  {e_before} {_score_bar(score_before)}  _{l_before}_\n"
            f"After   {e_after}  {_score_bar(score_after)}  _{l_after}_\n"
            f"Gain    🎉 `\\+{gain}%` improvement\n\n"
            f"🔑 *Keywords injected:*\n{_fmt_keywords(result['keywords_added'])}\n\n"
            f"💡 *Changes made:*\n_{_escape_markdown(result['summary_of_changes'])}_\n\n"
            f"📥 Your tailored resume is below — good luck\\! 🍀"
        )

        await prog_msg.edit_text(score_report, parse_mode="MarkdownV2")

        # Send DOCX
        stem = Path(resume_name).stem
        out_filename = f"{stem}_tailored_ats.docx"
        keyboard = [[
            InlineKeyboardButton("🔄 Tailor for Another JD", callback_data="restart_tailor"),
            InlineKeyboardButton("📈 My History",            callback_data="show_history"),
        ]]
        await query.message.reply_document(
            document=io.BytesIO(result["docx_bytes"]),
            filename=out_filename,
            caption="📄 ATS-optimised resume attached\\.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        progress_task.cancel()
        logger.error("Processing error: %s", e, exc_info=True)
        await prog_msg.edit_text(
            f"❌ *Processing failed\\.*\n\n`{_escape_markdown(str(e)[:300])}`\n\nType /tailor to try again\\.",
            parse_mode="MarkdownV2",
        )

    context.user_data.clear()
    return ConversationHandler.END


async def post_tailor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Tailor for Another JD' and 'My History' buttons after delivery."""
    query = update.callback_query
    await query.answer()

    if query.data == "restart_tailor":
        if context.user_data.get("resume_bytes"):
            await query.message.reply_text(
                "📋 *Paste the new Job Description* and I'll tailor the same resume for it\\.",
                parse_mode="MarkdownV2",
            )
            context.user_data.pop("jd_text", None)
        else:
            await query.message.reply_text(
                "Please start fresh with /tailor — the previous resume session has expired\\.",
                parse_mode="MarkdownV2",
            )

    elif query.data == "show_history":
        await _send_history(query.message, query.from_user.id)


# ─────────────────────────────────────────────────────────────────────────────
#  /scan  flow  (resume only → health report, no JD needed)
# ─────────────────────────────────────────────────────────────────────────────

async def scan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📊 *Quick ATS Scan*\n\n"
        "Send me your resume \\(PDF or DOCX\\) and I'll give you a detailed health report — "
        "no job description needed\\.\n\n"
        "_/cancel to stop\\._",
        parse_mode="MarkdownV2",
    )
    return SCAN_WAITING_RESUME


async def scan_receive_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "⚠️ Please send a PDF or DOCX file\\.",
            parse_mode="MarkdownV2",
        )
        return SCAN_WAITING_RESUME

    fname = doc.file_name or ""
    if not (fname.lower().endswith(".pdf") or fname.lower().endswith(".docx")):
        await update.message.reply_text(
            "⚠️ Only PDF and DOCX supported\\.",
            parse_mode="MarkdownV2",
        )
        return SCAN_WAITING_RESUME

    status_msg = await update.message.reply_text("🔍 *Scanning your resume…*", parse_mode="MarkdownV2")

    tg_file = await doc.get_file()
    file_bytes = bytes(await tg_file.download_as_bytearray())
    resume_ext = Path(fname).suffix.lower()

    try:
        processor = ResumeProcessor()
        scan = await processor.quick_scan(file_bytes, resume_ext)
        report = _build_scan_report(fname, scan, footer="scan")
        await status_msg.edit_text(report, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error("Scan error: %s", e, exc_info=True)
        await status_msg.edit_text(
            f"❌ Could not scan resume: `{_escape_markdown(str(e)[:200])}`",
            parse_mode="MarkdownV2",
        )

    return ConversationHandler.END


def _build_scan_report(fname: str, scan: dict, footer: str = "tailor") -> str:
    score   = scan["general_score"]
    wc      = scan["word_count"]
    flags   = scan["section_flags"]
    verdict = scan.get("overall_verdict", "")
    emoji, label = _score_badge(score)

    section_lines = "\n".join(
        f"  {'✅' if v else '❌'} {_escape_markdown(k.replace('_', ' ').title())}"
        for k, v in flags.items()
    )
    strengths = "\n".join(f"  ✦ {_escape_markdown(s)}" for s in scan.get("strengths", [])[:3]) or "  ✦ None detected"
    gaps      = "\n".join(f"  ✦ {_escape_markdown(g)}" for g in scan.get("gaps", [])[:3])      or "  ✦ None detected"
    tips      = "\n".join(f"  ✦ {_escape_markdown(t)}" for t in scan.get("formatting_tips", [])[:3]) or "  ✦ None"

    footer_line = (
        "\n📋 *Now paste the Job Description below* to tailor your resume for a specific role\\!"
        if footer == "tailor"
        else "\n💡 Run /tailor to optimise this resume for a specific job, or /tips for writing advice\\."
    )

    return (
        f"📊 *ATS Health Check — {_escape_markdown(fname)}*\n"
        f"{'─' * 34}\n\n"
        f"{emoji} *Score: {score}% — {label}*\n"
        f"{_score_bar(score)}\n"
        f"📝 Word count: `{wc}` words\n\n"
        f"*📋 Sections:*\n{section_lines}\n\n"
        f"*💪 Strengths:*\n{strengths}\n\n"
        f"*⚠️ Gaps:*\n{gaps}\n\n"
        f"*🎨 Formatting Tips:*\n{tips}\n\n"
        f"*🤖 AI Verdict:*\n_{_escape_markdown(verdict)}_\n"
        f"{'─' * 34}"
        f"{footer_line}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /keywords  flow  (JD text → keyword list, no resume needed)
# ─────────────────────────────────────────────────────────────────────────────

async def keywords_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🔑 *Keyword Extractor*\n\n"
        "Paste the full job description as a text message and I'll extract "
        "the top ATS keywords you should include in your resume\\.\n\n"
        "_/cancel to stop\\._",
        parse_mode="MarkdownV2",
    )
    return KW_WAITING_JD


async def keywords_receive_jd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jd_text = (update.message.text or "").strip()
    if len(jd_text) < 40:
        await update.message.reply_text(
            "⚠️ Paste the full job description \\(more text needed\\)\\.",
            parse_mode="MarkdownV2",
        )
        return KW_WAITING_JD

    status_msg = await update.message.reply_text("🔍 Extracting keywords…")

    try:
        processor = ResumeProcessor()

        # Get AI-enriched keyword breakdown
        kw_data = await processor.extract_keywords_with_ai(jd_text)

        must_have    = kw_data.get("must_have", [])
        nice_to_have = kw_data.get("nice_to_have", [])
        skills       = kw_data.get("skills", [])
        soft_skills  = kw_data.get("soft_skills", [])
        job_title    = kw_data.get("job_title", "")
        seniority    = kw_data.get("seniority", "")

        def fmt_list(items, limit=10):
            return "\n".join(f"  • `{_escape_markdown(i)}`" for i in items[:limit]) or "  • None"

        report = (
            f"🔑 *Keyword Analysis*\n"
            f"{'─' * 32}\n\n"
        )
        if job_title:
            report += f"*Role:* {_escape_markdown(job_title)}   |   *Level:* {_escape_markdown(seniority)}\n\n"

        report += (
            f"*🔴 Must\\-Have Keywords:*\n{fmt_list(must_have)}\n\n"
            f"*🟡 Nice\\-to\\-Have:*\n{fmt_list(nice_to_have)}\n\n"
            f"*🛠 Technical Skills:*\n{fmt_list(skills)}\n\n"
            f"*🤝 Soft Skills:*\n{fmt_list(soft_skills)}\n\n"
            f"{'─' * 32}\n"
            r"💡 _Use /tailor to automatically inject these into your resume\._"
        )

        await status_msg.edit_text(report, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error("Keyword extraction error: %s", e, exc_info=True)
        # Fallback to basic extraction
        processor = ResumeProcessor()
        top_kw = processor._extract_keywords(jd_text)[:20]
        basic = "\n".join(f"  • `{_escape_markdown(k)}`" for k in top_kw)
        await status_msg.edit_text(
            f"🔑 *Top Keywords from JD:*\n\n{basic}\n\n"
            "_Use /tailor to inject these into your resume\\._",
            parse_mode="MarkdownV2",
        )

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
#  /tips  flow  (role-specific resume tips)
# ─────────────────────────────────────────────────────────────────────────────

ROLE_TIPS = {
    "software": {
        "title": "Software Engineering",
        "tips": [
            r"Lead bullets with the tech stack: _'Built X using Python/Django/PostgreSQL…'_",
            "Quantify scale: users served, requests/sec, latency reduction, uptime %",
            "List GitHub, portfolio, or notable open\\-source contributions",
            "Include a dedicated _Technologies_ section: languages, frameworks, cloud, tools",
            "ATS parses skills separately — list both abbreviations and full names \\(e\\.g\\. 'ML / Machine Learning'\\)",
            "Show progression: Junior → Mid → Senior \\(title changes or scope growth\\)",
            "Include system design wins: _'Designed microservices reducing infra cost by 30%'_",
        ],
    },
    "data": {
        "title": "Data Science / Analytics",
        "tips": [
            r"Name your ML models and datasets: _'XGBoost model predicting churn \(AUC 0\.91\)'_",
            "Quantify business impact: revenue uplift, cost savings, decision accuracy",
            "List tools explicitly: Python, R, SQL, Spark, Tableau, PowerBI, dbt, Airflow",
            "Mention model deployment if applicable \\(MLflow, SageMaker, Docker\\)",
            "Include your Kaggle rank, published papers, or data blog if relevant",
            "Separate 'Modelling' skills from 'Engineering' skills — recruiters scan for both",
        ],
    },
    "product": {
        "title": "Product Management",
        "tips": [
            r"Frame every bullet as _Outcome → Impact_: 'Launched X → increased retention by Y%'",
            "Use PM keywords: roadmap, prioritization, OKRs, KPIs, GTM, A/B testing",
            "Show cross\\-functional leadership: _'Led 12\\-person squad across Eng, Design, Data'_",
            "Include metrics on features you shipped: DAU, conversion, NPS, revenue",
            "Avoid jargon like 'passionate about' — ATS and humans both dislike it",
            "Certifications to mention: PSPO, CPO, Google PM Certificate",
        ],
    },
    "marketing": {
        "title": "Marketing",
        "tips": [
            "Lead every bullet with a metric: _'Grew organic traffic 120% in 6 months'_",
            "List channels explicitly: SEO, SEM, email, social, content, paid, influencer",
            "Show funnel ownership: awareness → acquisition → activation → retention",
            "Name your tools: HubSpot, Marketo, Salesforce, GA4, Semrush, Klaviyo",
            "Include campaign ROI and ROAS figures wherever possible",
            "Content marketers: link your portfolio or top\\-performing article",
        ],
    },
    "finance": {
        "title": "Finance / Accounting",
        "tips": [
            "Lead with certifications front and centre: CPA, CFA, CMA, ACCA",
            "Use exact figures: _'\\$4\\.2M budget managed'_, _'Reduced DSO by 18 days'_",
            "ATS keywords: financial modelling, variance analysis, GAAP, IFRS, consolidation",
            "List systems: SAP, Oracle, QuickBooks, Hyperion, Adaptive Insights",
            "Show audit/compliance experience explicitly — recruiters filter on it",
            "Investment roles: include AUM, strategy returns, asset classes covered",
        ],
    },
    "design": {
        "title": "Design (UX/UI/Graphic)",
        "tips": [
            "Always include a portfolio link — it often matters more than the resume",
            "List tools: Figma, Sketch, Adobe XD, Illustrator, Photoshop, InVision",
            "Describe your design _process_, not just outputs: research → wireframe → test → ship",
            "Quantify UX impact: _'Redesigned checkout flow → 22% drop in cart abandonment'_",
            "Include user research methods you use: interviews, usability tests, card sorting",
            "ATS\\-friendly format: avoid submitting resumes with heavy graphics/tables",
        ],
    },
    "sales": {
        "title": "Sales",
        "tips": [
            r"Lead with quota attainment: _'Achieved 127% of \$1\.8M annual quota'_",
            "Include deal size, sales cycle length, and territory/vertical owned",
            "Mention your CRM: Salesforce, HubSpot, Outreach, Gong",
            "Use sales keywords: pipeline, ARR, MRR, SDR, AE, enterprise, SMB, outbound",
            "Show progression: SDR → AE → Sr\\. AE → Manager",
            "Awards and rankings: President's Club, Top Performer Q3 2023, etc\\.",
        ],
    },
    "general": {
        "title": "General / Other",
        "tips": [
            "Use a clean single\\-column layout — multi\\-column resumes confuse ATS parsers",
            "Start every bullet with a strong action verb: Led, Built, Delivered, Reduced…",
            "Include numbers in at least 60% of your bullets — they're the most ATS\\-friendly signal",
            "Keep resume to 1 page \\(0–5 yrs experience\\) or 2 pages \\(5\\+ yrs\\)",
            "Tailor your summary for each role — make it sound like you wrote it for them",
            "Save as \\.docx or \\.pdf — avoid Google Docs links or image\\-only PDFs",
            "Include both the acronym and full form: _'AWS \\(Amazon Web Services\\)'_",
        ],
    },
}


async def tips_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_tips_menu(update.message)
    return TIPS_WAITING_ROLE


async def _send_tips_menu(message):
    keyboard = [
        [
            InlineKeyboardButton("💻 Software Eng",   callback_data="tips_software"),
            InlineKeyboardButton("📊 Data Science",    callback_data="tips_data"),
        ],
        [
            InlineKeyboardButton("📦 Product",         callback_data="tips_product"),
            InlineKeyboardButton("📣 Marketing",        callback_data="tips_marketing"),
        ],
        [
            InlineKeyboardButton("💰 Finance",          callback_data="tips_finance"),
            InlineKeyboardButton("🎨 Design",            callback_data="tips_design"),
        ],
        [
            InlineKeyboardButton("📈 Sales",             callback_data="tips_sales"),
            InlineKeyboardButton("🌐 General",           callback_data="tips_general"),
        ],
    ]
    await message.reply_text(
        TIPS_PROMPT,
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def tips_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role_key = query.data.replace("tips_", "")
    role_data = ROLE_TIPS.get(role_key, ROLE_TIPS["general"])
    title = role_data["title"]
    # Tips are already escaped in ROLE_TIPS; join with numbering
    tips_list = "\n\n".join(f"  {i+1}\\. {t}" for i, t in enumerate(role_data["tips"]))

    msg = (
        f"💡 *Resume Tips — {_escape_markdown(title)}*\n"
        f"{'─' * 32}\n\n"
        f"{tips_list}\n\n"
        f"{'─' * 32}\n"
        r"🎯 Ready to apply these? Use /tailor to optimise your resume for a specific role\!"
    )
    await query.message.reply_text(msg, parse_mode="MarkdownV2")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
#  /history
# ─────────────────────────────────────────────────────────────────────────────

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_history(update.message, update.effective_user.id)


async def _send_history(message, user_id: int):
    records = _history.get(user_id, [])
    if not records:
        await message.reply_text(
            "📈 *Your Tailoring History*\n\nNo sessions yet\\. Run /tailor to get started\\!",
            parse_mode="MarkdownV2",
        )
        return

    lines = []
    for i, r in enumerate(records, 1):
        e_after, l_after = _score_badge(r["score_after"])
        lines.append(
            f"*{i}\\. {_escape_markdown(r['filename'])}*\n"
            f"   📅 {_escape_markdown(r['ts'])}\n"
            f"   Before: `{r['score_before']}%`  →  After: {e_after} `{r['score_after']}%`  \\(\\+{r['gain']}%\\)"
        )

    best = max(records, key=lambda x: x["gain"])
    body = "\n\n".join(lines)
    msg = (
        f"📈 *Your Last {len(records)} Tailoring Session\\(s\\)*\n"
        f"{'─' * 32}\n\n"
        f"{body}\n\n"
        f"{'─' * 32}\n"
        f"🏆 Best gain: `\\+{best['gain']}%` on _{_escape_markdown(best['filename'])}_"
    )
    await message.reply_text(msg, parse_mode="MarkdownV2")


# ─────────────────────────────────────────────────────────────────────────────
#  Fallback
# ─────────────────────────────────────────────────────────────────────────────

async def unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Not sure what to do? Here's what I can help with:\n\n"
        "/tailor — Tailor your resume to a job description\n"
        "/scan — Quick ATS health check\n"
        "/keywords — Extract keywords from a JD\n"
        "/tips — Role\\-specific resume writing tips\n"
        "/history — Your past sessions\n"
        "/help — Full usage guide",
        parse_mode="MarkdownV2",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set!")

    app = Application.builder().token(token).build()

    # ── Error handler ─────────────────────────────────────────────────────────
    app.add_error_handler(error_handler)

    # ── /tailor conversation ──────────────────────────────────────────────────
    tailor_conv = ConversationHandler(
        entry_points=[CommandHandler("tailor", tailor_start)],
        states={
            WAITING_RESUME: [MessageHandler(filters.Document.ALL, receive_resume)],
            WAITING_JD:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_jd)],
            PROCESSING:     [CallbackQueryHandler(confirm_tailor, pattern="^(confirm|cancel)_tailor$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # ── /scan conversation ────────────────────────────────────────────────────
    scan_conv = ConversationHandler(
        entry_points=[CommandHandler("scan", scan_start)],
        states={
            SCAN_WAITING_RESUME: [MessageHandler(filters.Document.ALL, scan_receive_resume)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # ── /keywords conversation ────────────────────────────────────────────────
    keywords_conv = ConversationHandler(
        entry_points=[CommandHandler("keywords", keywords_start)],
        states={
            KW_WAITING_JD: [MessageHandler(filters.TEXT & ~filters.COMMAND, keywords_receive_jd)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # ── /tips conversation ────────────────────────────────────────────────────
    tips_conv = ConversationHandler(
        entry_points=[CommandHandler("tips", tips_start)],
        states={
            TIPS_WAITING_ROLE: [CallbackQueryHandler(tips_callback, pattern="^tips_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(tailor_conv)
    app.add_handler(scan_conv)
    app.add_handler(keywords_conv)
    app.add_handler(tips_conv)

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(CommandHandler("history", history_cmd))

    app.add_handler(CallbackQueryHandler(menu_callback,        pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(post_tailor_callback, pattern="^(restart_tailor|show_history)$"))

    app.add_handler(MessageHandler(filters.ALL, unexpected_message))

    # ── Webhook / polling ─────────────────────────────────────────────────────
    webhook_url = os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", 8000))

    if webhook_url:
        logger.info("Webhook mode → port %s | %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
        )
    else:
        logger.info("Polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
