import time
import subprocess
import uuid
import aiohttp
import asyncio
import psutil
import sqlite3
import requests
import os
import shutil  # Missing import for shutil
import libtorrent as lt
from asyncio import Semaphore
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler
import nest_asyncio

nest_asyncio.apply()

API_TOKEN = "6816699414:AAGkNhn0oaK1NK9GuKod7gHesX8zV12M760"  # Replace with your bot's API token
upload_url = "https://temp.sh/upload"
active_tasks = {}

# Semaphore to limit the number of concurrent tasks to 20
MAX_CONCURRENT_TASKS = 20
semaphore = Semaphore(MAX_CONCURRENT_TASKS)

# Database Setup
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

# System Stats
def get_system_stats():
    cpu_usage = psutil.cpu_percent(interval=1)
    ram_usage = psutil.virtual_memory().percent
    free_storage = psutil.disk_usage('/').free / (1024 * 1024 * 1024)
    return cpu_usage, ram_usage, free_storage

# Upload File Function (after download)
def upload_file(file_path):
    try:
        upload_command = f'curl -F "file=@{file_path}" {upload_url}'
        result = subprocess.run(upload_command, shell=True, text=True, capture_output=True)

        if result.returncode == 0:
            return result.stdout.strip()  # Return the uploaded URL
        else:
            return None
    except Exception as e:
        return None

# Function to handle direct file download
async def download_direct_link(task_id, file_link, chat_id, progress_message, context, cancel_event):
    try:
        # Limit concurrent tasks
        async with semaphore:
            # Create a temporary directory to store the downloaded file
            temp_dir = os.path.join('./downloads', task_id)
            os.makedirs(temp_dir, exist_ok=True)

            # Start downloading the file in chunks
            headers = {'Range': 'bytes=0-'}
            response = requests.get(file_link, headers=headers, stream=True)
            total_size = int(response.headers.get('Content-Length', 0))
            downloaded_size = 0
            chunk_size = 1024 * 1024  # 1MB chunks

            # Track download progress
            file_path = os.path.join(temp_dir, 'downloaded_file')
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if cancel_event.is_set():
                        break  # Stop downloading if the task is canceled

                    f.write(chunk)
                    downloaded_size += len(chunk)

                    # Update progress every 1MB downloaded
                    progress = downloaded_size / total_size
                    progress_bar = create_progress_bar(progress)

                    # Create the progress text
                    progress_text = (
                        f"┌  🚀 [Download Progress: {progress * 100:.2f}%]\n\n"
                        f"   {progress_bar} {progress * 100:.2f}%\n"
                        f"├  🔄 ᴘʀᴏᴄᴇssᴇᴅ : {downloaded_size / (1024 * 1024):.2f} MB\n"
                        f"├  ⏬ ꜱᴛᴀᴛᴜs : Downloading\n"
                        f"└   ❎ <b>Click below to cancel the task:</b>"
                    )

                    # Update the message with progress
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_message.message_id,
                        text=progress_text,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]]
                        )  # Ensure reply markup is updated with cancel button
                    )
                    await asyncio.sleep(10)  # Sleep to avoid hitting rate limits

            # After download is complete
            uploaded_url = upload_file(file_path)
            if uploaded_url:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text=f"✅ File downloaded and uploaded successfully.\nURL: {uploaded_url}",
                    parse_mode='HTML'
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text="❌ Upload failed.",
                    parse_mode='HTML'
                )

            os.remove(file_path)  # Remove the file after upload
            clean_up_temp_dir(temp_dir)  # Clean up temporary files

    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_message.message_id,
            text=f"❌ Error occurred: {str(e)}",
            parse_mode='HTML'
        )


# Helper function to download chunks of the file
async def download_chunk(url, start_byte, end_byte, part_num, temp_dir, retries=3):
    headers = {'Range': f"bytes={start_byte}-{end_byte}"}
    
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers, stream=True)
            if response.status_code == 206:  # Partial content response
                part_file_path = os.path.join(temp_dir, f'part_{part_num}')
                with open(part_file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024):
                        f.write(chunk)
                return part_file_path
            else:
                raise Exception(f"Failed to download part {part_num}, Status code: {response.status_code}")
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.RequestException) as e:
            # If a network error occurs, retry the download
            if attempt < retries - 1:
                print(f"Retrying part {part_num} (Attempt {attempt + 1}/{retries})...")
                await asyncio.sleep(2)  # Wait before retrying
            else:
                raise Exception(f"Failed to download part {part_num} after {retries} attempts: {e}")

import uuid

