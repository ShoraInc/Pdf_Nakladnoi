import os
import time
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.error import TimedOut, NetworkError
from PIL import Image
from pdf2image import convert_from_path
from PyPDF2 import PdfMerger, PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token - можно задать через переменную окружения BOT_TOKEN
TOKEN = os.getenv('BOT_TOKEN', '8208894435:AAF7vMtFFWjqYzrToAkgJFPLl2phD2XdD6I')

# Temporary directory for processing
TEMP_DIR = '/tmp/pdf_bot'
os.makedirs(TEMP_DIR, exist_ok=True)

# Global storage for all processed pages
all_processed_pages = []

async def start(update: Update, context):
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        '👋 Привет! Отправь мне PDF-файлы, и я извлеку верхний левый квадрант каждой страницы.\n\n'
        '📄 Все обработанные страницы будут накапливаться.\n'
        '📤 /send - получить объединенный PDF со всеми страницами\n'
        '🗑️ /clear - очистить накопленные страницы\n'
        '📊 /status - показать текущий статус\n\n'
        '⚠️ Отправляйте файлы по одному для лучшей стабильности!'
    )

def extract_top_left_quadrant(pdf_path):
    """Extract top-left quadrant from each page of a PDF."""
    # Use system poppler for better compatibility
    images = convert_from_path(pdf_path, poppler_path='/opt/homebrew/bin')
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
        quadrant.save(quadrant_path)
        quadrant_images.append(quadrant_path)
    
    return quadrant_images

def create_pdf_from_images(image_paths):
    """Convert images to PDFs with each image on a full A4 page."""
    output_path = os.path.join(TEMP_DIR, 'combined_quadrants.pdf')
    
    # Create a new PDF with Reportlab
    c = canvas.Canvas(output_path, pagesize=A4)
    
    # A4 dimensions
    page_width, page_height = A4
    
    for img_path in image_paths:
        # Open the image
        img = Image.open(img_path)
        
        # Resize image to fill the entire page
        img_resized = img.resize((int(page_width), int(page_height)), Image.LANCZOS)
        
        # Save resized image temporarily
        resized_path = img_path.replace('.png', '_full.png')
        img_resized.save(resized_path)
        
        # Draw the image on the page
        c.drawImage(resized_path, 0, 0, width=page_width, height=page_height)
        
        # Move to next page
        c.showPage()
    
    # Save the PDF
    c.save()
    
    return output_path

async def handle_pdf(update: Update, context):
    """Handle incoming PDF files."""
    global all_processed_pages
    
    pdf_path = None
    wait_message = None
    
    try:
        # Inform user about processing
        wait_message = await update.message.reply_text('📥 Загрузка файла...')
        
        # Get the file with timeout handling
        try:
            pdf_file = await asyncio.wait_for(
                update.message.document.get_file(), 
                timeout=60.0  # 60 seconds timeout
            )
        except asyncio.TimeoutError:
            await update.message.reply_text('⏰ Таймаут при загрузке файла. Попробуйте еще раз.')
            return
        except (TimedOut, NetworkError) as e:
            await update.message.reply_text('🌐 Ошибка сети при загрузке файла. Попробуйте еще раз.')
            return
        
        # Download the file
        pdf_path = os.path.join(TEMP_DIR, f"temp_{int(time.time())}_{update.message.document.file_name}")
        
        try:
            await asyncio.wait_for(
                pdf_file.download_to_drive(pdf_path),
                timeout=120.0  # 2 minutes timeout for download
            )
        except asyncio.TimeoutError:
            await update.message.reply_text('⏰ Таймаут при скачивании файла. Файл слишком большой.')
            return
        except (TimedOut, NetworkError) as e:
            await update.message.reply_text('🌐 Ошибка сети при скачивании файла. Попробуйте еще раз.')
            return
        
        # Update status
        await wait_message.edit_text('🔄 Обработка PDF...')
        
        # Extract quadrants
        quadrant_images = extract_top_left_quadrant(pdf_path)
        
        # Add to global storage
        all_processed_pages.extend(quadrant_images)
        
        # Log the files for debugging
        logger.info(f"Added {len(quadrant_images)} pages to storage. Total pages: {len(all_processed_pages)}")
        for i, img_path in enumerate(quadrant_images):
            logger.info(f"  Page {i+1}: {os.path.basename(img_path)}")
        
        # Inform user about current status
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
        # Clean up the downloaded PDF file
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception as e:
                logger.error(f"Error removing temp file {pdf_path}: {e}")

async def send_combined_pdf(update: Update, context):
    """Send combined PDF with all processed pages."""
    global all_processed_pages
    
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
    global all_processed_pages
    
    page_count = len(all_processed_pages)
    if page_count == 0:
        await update.message.reply_text('📭 Нет накопленных страниц. Отправьте PDF файлы для обработки.')
    else:
        await update.message.reply_text(
            f'📊 Статус:\n'
            f'📄 Накоплено страниц: {page_count}\n'
            f'📤 Используйте /send для получения объединенного PDF\n'
            f'🗑️ Используйте /clear для очистки'
        )

def main():
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("send", send_combined_pdf))
    application.add_handler(CommandHandler("clear", clear_pages))
    application.add_handler(CommandHandler("status", status))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    
    # Start the Bot
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
