#!/bin/bash
# Lightning Detector Enhanced Setup Script v2.4-GPIO-Fixed
# Automated installation and configuration for Raspberry Pi
# Fixed GPIO permissions and environment setup

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
echo "🌩️  Lightning Detector Enhanced Setup v2.4-GPIO-Fixed"
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

# Verify critical files exist before starting
if [ ! -f "lightning.py" ] || [ ! -f "requirements.txt" ]; then
    print_error "'lightning.py' or 'requirements.txt' not found!"
    print_error "Please run this script from the root of the lightning detector project directory."
    exit 1
fi

# Check for existing virtual environment with permission issues
if [ -d "$VENV_NAME" ]; then
    print_warning "Existing virtual environment found. Removing to ensure clean setup..."
    # Use sudo to ensure complete removal
    sudo rm -rf "$VENV_NAME"
    print_success "Virtual environment removed."
fi

# 2. System Preparation
print_status "Updating system packages (this may take a few minutes)..."
sudo apt-get update && sudo apt-get upgrade -y

print_status "Installing required system dependencies..."
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    build-essential \
    libgpiod2 \
    python3-libgpiod \
    logrotate

# 3. GPIO and Hardware Setup
print_hardened "Setting up GPIO permissions and hardware interfaces..."

# Create gpio group if it doesn't exist
if ! getent group gpio > /dev/null 2>&1; then
    print_status "Creating gpio group..."
    sudo groupadd -f -r gpio
fi

# Add user to required groups
print_status "Adding user '$USER' to hardware access groups..."
sudo usermod -a -G gpio,spi,i2c,dialout "$USER"

# Set up GPIO udev rules for proper permissions
print_status "Creating udev rules for GPIO access..."
sudo tee /etc/udev/rules.d/99-gpio.rules > /dev/null << 'EOF'
# GPIO access rules for Lightning Detector
SUBSYSTEM=="bcm2835-gpiomem", KERNEL=="gpiomem", GROUP="gpio", MODE="0660"
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", ACTION=="add", PROGRAM="/bin/sh -c 'chown root:gpio /sys/class/gpio/export /sys/class/gpio/unexport ; chmod 220 /sys/class/gpio/export /sys/class/gpio/unexport'"
SUBSYSTEM=="gpio", KERNEL=="gpio*", ACTION=="add", PROGRAM="/bin/sh -c 'chown root:gpio /sys%p/active_low /sys%p/direction /sys%p/edge /sys%p/value ; chmod 660 /sys%p/active_low /sys%p/direction /sys%p/edge /sys%p/value'"
# Additional rule for gpiochip devices
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
KERNEL=="gpiomem", GROUP="gpio", MODE="0660"
EOF

# Set up SPI permissions
print_status "Setting up SPI permissions..."
sudo tee /etc/udev/rules.d/99-spi.rules > /dev/null << 'EOF'
# SPI access rules for Lightning Detector
SUBSYSTEM=="spidev", KERNEL=="spidev[0-9]*.[0-9]*", GROUP="spi", MODE="0660"
EOF

# Enable SPI interface
print_hardened "Enabling SPI interface for reliable sensor communication..."
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

# Enable I2C as well (sometimes needed for certain sensor configurations)
if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
    echo "dtparam=i2c_arm=on" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    REBOOT_REQUIRED=true
fi

# Load SPI kernel modules if not already loaded
if ! lsmod | grep -q "spi_bcm2835"; then
    print_status "Loading SPI kernel modules..."
    sudo modprobe spi_bcm2835
    sudo modprobe spidev
fi

# Check if SPI devices are available
if ! ls /dev/spidev* >/dev/null 2>&1; then
    REBOOT_REQUIRED=true
    print_warning "SPI devices not yet available. Reboot required to activate."
fi

# Reload udev rules
print_status "Reloading udev rules..."
sudo udevadm control --reload-rules && sudo udevadm trigger

# 4. GPIO Memory Access Setup
print_status "Setting up GPIO memory access..."
if [ ! -c /dev/gpiomem ]; then
    print_warning "/dev/gpiomem not found. Creating it..."
    # Try to create gpiomem device
    sudo modprobe bcm2835_gpiomem 2>/dev/null || true
fi

if [ -c /dev/gpiomem ]; then
    # Ensure gpiomem has correct permissions
    sudo chown root:gpio /dev/gpiomem
    sudo chmod g+rw /dev/gpiomem
    print_success "/dev/gpiomem permissions set correctly"
else
    print_warning "/dev/gpiomem still not found. GPIO access may require sudo."
fi

# Set up alternative GPIO access methods
print_status "Setting up alternative GPIO access..."
# Ensure /dev/gpiochip* devices have correct permissions
if ls /dev/gpiochip* >/dev/null 2>&1; then
    sudo chown root:gpio /dev/gpiochip*
    sudo chmod g+rw /dev/gpiochip*
    print_success "gpiochip devices permissions set correctly"
