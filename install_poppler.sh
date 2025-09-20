#!/bin/bash
# Install poppler for Render deployment

echo "🔧 Installing poppler-utils..."

# Update package list
apt-get update -y

# Install poppler-utils and other dependencies
apt-get install -y poppler-utils

# Verify installation
if command -v pdftoppm >/dev/null 2>&1; then
    echo "✅ Poppler installed successfully"
    pdftoppm -v
else
    echo "❌ Poppler installation failed"
    exit 1
fi

echo "🎉 Setup complete!"