async def torrent(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id

    # Ensure there is exactly one argument (the magnet link)
    if len(context.args) != 1:
        await context.bot.send_message(chat_id=chat_id, text="Please provide a magnet link.")
        return

    magnet_link = context.args[0]  # Get the magnet link from the user's message

    # Generate a unique task ID for this download
    task_id = str(uuid.uuid4())

    # Create an event to manage the task's cancellation
    active_tasks[task_id] = asyncio.Event()

    # Prepare the cancel button and keyboard
    cancel_button = InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
    keyboard = InlineKeyboardMarkup([[cancel_button]])

    # Send an initial progress message
    progress_message = await context.bot.send_message(
        chat_id=chat_id,
        text="🚀 <b>Started downloading the torrent...</b>",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # Start the torrent download task with the necessary parameters
    asyncio.create_task(download_torrent(
        task_id=task_id,  # Task ID to identify the task
        magnet_link=magnet_link,  # Magnet link for the torrent
        chat_id=chat_id,  # The chat ID to send messages to
        progress_message=progress_message,  # Message object to update progress
        context=context,  # The context object
        cancel_event=active_tasks[task_id]  # Event to manage cancellation
    ))


# Function to cleanup and remove temporary directory safely
def clean_up_temp_dir(temp_dir):
    try:
        shutil.rmtree(temp_dir)  # This will remove the directory and all its contents
    except Exception as e:
        print(f"Error cleaning up directory {temp_dir}: {str(e)}")

# Torrent Download Function (Corrected for modern libtorrent)
def create_progress_bar(progress, bar_length=20):
    block = int(round(bar_length * progress))
    progress_bar = "█" * block + "-" * (bar_length - block)
    return progress_bar

async def download_torrent(task_id, magnet_link, chat_id, progress_message, context, cancel_event):
    try:
        # Limit concurrent tasks
        async with semaphore:
            # Setup libtorrent session
            ses = lt.session()
            ses.listen_on(6881, 6891)  # Listen on the ports

            # Parameters to specify where to save the torrent and other settings
            params = {
                'save_path': './downloads/',  # Directory to save the downloaded files
                'storage_mode': lt.storage_mode_t(2),  # Use the default storage mode
            }

            # Add the magnet URI (using the corrected method)
            torrent_handle = ses.add_torrent({'url': magnet_link, **params})

            # Wait for the torrent to start downloading
            while not torrent_handle.has_metadata():
                await asyncio.sleep(1)  # Wait for the metadata to load

            # Track the download progress
            last_updated = time.time()
            last_progress_text = None  # To store the previous message text
            last_keyboard_markup = None  # To store the previous reply markup

            while not torrent_handle.is_seed():
                status = torrent_handle.status()
                percent_done = status.progress * 100
                downloaded_size = int(status.total_done)
                speed = status.download_rate  # Download speed in bytes per second

                # Get system stats
                cpu_usage, ram_usage, free_storage = get_system_stats()

                # Create progress bar
                progress_bar = create_progress_bar(status.progress)

                # Create the progress text
                progress_text = (
                    f"┌  🚀 [Download Progress: {percent_done:.2f}%]\n\n"
                    f"   {progress_bar} {percent_done:.2f}%\n"
                    f"├  🔄 ᴘʀᴏᴄᴇssᴇᴅ : {downloaded_size / (1024 * 1024):.2f} MB\n"
                    f"├  ⏬ ꜱᴛᴀᴛᴜs : Downloading\n"
                    f"├  ⚡️ sᴘᴇᴇᴅ : {speed / 1024:.2f} KB/s\n"
                    f"├  🖥 ᴄᴘᴜ : {cpu_usage:.1f}%\n"
                    f"├  🎮 ʀᴀᴍ : {ram_usage:.1f}%\n"
                    f"├  💿 ғʀᴇᴇ : {free_storage:.2f} GB\n"
                    f"└   ❎ <b>Click below to cancel the task:</b>"
                )

                # Check if the progress text or keyboard markup has changed
                if progress_text != last_progress_text or last_keyboard_markup is None:
                    # Update the message only if the text or keyboard has changed
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_message.message_id,
                        text=progress_text,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]]
                        )  # Ensure reply markup is updated with cancel button
                    )
                    last_progress_text = progress_text  # Store the new progress text
                    last_keyboard_markup = InlineKeyboardMarkup(
                        [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]]
                    )  # Store the new reply markup

                await asyncio.sleep(10)  # Update every second

            # Upload the torrent file
            downloaded_file = os.path.join('./downloads', torrent_handle.name())
            uploaded_url = upload_file(downloaded_file)
            if uploaded_url:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text=f"✅ Torrent downloaded and uploaded successfully.\nURL: {uploaded_url}",
                    parse_mode='HTML'
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                    text="❌ Upload failed.",
                    parse_mode='HTML'
                )

            os.remove(downloaded_file)  # Remove the file after upload

    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_message.message_id,
            text=f"❌ Error occurred: {str(e)}",
            parse_mode='HTML'
        )



# Direct Download Command
async def direct_download(update: Update, context: CallbackContext) -> None:
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
        parse_mode='HTML'
    )

    # Start the direct download task immediately without waiting for semaphore
    asyncio.create_task(download_direct_link(task_id, file_link, chat_id, progress_message, context, active_tasks[task_id]))



# Start Command
async def start(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id
    start_text = "Welcome to the download bot! Use /torrent <magnet_link> to start downloading a torrent, or /download <file_link> to download a direct file."
    await context.bot.send_message(chat_id=chat_id, text=start_text)

# Cancel Task Command
async def cancel_task(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    task_id = query.data.split("_")[1]  # Extract task_id from the callback data
    if task_id in active_tasks:
        active_tasks[task_id].set()  # Set the event to stop the task
        await query.answer(text="Task is being canceled...")
    else:
        await query.answer(text="No active task found.")

# Main function to run the bot
def main() -> None:
    create_db_table()

    application = Application.builder().token(API_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("torrent", torrent))
    application.add_handler(CommandHandler("download", direct_download))
    application.add_handler(CallbackQueryHandler(cancel_task, pattern="^cancel_"))

    application.run_polling()

if __name__ == '__main__':
    main()
