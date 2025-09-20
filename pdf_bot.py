import os
import sys
import time
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.error import TimedOut, NetworkError
from PIL import Image
from pdf2image import convert_from_path
from PyPDF2 import PdfMerger, PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from flask import Flask

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

PORT = int(os.getenv('PORT', 8080))

# Temporary directory for processing
TEMP_DIR = '/tmp/pdf_bot'
os.makedirs(TEMP_DIR, exist_ok=True)

# Global storage for all processed pages
all_processed_pages = []
start_time = datetime.now()

# Simple Flask app for monitoring
app = Flask(__name__)

@app.route('/health')
def health():
    return {'status': 'healthy', 'pages': len(all_processed_pages)}

@app.route('/ping')
def ping():
    return 'PONG'

@app.route('/')
def home():
    uptime = datetime.now() - start_time
    return f"PDF Bot Running! Uptime: {str(uptime).split('.')[0]}, Pages: {len(all_processed_pages)}"

async def start(update: Update, context):
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        'Привет! Отправь мне PDF-файлы, и я извлеку верхний левый квадрант каждой страницы.\n\n'
        'Все обработанные страницы будут накапливаться.\n'
        '/send - получить объединенный PDF со всеми страницами\n'
        '/clear - очистить накопленные страницы\n'
        '/status - показать текущий статус\n\n'
        'Отправляйте файлы по одному для лучшей стабильности!'
    )

def extract_top_left_quadrant(pdf_path):
    """Extract top-left quadrant from each page of a PDF."""
    try:
        images = convert_from_path(pdf_path)
    except Exception:
        try:
            images = convert_from_path(pdf_path, poppler_path='/opt/homebrew/bin')
        except Exception:
            images = convert_from_path(pdf_path, poppler_path='/usr/bin')
    
    quadrant_images = []
    timestamp = int(time.time() * 1000)
    
    for i, img in enumerate(images):
        width, height = img.size
        quadrant_width = width // 2
        quadrant_height = height // 2
        quadrant = img.crop((0, 0, quadrant_width, quadrant_height))
        
        quadrant_path = os.path.join(TEMP_DIR, f'quadrant_{timestamp}_{i}.png')
        quadrant.save(quadrant_path, optimize=True)
        quadrant_images.append(quadrant_path)
    
    return quadrant_images

def create_pdf_from_images(image_paths):
    """Convert images to PDFs with each image on a full A4 page."""
    output_path = os.path.join(TEMP_DIR, 'combined_quadrants.pdf')
    
    c = canvas.Canvas(output_path, pagesize=A4)
    page_width, page_height = A4
    
    for img_path in image_paths:
        img = Image.open(img_path)
        img_resized = img.resize((int(page_width), int(page_height)), Image.LANCZOS)
        
        resized_path = img_path.replace('.png', '_full.png')
        img_resized.save(resized_path)
        
        c.drawImage(resized_path, 0, 0, width=page_width, height=page_height)
        c.showPage()
    
    c.save()
    return output_path

async def handle_pdf(update: Update, context):
    """Handle incoming PDF files."""
    global all_processed_pages
    
    pdf_path = None
    wait_message = None
    
    try:
        wait_message = await update.message.reply_text('Загрузка файла...')
        
        pdf_file = await asyncio.wait_for(
            update.message.document.get_file(), 
            timeout=60.0
        )
        
        pdf_path = os.path.join(TEMP_DIR, f"temp_{int(time.time())}_{update.message.document.file_name}")
        
        await asyncio.wait_for(
            pdf_file.download_to_drive(pdf_path),
            timeout=120.0
        )
        
        await wait_message.edit_text('Обработка PDF...')
        
        quadrant_images = extract_top_left_quadrant(pdf_path)
        all_processed_pages.extend(quadrant_images)
        
        total_pages = len(all_processed_pages)
        await wait_message.edit_text(
            f'PDF обработан! Извлечено {len(quadrant_images)} страниц.\n'
            f'Всего накоплено страниц: {total_pages}\n'
            f'Используй /send для получения объединенного PDF'
        )
    
    except Exception as e:
        error_msg = f'Ошибка: {str(e)}'
        if wait_message:
            await wait_message.edit_text(error_msg)
        else:
            await update.message.reply_text(error_msg)
        logger.error(f"Error processing PDF: {e}")
    
    finally:
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception as e:
                logger.error(f"Error removing temp file: {e}")

async def send_combined_pdf(update: Update, context):
    """Send combined PDF with all processed pages."""
    global all_processed_pages
    
    if not all_processed_pages:
        await update.message.reply_text('Нет обработанных страниц!')
        return
    
    result_pdf_path = None
    wait_message = None
    
    try:
        wait_message = await update.message.reply_text('Создание объединенного PDF...')
        
        result_pdf_path = create_pdf_from_images(all_processed_pages)
        
        file_size = os.path.getsize(result_pdf_path)
        if file_size > 50 * 1024 * 1024:
            await wait_message.edit_text('PDF слишком большой (>50MB).')
            return
        
        await wait_message.edit_text('Отправка PDF...')
        
        with open(result_pdf_path, 'rb') as pdf_file:
            await asyncio.wait_for(
                update.message.reply_document(
                    pdf_file, 
                    caption=f'Объединенный PDF готов! Всего страниц: {len(all_processed_pages)}'
                ),
                timeout=300.0
            )
        
        await wait_message.edit_text('PDF успешно отправлен!')
            
    except Exception as e:
        error_msg = f'Ошибка при создании PDF: {str(e)}'
        if wait_message:
            await wait_message.edit_text(error_msg)
        else:
            await update.message.reply_text(error_msg)
        logger.error(f"Error creating combined PDF: {e}")
    
    finally:
        if result_pdf_path and os.path.exists(result_pdf_path):
            try:
                os.remove(result_pdf_path)
            except Exception as e:
                logger.error(f"Error removing result file: {e}")

async def clear_pages(update: Update, context):
    """Clear all accumulated pages."""
    global all_processed_pages
    
    page_count = len(all_processed_pages)
    all_processed_pages.clear()
    
    try:
        for file in os.listdir(TEMP_DIR):
            if file.startswith('quadrant_') and file.endswith('.png'):
                file_path = os.path.join(TEMP_DIR, file)
                if os.path.exists(file_path):
                    os.remove(file_path)
    except Exception as e:
        logger.error(f"Error cleaning up temp files: {e}")
    
    await update.message.reply_text(f'Очищено {page_count} страниц!')

async def status(update: Update, context):
    """Show current status."""
    global all_processed_pages
    
    page_count = len(all_processed_pages)
    uptime = datetime.now() - start_time
    
    if page_count == 0:
        await update.message.reply_text('Нет накопленных страниц.')
    else:
        status_msg = f'Накоплено страниц: {page_count}\n'
        status_msg += f'Время работы: {str(uptime).split(".")[0]}\n'
        status_msg += f'Используйте /send для получения PDF'
        await update.message.reply_text(status_msg)

def run_flask():
    """Run Flask server in background."""
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

def main():
    """Start the bot."""
    logger.info("Starting PDF bot...")
    
    # Start Flask server in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask server started on port {PORT}")
    
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("send", send_combined_pdf))
    application.add_handler(CommandHandler("clear", clear_pages))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    
    # Start polling
    logger.info("Starting bot polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()