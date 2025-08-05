import os
import smtplib
import time
import threading
import re
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

import asyncio
import nest_asyncio
nest_asyncio.apply()  # السماح بتشغيل asyncio داخل حلقة أحداث نشطة مسبقًا

user_sessions = {}
stop_flags = {}
last_button_click = {}
button_ban = {}

EMAIL_REGEX = r"[^@]+@[^@]+\.[^@]+"
MAX_SENDERS = 35
MAX_RETRIES = 2

def is_banned(user_id):
    now = time.time()
    if user_id in button_ban and button_ban[user_id] > now:
        return True
    if user_id in last_button_click and now - last_button_click[user_id] < 3:
        button_ban[user_id] = now + 60
        return True
    last_button_click[user_id] = now
    return False

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إضافة حسابات", callback_data="add_senders"),
         InlineKeyboardButton("تعيين المستلم", callback_data="set_receiver"),
         InlineKeyboardButton("تعيين الموضوع", callback_data="set_subject")],
        [InlineKeyboardButton("تعيين الرسالة", callback_data="set_body"),
         InlineKeyboardButton("تعيين التأخير", callback_data="set_delay"),
         InlineKeyboardButton("تعيين العدد", callback_data="set_count")],
        [InlineKeyboardButton("بدء الإرسال", callback_data="start_sending"),
         InlineKeyboardButton("عرض المعلومات", callback_data="show_info")]
    ])

def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ الرجوع", callback_data="back_to_menu")]
    ])

