# Lightning Detector v2.0
## Event-Driven & Hardened Architecture

A robust, real-time lightning detection system built for Raspberry Pi using the CJMCU-3935 (AS3935) sensor module. This system provides a web-based monitoring dashboard, Slack alerts, and implements a hardened, event-driven architecture for maximum reliability and efficiency.

## 🌩️ Features

### Core Functionality

**Real-time Lightning Detection**: Uses an interrupt-driven approach to immediately detect lightning strikes and atmospheric disturbers.

**Multi-Zone Alerts**: Issues distinct WARNING and CRITICAL alerts based on configurable distance thresholds. The default warning distance is 30km and the critical distance is 10km.

**Web Dashboard**: A modern, responsive interface built with Flask and Bootstrap to visualize system status, alert states, and recent lightning events.

**Slack Integration**: Delivers rich, formatted notifications for alerts and system status changes directly to a configured Slack channel.

**All-Clear Notifications**: Automatically sends an "All Clear" message after a configurable period of inactivity in an alert zone.

### Advanced Features

**Dynamic Noise Handling**: The system can differentiate between transient "disturbers" and persistent noise, automatically raising the sensor's noise floor to prevent false alarms and reverting after a quiet period.

**Indoor/Outdoor Modes**: A specific indoor mode adjusts the sensor's Analog Front-End (AFE) gain to reject common man-made, indoor noise sources.

**Configurable Sensitivity & Energy Filtering**: Allows tuning the sensor's detection sensitivity and setting a minimum energy threshold for a strike to trigger an alert.

**Comprehensive Logging**: Utilizes Python's RotatingFileHandler to create log files with configurable levels, sizes, and backup counts to prevent disk space issues.

**RESTful API**: Provides a JSON endpoint for at-a-glance system status, suitable for integration with other monitoring tools.

## 📋 Requirements

### Hardware
- Raspberry Pi (any model with GPIO pins)
- CJMCU-3935 Lightning Detector Module (AS3935 chip)
- Jumper wires for connections

### Software
- Raspberry Pi OS (or other compatible Linux distribution)
- Python 3.7+
- Enabled SPI interface on the Raspberry Pi
- Python libraries: Flask, RPi.GPIO, spidev, requests
- Internet connection (for Slack notifications)

## 🔧 Hardware Setup

### Wiring Diagram

Connect the CJMCU-3935 module to your Raspberry Pi's GPIO header. The default configuration uses the following BCM pins:

| CJMCU-3935 Pin | RPi Pin (BCM) | RPi Pin (Physical) | Description |
|----------------|---------------|-------------------|-------------|
| VCC | 3.3V Power | Pin 1 or 17 | Power Supply |
| GND | Ground | Pin 6, 9, etc. | Ground |
| MISO | GPIO 9 | Pin 21 | Master In Slave Out |
| MOSI | GPIO 10 | Pin 19 | Master Out Slave In |
| SCLK | GPIO 11 | Pin 23 | Serial Clock |
| CS | GPIO 8 | Pin 24 | SPI Chip Select (CE0) |
| IRQ | GPIO 2 | Pin 3 | Interrupt Request |

## 🚀 Installation

### Automated Setup

The provided setup script automates the entire installation process.

1. Download the project files to your Raspberry Pi.

2. Make the setup script executable and run it:

```bash
chmod +x setup.sh
./setup.sh
```

The script will perform the following actions:
- Update system packages.
- Install Python dependencies via pip.
- Enable the SPI interface using raspi-config.
- Add your user to the gpio and spi groups.
- Create a Python virtual environment.
- Generate the initial config.ini file.
- Optionally create and enable a systemd service for auto-starting on boot.

**Important**: Reboot your Raspberry Pi for all permission changes to take effect:

```bash
sudo reboot
```

## ⚙️ Configuration

After installation, edit the config.ini file to customize your setup. Key settings are listed below.

### Essential Settings

```ini
[SENSOR]
# Set to true if sensor is indoors to reject man-made noise.
indoor = false

# Detection sensitivity: low, medium, or high.
sensitivity = medium

[SLACK]
# Get token from https://api.slack.com/apps
bot_token = xoxb-YOUR-BOT-TOKEN-HERE

# Channel name or user ID to send messages to.
channel = #alerts

# Master switch for all Slack notifications.
enabled = true

[ALERTS]
# Distance thresholds in kilometers.
critical_distance = 10
warning_distance = 30
```