fi

# 5. Application Setup
print_status "Setting up Python virtual environment..."

# Create virtual environment
print_status "Creating new virtual environment..."
python3 -m venv "$VENV_NAME" --system-site-packages

# Activate virtual environment
source "$VENV_NAME/bin/activate"

# Upgrade pip
print_status "Upgrading pip in virtual environment..."
python -m pip install --upgrade pip setuptools wheel

print_status "Installing Python dependencies from requirements.txt..."
# Install RPi.GPIO with specific options to ensure it works in venv
CFLAGS=-fcommon pip install RPi.GPIO --no-binary :all: --force-reinstall

# Install other requirements
pip install -r requirements.txt

# Verify RPi.GPIO installation
print_status "Verifying RPi.GPIO installation..."
if python -c "import RPi.GPIO; print('RPi.GPIO version:', RPi.GPIO.VERSION)" 2>/dev/null; then
    print_success "RPi.GPIO installed successfully"
else
    print_error "RPi.GPIO installation verification failed"
fi

deactivate

# 6. Configuration File
print_status "Checking for configuration file..."
if [ ! -f "config.ini" ]; then
    if [ -f "config.ini.example" ]; then
        cp config.ini.example config.ini
        print_success "Created config.ini from example file."
    else
        print_hardened "Creating default production config.ini..."
        cat > config.ini <<'EOF'
[SYSTEM]
debug = false

[SENSOR]
spi_bus = 0
spi_device = 0
irq_pin = 22
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
    fi
else
    print_warning "Existing config.ini found. Please ensure it is configured correctly."
fi

# 7. Log Rotation
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

# 8. Systemd Service Setup
SERVICE_REPLY=""
echo
read -p "🚀 Set up systemd service to auto-start on boot? (y/n): " -n 1 -r SERVICE_REPLY
echo
if [[ $SERVICE_REPLY =~ ^[Yy]$ ]]; then
    print_hardened "Creating production-ready systemd service..."
    SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

    # Get the primary group for the user
    USER_GROUP=$(id -gn $USER)

    sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Lightning Detector Service v2.4-GPIO-Fixed
Documentation=https://github.com/your-repo/your-project
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$USER_GROUP
# Add supplementary groups for hardware access
SupplementaryGroups=gpio spi i2c dialout
WorkingDirectory=$PROJECT_DIR
Environment="PYTHONUNBUFFERED=1"
Environment="PATH=$PROJECT_DIR/$VENV_NAME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Main execution command
ExecStart=$PROJECT_DIR/$VENV_NAME/bin/python3 $PROJECT_DIR/lightning.py

# Pre-start command to ensure permissions and wait for devices
ExecStartPre=/bin/bash -c 'sleep 5; ls -la /dev/gpio* /dev/spidev* || true'

# --- Hardening and Reliability ---
Restart=always
RestartSec=10
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

# Device access
DeviceAllow=/dev/gpiomem rw
DeviceAllow=/dev/gpiochip0 rw
DeviceAllow=/dev/spidev0.0 rw
DeviceAllow=/dev/spidev0.1 rw
PrivateDevices=no

# Additional security hardening (adjusted for GPIO access)
ProtectSystem=strict
ReadWritePaths=$PROJECT_DIR
ReadOnlyPaths=/sys /proc

[Install]
WantedBy=multi-user.target
EOF

    print_status "Reloading systemd daemon and enabling the service..."
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME.service"
    print_success "$SERVICE_NAME service created and enabled."
    print_status "You can manage the service with: sudo systemctl [start|stop|status] $SERVICE_NAME"
fi

# 9. Create enhanced test script to verify GPIO access
print_status "Creating enhanced GPIO test script..."
cat > test_gpio_enhanced.py << 'EOF'
#!/usr/bin/env python3
"""Enhanced GPIO Test for Lightning Detector"""
import sys
import os
import time

print("Enhanced GPIO Access Test")
print("=" * 50)
print(f"Running as user: {os.getuid()} (UID), {os.getgid()} (GID)")
print(f"Username: {os.getenv('USER', 'unknown')}")
print(f"Groups: {os.getgroups()}")
print(f"Python: {sys.executable}")
print()

# Check device permissions
print("Device Permissions:")
devices = ['/dev/gpiomem', '/dev/gpiochip0', '/dev/spidev0.0', '/dev/spidev0.1']
for device in devices:
    if os.path.exists(device):
        stat = os.stat(device)
        perms = oct(stat.st_mode)[-3:]
        print(f"  {device}: {perms} (exists)")
    else:
        print(f"  {device}: NOT FOUND")
print()

# Test GPIO access methods
print("Testing GPIO Libraries:")

