#!/usr/bin/env bash

set -e  # Exit immediately if a command fails

SERVICE_NAME="wth-edge-device.service"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SOURCE="$PROJECT_DIR/$SERVICE_NAME"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME"

echo "🔧 Installing $SERVICE_NAME..."

# Check if service file exists
if [ ! -f "$SERVICE_SOURCE" ]; then
    echo "❌ Service file not found at $SERVICE_SOURCE"
    exit 1
fi

# Copy service file
echo "📂 Copying service file to $SERVICE_DEST"
sudo cp "$SERVICE_SOURCE" "$SERVICE_DEST"

# Set correct permissions
sudo chmod 644 "$SERVICE_DEST"

# Reload systemd
echo "🔄 Reloading systemd daemon"
sudo systemctl daemon-reload

# Enable service on boot
echo "🚀 Enabling service"
sudo systemctl enable "$SERVICE_NAME"

# Ask user if they want to start it now
read -p "Start the service now? (y/n): " START_NOW
if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
    sudo systemctl start "$SERVICE_NAME"
    echo "✅ Service started"
else
    echo "ℹ️ Service installed but not started"
fi

echo "🎉 Done!"
echo "Check status with: systemctl status $SERVICE_NAME"