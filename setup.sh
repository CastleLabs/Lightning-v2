#!/bin/bash
# Lightning Detector Hardened Setup Script v2.0
# Automated installation and configuration for Raspberry Pi
# Implements a reliable, event-driven architecture.

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }
print_hardened() { echo -e "${PURPLE}[HARDENED]${NC} $1"; }

# Header
echo -e "${CYAN}"
echo "🌩️  Lightning Detector Hardened Setup v2.0"
echo "=============================================="
echo "Implementing Event-Driven Architecture for Maximum Reliability"
echo -e "${NC}"

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    print_error "Please do not run this script as root. Run as a regular user."
    exit 1
fi

# System updates
print_status "Updating system packages..."
sudo apt-get update && sudo apt-get upgrade -y

# Install dependencies
print_status "Installing system dependencies..."
sudo apt-get install -y python3 python3-pip python3-venv git

# Enable SPI
print_hardened "Enabling SPI interface for reliable sensor communication..."
sudo raspi-config nonint do_spi 0
print_success "SPI interface enabled."

# GPIO permissions
print_hardened "Setting up GPIO permissions..."
sudo usermod -a -G gpio,spi "$USER"
print_warning "A REBOOT or RE-LOGIN is required for GPIO/SPI permissions to apply."

# Create project directory and venv
print_status "Creating Python virtual environment..."
python3 -m venv lightning_detector_env
source lightning_detector_env/bin/activate
pip install --upgrade pip
pip install Flask RPi.GPIO spidev requests

# Create config.ini v2.0
if [ ! -f "config.ini" ]; then
    print_hardened "Creating new config.ini for v2.0..."
    cat > config.ini << EOF
[SYSTEM]
debug = false

[SENSOR]
spi_bus = 0
spi_device = 0
irq_pin = 2
indoor = false
sensitivity = medium
auto_start = true
polling_interval = 1.0

[NOISE_HANDLING]
enabled = true
event_threshold = 15
time_window_seconds = 120
raised_noise_floor_level = 5
revert_delay_minutes = 10

[SLACK]
bot_token =
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
    print_success "config.ini v2.0 created."
else
    print_warning "Existing config.ini found. Please review and add 'indoor = false' to the [SENSOR] section."
fi

# Systemd service setup
SERVICE_REPLY=""
echo
read -p "🚀 Set up hardened systemd service (auto-start on boot)? (y/n): " -n 1 -r SERVICE_REPLY
echo
if [[ $SERVICE_REPLY =~ ^[Yy]$ ]]; then
    print_hardened "Creating systemd service for v2.0..."
    sudo tee /etc/systemd/system/lightning-detector.service > /dev/null <<EOF
[Unit]
Description=Lightning Detector Service v2.0 (Event-Driven)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/lightning_detector_env/bin/python lightning.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable lightning-detector.service
    print_success "Hardened service v2.0 created and enabled!"
fi

# Create start.sh
print_status "Creating startup script (start.sh)..."
cat > start.sh << 'EOF'
#!/bin/bash
# Lightning Detector Hardened Startup Script v2.0
echo "Starting Lightning Detector v2.0..."
echo "Architecture: Event-Driven"
cd "$(dirname "$0")"
source lightning_detector_env/bin/activate
python3 lightning.py
EOF
chmod +x start.sh

IP_ADDRESS=$(hostname -I | awk '{print $1}')

# Completion summary
echo
print_success "Lightning Detector v2.0 setup complete!"
echo
echo "📋 Next Steps:"
echo "┌─────────────────────────────────────────────────────────────────┐"
echo "│ ⚠️  IMPORTANT: Please REBOOT now to apply all changes.          │"
echo "│    sudo reboot                                                  │"
echo "│                                                                 │"
echo "│ ⚙️  Configuration v2.0:                                         │"
echo "│    Edit config.ini to add your Slack bot token and set          │"
echo "│    'indoor = true' if the sensor is used inside.                │"
echo "│                                                                 │"
echo "│ 🚀 Running Options (after reboot):                              │"
echo "│    - Manually: ./start.sh                                       │"
if [[ $SERVICE_REPLY =~ ^[Yy]$ ]]; then
echo "│    - Service:  sudo systemctl start lightning-detector          │"
fi
echo "│                                                                 │"
echo "│ 🌐 Web Interface: http://$IP_ADDRESS:5000                   │"
echo "└─────────────────────────────────────────────────────────────────┘"
