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

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skip loading .env file

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token - задается через переменную окружения BOT_TOKEN
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

# Temporary directory for processing
TEMP_DIR = '/tmp/pdf_bot'
os.makedirs(TEMP_DIR, exist_ok=True)

# Global storage for all processed pages
all_processed_pages = []

# Keep-alive mechanism
last_activity = datetime.now()

def keep_alive_worker():
    """Background worker to keep the service alive"""
    global last_activity
    
    while True:
        try:
            current_time = datetime.now()
            # Send a heartbeat every 10 minutes
            if current_time - last_activity > timedelta(minutes=10):
                logger.info(f"Heartbeat - Bot is alive at {current_time}")
                last_activity = current_time
            
            # Clean up old temporary files every hour
            try:
                current_timestamp = time.time()
                for file in os.listdir(TEMP_DIR):
                    if file.startswith('quadrant_') or file.startswith('temp_'):
                        file_path = os.path.join(TEMP_DIR, file)
                        if os.path.exists(file_path):
                            file_age = current_timestamp - os.path.getmtime(file_path)
                            # Remove files older than 1 hour
                            if file_age > 3600:
                                os.remove(file_path)
                                logger.info(f"Cleaned up old file: {file}")
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
            
            time.sleep(300)  # Sleep for 5 minutes
            
        except Exception as e:
            logger.error(f"Keep-alive worker error: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

def update_activity():
    """Update last activity timestamp"""
    global last_activity
    last_activity = datetime.now()

async def start(update: Update, context):
    """Send a message when the command /start is issued."""
    update_activity()
    await update.message.reply_text(
        '👋 Привет! Отправь мне PDF-файлы, и я извлеку верхний левый квадрант каждой страницы.\n\n'
        '📄 Все обработанные страницы будут накапливаться.\n'
        '📤 /send - получить объединенный PDF со всеми страницами\n'
        '🗑️ /clear - очистить накопленные страницы\n'
        '📊 /status - показать текущий статус\n'
        '🔄 /health - проверить здоровье бота\n\n'
        '⚠️ Отправляйте файлы по одному для лучшей стабильности!\n'
        f'🤖 Бот запущен: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
    )

def extract_top_left_quadrant(pdf_path):
    """Extract top-left quadrant from each page of a PDF."""
    # Use system poppler - try different paths for different environments
    try:
        # Try without poppler_path first (for Render/Linux)
        images = convert_from_path(pdf_path, dpi=150)  # Reduced DPI for faster processing
    except Exception:
        try:
            # Try with Homebrew path (for macOS)
            images = convert_from_path(pdf_path, poppler_path='/opt/homebrew/bin', dpi=150)
        except Exception:
            # Try with system path
            images = convert_from_path(pdf_path, poppler_path='/usr/bin', dpi=150)
    
    quadrant_images = []
    
    # Create unique timestamp for this PDF processing
    timestamp = int(time.time() * 1000)  # milliseconds for uniqueness
    
    for i, img in enumerate(images):
        # Get image dimensions
        width, height = img.size
        
        # Calculate quadrant dimensions
        quadrant_width = width // 2
        quadrant_height = height // 2
        
        # Crop top-left quadrant
        quadrant = img.crop((0, 0, quadrant_width, quadrant_height))
        
        # Save quadrant with unique name
        quadrant_path = os.path.join(TEMP_DIR, f'quadrant_{timestamp}_{i}.png')
        quadrant.save(quadrant_path, optimize=True)  # Optimize PNG size
        quadrant_images.append(quadrant_path)
    
    return quadrant_images

def create_pdf_from_images(image_paths):
    """Convert images to PDFs with each image on a full A4 page."""
    timestamp = int(time.time())
    output_path = os.path.join(TEMP_DIR, f'combined_quadrants_{timestamp}.pdf')
    
    # Create a new PDF with Reportlab
    c = canvas.Canvas(output_path, pagesize=A4)
    
    # A4 dimensions
    page_width, page_height = A4
    
    for i, img_path in enumerate(image_paths):
        try:
            # Open the image
            img = Image.open(img_path)
            
            # Resize image to fill the entire page
            img_resized = img.resize((int(page_width), int(page_height)), Image.LANCZOS)
            
            # Save resized image temporarily
            resized_path = img_path.replace('.png', f'_full_{i}.png')
            img_resized.save(resized_path, optimize=True)
            
            # Draw the image on the page
            c.drawImage(resized_path, 0, 0, width=page_width, height=page_height)
            
            # Clean up resized image immediately
            try:
                os.remove(resized_path)
            except:
                pass
            
            # Move to next page
            c.showPage()
            
        except Exception as e:
            logger.error(f"Error processing image {img_path}: {e}")
            continue
    
    # Save the PDF
    c.save()
    
    return output_path

async def handle_pdf(update: Update, context):
    """Handle incoming PDF files."""
    global all_processed_pages
    update_activity()
    
    pdf_path = None
    wait_message = None
    
    try:
        # Check file size before downloading
        file_size = update.message.document.file_size
        if file_size > 20 * 1024 * 1024:  # 20MB limit
            await update.message.reply_text('❌ Файл слишком большой (>20MB). Отправьте файл поменьше.')
            return
        
        # Inform user about processing
        wait_message = await update.message.reply_text('📥 Загрузка файла...')
        
        # Get the file with timeout handling
        try:
            pdf_file = await asyncio.wait_for(
                update.message.document.get_file(), 
                timeout=60.0  # 60 seconds timeout
            )
        except asyncio.TimeoutError:
            await wait_message.edit_text('⏰ Таймаут при загрузке файла. Попробуйте еще раз.')
            return
        except (TimedOut, NetworkError) as e:
            await wait_message.edit_text('🌐 Ошибка сети при загрузке файла. Попробуйте еще раз.')
            return
        
        # Download the file
        pdf_path = os.path.join(TEMP_DIR, f"temp_{int(time.time())}_{update.message.document.file_name}")
        
        try:
            await asyncio.wait_for(
                pdf_file.download_to_drive(pdf_path),
                timeout=120.0  # 2 minutes timeout for download
            )
        except asyncio.TimeoutError:
            await wait_message.edit_text('⏰ Таймаут при скачивании файла. Файл слишком большой.')
            return
        except (TimedOut, NetworkError) as e:
            await wait_message.edit_text('🌐 Ошибка сети при скачивании файла. Попробуйте еще раз.')
            return
        
        # Update status
        await wait_message.edit_text('🔄 Обработка PDF...')
        
        # Extract quadrants
        quadrant_images = extract_top_left_quadrant(pdf_path)
        
        # Add to global storage
        all_processed_pages.extend(quadrant_images)
        
        # Log the files for debugging
        logger.info(f"Added {len(quadrant_images)} pages to storage. Total pages: {len(all_processed_pages)}")
        
        # Inform user about current status
        total_pages = len(all_processed_pages)
        await wait_message.edit_text(
            f'✅ PDF обработан! Извлечено {len(quadrant_images)} страниц.\n'
            f'📄 Всего накоплено страниц: {total_pages}\n'
            f'📤 Используй /send для получения объединенного PDF'
        )
        
        update_activity()
    
    except Exception as e:
        error_msg = f'❌ Ошибка: {str(e)}'
        if wait_message:
            await wait_message.edit_text(error_msg)
        else:
            await update.message.reply_text(error_msg)
        logger.error(f"Error processing PDF: {e}")
    
    finally:
        # Clean up the downloaded PDF file
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception as e:
                logger.error(f"Error removing temp file {pdf_path}: {e}")

async def send_combined_pdf(update: Update, context):
    """Send combined PDF with all processed pages."""
    global all_processed_pages
    update_activity()
    
    if not all_processed_pages:
        await update.message.reply_text('📭 Нет обработанных страниц. Сначала отправь PDF файлы!')
        return
    
    result_pdf_path = None
    wait_message = None
    
    try:
        wait_message = await update.message.reply_text('🔄 Создание объединенного PDF...')
        
        # Create combined PDF
        result_pdf_path = create_pdf_from_images(all_processed_pages)
        
        # Check file size
        file_size = os.path.getsize(result_pdf_path)
        if file_size > 50 * 1024 * 1024:  # 50MB limit
            await wait_message.edit_text('❌ PDF слишком большой (>50MB). Попробуйте /clear и отправьте меньше файлов.')
            return
        
        # Send result with timeout
        await wait_message.edit_text('📤 Отправка PDF...')
        
        try:
            with open(result_pdf_path, 'rb') as pdf_file:
                await asyncio.wait_for(
                    update.message.reply_document(
                        pdf_file, 
                        filename=f'combined_quadrants_{int(time.time())}.pdf',
                        caption=f'✅ Объединенный PDF готов! Всего страниц: {len(all_processed_pages)}'
                    ),
                    timeout=300.0  # 5 minutes timeout for sending
                )
        except asyncio.TimeoutError:
            await wait_message.edit_text('⏰ Таймаут при отправке файла. Файл слишком большой.')
            return
        except (TimedOut, NetworkError) as e:
            await wait_message.edit_text('🌐 Ошибка сети при отправке файла. Попробуйте еще раз.')
            return
        
        await wait_message.edit_text('✅ PDF успешно отправлен!')
        update_activity()
            
    except Exception as e:
        error_msg = f'❌ Ошибка при создании PDF: {str(e)}'
        if wait_message:
            await wait_message.edit_text(error_msg)
        else:
            await update.message.reply_text(error_msg)
        logger.error(f"Error creating combined PDF: {e}")
    
    finally:
        # Clean up the result file
        if result_pdf_path and os.path.exists(result_pdf_path):
            try:
                os.remove(result_pdf_path)
            except Exception as e:
                logger.error(f"Error removing result file {result_pdf_path}: {e}")

async def clear_pages(update: Update, context):
    """Clear all accumulated pages."""
    global all_processed_pages
    update_activity()
    
    page_count = len(all_processed_pages)
    all_processed_pages.clear()
    
    # Clean up temporary image files
    try:
        for file in os.listdir(TEMP_DIR):
            if file.startswith('quadrant_') and file.endswith('.png'):
                file_path = os.path.join(TEMP_DIR, file)
                if os.path.exists(file_path):
                    os.remove(file_path)
        logger.info(f"Cleaned up {page_count} temporary image files")
    except Exception as e:
        logger.error(f"Error cleaning up temp files: {e}")
    
    await update.message.reply_text(f'🗑️ Очищено {page_count} страниц. Готов к новым файлам!')

async def status(update: Update, context):
    """Show current status."""
    global all_processed_pages, last_activity
    update_activity()
    
    page_count = len(all_processed_pages)
    uptime = datetime.now() - last_activity
    
    status_msg = f'📊 Статус:\n'
    if page_count == 0:
        status_msg += '📭 Нет накопленных страниц.\n'
    else:
        status_msg += f'📄 Накоплено страниц: {page_count}\n'
        status_msg += f'📤 Используйте /send для получения PDF\n'
        status_msg += f'🗑️ Используйте /clear для очистки\n'
    
    status_msg += f'⏰ Последняя активность: {last_activity.strftime("%H:%M:%S")}'
    
    await update.message.reply_text(status_msg)

async def health_check(update: Update, context):
    """Health check command."""
    global last_activity
    update_activity()
    
    current_time = datetime.now()
    uptime = current_time - last_activity
    
    # Check disk space
    disk_usage = "Unknown"
    try:
        import shutil
        total, used, free = shutil.disk_usage(TEMP_DIR)
        disk_usage = f"{free // (1024**2)} MB free"
    except:
        pass
    
    # Check memory usage
    memory_usage = "Unknown"
    try:
        import psutil
        memory = psutil.virtual_memory()
        memory_usage = f"{memory.percent}% used"
    except:
        pass
    
    health_msg = (
        f'🏥 Проверка здоровья:\n'
        f'✅ Бот работает\n'
        f'⏰ Текущее время: {current_time.strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'💾 Диск: {disk_usage}\n'
        f'🧠 Память: {memory_usage}\n'
        f'📁 Temp файлов: {len(os.listdir(TEMP_DIR))}\n'
        f'📄 Накоплено страниц: {len(all_processed_pages)}'
    )
    
    await update.message.reply_text(health_msg)

def main():
    """Start the bot."""
    # Start keep-alive worker in background
    keepalive_thread = threading.Thread(target=keep_alive_worker, daemon=True)
    keepalive_thread.start()
    
    logger.info("Starting PDF bot with keep-alive mechanism...")
    logger.info(f"Python version: {os.sys.version}")
    logger.info(f"Temp directory: {TEMP_DIR}")
    
    # Check if running on Render
    if os.getenv('RENDER'):
        logger.info("Running on Render platform")
        port = int(os.getenv('PORT', 8080))
        
        # Start simple HTTP server for Render health checks
        def start_http_server():
            from http.server import HTTPServer, BaseHTTPRequestHandler
            
            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == '/health':
                        self.send_response(200)
                        self.send_header('Content-type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'OK')
                    else:
                        self.send_response(404)
                        self.end_headers()
                
                def log_message(self, format, *args):
                    pass  # Suppress HTTP logs
            
            server = HTTPServer(('0.0.0.0', port), HealthHandler)
            server.serve_forever()
        
        # Start HTTP server in background for Render
        http_thread = threading.Thread(target=start_http_server, daemon=True)
        http_thread.start()
        logger.info(f"HTTP health check server started on port {port}")
    
    application = Application.builder().token(TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("send", send_combined_pdf))
    application.add_handler(CommandHandler("clear", clear_pages))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("health", health_check))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    
    # Start the Bot with error handling and retries
    max_retries = 5
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            logger.info(f"Starting bot polling (attempt {retry_count + 1})...")
            application.run_polling(
                drop_pending_updates=True,
                poll_interval=2.0,  # Poll every 2 seconds
                timeout=20,         # 20 seconds timeout for getUpdates
                read_timeout=30,    # 30 seconds read timeout
                write_timeout=30    # 30 seconds write timeout
            )
            break  # If we reach here, polling ended normally
            
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
            break
        except Exception as e:
            retry_count += 1
            logger.error(f"Bot crashed (attempt {retry_count}/{max_retries}): {e}")
            
            if retry_count < max_retries:
                wait_time = min(60 * retry_count, 300)  # Exponential backoff, max 5 minutes
                logger.info(f"Restarting in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                logger.error("Max retries reached, giving up")
                raise

if __name__ == '__main__':
    main()