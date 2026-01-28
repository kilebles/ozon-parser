#!/bin/bash
set -e

# Ozon Parser Installation Script
# Run as root: sudo bash install.sh

APP_DIR="/opt/ozon-parser"
USER="ozon"
LOG_DIR="/var/log/ozon-parser"

echo "=== Ozon Parser Installation ==="

# Create user if not exists
if ! id "$USER" &>/dev/null; then
    echo "Creating user $USER..."
    useradd -r -s /bin/false -d "$APP_DIR" "$USER"
fi

# Create directories
echo "Creating directories..."
mkdir -p "$APP_DIR"
mkdir -p "$LOG_DIR"

# Copy application files
echo "Copying application files..."
cp -r app "$APP_DIR/"
cp pyproject.toml "$APP_DIR/"
cp uv.lock "$APP_DIR/"

# Copy credentials and env (must exist)
if [ -f "credentials.json" ]; then
    cp credentials.json "$APP_DIR/"
else
    echo "WARNING: credentials.json not found. Copy it manually to $APP_DIR/"
fi

if [ -f ".env" ]; then
    cp .env "$APP_DIR/"
else
    echo "WARNING: .env not found. Copy it manually to $APP_DIR/"
fi

# Copy browser data if exists (important for avoiding captcha)
if [ -d "browser_data" ]; then
    echo "Copying browser session data..."
    cp -r browser_data "$APP_DIR/"
else
    echo "WARNING: browser_data/ not found."
    echo "  Copy it from your local machine to avoid captcha on first run:"
    echo "  scp -r browser_data/ user@server:$APP_DIR/"
fi

# Set ownership
chown -R "$USER:$USER" "$APP_DIR"
chown -R "$USER:$USER" "$LOG_DIR"

# Install uv if not present
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Create virtual environment and install dependencies
echo "Installing Python dependencies..."
cd "$APP_DIR"
sudo -u "$USER" uv sync --frozen

# Install Playwright browsers
echo "Installing Playwright browsers..."
sudo -u "$USER" "$APP_DIR/.venv/bin/playwright" install chromium
sudo -u "$USER" "$APP_DIR/.venv/bin/playwright" install-deps chromium

# Install systemd files
echo "Installing systemd service and timer..."
cp deploy/ozon-parser.service /etc/systemd/system/
cp deploy/ozon-parser.timer /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable and start timer
systemctl enable ozon-parser.timer
systemctl start ozon-parser.timer

echo ""
echo "=== Installation complete ==="
echo ""
echo "Commands:"
echo "  Run now:           sudo systemctl start ozon-parser"
echo "  Check status:      sudo systemctl status ozon-parser"
echo "  View logs:         tail -f $LOG_DIR/parser.log"
echo "  Check timer:       systemctl list-timers ozon-parser.timer"
echo "  Stop timer:        sudo systemctl stop ozon-parser.timer"
echo ""
echo "Config files:"
echo "  App directory:     $APP_DIR"
echo "  Environment:       $APP_DIR/.env"
echo "  Credentials:       $APP_DIR/credentials.json"
echo "  Browser session:   $APP_DIR/browser_data/"
echo "  Logs:              $LOG_DIR/parser.log"
echo ""
echo "IMPORTANT: If browser_data/ was not copied, copy it from your local machine:"
echo "  scp -r browser_data/ user@server:$APP_DIR/"
echo "  sudo chown -R $USER:$USER $APP_DIR/browser_data"
