import os
import logging
import asyncio
import json
import re
import io
import tempfile
from pathlib import Path
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

# ─── States ─────────────────────────────────────────────────────────────────
WAITING_RESUME, WAITING_JD, PROCESSING = range(3)

# ─── Helpers ─────────────────────────────────────────────────────────────────
WELCOME_TEXT = """
👋 *Welcome to Resume ATS Optimizer Bot!*

I help you tailor your resume to maximize your ATS (Applicant Tracking System) score for any job.

*Here's what I can do:*
• 📄 Accept your resume (PDF or DOCX)
• 📋 Analyze the job description
• 🤖 Use AI to tailor your resume with optimized keywords
• 📊 Show your ATS score improvement
• 📥 Send back the tailored resume as DOCX

*Commands:*
/start — Show this welcome message
/tailor — Start the resume tailoring process
/help — How to get the best results
/cancel — Cancel current operation

Ready? Type /tailor to begin! 🚀
"""

HELP_TEXT = """
*📖 How to Get the Best Results*

1️⃣ *Prepare your resume* — PDF or DOCX format works best. Make sure it has clear sections (Summary, Experience, Skills, Education).

2️⃣ *Copy the full job description* — Include the job title, responsibilities, and requirements. More details = better tailoring.

3️⃣ *Wait for processing* — The AI will:
   • Extract keywords from the JD
   • Match & enhance your skills section
   • Optimize your summary/objective
   • Add relevant keywords naturally
   • Score your ATS match (before & after)

4️⃣ *Download your tailored resume* — A DOCX file is sent back to you, ready to submit!

*💡 Tips:*
• Use a clean, single-column resume for best ATS compatibility
• The bot preserves your formatting and only enhances content
• Run it for each job you apply to — every JD is unique!

Type /tailor to start!
"""

# ─── Command Handlers ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def tailor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📄 *Step 1 of 2 — Upload Your Resume*\n\n"
        "Please send me your resume as a *PDF* or *DOCX* file.\n\n"
        "_Type /cancel to stop at any time._",
        parse_mode="Markdown",
    )
    return WAITING_RESUME


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Operation cancelled. Type /tailor to start again."
    )
    return ConversationHandler.END


# ─── Conversation Steps ───────────────────────────────────────────────────────

async def receive_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "⚠️ Please send a *file* (PDF or DOCX), not an image or text.",
            parse_mode="Markdown",
        )
        return WAITING_RESUME

    fname = doc.file_name or ""
    if not (fname.lower().endswith(".pdf") or fname.lower().endswith(".docx")):
        await update.message.reply_text(
            "⚠️ Only *PDF* and *DOCX* files are supported. Please try again.",
            parse_mode="Markdown",
        )
        return WAITING_RESUME

    status_msg = await update.message.reply_text("⬇️ Downloading your resume…")

    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()

    context.user_data["resume_bytes"] = bytes(file_bytes)
    context.user_data["resume_name"] = fname
    context.user_data["resume_ext"] = Path(fname).suffix.lower()

    await status_msg.edit_text(
        "✅ Resume received!\n\n"
        "📋 *Step 2 of 2 — Paste the Job Description*\n\n"
        "Copy and paste the full job description (title, responsibilities, requirements) as a text message.",
        parse_mode="Markdown",
    )
    return WAITING_JD


async def receive_jd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jd_text = update.message.text or ""
    if len(jd_text.strip()) < 50:
        await update.message.reply_text(
            "⚠️ The job description seems too short. Please paste the full JD (at least a few sentences)."
        )
        return WAITING_JD

    context.user_data["jd_text"] = jd_text.strip()

    keyboard = [
        [
            InlineKeyboardButton("🚀 Start Tailoring", callback_data="confirm_tailor"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]
    ]
    preview = jd_text[:300] + ("…" if len(jd_text) > 300 else "")
    await update.message.reply_text(
        f"📋 *Job Description received!*\n\n_{preview}_\n\n"
        "Ready to tailor your resume?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PROCESSING


async def confirm_tailor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled. Type /tailor to start again.")
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text(
        "⚙️ *Processing your resume…*\n\n"
        "🔍 Extracting keywords from JD…\n"
        "🤖 AI is tailoring your resume…\n"
        "📊 Calculating ATS scores…\n\n"
        "_This may take 20–40 seconds. Please wait!_",
        parse_mode="Markdown",
    )

    user_id = query.from_user.id
    resume_bytes = context.user_data.get("resume_bytes")
    resume_ext = context.user_data.get("resume_ext")
    jd_text = context.user_data.get("jd_text")
    resume_name = context.user_data.get("resume_name", "resume")

    try:
        processor = ResumeProcessor()
        result = await processor.process(resume_bytes, resume_ext, jd_text)

        # Send ATS score report
        score_text = (
            f"✅ *Resume Tailored Successfully!*\n\n"
            f"📊 *ATS Score Report*\n"
            f"{'─' * 30}\n"
            f"Before: `{result['score_before']}%`\n"
            f"After:  `{result['score_after']}%`\n"
            f"Gain:   `+{result['score_after'] - result['score_before']}%` 🎉\n\n"
            f"🔑 *Top Keywords Added:*\n{_fmt_keywords(result['keywords_added'])}\n\n"
            f"💡 *Changes Made:*\n{result['summary_of_changes']}\n\n"
            f"📥 Your tailored resume is attached below!"
        )

        await query.message.reply_text(score_text, parse_mode="Markdown")

        # Send the tailored DOCX
        stem = Path(resume_name).stem
        out_filename = f"{stem}_tailored.docx"
        await query.message.reply_document(
            document=io.BytesIO(result["docx_bytes"]),
            filename=out_filename,
            caption="📄 Your ATS-optimised resume. Good luck with your application! 🍀",
        )

    except Exception as e:
        logger.error("Processing error: %s", e, exc_info=True)
        await query.message.reply_text(
            "❌ *Something went wrong during processing.*\n\n"
            f"Error: `{str(e)[:200]}`\n\n"
            "Please try again with /tailor or contact support.",
            parse_mode="Markdown",
        )

    context.user_data.clear()
    return ConversationHandler.END


def _fmt_keywords(keywords: list) -> str:
    if not keywords:
        return "_None_"
    return "\n".join(f"  • `{kw}`" for kw in keywords[:15])


# ─── Fallback ────────────────────────────────────────────────────────────────

async def unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Use /tailor to start tailoring your resume, or /help for guidance."
    )


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set!")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("tailor", tailor_start)],
        states={
            WAITING_RESUME: [MessageHandler(filters.Document.ALL, receive_resume)],
            WAITING_JD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_jd)],
            PROCESSING: [CallbackQueryHandler(confirm_tailor)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.ALL, unexpected_message))

    port = int(os.environ.get("PORT", 8443))
    webhook_url = os.environ.get("WEBHOOK_URL")  # e.g. https://your-app.koyeb.app

    if webhook_url:
        logger.info("Starting webhook on port %d → %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=f"{webhook_url}/webhook",
            url_path="/webhook",
        )
    else:
        logger.info("No WEBHOOK_URL set — running in polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
