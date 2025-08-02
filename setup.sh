#!/bin/bash
# Lightning Detector Enhanced Setup Script v2.2-Production
# Automated installation and configuration for Raspberry Pi
# Implements a reliable, event-driven architecture with production enhancements.

set -e # Exit on any error

# --- Script Configuration ---
PROJECT_DIR=$(pwd)
VENV_NAME="lightning_detector_env"
SERVICE_NAME="lightning-detector"
REBOOT_REQUIRED=false

# --- Colors for output ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# --- Helper Functions ---
print_status() { echo -e "\n${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }
print_hardened() { echo -e "${PURPLE}[ENHANCED]${NC} $1"; }

# --- Main Script ---
clear
echo -e "${CYAN}"
echo "🌩️  Lightning Detector Enhanced Setup v2.2-Production"
echo "======================================================="
echo "This script will install and configure the lightning detector application."
echo -e "${NC}"

# 1. Pre-flight Checks
print_status "Running pre-flight checks..."
if [ "$EUID" -eq 0 ]; then
    print_error "This script should not be run as root. Please run as a regular user with sudo privileges."
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    print_error "Python 3 is not installed. Please install it before running this script."
    exit 1
fi

# FIX: Verify critical files exist before starting
if [ ! -f "lightning.py" ] || [ ! -f "requirements.txt" ]; then
    print_error "'lightning.py' or 'requirements.txt' not found!"
    print_error "Please run this script from the root of the lightning detector project directory."
    exit 1
fi

# 2. System Preparation
print_status "Updating system packages (this may take a few minutes)..."
sudo apt-get update && sudo apt-get upgrade -y

print_status "Installing required system dependencies..."
# FIX: Added python3-venv to the package list
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    build-essential \
    logrotate

# 3. Hardware Interface Configuration
print_hardened "Enabling SPI interface for reliable sensor communication..."
# FIX: Robustly find the boot config file location
BOOT_CONFIG="/boot/config.txt"
if [ ! -f "$BOOT_CONFIG" ]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
fi

if [ ! -f "$BOOT_CONFIG" ]; then
    print_error "Could not find boot config file at /boot/config.txt or /boot/firmware/config.txt. Exiting."
    exit 1
fi

if ! grep -q "^dtparam=spi=on" "$BOOT_CONFIG"; then
    echo "dtparam=spi=on" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    REBOOT_REQUIRED=true
    print_success "SPI enabled in boot configuration. A reboot will be required."
else
    print_status "SPI interface is already enabled in $BOOT_CONFIG."
fi

# FIX: Check if SPI devices are actually available after enabling
if ! ls /dev/spidev* >/dev/null 2>&1; then
    # If devices are not present, a reboot is definitely needed
    REBOOT_REQUIRED=true
fi

print_hardened "Setting up GPIO permissions for user '$USER'..."
sudo usermod -a -G gpio,spi,i2c "$USER"

# 4. Application Setup
print_status "Setting up Python virtual environment..."
if [ ! -d "$VENV_NAME" ]; then
    python3 -m venv "$VENV_NAME"
    print_success "Virtual environment created."
else
    print_status "Virtual environment already exists."
fi

source "$VENV_NAME/bin/activate"
pip install --upgrade pip

print_status "Installing Python dependencies from requirements.txt..."
pip install -r requirements.txt
deactivate

# 5. Configuration File
print_status "Checking for configuration file..."
if [ ! -f "config.ini" ]; then
    print_hardened "Creating default production config.ini..."
    cp config.ini.example config.ini 2>/dev/null || cat >config.ini <<'EOF'
[SYSTEM]
debug = false

[SENSOR]
spi_bus = 0
spi_device = 0
irq_pin = 2
indoor = false
sensitivity = medium
auto_start = true

[NOISE_HANDLING]
enabled = true
event_threshold = 15
time_window_seconds = 120
raised_noise_floor_level = 5
revert_delay_minutes = 10

[SLACK]
bot_token = xoxb-YOUR-BOT-TOKEN-HERE
channel = #alerts
enabled = true

[ALERTS]
critical_distance = 10
warning_distance = 30
all_clear_timer = 15
energy_threshold = 100000

[LOGGING]
level = INFO
max_file_size = 10
backup_count = 5
EOF
    print_success "Default config.ini created. Please edit it with your settings."
else
    print_warning "Existing config.ini found. Please ensure it is configured correctly."
fi

# 6. Log Rotation
print_hardened "Setting up log rotation for lightning_detector.log..."
LOGROTATE_CONF="/etc/logrotate.d/$SERVICE_NAME"
sudo tee "$LOGROTATE_CONF" >/dev/null <<EOF
$PROJECT_DIR/lightning_detector.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 644 $USER $USER
}
EOF
print_success "Log rotation configured."

# 7. Systemd Service Setup
SERVICE_REPLY=""
echo
read -p "🚀 Set up systemd service to auto-start on boot? (y/n): " -n 1 -r SERVICE_REPLY
echo
if [[ $SERVICE_REPLY =~ ^[Yy]$ ]]; then
    print_hardened "Creating production-ready systemd service..."
    SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

    # FIX: Use a more robust Group setting and improved network dependencies
    sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Lightning Detector Service v2.2-Production
Documentation=https://github.com/your-repo/your-project
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$PROJECT_DIR
Environment="PYTHONUNBUFFERED=1"

# Main execution command
ExecStart=$PROJECT_DIR/$VENV_NAME/bin/python3 lightning.py

# --- Hardening and Reliability ---
# Automatically restart the service if it fails
Restart=always
# Wait 10 seconds before restarting to prevent fast crash loops
RestartSec=10
# Give the application 30 seconds to shut down gracefully
TimeoutStopSec=30
# Send output to systemd journal
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    print_status "Reloading systemd daemon and enabling the service..."
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME.service"
    print_success "$SERVICE_NAME service created and enabled."
    print_status "You can manage the service with: sudo systemctl [start|stop|status] $SERVICE_NAME"
fi

# --- Final Instructions ---
echo
echo -e "${CYAN}===========================================${NC}"
print_success "Setup Complete!"
echo -e "${CYAN}===========================================${NC}"
echo

print_warning "IMPORTANT: Please edit the 'config.ini' file, especially the Slack bot token."

if [[ "$REBOOT_REQUIRED" = true ]]; then
    print_error "A system reboot is REQUIRED to apply hardware interface changes and new group permissions."
    read -p "Reboot now? (y/n): " -n 1 -r REBOOT_REPLY
    echo
    if [[ $REBOOT_REPLY =~ ^[Yy]$ ]]; then
        print_status "Rebooting now..."
        sudo reboot
    else
        print_warning "Please remember to reboot your system manually via 'sudo reboot'."
    fi
else
    print_status "You can now start the service with: sudo systemctl start $SERVICE_NAME"
    print_warning "A reboot or re-login is still recommended to apply new group permissions for '$USER'."
fi

echo

