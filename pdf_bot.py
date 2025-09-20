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

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s', 
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Reduce noise
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.INFO)

# Bot configuration
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8080))

# Detect if running on Render
IS_RENDER = bool(os.getenv('RENDER'))
USE_WEBHOOK = IS_RENDER and bool(WEBHOOK_URL)

# Temporary directory
TEMP_DIR = '/tmp/pdf_bot'
os.makedirs(TEMP_DIR, exist_ok=True)

# Global storage
all_processed_pages = []
start_time = datetime.now()

# Flask app (only if webhook mode)
if USE_WEBHOOK:
    from flask import Flask, request
    app = Flask(__name__)

# Telegram application
application = None

async def start(update: Update, context):
    """Send a message when the command /start is issued."""
    uptime = datetime.now() - start_time
    mode = "Webhook" if USE_WEBHOOK else "Polling"
    
    await update.message.reply_text(
        f'👋 Привет! Отправь мне PDF-файлы, и я извлеку верхний левый квадрант каждой страницы.\n\n'
        f'📄 Все обработанные страницы будут накапливаться.\n'
        f'📤 /send - получить объединенный PDF\n'
        f'🗑️ /clear - очистить накопленные страницы\n'
        f'📊 /status - показать статус\n'
        f'🔧 Режим: {mode}\n'
        f'⏰ Время работы: {str(uptime).split(".")[0]}\n\n'
        f'⚠️ Отправляйте файлы по одному для лучшей стабильности!'
    )

def extract_top_left_quadrant(pdf_path):
    """Extract top-left quadrant from each page of a PDF."""
    try:
        images = convert_from_path(pdf_path, dpi=150)
    except Exception:
        try:
            images = convert_from_path(pdf_path, poppler_path='/opt/homebrew/bin', dpi=150)
        except Exception:
            images = convert_from_path(pdf_path, poppler_path='/usr/bin', dpi=150)
    
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
    timestamp = int(time.time())
    output_path = os.path.join(TEMP_DIR, f'combined_quadrants_{timestamp}.pdf')
    
    c = canvas.Canvas(output_path, pagesize=A4)
    page_width, page_height = A4
    
    for i, img_path in enumerate(image_paths):
        try:
            img = Image.open(img_path)
            img_resized = img.resize((int(page_width), int(page_height)), Image.LANCZOS)
            
            resized_path = img_path.replace('.png', f'_full_{i}.png')
            img_resized.save(resized_path, optimize=True)
            
            c.drawImage(resized_path, 0, 0, width=page_width, height=page_height)
            
            try:
                os.remove(resized_path)
            except:
                pass
                
            c.showPage()
            
        except Exception as e:
            logger.error(f"Error processing image {img_path}: {e}")
            continue
    
    c.save()
    return output_path

async def handle_pdf(update: Update, context):
    """Handle incoming PDF files."""
    global all_processed_pages
    
    pdf_path = None
    wait_message = None
    
    try:
        file_size = update.message.document.file_size
        if file_size > 20 * 1024 * 1024:
            await update.message.reply_text('❌ Файл слишком большой (>20MB).')
            return
        
        wait_message = await update.message.reply_text('📥 Загрузка файла...')
        
        pdf_file = await asyncio.wait_for(
            update.message.document.get_file(), 
            timeout=60.0
        )
        
        pdf_path = os.path.join(TEMP_DIR, f"temp_{int(time.time())}_{update.message.document.file_name}")
        
        await asyncio.wait_for(
            pdf_file.download_to_drive(pdf_path),
            timeout=120.0
        )
        
        await wait_message.edit_text('🔄 Обработка PDF...')
        
        quadrant_images = extract_top_left_quadrant(pdf_path)
        all_processed_pages.extend(quadrant_images)
        
        total_pages = len(all_processed_pages)
        await wait_message.edit_text(
            f'✅ PDF обработан! Извлечено {len(quadrant_images)} страниц.\n'
            f'📄 Всего накоплено страниц: {total_pages}\n'
            f'📤 Используй /send для получения объединенного PDF'
        )
    
    except Exception as e:
        error_msg = f'❌ Ошибка: {str(e)}'
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
        await update.message.reply_text('📭 Нет обработанных страниц!')
        return
    
    result_pdf_path = None
    wait_message = None
    
    try:
        wait_message = await update.message.reply_text('🔄 Создание объединенного PDF...')
        
        result_pdf_path = create_pdf_from_images(all_processed_pages)
        
        file_size = os.path.getsize(result_pdf_path)
        if file_size > 50 * 1024 * 1024:
            await wait_message.edit_text('❌ PDF слишком большой (>50MB).')
            return
        
        await wait_message.edit_text('📤 Отправка PDF...')
        
        with open(result_pdf_path, 'rb') as pdf_file:
            await asyncio.wait_for(
                update.message.reply_document(
                    pdf_file, 
                    filename=f'combined_quadrants_{int(time.time())}.pdf',
                    caption=f'✅ Объединенный PDF готов! Всего страниц: {len(all_processed_pages)}'
                ),
                timeout=300.0
            )
        
        await wait_message.edit_text('✅ PDF успешно отправлен!')
            
    except Exception as e:
        error_msg = f'❌ Ошибка при создании PDF: {str(e)}'
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
    
    await update.message.reply_text(f'🗑️ Очищено {page_count} страниц!')

