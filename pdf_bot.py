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

# Bot configuration
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # https://your-app-name.onrender.com/webhook
PORT = int(os.getenv('PORT', 8080))

# Temporary directory for processing
TEMP_DIR = '/tmp/pdf_bot'
os.makedirs(TEMP_DIR, exist_ok=True)

# Global storage for all processed pages
all_processed_pages = []

# Flask app for webhook
app = Flask(__name__)

# Telegram application
application = None

async def start(update: Update, context):
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        '👋 Привет! Отправь мне PDF-файлы, и я извлеку верхний левый квадрант каждой страницы.\n\n'
        '📄 Все обработанные страницы будут накапливаться.\n'
        '📤 /send - получить объединенный PDF со всеми страницами\n'
        '🗑️ /clear - очистить накопленные страницы\n'
        '📊 /status - показать текущий статус\n'
        '🌐 Работаю через Webhook на Render!\n\n'
        '⚠️ Отправляйте файлы по одному для лучшей стабильности!'
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
    global all_processed_pages
    
    page_count = len(all_processed_pages)
    current_time = datetime.now()
    
    if page_count == 0:
        status_msg = '📭 Нет накопленных страниц.'
    else:
        status_msg = f'📄 Накоплено страниц: {page_count}\n📤 /send для получения PDF'
    
    status_msg += f'\n⏰ Время: {current_time.strftime("%H:%M:%S")}\n🌐 Webhook режим активен'
    
    await update.message.reply_text(status_msg)

# Flask routes
@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming webhook requests from Telegram."""
    try:
        json_data = request.get_json()
        logger.info(f"📨 Received webhook data: {bool(json_data)}")
        
        if json_data:
            update = Update.de_json(json_data, application.bot)
            
            # Process update in async context
            def run_async():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(application.process_update(update))
                finally:
                    loop.close()
            
            # Run in separate thread to avoid blocking Flask
            import threading
            thread = threading.Thread(target=run_async)
            thread.daemon = True
            thread.start()
        
        return 'OK'
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return 'ERROR', 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint for Render."""
    return {
        'status': 'healthy',
        'time': datetime.now().isoformat(),
        'pages': len(all_processed_pages)
    }

@app.route('/', methods=['GET'])
def home():
    """Home page."""
    return f"""
    <h1>🤖 PDF Bot is Running!</h1>
    <p>Status: ✅ Healthy</p>
    <p>Time: {datetime.now()}</p>
    <p>Pages accumulated: {len(all_processed_pages)}</p>
    <p>Bot: @{TOKEN.split(':')[0] if TOKEN else 'Unknown'}</p>
    """

async def setup_webhook():
    """Set up webhook for Telegram bot."""
    global application
    
    try:
        # Initialize application
        application = Application.builder().token(TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("send", send_combined_pdf))
        application.add_handler(CommandHandler("clear", clear_pages))
        application.add_handler(CommandHandler("status", status))
        application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
        
        # Initialize application
        await application.initialize()
        await application.start()
        
        # Set webhook
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL}/webhook"
            await application.bot.set_webhook(webhook_url)
            logger.info(f"✅ Webhook set to: {webhook_url}")
            
            # Test webhook
            webhook_info = await application.bot.get_webhook_info()
            logger.info(f"🔍 Webhook info: {webhook_info.url}")
        else:
            logger.warning("⚠️ WEBHOOK_URL not set - bot will work in development mode")
                
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
    
    # Setup webhook
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_webhook())
    loop.close()
    
    # Start Flask app
    logger.info("🌐 Starting Flask webhook server...")
    app.run(host='0.0.0.0', port=PORT, debug=False)

if __name__ == '__main__':
    main()