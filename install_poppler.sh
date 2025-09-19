#!/bin/bash
# Install poppler for Render deployment

# Update package list
apt-get update

# Install poppler-utils
apt-get install -y poppler-utils

# Verify installation
pdftoppm -h > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "✅ Poppler installed successfully"
else
    echo "❌ Poppler installation failed"
    exit 1
fi
