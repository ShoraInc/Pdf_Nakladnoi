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
from flask import Flask, request

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

# Reduce noise from other loggers
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.INFO)

# Bot configuration
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # https://your-app-name.onrender.com
PORT = int(os.getenv('PORT', 8080))

# Temporary directory for processing
TEMP_DIR = '/tmp/pdf_bot'
os.makedirs(TEMP_DIR, exist_ok=True)

# Global storage for all processed pages
all_processed_pages = []
start_time = datetime.now()

# Flask app for webhook
app = Flask(__name__)

# Telegram application
application = None

async def start(update: Update, context):
    """Send a message when the command /start is issued."""
    uptime = datetime.now() - start_time
    await update.message.reply_text(
        f'👋 Привет! Отправь мне PDF-файлы, и я извлеку верхний левый квадрант каждой страницы.\n\n'
        f'📄 Все обработанные страницы будут накапливаться.\n'
        f'📤 /send - получить объединенный PDF со всеми страницами\n'
        f'🗑️ /clear - очистить накопленные страницы\n'
        f'📊 /status - показать текущий статус\n'
        f'🌐 Работаю через Webhook на Render!\n'
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
    
    if page_count == 0:
        status_msg = '📭 Нет накопленных страниц.'
    else:
        status_msg = f'📄 Накоплено страниц: {page_count}\n📤 /send для получения PDF'
    
    status_msg += f'\n⏰ Время: {current_time.strftime("%H:%M:%S")}'
    status_msg += f'\n🚀 Время работы: {str(uptime).split(".")[0]}'
    status_msg += f'\n🌐 Webhook режим активен'
    
    await update.message.reply_text(status_msg)

# Flask routes
@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming webhook requests from Telegram."""
    try:
        json_data = request.get_json()
        logger.info(f"📨 Received webhook data: {json_data.get('message', {}).get('text', 'No text')[:50] if json_data else 'No data'}")
        
        if json_data and application:
            update = Update.de_json(json_data, application.bot)
            
            # Process update in async context
            def run_async():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                    async def process():
                        try:
                            await application.process_update(update)
                            logger.info("✅ Update processed successfully")
                        except Exception as e:
                            logger.error(f"❌ Error processing update: {e}")
                    
                    loop.run_until_complete(process())
                except Exception as e:
                    logger.error(f"❌ Async processing error: {e}")
                finally:
                    try:
                        loop.close()
                    except:
                        pass
            
            # Run in separate thread to avoid blocking Flask
            thread = threading.Thread(target=run_async)
            thread.daemon = True
            thread.start()
        
        return 'OK'
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        import traceback
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return 'ERROR', 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint for external monitoring services."""
    uptime_seconds = int((datetime.now() - start_time).total_seconds())
    
    # Clean up old files periodically
    try:
        if uptime_seconds % 3600 == 0:  # Every hour
            cleanup_old_files()
    except:
        pass
    
    return {
        'status': 'healthy',
        'time': datetime.now().isoformat(),
        'uptime_seconds': uptime_seconds,
        'pages_accumulated': len(all_processed_pages),
        'temp_files': len([f for f in os.listdir(TEMP_DIR) if f.startswith('quadrant_')])
    }

@app.route('/ping', methods=['GET', 'POST'])
def ping():
    """Simple ping endpoint for monitoring services."""
    return 'PONG'

@app.route('/debug', methods=['GET'])
def debug():
    """Debug endpoint to check bot status."""
    return {
        'application_initialized': application is not None,
        'bot_username': application.bot.username if application and application.bot else None,
        'webhook_url': WEBHOOK_URL,
        'handlers_count': len(application.handlers) if application else 0,
        'temp_dir': TEMP_DIR,
        'pages_count': len(all_processed_pages)
    }

@app.route('/', methods=['GET'])
def home():
    """Home page with bot status."""
    uptime = datetime.now() - start_time
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PDF Bot Status</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }}
            .container {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #2e7d32; }}
            .status {{ color: #4caf50; font-weight: bold; }}
            .info {{ margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 PDF Bot Status</h1>
            <div class="status">✅ Running</div>
            <div class="info">⏰ Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            <div class="info">🚀 Uptime: {str(uptime).split('.')[0]}</div>
            <div class="info">📄 Pages Accumulated: {len(all_processed_pages)}</div>
            <div class="info">🌐 Mode: Webhook</div>
            <div class="info">🔧 Version: 1.0</div>
            
            <hr style="margin: 20px 0;">
            
            <h3>Monitoring Endpoints:</h3>
            <ul>
                <li><a href="/health">/health</a> - Health check (JSON)</li>
                <li><a href="/ping">/ping</a> - Simple ping</li>
            </ul>
            
            <p><strong>For UptimeRobot:</strong> Use /ping endpoint</p>
            <p><strong>For Cron-job.org:</strong> Use /health endpoint</p>
        </div>
    </body>
    </html>
    """

def cleanup_old_files():
    """Clean up old temporary files."""
    try:
        current_time = time.time()
        for file in os.listdir(TEMP_DIR):
            if file.startswith('quadrant_') or file.startswith('temp_') or file.startswith('combined_'):
                file_path = os.path.join(TEMP_DIR, file)
                if os.path.exists(file_path):
                    file_age = current_time - os.path.getmtime(file_path)
                    if file_age > 3600:  # Remove files older than 1 hour
                        os.remove(file_path)
                        logger.info(f"Cleaned up old file: {file}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

async def setup_webhook():
    """Set up webhook for Telegram bot."""
    global application
    
    try:
        application = Application.builder().token(TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("send", send_combined_pdf))
        application.add_handler(CommandHandler("clear", clear_pages))
        application.add_handler(CommandHandler("status", status))
        application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
        
        await application.initialize()
        await application.start()
        
        # Set webhook
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL}/webhook"
            await application.bot.set_webhook(webhook_url)
            logger.info(f"✅ Webhook set to: {webhook_url}")
            
            webhook_info = await application.bot.get_webhook_info()
            logger.info(f"🔍 Webhook info: {webhook_info.url}")
        else:
            logger.warning("⚠️ WEBHOOK_URL not set - running in development mode")
                
        logger.info("✅ Bot setup complete")
        
    except Exception as e:
        logger.error(f"❌ Failed to setup webhook: {e}")
        raise

def main():
    """Start the Flask app with webhook."""
    logger.info("="*50)
    logger.info("🚀 STARTING PDF BOT WITH WEBHOOK")
    logger.info("="*50)
    
    logger.info(f"Port: {PORT}")
    logger.info(f"Webhook URL: {WEBHOOK_URL}")
    logger.info(f"Bot token: {TOKEN[:10]}..." if TOKEN else "No token")
    logger.info(f"Temp directory: {TEMP_DIR}")
    
    # Setup webhook
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_webhook())
    loop.close()
    
    # Start cleanup thread
    cleanup_thread = threading.Thread(target=lambda: [time.sleep(3600), cleanup_old_files()], daemon=True)
    cleanup_thread.start()
    
    # Start Flask app
    logger.info("🌐 Starting Flask webhook server...")
    logger.info("📊 Bot ready for monitoring!")
    app.run(host='0.0.0.0', port=PORT, debug=False)

if __name__ == '__main__':
    main()