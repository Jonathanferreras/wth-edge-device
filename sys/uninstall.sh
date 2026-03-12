#!/usr/bin/env bash

set -e  # Exit immediately if a command fails

SERVICE_NAME="wth-edge-device.service"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME"

echo "🗑️ Uninstalling $SERVICE_NAME..."

# Stop service if running

if systemctl is-active --quiet "$SERVICE_NAME"; then
echo "⏹️ Stopping service..."
sudo systemctl stop "$SERVICE_NAME"
else
echo "ℹ️ Service is not running"
fi

# Disable service

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
echo "🚫 Disabling service..."
sudo systemctl disable "$SERVICE_NAME"
else
echo "ℹ️ Service is not enabled"
fi

# Remove service file

if [ -f "$SERVICE_DEST" ]; then
echo "🗑️ Removing service file..."
sudo rm "$SERVICE_DEST"
else
echo "ℹ️ Service file not found"
fi

# Reload systemd

echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "🧹 Resetting failed state..."
sudo systemctl reset-failed

echo "✅ $SERVICE_NAME has been removed"