async def status(update: Update, context):
    """Show current status."""
    global all_processed_pages, start_time
    
    page_count = len(all_processed_pages)
    current_time = datetime.now()
    uptime = current_time - start_time
    mode = "Webhook" if USE_WEBHOOK else "Polling"
    
    if page_count == 0:
        status_msg = '📭 Нет накопленных страниц.'
    else:
        status_msg = f'📄 Накоплено страниц: {page_count}\n📤 /send для получения PDF'
    
    status_msg += f'\n⏰ Время: {current_time.strftime("%H:%M:%S")}'
    status_msg += f'\n🚀 Время работы: {str(uptime).split(".")[0]}'
    status_msg += f'\n🔧 Режим: {mode}'
    status_msg += f'\n🌐 Платформа: {"Render" if IS_RENDER else "Local"}'
    
    await update.message.reply_text(status_msg)

# Flask routes (only for webhook mode)
if USE_WEBHOOK:
    
    @app.route('/webhook', methods=['POST'])
    def webhook():
        """Handle incoming webhook requests from Telegram."""
        try:
            json_data = request.get_json()
            logger.info(f"📨 Received webhook data")
            
            if json_data and application:
                update = Update.de_json(json_data, application.bot)
                
                # Simple synchronous processing
                try:
                    # Create a new event loop and run the async function
                    asyncio.run(application.process_update(update))
                    logger.info("✅ Update processed successfully")
                except Exception as e:
                    logger.error(f"❌ Error processing update: {e}")
            
            return 'OK'
        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
            return 'ERROR', 500

    @app.route('/health', methods=['GET'])
    def health():
        """Health check endpoint."""
        uptime_seconds = int((datetime.now() - start_time).total_seconds())
        return {
            'status': 'healthy',
            'time': datetime.now().isoformat(),
            'uptime_seconds': uptime_seconds,
            'pages_accumulated': len(all_processed_pages),
            'mode': 'webhook'
        }

    @app.route('/ping', methods=['GET', 'POST'])
    def ping():
        """Simple ping endpoint."""
        return 'PONG'

    @app.route('/', methods=['GET'])
    def home():
        """Home page."""
        uptime = datetime.now() - start_time
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>PDF Bot Status</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }}
                .container {{ background: white; padding: 30px; border-radius: 10px; }}
                h1 {{ color: #2e7d32; }}
                .status {{ color: #4caf50; font-weight: bold; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🤖 PDF Bot Status</h1>
                <div class="status">✅ Running (Webhook Mode)</div>
                <p>⏰ Uptime: {str(uptime).split('.')[0]}</p>
                <p>📄 Pages: {len(all_processed_pages)}</p>
                <p>🌐 Render Platform</p>
            </div>
        </body>
        </html>
        """

def cleanup_old_files():
    """Clean up old temporary files."""
    try:
        current_time = time.time()
        for file in os.listdir(TEMP_DIR):
            if any(file.startswith(prefix) for prefix in ['quadrant_', 'temp_', 'combined_']):
                file_path = os.path.join(TEMP_DIR, file)
                if os.path.exists(file_path):
                    file_age = current_time - os.path.getmtime(file_path)
                    if file_age > 3600:  # 1 hour
                        os.remove(file_path)
                        logger.info(f"Cleaned up old file: {file}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

async def setup_application():
    """Setup Telegram application with handlers."""
    global application
    
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("send", send_combined_pdf))
    application.add_handler(CommandHandler("clear", clear_pages))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    
    logger.info("✅ Application handlers added")

async def main():
    """Main function to run the bot."""
    logger.info("="*50)
    logger.info("🚀 STARTING PDF BOT")
    logger.info("="*50)
    
    logger.info(f"Mode: {'Webhook' if USE_WEBHOOK else 'Polling'}")
    logger.info(f"Platform: {'Render' if IS_RENDER else 'Local'}")
    logger.info(f"Port: {PORT}")
    logger.info(f"Webhook URL: {WEBHOOK_URL}")
    logger.info(f"Bot token: {TOKEN[:10]}...")
    
    # Setup application
    await setup_application()
    
    if USE_WEBHOOK:
        # Webhook mode
        logger.info("🌐 Starting webhook mode...")
        
        # Initialize and start application
        await application.initialize()
        await application.start()
        
        # Set webhook
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await application.bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook set to: {webhook_url}")
        
        # Start Flask in a separate thread
        def run_flask():
            app.run(host='0.0.0.0', port=PORT, debug=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("🌐 Flask server started")
        
        # Keep main thread alive
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep 1 hour
                cleanup_old_files()
        except KeyboardInterrupt:
            logger.info("🛑 Shutting down...")
        finally:
            await application.stop()
            await application.shutdown()
    
    else:
        # Polling mode
        logger.info("🔄 Starting polling mode...")
        
        # Start cleanup thread
        cleanup_thread = threading.Thread(
            target=lambda: [time.sleep(3600), cleanup_old_files()], 
            daemon=True
        )
        cleanup_thread.start()
        
        # Run polling
        application.run_polling(
            drop_pending_updates=True,
            poll_interval=2.0,
            timeout=20,
            read_timeout=30,
            write_timeout=30
        )

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"💥 Bot crashed: {e}")
        raise