### Slack Bot Setup

1. Create a new Slack App at https://api.slack.com/apps.
2. Navigate to "OAuth & Permissions" and add the chat:write scope.
3. Install the app to your workspace and copy the "Bot User OAuth Token" (starts with xoxb-).
4. Paste the token into the bot_token field in config.ini.
5. Invite the bot to the channel you specified in config.ini.

## 🎯 Usage

### Starting the Application

#### Method 1: Direct Launch
Use the provided startup script. This will activate the virtual environment and run the Python script.

```bash
./start.sh
```

#### Method 2: Systemd Service
If you enabled the service during setup, you can manage it with systemctl.

```bash
# Start the service
sudo systemctl start lightning-detector

# Enable the service to auto-start on boot
sudo systemctl enable lightning-detector

# Check the service status and logs
sudo systemctl status lightning-detector
journalctl -u lightning-detector -f
```

### Accessing the Web Interface

Open your browser and navigate to your Raspberry Pi's IP address on port 5000:
```
http://<YOUR_RASPBERRY_PI_IP>:5000
```

#### Web Interface Features

**Dashboard (/)**: Displays the active state of the Warning and Critical alert zones, shows sensor status including operating mode and noise levels, lists the 15 most recent lightning events, and provides quick action buttons to manage the detector.

**Configuration (/config)**: A web form that allows you to view and edit all settings from config.ini directly, test the Slack connection, and save your changes (a restart is required for changes to apply).

### API and Routes

| Endpoint | Method | Description |
|----------|--------|-------------|
| /api/status | GET | Returns a JSON object with the current system status, including sensor activity, alert states, and monitoring thread status. |
| /start_monitoring | GET | Starts the monitoring thread. |
| /stop_monitoring | GET | Stops the monitoring thread and cancels all active timers. |
| /reset_alerts | GET | Resets all active alert states and noise mitigation modes to their defaults. |
| /test_alerts/<type> | GET | In debug mode, triggers a test warning or critical alert. |
| /test_slack | GET | Sends a test message to the configured Slack channel. |

## 🛡️ Advanced Features

### Dynamic Noise Handling

The system intelligently adapts to environmental interference using logic from lightning.py.

**Transient Disturbers (INT_D)**: The system counts these events over a time window (default: 15 events in 120 seconds). If the threshold is exceeded, it temporarily elevates the sensor's noise floor to "High" mode to prevent false alarms.

**Persistent Noise (INT_NH)**: An INT_NH interrupt from the sensor indicates the noise floor is saturated. The system immediately sets the noise floor to its maximum "Critical" level.

In both cases, the system will automatically revert to the normal noise floor after a configurable quiet period (default: 10 minutes).

### Alert Logic

Alerts are triggered based on a combination of distance and energy.

- An event's energy must be above energy_threshold to be considered for an alert.
- An alert notification is only sent when a zone first becomes active to prevent spam.
- Each new strike within an active zone resets the all_clear_timer (default: 15 minutes).
- If the timer completes without being reset, an "All Clear" message is sent for that zone.

## 🔍 Troubleshooting

### Common Issues

**"Sensor initialization failed"**: Double-check your wiring against the diagram. Ensure the SPI interface is enabled by running `ls /dev/spi*` (you should see spidev0.0 or similar). Verify you are using a stable 3.3V power supply.

**"Permission denied" on startup**: This usually means the user is not in the gpio or spi groups. Ensure you have rebooted after running the setup.sh script. You can verify your groups by running the `groups` command.

**No Slack Notifications**: Use the "Test Slack Connection" button on the configuration page to isolate the issue. Verify your bot_token is correct and that the bot has been invited to the specified channel in Slack.

**False Alarms or Missed Strikes**: Adjust the sensitivity setting in the config file. If the sensor is indoors, ensure `indoor = true` is set to help reject electrical interference.

### Viewing Logs

The primary log file is `lightning_detector.log`. For a live view of the logs:

```bash
tail -f lightning_detector.log
```

If running as a systemd service, use journalctl:

```bash
journalctl -u lightning-detector -f
```