def info_menu(session):
    return (
        f"المعلومات المضافة:\n\n"
        f"المستلم: {session.get('receiver', 'غير محدد')}\n"
        f"الموضوع: {session.get('subject', 'غير محدد')}\n"
        f"الرسالة: {session.get('body', 'غير محددة')}\n"
        f"العدد: {session.get('count', 'غير محدد')}\n"
        f"التأخير: {session.get('delay', 'غير محدد')}\n"
        f"عدد الحسابات: {len(session.get('senders', []))}"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {
        "receiver": context.user_data.get("receiver"),
        "subject": context.user_data.get("subject"),
        "body": context.user_data.get("body"),
        "delay": context.user_data.get("delay", 2),
        "count": context.user_data.get("count", 10),
        "senders": context.user_data.get("senders", [])
    }
    stop_flags[user_id] = False
    await update.message.reply_text(
        'Welcome to the mail bot\n<a href="https://e.top4top.io/p_3484ox1ie1.jpg">&#8203;</a>',
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
        reply_markup=main_menu()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if is_banned(user_id):
        await query.answer("تم حظرك مؤقتاً بسبب الضغط السريع، جرب بعد دقيقة.", show_alert=True)
        return

    session = user_sessions.setdefault(user_id, {})
    context.user_data["step"] = None

    if data == "add_senders":
        context.user_data["step"] = "senders"
        await query.edit_message_text("أرسل الحسابات بهذا الشكل:\nemail:password\nواحد في كل سطر.")
    elif data == "set_receiver":
        context.user_data["step"] = "receiver"
        await query.edit_message_text("أرسل البريد المستلم:")
    elif data == "set_subject":
        context.user_data["step"] = "subject"
        await query.edit_message_text("أرسل عنوان الرسالة:")
    elif data == "set_body":
        context.user_data["step"] = "body"
        await query.edit_message_text("أرسل نص الرسالة (سيتم الحفاظ على المسافات والتنسيق):")
    elif data == "set_delay":
        context.user_data["step"] = "delay"
        await query.edit_message_text("أرسل الوقت بين كل رسالة (بالثواني):")
    elif data == "set_count":
        context.user_data["step"] = "count"
        await query.edit_message_text("أرسل عدد الرسائل لكل حساب:")
    elif data == "show_info":
        await query.edit_message_text(
            info_menu(session),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=back_button()
        )
    elif data == "back_to_menu":
        await query.edit_message_text("تم الرجوع للقائمة:", reply_markup=main_menu())
    elif data == "start_sending":
        required = ["senders", "receiver", "subject", "body"]
        if any(not session.get(k) for k in required):
            await query.edit_message_text("تأكد من إدخال جميع البيانات.")
            return
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
        msg = await context.bot.send_message(chat_id=query.message.chat_id, text="جاري الإرسال...\n/stop لإيقاف العملية")
        threading.Thread(target=send_all_emails, args=(context, user_id, msg)).start()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.setdefault(user_id, {})
    step = context.user_data.get("step")
    if not step:
        await update.message.reply_text("استخدم /start للرجوع للقائمة.")
        return

    value = update.message.text.strip()
    if step == "senders":
        pairs = []
        for line in value.splitlines():
            if ":" in line:
                email, pwd = line.split(":", 1)
                if re.match(EMAIL_REGEX, email.strip()) and pwd.strip():
                    pairs.append((email.strip(), pwd.strip()))
        if pairs:
            if len(session.get("senders", [])) + len(pairs) > MAX_SENDERS:
                await update.message.reply_text(f"الحد الأقصى {MAX_SENDERS} حساب.")
            else:
                session.setdefault("senders", []).extend(pairs)
                context.user_data["senders"] = session["senders"]
                await update.message.reply_text(f"تمت إضافة {len(pairs)} حساب.")
        else:
            await update.message.reply_text("لم يتم التعرف على أي بريد صحيح.")
    elif step in ["receiver", "subject", "body"]:
        session[step] = value
        context.user_data[step] = value
        await update.message.reply_text("تم الحفظ.")
    elif step == "delay":
        try:
            session["delay"] = float(value)
            context.user_data["delay"] = float(value)
            await update.message.reply_text("تم التحديث.")
        except:
            await update.message.reply_text("أدخل رقم صحيح.")
    elif step == "count":
        try:
            session["count"] = int(value)
            context.user_data["count"] = int(value)
            await update.message.reply_text("تم التحديث.")
        except:
            await update.message.reply_text("أدخل عدد صحيح.")

    context.user_data["step"] = None
    await update.message.reply_text("استخدم /start للرجوع للقائمة.", reply_markup=main_menu())

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stop_flags[user_id] = True
    await update.message.reply_text("تم إيقاف الإرسال لهذا المستخدم.")

def send_all_emails(context: ContextTypes.DEFAULT_TYPE, user_id: int, msg):
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_send_emails_async(context, user_id, msg))

async def _send_emails_async(context, user_id, msg):
    session = user_sessions[user_id]
    receiver = session["receiver"]
    subject = session["subject"]
    body = session["body"]
    delay = session["delay"]
    count = session["count"]
    senders = session["senders"][:MAX_SENDERS]
    total_sent = 0
    stats = {s: 0 for s, _ in senders}

    async def update_status():
        text = "حالة الإرسال:\n" + "\n".join(f"{s} - {stats[s]}" for s, _ in senders)
        try:
            await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=text)
        except:
            pass

    for email, pwd in senders:
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                for attempt in range(MAX_RETRIES):
                    try:
                        server.login(email, pwd)
                        break
                    except:
                        time.sleep(2)
                else:
                    await context.bot.send_message(chat_id=msg.chat_id, text=f"فشل الدخول إلى {email}")
                    continue

                for i in range(count):
                    if stop_flags.get(user_id): return
                    try:
                        message = MIMEMultipart("alternative")
                        message["From"] = email
                        message["To"] = receiver

                        unique_subject = f"{subject} (ID:{time.time_ns()})"
                        message["Subject"] = Header(unique_subject, 'utf-8')

                        message_id = f"{time.time_ns()}.{random.randint(1000,9999)}@{email.split('@')[-1]}"
                        message["Message-ID"] = f"<{message_id}>"
                        message["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())

                        unique_body = f"{body}\n\n---\nرقم الرسالة: {i+1}\nمعرف فريد: {message_id}"
                        message.attach(MIMEText(unique_body.replace('\\n', '<br>'), "html", 'utf-8'))

                        server.sendmail(email, receiver, message.as_string())
                        stats[email] += 1
                        total_sent += 1
                        await update_status()
                        time.sleep(delay)
                    except Exception as e:
                        await context.bot.send_message(chat_id=msg.chat_id, text=f"خطأ من {email}: {str(e)}")
                        break
        except Exception as e:
            await context.bot.send_message(chat_id=msg.chat_id, text=f"تعذر فتح الاتصال: {email} - {str(e)}")

    await context.bot.send_message(chat_id=msg.chat_id, text=f"تم الإرسال بنجاح. المجموع: {total_sent} رسالة.", disable_web_page_preview=True)

if __name__ == "__main__":
    async def main_wrapper():
        TOKEN = os.environ.get("TOKEN")
        if not TOKEN:
            print("خطأ: متغير البيئة TOKEN غير معرّف.")
            return

        app = Application.builder().token(TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("stop", stop_command))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

        await app.run_polling()

    try:
        asyncio.run(main_wrapper())
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            loop = asyncio.get_event_loop()
            loop.run_until_complete(main_wrapper())
        else:
            raise
