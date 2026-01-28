#!/bin/bash
set -e

# Ozon Parser Uninstallation Script
# Run as root: sudo bash uninstall.sh

APP_DIR="/opt/ozon-parser"
USER="ozon"
LOG_DIR="/var/log/ozon-parser"

echo "=== Ozon Parser Uninstallation ==="

# Stop and disable services
echo "Stopping services..."
systemctl stop ozon-parser.timer 2>/dev/null || true
systemctl stop ozon-parser 2>/dev/null || true
systemctl disable ozon-parser.timer 2>/dev/null || true

# Remove systemd files
echo "Removing systemd files..."
rm -f /etc/systemd/system/ozon-parser.service
rm -f /etc/systemd/system/ozon-parser.timer
systemctl daemon-reload

# Ask before removing data
read -p "Remove application directory $APP_DIR? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$APP_DIR"
    echo "Application directory removed."
fi

read -p "Remove log directory $LOG_DIR? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$LOG_DIR"
    echo "Log directory removed."
fi

read -p "Remove user $USER? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    userdel "$USER" 2>/dev/null || true
    echo "User removed."
fi

echo ""
echo "=== Uninstallation complete ==="