# Method 1: RPi.GPIO
try:
    import RPi.GPIO as GPIO
    print("✓ RPi.GPIO module imported successfully")
    print(f"  Version: {GPIO.VERSION}")

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    # Test setting up a pin
    test_pin = 22
    GPIO.setup(test_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"✓ Successfully configured GPIO pin {test_pin}")

    # Read pin state
    state = GPIO.input(test_pin)
    print(f"✓ Pin {test_pin} state: {'HIGH' if state else 'LOW'}")

    # Test edge detection
    GPIO.add_event_detect(test_pin, GPIO.FALLING, bouncetime=20)
    print(f"✓ Successfully added edge detection to pin {test_pin}")

    GPIO.remove_event_detect(test_pin)
    GPIO.cleanup()
    print("✓ GPIO cleanup successful")

except Exception as e:
    print(f"✗ RPi.GPIO test failed: {e}")
    print("  This is the primary method used by lightning.py")

print()

# Method 2: gpiozero (alternative)
try:
    from gpiozero import Button
    print("✓ gpiozero module available (alternative method)")
    button = Button(22, pull_up=True)
    print(f"✓ gpiozero can access GPIO pin 22")
    button.close()
except Exception as e:
    print(f"✗ gpiozero not available: {e}")

print()

# Test SPI access
print("Testing SPI Access:")
try:
    import spidev
    spi = spidev.SpiDev()
    spi.open(0, 0)
    print("✓ SPI device 0.0 opened successfully")
    spi.max_speed_hz = 2000000
    print("✓ SPI speed configured")
    spi.close()
    print("✓ SPI closed successfully")
except Exception as e:
    print(f"✗ SPI test failed: {e}")

print()
print("Summary:")
if 'GPIO test failed' not in locals():
    print("✅ All GPIO tests passed! Your system is ready.")
else:
    print("❌ GPIO access issues detected.")
    print("\nTroubleshooting:")
    print("1. Have you rebooted after running setup.sh?")
    print("2. Try logging out and back in for group changes")
    print("3. Run 'groups' to verify you're in the gpio group")
    print("4. If still failing, the service may need to run with sudo")
EOF

chmod +x test_gpio_enhanced.py
print_success "Enhanced GPIO test script created: test_gpio_enhanced.py"

# Create a wrapper script for development testing
print_status "Creating development wrapper script..."
cat > run_dev.sh << EOF
#!/bin/bash
# Development runner with proper environment
source $VENV_NAME/bin/activate
export PYTHONPATH=$PROJECT_DIR
exec python3 lightning.py
EOF
chmod +x run_dev.sh
print_success "Development wrapper created: run_dev.sh"

# --- Final Instructions ---
echo
echo -e "${CYAN}===========================================${NC}"
print_success "Setup Complete!"
echo -e "${CYAN}===========================================${NC}"
echo

print_warning "IMPORTANT: Please edit the 'config.ini' file, especially the Slack bot token."

if [[ "$REBOOT_REQUIRED" = true ]]; then
    print_error "A system reboot is REQUIRED to apply hardware interface changes and new group permissions."
    echo
    echo "After reboot, test GPIO access with:"
    echo "  ./$VENV_NAME/bin/python test_gpio_enhanced.py"
    echo
    echo "Or run the application in development mode with:"
    echo "  ./run_dev.sh"
    echo
    read -p "Reboot now? (y/n): " -n 1 -r REBOOT_REPLY
    echo
    if [[ $REBOOT_REPLY =~ ^[Yy]$ ]]; then
        print_status "Rebooting now..."
        sudo reboot
    else
        print_warning "Please remember to reboot your system manually via 'sudo reboot'."
    fi
else
    # Need to re-login for group changes
    print_warning "You need to log out and back in for group membership changes to take effect."
    echo
    print_status "Testing GPIO access..."
    echo
    # Test with the virtual environment
    if ./$VENV_NAME/bin/python test_gpio_enhanced.py; then
        echo
        print_success "GPIO access is working! You can start the application with:"
        echo "  ./run_dev.sh"
        echo
        echo "Or start the service with:"
        echo "  sudo systemctl start $SERVICE_NAME"
    else
        echo
        print_warning "GPIO test failed. Please log out and back in, then test with:"
        echo "  ./$VENV_NAME/bin/python test_gpio_enhanced.py"
    fi
fi

echo
print_status "Quick reference commands:"
echo "  Development mode:  ./run_dev.sh"
echo "  Start service:     sudo systemctl start $SERVICE_NAME"
echo "  Stop service:      sudo systemctl stop $SERVICE_NAME"
echo "  View status:       sudo systemctl status $SERVICE_NAME"
echo "  View logs:         sudo journalctl -u $SERVICE_NAME -f"
echo "  Test GPIO:         ./$VENV_NAME/bin/python test_gpio_enhanced.py"
echo
