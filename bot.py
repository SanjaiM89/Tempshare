import os
import time
import uuid
import asyncio
import sqlite3
import requests
import shutil
import psutil
import subprocess
import libtorrent as lt
import aiohttp
from asyncio import Semaphore
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler
import nest_asyncio

# Apply nested asyncio compatibility
nest_asyncio.apply()

API_TOKEN = "6816699414:AAGkNhn0oaK1NK9GuKod7gHesX8zV12M760"  # Replace with your bot's API token
UPLOAD_URL = "https://temp.sh/upload"  # Replace with your desired upload URL

# Active tasks map for managing cancel events
active_tasks = {}

# Semaphore to limit the number of concurrent tasks
MAX_CONCURRENT_TASKS = 20
semaphore = Semaphore(MAX_CONCURRENT_TASKS)

# Database setup
def get_db_connection():
    conn = sqlite3.connect('tasks.db')
    return conn

def create_db_table():
    conn = get_db_connection()
    conn.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        status TEXT,
        file_url TEXT,
        progress REAL,
        downloaded_size INTEGER
    )
    ''')
    conn.commit()
    conn.close()

# Get system stats for display
def get_system_stats():
    cpu_usage = psutil.cpu_percent(interval=1)
    ram_usage = psutil.virtual_memory().percent
    free_storage = psutil.disk_usage('/').free / (1024 * 1024 * 1024)
    return cpu_usage, ram_usage, free_storage

# Helper to create a progress bar
def create_progress_bar(progress, bar_length=20):
    block = int(round(bar_length * progress))
    return "█" * block + "-" * (bar_length - block)

# Helper to clean up temporary directories
def clean_up_temp_dir(temp_dir):
    try:
        shutil.rmtree(temp_dir)
    except Exception as e:
        print(f"Error cleaning up directory {temp_dir}: {e}")

# Function to upload a file
def upload_file(file_path):
    try:
        upload_command = f'curl -F "file=@{file_path}" {UPLOAD_URL}'
        result = subprocess.run(upload_command, shell=True, text=True, capture_output=True)
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None

# Torrent downloading command
async def torrent(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    if len(context.args) != 1:
        await context.bot.send_message(chat_id=chat_id, text="Please provide a magnet link.")
        return

    magnet_link = context.args[0]
    task_id = str(uuid.uuid4())
    active_tasks[task_id] = asyncio.Event()

    cancel_button = InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
    keyboard = InlineKeyboardMarkup([[cancel_button]])

    progress_message = await context.bot.send_message(
        chat_id=chat_id,
        text="🚀 <b>Started downloading the torrent...</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    asyncio.create_task(download_torrent(task_id, magnet_link, chat_id, progress_message, context, active_tasks[task_id]))

# Main torrent download function
async def download_torrent(task_id, magnet_link, chat_id, progress_message, context, cancel_event):
    try:
        async with semaphore:
            ses = lt.session()
            ses.listen_on(6881, 6891)
            params = {'save_path': './downloads/'}
            torrent_handle = ses.add_torrent({'url': magnet_link, **params})

            while not torrent_handle.has_metadata():
                await asyncio.sleep(1)

            while not torrent_handle.is_seed():
                status = torrent_handle.status()
                percent_done = status.progress * 100
                progress_bar = create_progress_bar(status.progress)

                progress_text = f"""
🚀 <b>Downloading...</b>\n\n
{progress_bar} {percent_done:.2f}%
🔄 Downloaded: {status.total_done / (1024 * 1024):.2f} MB
"""

                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text=progress_text,
                    parse_mode="HTML"
                )

                if cancel_event.is_set():
                    break

                await asyncio.sleep(5)

            if not cancel_event.is_set():
                downloaded_file = os.path.join('./downloads', torrent_handle.name())
                uploaded_url = upload_file(downloaded_file)
                final_message = f"✅ Uploaded: {uploaded_url}" if uploaded_url else "❌ Upload failed."
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text=final_message,
                    parse_mode="HTML"
                )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_message.message_id,
            text=f"❌ Error: {e}",
            parse_mode="HTML"
        )

# Direct file download command
async def direct_download(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    if len(context.args) != 1:
        await context.bot.send_message(chat_id=chat_id, text="Please provide a direct download link.")
        return

    file_link = context.args[0]
    task_id = str(uuid.uuid4())
    active_tasks[task_id] = asyncio.Event()

    cancel_button = InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
    keyboard = InlineKeyboardMarkup([[cancel_button]])
    progress_message = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🚀 <b>Started downloading the file...</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    asyncio.create_task(download_direct_link(task_id, file_link, chat_id, progress_message, context, active_tasks[task_id]))

# Main direct file download function
async def download_direct_link(task_id, file_link, chat_id, progress_message, context, cancel_event):
    try:
        async with semaphore:
            temp_dir = os.path.join('./downloads', task_id)
            os.makedirs(temp_dir, exist_ok=True)

            response = requests.get(file_link, stream=True)
            total_size = int(response.headers.get('Content-Length', 0))
            downloaded_size = 0
            chunk_size = 1024 * 1024  # 1 MB chunks

            file_path = os.path.join(temp_dir, 'downloaded_file')
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if cancel_event.is_set():
                        break
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    progress = downloaded_size / total_size
                    progress_bar = create_progress_bar(progress)

                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_message.message_id,
                        text=f"🚀 Progress: {progress * 100:.2f}%\n\n{progress_bar}",
                        parse_mode="HTML"
                    )

            if not cancel_event.is_set():
                uploaded_url = upload_file(file_path)
                final_message = f"✅ Uploaded: {uploaded_url}" if uploaded_url else "❌ Upload failed."
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text=final_message,
                    parse_mode="HTML"
                )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_message.message_id,
            text=f"❌ Error: {e}",
            parse_mode="HTML"
        )

# Start command
async def start(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    start_text = "Welcome! Use /torrent <magnet_link> to start downloading or /download <file_link> for direct download."
    await context.bot.send_message(chat_id=chat_id, text=start_text)

# Cancel a task
async def cancel_task(update: Update, context: CallbackContext):
    query = update.callback_query
    task_id = query.data.split("_")[1]
    if task_id in active_tasks:
        active_tasks[task_id].set()
        await query.answer("Task is being canceled...")
    else:
        await query.answer("No active task found.")

# Main entry point
def main():
    create_db_table()
    application = Application.builder().token(API_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("torrent", torrent))
    application.add_handler(CommandHandler("download", direct_download))
    application.add_handler(CallbackQueryHandler(cancel_task, pattern="^cancel_"))
    application.run_polling()

if __name__ == "__main__":
    main()
