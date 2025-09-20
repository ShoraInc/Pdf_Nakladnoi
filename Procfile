release: chmod +x install_poppler.sh && ./install_poppler.sh
web: gunicorn --bind 0.0.0.0:$PORT pdf_bot:app --worker-class eventlet