#!/usr/bin/env python3
"""
CJMCU-3935 Lightning Detector Flask Application v2.0 Production-Ready
Event-Driven & Hardened Architecture - Enhanced Reliability Update

This application provides a web-based interface for monitoring lightning activity
using the AS3935 sensor chip. It features:
- Event-driven interrupt-based detection (no polling)
- Automatic noise floor adjustment
- Slack notifications for alerts
- Web dashboard with real-time status
- Configurable alert zones (warning/critical)
- Automatic recovery from sensor failures
- Thread-safe operation for reliability
- Production-grade reliability enhancements

Author: Lightning Detector Project
Version: 2.0-Production-Enhanced
"""

import configparser
import os
import threading
import time
import json
import logging
import atexit
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from collections import deque
from enum import Enum
from queue import Queue, Empty

import requests
import RPi.GPIO as GPIO
import spidev
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, Response
from werkzeug.serving import WSGIRequestHandler

# --- Constants and Enumerations ---
class AlertLevel(Enum):
    """Enumeration for different alert severity levels"""
    WARNING = "warning"
    CRITICAL = "critical"
    ALL_CLEAR = "all_clear"

# --- Rate Limiting Filter for Logging ---
class RateLimitFilter(logging.Filter):
    """Rate limit repetitive log messages to prevent log spam"""
    def __init__(self, rate=10):
        super().__init__()
        self.rate = rate
        self.messages = {}

    def filter(self, record):
        current_time = time.time()
        msg = record.getMessage()

        if msg in self.messages:
            last_time, count = self.messages[msg]
            if current_time - last_time < 60:  # Within 1 minute
                if count >= self.rate:
                    return False  # Suppress
                self.messages[msg] = (last_time, count + 1)
            else:
                self.messages[msg] = (current_time, 1)
        else:
            self.messages[msg] = (current_time, 1)

        return True

# --- Global Configuration and State Management ---
# ConfigParser instance for reading configuration from config.ini
CONFIG = configparser.ConfigParser()

# Main monitoring state - thread-safe dictionary holding all shared state
# This dictionary is protected by the 'lock' member for thread safety
MONITORING_STATE = {
    "lock": threading.Lock(),                    # Protects all state modifications
    "stop_event": threading.Event(),             # Signals threads to stop
    "events": deque(maxlen=100),                 # Circular buffer of lightning events
    "status": {                                  # Current system status
        'last_reading': None,                    # ISO timestamp of last sensor reading
        'sensor_active': False,                  # Is monitoring thread running?
        'status_message': 'Not started',         # Human-readable status
        'indoor_mode': False,                    # Indoor/outdoor mode from config
        'noise_mode': 'Normal',                  # Current noise mitigation: Normal/High/Critical
        'sensor_healthy': True,                  # Is sensor responding correctly?
        'last_error': None                       # Last error message if any
    },
    "thread": None,                              # Reference to monitoring thread
    "noise_events": deque(maxlen=50),            # Buffer for counting disturber events
    "noise_revert_timer": None,                  # Timer to revert noise floor changes
    "watchdog_thread": None,                     # Thread monitoring the monitoring thread
    "last_interrupt_time": 0,                    # For interrupt storm detection
    "interrupt_count": 0,                        # Count interrupts for storm detection
    "interrupt_storm_detected": False            # Flag for interrupt storm condition
}

# Alert state management - separate from monitoring state for clarity
ALERT_STATE = {
    "warning_timer": None,                       # Timer for warning zone all-clear
    "critical_timer": None,                      # Timer for critical zone all-clear
    "warning_active": False,                     # Is warning alert currently active?
    "critical_active": False,                    # Is critical alert currently active?
    "last_warning_strike": None,                 # Timestamp of last warning zone strike
    "last_critical_strike": None,                # Timestamp of last critical zone strike
    "timer_lock": threading.Lock(),              # Protects timer operations
    "active_timers": []                          # Track all active timers
}

# Slack notification queue for non-blocking alerts
SLACK_QUEUE = Queue(maxsize=100)
SLACK_WORKER_THREAD = None

# Sensor instance and initialization lock
# The lock ensures only one thread can initialize/access the sensor at a time
SENSOR_INIT_LOCK = threading.Lock()
sensor = None  # Global sensor object

# --- Flask Application Setup ---
app = Flask(__name__)
app.secret_key = 'lightning-detector-hardened-v20-production-enhanced-secret-key'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request size

# Set request timeout
WSGIRequestHandler.timeout = 30

# --- AS3935 Sensor Driver Class ---
class AS3935LightningDetector:
    """
    Hardened driver for CJMCU-3935 (AS3935) Lightning Detector

    This class provides low-level communication with the AS3935 chip via SPI.
    It includes retry logic, error handling, and proper initialization sequences
    as specified in the AS3935 datasheet.
    """

    # AS3935 Register addresses (from datasheet)
    REG_AFE_GAIN = 0x00      # Analog Front-End gain and power settings
    REG_PWD = 0x00           # Power down control (shared with AFE_GAIN)
    REG_MIXED_MODE = 0x01    # Noise floor level and watchdog threshold
    REG_SREJ = 0x02          # Spike rejection settings
    REG_LCO_FDIV = 0x03      # Local oscillator frequency settings
    REG_MASK_DIST = 0x03     # Mask disturber events (shared register)
    REG_DISP_LCO = 0x08      # Display oscillator on IRQ pin for tuning
    REG_PRESET = 0x3C        # Preset register for testing

    # Interrupt reason bit masks
    INT_NH = 0x01            # Noise level too high
    INT_D = 0x04             # Disturber detected
    INT_L = 0x08             # Lightning detected

    def __init__(self, spi_bus=0, spi_device=0, irq_pin=2):
        """
        Initialize the AS3935 sensor

        Args:
            spi_bus: SPI bus number (0 or 1 on Raspberry Pi)
            spi_device: SPI chip select (0 or 1)
            irq_pin: GPIO pin number for interrupt signal (BCM numbering)
        """
        self.spi = None
        self.irq_pin = irq_pin
        self.is_initialized = False
        self.original_noise_floor = 0x02  # Default noise floor level

        try:
            # Initialize SPI communication
            self.spi = spidev.SpiDev()
            self.spi.open(spi_bus, spi_device)
            self.spi.max_speed_hz = 2000000  # 2MHz max per datasheet
            self.spi.mode = 0b01              # CPOL=0, CPHA=1

            # Configure GPIO for interrupt pin
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.irq_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            # Power up and configure the sensor
            self.power_up()
            self.is_initialized = True

        except Exception as e:
            # Clean up on initialization failure
            self.cleanup()
            raise e

    def _write_register(self, reg, value, retries=3):
        """
        Write a value to a sensor register with retry logic

        Args:
            reg: Register address (0x00-0x3F)
            value: 8-bit value to write
            retries: Number of retry attempts on failure
        """
        if not self.spi:
            return

        for attempt in range(retries):
            try:
                # AS3935 expects [register_address, data_byte]
                self.spi.xfer2([reg, value])
                return
            except IOError as e:
                if attempt == retries - 1:
                    app.logger.error(f"SPI write failed after {retries} attempts: {e}")
                    raise
                time.sleep(0.001)  # Brief delay before retry

    def _read_register(self, reg, retries=3):
        """
        Read a value from a sensor register with retry logic

        Args:
            reg: Register address (0x00-0x3F)
            retries: Number of retry attempts on failure

        Returns:
            8-bit register value
        """
        if not self.spi:
            return 0

        for attempt in range(retries):
            try:
                # AS3935 read: set bit 6 of address byte, then read response
                result = self.spi.xfer2([reg | 0x40, 0x00])
                return result[1]
            except IOError as e:
                if attempt == retries - 1:
                    app.logger.error(f"SPI read failed after {retries} attempts: {e}")
                    raise
                time.sleep(0.001)
        return 0

    def power_up(self):
        """
        Initialize and calibrate the sensor according to datasheet specifications

        This performs the complete initialization sequence including:
        - Power up from power-down mode
        - Oscillator calibration
        - Setting indoor/outdoor mode
        - Configuring sensitivity settings
        """
        try:
            # Step 1: Power up sequence (PWD bit = 0)
            self._write_register(self.REG_PWD, 0x96)
            time.sleep(0.003)  # Wait for oscillator to stabilize

            # Step 2: Verify sensor is responding
            test_read = self._read_register(self.REG_PWD)
            if test_read != 0x96:
                raise Exception(f"Sensor not responding correctly. Expected 0x96, got {test_read:#04x}")

            # Step 3: Calibrate internal oscillators
            self._write_register(self.REG_LCO_FDIV, 0b10010111)
            self._write_register(self.REG_DISP_LCO, 0x80)
            time.sleep(0.002)
            self._write_register(self.REG_DISP_LCO, 0x00)
            time.sleep(0.002)

            # Step 4: Configure for indoor/outdoor mode
            is_indoor = get_config_boolean('SENSOR', 'indoor', False)
            sensitivity = CONFIG.get('SENSOR', 'sensitivity', fallback='medium')

            # AFE Gain settings from datasheet
            # Indoor: AFE_GB=10010 (18x gain)
            # Outdoor: AFE_GB=01110 (14x gain)
            afe_gain = 0b00010010 if is_indoor else 0b00001110

            # Add PWD bit (must be 0 for normal operation)
            afe_gain = (afe_gain << 1) | 0b0

            # Sensitivity mappings
            sensitivity_map = {
                'low': {'srej': 0x03, 'nf_lev': 0x04, 'wdth': 0x03},
                'medium': {'srej': 0x02, 'nf_lev': 0x02, 'wdth': 0x02},
                'high': {'srej': 0x01, 'nf_lev': 0x01, 'wdth': 0x01}
            }
            settings = sensitivity_map.get(sensitivity, sensitivity_map['medium'])

            self.original_noise_floor = settings['nf_lev']

            # Register 0x01: [NF_LEV(3 bits)][WDTH(4 bits)]
            reg01_value = (settings['nf_lev'] << 4) | settings['wdth']

            # Write configuration
            self._write_register(self.REG_AFE_GAIN, afe_gain)
            self._write_register(self.REG_MIXED_MODE, reg01_value)

            # Set spike rejection
            current_srej = self._read_register(self.REG_SREJ)
            new_srej = (settings['srej'] << 4) | (current_srej & 0x0F)
            self._write_register(self.REG_SREJ, new_srej)

            # Update global status
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['indoor_mode'] = is_indoor
                MONITORING_STATE['status']['sensor_healthy'] = True

            app.logger.info(f"Sensor powered up. Mode: {'Indoor' if is_indoor else 'Outdoor'}, "
                          f"Sensitivity: {sensitivity}, Noise floor: {settings['nf_lev']}")

        except Exception as e:
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_healthy'] = False
                MONITORING_STATE['status']['last_error'] = str(e)
            raise

    def set_noise_floor(self, level):
        """
        Dynamically adjust the noise floor level

        Args:
            level: Noise floor level (0-7, where 7 is least sensitive)
        """
        if not (0x00 <= level <= 0x07):
            app.logger.error(f"Invalid noise floor level: {level}. Must be 0-7.")
            return

        try:
            # Read current register to preserve watchdog threshold
            current_reg_val = self._read_register(self.REG_MIXED_MODE)
            preserved_wdth = current_reg_val & 0x0F

            # Update noise floor while preserving watchdog
            new_reg_val = (level << 4) | preserved_wdth
            self._write_register(self.REG_MIXED_MODE, new_reg_val)

            app.logger.info(f"Noise floor dynamically set to level {level}")

        except IOError as e:
            app.logger.error(f"SPI Error setting noise floor: {e}")
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_healthy'] = False
                MONITORING_STATE['status']['last_error'] = str(e)

    def get_interrupt_reason(self):
        """Read interrupt status register to determine interrupt cause"""
        return self._read_register(0x03) & 0x0F

    def get_lightning_distance(self):
        """
        Read estimated distance to lightning strike

        Returns:
            Distance in km (1-63), or 0x3F for out of range
        """
        return self._read_register(0x07) & 0x3F

    def get_lightning_energy(self):
        """
        Read lightning energy value (arbitrary units)

        Returns:
            20-bit energy value
        """
        lsb = self._read_register(0x04)
        msb = self._read_register(0x05)
        mmsb = self._read_register(0x06) & 0x1F
        return (mmsb << 16) | (msb << 8) | lsb

    def verify_spi_connection(self):
        """
        Verify SPI connection is working properly

        Returns:
            True if connection verified, False otherwise
        """
        try:
            # Use preset register for testing
            test_value = 0x96
            self._write_register(self.REG_PRESET, test_value)
            time.sleep(0.001)

            read_value = self._read_register(self.REG_PRESET)
            if read_value != test_value:
                app.logger.error(f"SPI verification failed: wrote {test_value:#04x}, read {read_value:#04x}")
                return False

            return True
        except Exception as e:
            app.logger.error(f"SPI verification error: {e}")
            return False

    def cleanup(self):
        """
        Clean up sensor resources safely

        This only cleans up resources used by this sensor instance,
        not system-wide GPIO settings.
        """
        try:
            if self.spi:
                self.spi.close()
                self.spi = None

            if self.is_initialized and self.irq_pin is not None:
                try:
                    # Only reset this specific pin, not all GPIO
                    if GPIO.gpio_function(self.irq_pin) == GPIO.IN:
                        GPIO.setup(self.irq_pin, GPIO.IN)
                except Exception as e:
                    app.logger.debug(f"GPIO cleanup for pin {self.irq_pin}: {e}")

            app.logger.info(f"Sensor resources cleaned up")

        except Exception as e:
            app.logger.error(f"Error during hardware cleanup: {e}")

# --- Configuration Helper Functions ---
def get_config_int(section, key, fallback):
    """
    Safely retrieve an integer value from configuration

    Args:
        section: INI file section name
        key: Configuration key
        fallback: Default value if not found or invalid

    Returns:
        Integer value from config or fallback
    """
    try:
        return CONFIG.getint(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def get_config_float(section, key, fallback):
    """Safely retrieve a float value from configuration"""
    try:
        return CONFIG.getfloat(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def get_config_boolean(section, key, fallback):
    """Safely retrieve a boolean value from configuration"""
    try:
        return CONFIG.getboolean(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def validate_config():
    """
    Validate critical configuration values

    Returns:
        True if configuration is valid, False otherwise
    """
    errors = []
    warnings = []

    # Validate distance settings
    critical = get_config_int('ALERTS', 'critical_distance', 10)
    warning = get_config_int('ALERTS', 'warning_distance', 30)

    if critical >= warning:
        errors.append("Critical distance must be less than warning distance")

    if critical < 1 or critical > 63:  # AS3935 max distance
        errors.append("Critical distance must be between 1 and 63 km")

    if warning < 1 or warning > 63:
        errors.append("Warning distance must be between 1 and 63 km")

    # Validate SPI settings
    spi_bus = get_config_int('SENSOR', 'spi_bus', 0)
    if spi_bus not in [0, 1]:
        errors.append("SPI bus must be 0 or 1")

    # Validate GPIO pin
    irq_pin = get_config_int('SENSOR', 'irq_pin', 2)
    if irq_pin < 0 or irq_pin > 27:  # BCM pin range
        errors.append("IRQ pin must be between 0 and 27")

    # Check for reserved pins
    reserved_pins = [0, 1, 14, 15]  # UART pins
    if irq_pin in reserved_pins:
        warnings.append(f"IRQ pin {irq_pin} may conflict with system functions")

    # Validate noise handling settings
    if get_config_boolean('NOISE_HANDLING', 'enabled', True):
        event_threshold = get_config_int('NOISE_HANDLING', 'event_threshold', 15)
        if event_threshold < 5:
            warnings.append("Event threshold < 5 may cause frequent noise floor changes")
        elif event_threshold > 50:
            warnings.append("Event threshold > 50 may not respond to noise quickly enough")

        noise_floor = get_config_int('NOISE_HANDLING', 'raised_noise_floor_level', 5)
        if noise_floor < 0 or noise_floor > 7:
            errors.append("Raised noise floor level must be between 0 and 7")

    # Log warnings and errors
    for warning in warnings:
        app.logger.warning(f"Configuration warning: {warning}")

    for error in errors:
        app.logger.error(f"Configuration error: {error}")

    return len(errors) == 0

# --- Sensor Initialization and Management ---
def initialize_sensor_with_retry(max_retries=5, retry_delay=5):
    """
    Initialize sensor with exponential backoff retry logic

    Args:
        max_retries: Maximum number of initialization attempts
        retry_delay: Base delay between retries (exponentially increased)

    Returns:
        True if initialization successful, False otherwise
    """
    global sensor

    for attempt in range(max_retries):
        try:
            with SENSOR_INIT_LOCK:
                # Clean up any existing sensor instance
                if sensor:
                    sensor.cleanup()
                    sensor = None

                # Create new sensor instance
                sensor = AS3935LightningDetector(
                    spi_bus=get_config_int('SENSOR', 'spi_bus', 0),
                    spi_device=get_config_int('SENSOR', 'spi_device', 0),
                    irq_pin=get_config_int('SENSOR', 'irq_pin', 2)
                )

                # Verify sensor is responsive by reading a register
                test_value = sensor._read_register(0x00)
                app.logger.info(f"Sensor initialized successfully (test read: {test_value:#04x})")

                # Update global status
                with MONITORING_STATE['lock']:
                    MONITORING_STATE['status']['sensor_active'] = True
                    MONITORING_STATE['status']['sensor_healthy'] = True
                    MONITORING_STATE['status']['status_message'] = "Monitoring (Event-Driven)"
                    MONITORING_STATE['status']['last_error'] = None

                return True

        except Exception as e:
            app.logger.error(f"Sensor init attempt {attempt + 1}/{max_retries} failed: {e}")

            # Update status with failure information
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_active'] = False
                MONITORING_STATE['status']['sensor_healthy'] = False
                MONITORING_STATE['status']['last_error'] = str(e)
                MONITORING_STATE['status']['status_message'] = f"Init failed (attempt {attempt + 1})"

            # Wait before retry with exponential backoff
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                app.logger.info(f"Waiting {delay}s before retry...")

                # Interruptible sleep
                for _ in range(delay * 10):
                    if MONITORING_STATE['stop_event'].is_set():
                        return False
                    time.sleep(0.1)

    # All retries exhausted
    with MONITORING_STATE['lock']:
        MONITORING_STATE['status']['status_message'] = "Fatal: Max retries exceeded"

    return False

def perform_sensor_health_check():
    """
    Perform a comprehensive health check on the sensor

    Returns:
        True if sensor is healthy, False otherwise
    """
    try:
        with SENSOR_INIT_LOCK:
            if not sensor or not sensor.is_initialized:
                return False

            # Read and verify power register
            pwd_reg = sensor._read_register(0x00)
            if (pwd_reg & 0x01) != 0:  # Check if powered down
                app.logger.warning(f"Sensor appears to be powered down: {pwd_reg:#04x}")
                return False

            # Enhanced SPI verification
            if not sensor.verify_spi_connection():
                return False

            # Try reading multiple registers to ensure communication
            try:
                sensor._read_register(0x01)  # Noise floor register
                sensor._read_register(0x02)  # Spike rejection register
            except:
                return False

            # Update status on success
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_healthy'] = True
                MONITORING_STATE['status']['last_error'] = None

            return True

    except Exception as e:
        app.logger.error(f"Sensor health check failed: {e}")
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = str(e)
        return False

# --- Lightning Detection and Event Handling ---
def handle_sensor_interrupt(channel):
    """
    GPIO interrupt callback function with storm detection

    This is called on the falling edge of the IRQ pin when the sensor
    detects lightning, noise, or disturbers. It reads the interrupt
    reason and dispatches to appropriate handlers.

    Args:
        channel: GPIO channel that triggered the interrupt
    """
    # Quick check before acquiring locks
    if MONITORING_STATE['stop_event'].is_set():
        return

    # Interrupt storm detection
    current_time = time.time()

    with MONITORING_STATE['lock']:
        # Check for interrupt storm (>100 interrupts/second)
        if current_time - MONITORING_STATE['last_interrupt_time'] < 0.01:
            MONITORING_STATE['interrupt_count'] += 1
            if MONITORING_STATE['interrupt_count'] > 100:
                if not MONITORING_STATE['interrupt_storm_detected']:
                    app.logger.critical("Interrupt storm detected! Disabling interrupts temporarily")
                    MONITORING_STATE['interrupt_storm_detected'] = True
                    # Temporarily disable interrupt
                    GPIO.remove_event_detect(channel)
                    # Re-enable after 5 seconds
                    def re_enable_interrupt():
                        try:
                            GPIO.add_event_detect(
                                channel, GPIO.FALLING,
                                callback=handle_sensor_interrupt,
                                bouncetime=20
                            )
                            with MONITORING_STATE['lock']:
                                MONITORING_STATE['interrupt_storm_detected'] = False
                                MONITORING_STATE['interrupt_count'] = 0
                            app.logger.info("Interrupts re-enabled after storm")
                        except Exception as e:
                            app.logger.error(f"Failed to re-enable interrupts: {e}")

                    timer = threading.Timer(5.0, re_enable_interrupt)
                    timer.daemon = True
                    timer.start()
                return
        else:
            MONITORING_STATE['interrupt_count'] = 0
            MONITORING_STATE['interrupt_storm_detected'] = False

        MONITORING_STATE['last_interrupt_time'] = current_time

    # Debounce delay per datasheet
    time.sleep(0.002)

    # Use a timeout to prevent deadlocks
    acquired = SENSOR_INIT_LOCK.acquire(timeout=0.5)
    if not acquired:
        app.logger.warning("Could not acquire sensor lock in interrupt handler")
        return

    try:
        # Verify sensor is still initialized
        if not sensor or not sensor.is_initialized:
            return

        # Read interrupt reason with retry logic
        interrupt_reason = None
        for attempt in range(3):
            try:
                interrupt_reason = sensor.get_interrupt_reason()
                break
            except IOError:
                if attempt == 2:
                    raise
                time.sleep(0.001)

        if interrupt_reason is None:
            return

        # Update last reading timestamp
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['last_reading'] = datetime.now().isoformat()

        # Dispatch to appropriate handler based on interrupt type
        if interrupt_reason == sensor.INT_L:
            handle_lightning_event()
        elif interrupt_reason == sensor.INT_D:
            handle_disturber_event()
        elif interrupt_reason == sensor.INT_NH:
            handle_noise_high_event()
        else:
            app.logger.debug(f"Unknown interrupt reason: {interrupt_reason:#04x}")

    except Exception as e:
        app.logger.error(f"Error in interrupt handler: {e}")
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = f"Interrupt error: {str(e)}"

    finally:
        SENSOR_INIT_LOCK.release()

def handle_lightning_event():
    """
    Process a lightning detection event

    This function reads the distance and energy values, checks alert
    conditions, logs the event, and sends notifications if needed.
    """
    try:
        # Read lightning parameters
        distance = sensor.get_lightning_distance()
        energy = sensor.get_lightning_energy()

        # Validate readings
        if distance == 0x3F:  # Out of range indicator
            app.logger.warning("Lightning detected but out of range (>63km)")
            return

        if distance == 0:  # Invalid reading
            app.logger.warning("Lightning detected with invalid distance (0)")
            return

        # Check if this event should trigger alerts
        alert_result = check_alert_conditions(distance, energy)

        # Create event record
        event = {
            'timestamp': datetime.now().isoformat(),
            'distance': distance,
            'energy': energy,
            'energy_formatted': f"{energy:,}",
            'alert_sent': alert_result.get('send_alert', False),
            'alert_level': alert_result.get('level').value if alert_result.get('level') else None
        }

        # Store event in circular buffer
        with MONITORING_STATE['lock']:
            MONITORING_STATE['events'].append(event)
            MONITORING_STATE['status']['sensor_healthy'] = True

        app.logger.info(f"⚡ Lightning detected: {distance}km, energy: {energy}")

        # Send alerts if needed
        if alert_result.get('send_alert'):
            level = alert_result.get('level')
            if level == AlertLevel.CRITICAL:
                send_slack_notification(
                    f"🚨 CRITICAL: Lightning strike detected! Distance: {distance}km",
                    distance, energy, level
                )
            elif level == AlertLevel.WARNING:
                send_slack_notification(
                    f"⚠️ WARNING: Lightning detected. Distance: {distance}km",
                    distance, energy, level
                )

    except Exception as e:
        app.logger.error(f"Error handling lightning event: {e}")
        raise

# --- Alert System Functions ---
def check_alert_conditions(distance, energy):
    """
    Determine if a lightning event should trigger alerts

    Args:
        distance: Distance to strike in km
        energy: Energy level of strike

    Returns:
        Dictionary with 'send_alert' boolean and 'level' AlertLevel enum
    """
    with ALERT_STATE["timer_lock"]:
        now = datetime.now()
        should_send_alert = False
        alert_level = None

        # Check energy threshold
        energy_threshold = get_config_int('ALERTS', 'energy_threshold', 100000)
        if energy < energy_threshold:
            return {"send_alert": False, "level": None}

        # Get configured distances
        critical_distance = get_config_int('ALERTS', 'critical_distance', 10)
        warning_distance = get_config_int('ALERTS', 'warning_distance', 30)

        # Check for critical alert
        if distance <= critical_distance:
            ALERT_STATE["last_critical_strike"] = now

            # Send alert if this is the first critical strike
            if not ALERT_STATE["critical_active"]:
                ALERT_STATE["critical_active"] = True
                should_send_alert = True
                alert_level = AlertLevel.CRITICAL

                # Cancel warning state if active
                ALERT_STATE["warning_active"] = False

            # Reset or start all-clear timer
            schedule_all_clear_message(AlertLevel.CRITICAL)

        # Check for warning alert (only if not in critical zone)
        elif distance <= warning_distance and not ALERT_STATE["critical_active"]:
            ALERT_STATE["last_warning_strike"] = now

            # Send alert if this is the first warning strike
            if not ALERT_STATE["warning_active"]:
                ALERT_STATE["warning_active"] = True
                should_send_alert = True
                alert_level = AlertLevel.WARNING

            # Reset or start all-clear timer
            schedule_all_clear_message(AlertLevel.WARNING)

    return {"send_alert": should_send_alert, "level": alert_level}

def schedule_all_clear_message(alert_level):
    """
    Schedule an all-clear message after no activity for configured time

    Args:
        alert_level: AlertLevel enum indicating which zone to monitor
    """
    delay_minutes = get_config_int('ALERTS', 'all_clear_timer', 15)

    def send_all_clear():
        """Timer callback to send all-clear notification"""
        # Check if monitoring is still active
        if MONITORING_STATE['stop_event'].is_set():
            return

        with ALERT_STATE["timer_lock"]:
            now = datetime.now()

            # Handle warning zone all-clear
            if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_active"]:
                # Verify enough time has passed since last strike
                if ALERT_STATE["warning_timer"] and ALERT_STATE["last_warning_strike"]:
                    if (now - ALERT_STATE["last_warning_strike"]) >= timedelta(minutes=delay_minutes):
                        send_slack_notification(
                            f"🟢 All Clear: No lightning detected within "
                            f"{get_config_int('ALERTS', 'warning_distance', 30)}km for {delay_minutes} minutes.",
                            alert_level=AlertLevel.ALL_CLEAR,
                            previous_level=AlertLevel.WARNING
                        )
                        ALERT_STATE["warning_active"] = False
                        ALERT_STATE["warning_timer"] = None

            # Handle critical zone all-clear
            elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_active"]:
                if ALERT_STATE["critical_timer"] and ALERT_STATE["last_critical_strike"]:
                    if (now - ALERT_STATE["last_critical_strike"]) >= timedelta(minutes=delay_minutes):
                        send_slack_notification(
                            f"🟢 All Clear: No lightning detected within "
                            f"{get_config_int('ALERTS', 'critical_distance', 10)}km for {delay_minutes} minutes.",
                            alert_level=AlertLevel.ALL_CLEAR,
                            previous_level=AlertLevel.CRITICAL
                        )
                        ALERT_STATE["critical_active"] = False
                        ALERT_STATE["critical_timer"] = None

    # Cancel existing timer if present
    with ALERT_STATE["timer_lock"]:
        if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_timer"]:
            ALERT_STATE["warning_timer"].cancel()
        elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_timer"]:
            ALERT_STATE["critical_timer"].cancel()

        # Clean up dead timers from tracking list
        ALERT_STATE["active_timers"] = [t for t in ALERT_STATE["active_timers"] if t.is_alive()]

        # Create and start new timer
        timer = threading.Timer(delay_minutes * 60, send_all_clear)
        timer.daemon = True
        timer.start()

        # Track new timer
        ALERT_STATE["active_timers"].append(timer)

        # Log if too many timers
        if len(ALERT_STATE["active_timers"]) > 10:
            app.logger.warning(f"High number of active timers: {len(ALERT_STATE['active_timers'])}")

        # Store timer reference
        if alert_level == AlertLevel.WARNING:
            ALERT_STATE["warning_timer"] = timer
        elif alert_level == AlertLevel.CRITICAL:
            ALERT_STATE["critical_timer"] = timer

def cleanup_alert_timers():
    """Cancel all active alert timers during shutdown"""
    with ALERT_STATE["timer_lock"]:
        # Cancel warning timer
        if ALERT_STATE["warning_timer"]:
            ALERT_STATE["warning_timer"].cancel()
            ALERT_STATE["warning_timer"] = None

        # Cancel critical timer
        if ALERT_STATE["critical_timer"]:
            ALERT_STATE["critical_timer"].cancel()
            ALERT_STATE["critical_timer"] = None

        # Cancel all tracked timers
        for timer in ALERT_STATE["active_timers"]:
            if timer.is_alive():
                timer.cancel()
        ALERT_STATE["active_timers"].clear()

        # Reset alert states
        ALERT_STATE["warning_active"] = False
        ALERT_STATE["critical_active"] = False

    app.logger.info("Alert timers cleaned up")

# --- Slack Notification System ---
def slack_worker():
    """
    Background worker thread for sending Slack notifications

    This runs continuously, pulling messages from the queue and sending them.
    This design prevents Slack API calls from blocking the interrupt handler.
    """
    while True:
        try:
            # Block for up to 1 second waiting for a message
            message_data = SLACK_QUEUE.get(timeout=1)

            if message_data is None:  # Shutdown signal
                break

            # Attempt to send with retries
            for attempt in range(3):
                try:
                    _send_slack_notification_internal(**message_data)
                    break
                except Exception as e:
                    if attempt == 2:
                        app.logger.error(f"Failed to send Slack notification after 3 attempts: {e}")
                    else:
                        time.sleep(1)  # Brief delay before retry

        except Empty:
            # No messages in queue, continue waiting
            continue
        except Exception as e:
            app.logger.error(f"Slack worker error: {e}")

def send_slack_notification(message, distance=None, energy=None, alert_level=None, previous_level=None):
    """
    Queue a Slack notification for sending with priority handling

    This is the public interface for sending notifications. It adds messages
    to a queue for processing by the background worker thread.

    Args:
        message: Main notification text
        distance: Distance to lightning strike (optional)
        energy: Energy level of strike (optional)
        alert_level: AlertLevel enum for notification type
        previous_level: Previous AlertLevel for all-clear messages
    """
    if not get_config_boolean('SLACK', 'enabled', False):
        return

    msg_data = {
        'message': message,
        'distance': distance,
        'energy': energy,
        'alert_level': alert_level,
        'previous_level': previous_level,
        'timestamp': time.time()  # Add timestamp for queue management
    }

    try:
        SLACK_QUEUE.put_nowait(msg_data)
    except:
        # Queue full - handle based on priority
        if alert_level in [AlertLevel.CRITICAL, AlertLevel.WARNING]:
            # For critical messages, force space by removing oldest
            try:
                # Find and remove oldest non-critical message
                temp_queue = []
                removed = False

                while not SLACK_QUEUE.empty():
                    try:
                        item = SLACK_QUEUE.get_nowait()
                        if not removed and item.get('alert_level') not in [AlertLevel.CRITICAL, AlertLevel.WARNING]:
                            removed = True
                            app.logger.warning(f"Removed non-critical message to make space for {alert_level.value}")
                        else:
                            temp_queue.append(item)
                    except Empty:
                        break

                # Put items back
                for item in temp_queue:
                    SLACK_QUEUE.put_nowait(item)

                # Try to add critical message again
                if removed:
                    SLACK_QUEUE.put_nowait(msg_data)
                else:
                    app.logger.error("Failed to queue critical Slack notification - queue full of critical messages")
            except:
                app.logger.error("Failed to manage Slack queue for critical message")
        else:
            app.logger.warning("Slack queue full, dropping non-critical notification")

def _send_slack_notification_internal(message, distance=None, energy=None, alert_level=None, previous_level=None, timestamp=None):
    """
    Internal function to actually send Slack notification

    This is called by the worker thread and handles the actual API communication.
    """
    bot_token = CONFIG.get('SLACK', 'bot_token', fallback='')
    channel = CONFIG.get('SLACK', 'channel', fallback='#alerts')

    if not bot_token:
        app.logger.warning("Slack is enabled, but Bot Token is not configured")
        return

    url = 'https://slack.com/api/chat.postMessage'

    # Determine notification styling based on alert level
    if alert_level == AlertLevel.CRITICAL:
        color, emoji, urgency = "#ff0000", ":rotating_light:", "CRITICAL"
    elif alert_level == AlertLevel.WARNING:
        color, emoji, urgency = "#ff9900", ":warning:", "WARNING"
    elif alert_level == AlertLevel.ALL_CLEAR:
        color, emoji, urgency = "#00ff00", ":white_check_mark:", "ALL CLEAR"
    else:
        color, emoji, urgency = "#ffcc00", ":zap:", "INFO"

    # Build Slack message blocks
    blocks = []

    # Main message block
    if alert_level in [AlertLevel.WARNING, AlertLevel.CRITICAL]:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{urgency} LIGHTNING ALERT* {emoji}\n{message}"
            }
        })

        # Add details if available
        if distance is not None and energy is not None:
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Distance:*\n{distance} km"},
                    {"type": "mrkdwn", "text": f"*Energy Level:*\n{energy:,}"},
                    {"type": "mrkdwn", "text": f"*Alert Level:*\n{urgency}"},
                    {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now().strftime('%H:%M:%S')}"}
                ]
            })

        # Add context message
        if alert_level == AlertLevel.CRITICAL:
            context_text = ":exclamation: *Very close strike. Take shelter immediately.*"
        else:
            context_text = ":cloud_with_lightning: *Lightning activity in the area. Be prepared.*"

        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": context_text}]
        })

    elif alert_level == AlertLevel.ALL_CLEAR:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} *{urgency}*\n{message}"}
        })

        # Add context about which zone cleared
        previous_urgency = "WARNING" if previous_level == AlertLevel.WARNING else "CRITICAL"
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":information_source: No strikes in {previous_urgency.lower()} zone for "
                       f"{get_config_int('ALERTS', 'all_clear_timer', 15)} min."
            }]
        })
    else:
        # Generic message
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} {message}"}
        })

    # Build payload
    payload = {
        'channel': channel,
        'text': message,  # Fallback text
        'blocks': blocks,
        'icon_emoji': emoji
    }

    # Add color attachment for critical alerts
    if alert_level in [AlertLevel.CRITICAL, AlertLevel.WARNING, AlertLevel.ALL_CLEAR]:
        payload['attachments'] = [{'color': color, 'fallback': message}]

    headers = {
        'Authorization': f'Bearer {bot_token}',
        'Content-Type': 'application/json'
    }

    # Send to Slack API
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()

        result = response.json()
        if not result.get('ok'):
            app.logger.error(f"Slack API error: {result.get('error', 'Unknown error')}")

    except requests.exceptions.Timeout:
        app.logger.warning("Slack notification timed out - continuing operation")
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Slack notification failed: {e}")
    except Exception as e:
        app.logger.error(f"Unexpected error sending Slack notification: {e}")

# --- Dynamic Noise Handling ---
def handle_disturber_event():
    """
    Handle transient disturber events by counting occurrences

    If too many disturbers are detected within a time window, the noise
    floor is raised to reduce sensitivity.
    """
    if not get_config_boolean('NOISE_HANDLING', 'enabled', False):
        return

    now = datetime.now()
    threshold = get_config_int('NOISE_HANDLING', 'event_threshold', 15)
    window = timedelta(seconds=get_config_int('NOISE_HANDLING', 'time_window_seconds', 120))
    revert_delay = get_config_int('NOISE_HANDLING', 'revert_delay_minutes', 10) * 60

    with MONITORING_STATE['lock']:
        # Add this event to the buffer
        MONITORING_STATE['noise_events'].append(now)

        # Remove events outside the time window
        while MONITORING_STATE['noise_events'] and (now - MONITORING_STATE['noise_events'][0]) > window:
            MONITORING_STATE['noise_events'].popleft()

        # Failsafe: prevent buffer from growing too large
        if len(MONITORING_STATE['noise_events']) > 100:
            # Keep only the most recent 50 events
            MONITORING_STATE['noise_events'] = deque(
                list(MONITORING_STATE['noise_events'])[-50:],
                maxlen=50
            )
            app.logger.warning("Noise events buffer exceeded expected size, truncated")

        # Check if threshold exceeded
        if len(MONITORING_STATE['noise_events']) >= threshold and MONITORING_STATE['status']['noise_mode'] != 'Critical':
            # Cancel existing revert timer
            if MONITORING_STATE.get('noise_revert_timer'):
                MONITORING_STATE['noise_revert_timer'].cancel()

            # Raise noise floor if not already raised
            if MONITORING_STATE['status']['noise_mode'] != 'High':
                with SENSOR_INIT_LOCK:
                    if sensor and sensor.is_initialized:
                        app.logger.warning(
                            f"Disturber threshold exceeded ({len(MONITORING_STATE['noise_events'])} events). "
                            f"Elevating noise floor to High."
                        )
                        sensor.set_noise_floor(get_config_int('NOISE_HANDLING', 'raised_noise_floor_level', 5))
                        MONITORING_STATE['status']['noise_mode'] = 'High'

            # Schedule reversion to normal
            timer = threading.Timer(revert_delay, revert_noise_floor, args=['High'])
            timer.daemon = True
            timer.start()
            MONITORING_STATE['noise_revert_timer'] = timer

def handle_noise_high_event():
    """
    Handle persistent noise events (INT_NH interrupt)

    This indicates the noise level is consistently too high, so we
    immediately set the noise floor to maximum.
    """
    if not get_config_boolean('NOISE_HANDLING', 'enabled', False):
        return

    with MONITORING_STATE['lock']:
        # Already at maximum?
        if MONITORING_STATE['status']['noise_mode'] == 'Critical':
            return

        # Cancel any existing timer
        if MONITORING_STATE.get('noise_revert_timer'):
            MONITORING_STATE['noise_revert_timer'].cancel()

        # Set noise floor to maximum
        with SENSOR_INIT_LOCK:
            if sensor and sensor.is_initialized:
                app.logger.critical("Persistent high noise detected (INT_NH). Elevating noise floor to Critical.")
                sensor.set_noise_floor(7)  # Maximum noise floor
                MONITORING_STATE['status']['noise_mode'] = 'Critical'

        # Schedule reversion
        revert_delay = get_config_int('NOISE_HANDLING', 'revert_delay_minutes', 10) * 60
        timer = threading.Timer(revert_delay, revert_noise_floor, args=['Critical'])
        timer.daemon = True
        timer.start()
        MONITORING_STATE['noise_revert_timer'] = timer

def revert_noise_floor(level_to_revert):
    """
    Revert the sensor's noise floor to normal after quiet period

    Args:
        level_to_revert: The noise mode to revert from ('High' or 'Critical')
    """
    with SENSOR_INIT_LOCK:
        if sensor and sensor.is_initialized:
            with MONITORING_STATE['lock']:
                current_mode = MONITORING_STATE['status']['noise_mode']

                # Only revert if we're still in the expected mode
                if current_mode == level_to_revert:
                    app.logger.info(f"Reverting noise floor from {current_mode} to Normal")
                    sensor.set_noise_floor(sensor.original_noise_floor)
                    MONITORING_STATE['status']['noise_mode'] = 'Normal'
                    MONITORING_STATE['noise_events'].clear()

                    # Clear timer reference
                    if MONITORING_STATE.get('noise_revert_timer'):
                        MONITORING_STATE['noise_revert_timer'] = None

# --- Core Monitoring Thread ---
def lightning_monitoring():
    """
    Main monitoring thread with event-driven architecture

    This thread initializes the sensor, sets up GPIO interrupts, and
    monitors sensor health. All actual lightning detection is handled
    via interrupts, making this highly efficient.
    """
    global sensor

    app.logger.info("Starting lightning monitoring thread v2.0-Production-Enhanced")

    # Initialize sensor with retry logic
    if not initialize_sensor_with_retry():
        app.logger.critical("Failed to initialize sensor after all retries")
        return

    # Setup GPIO interrupt detection
    interrupt_configured = False
    try:
        GPIO.add_event_detect(
            sensor.irq_pin,
            GPIO.FALLING,
            callback=handle_sensor_interrupt,
            bouncetime=20  # 20ms debounce
        )
        interrupt_configured = True
        app.logger.info("GPIO interrupt configured successfully")
    except Exception as e:
        app.logger.error(f"Failed to setup GPIO interrupt: {e}")
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = str(e)
        return

    # Main monitoring loop
    last_health_check = time.time()
    health_check_interval = 300  # 5 minutes
    consecutive_failures = 0
    max_consecutive_failures = 3

    try:
        while not MONITORING_STATE['stop_event'].is_set():
            # Non-blocking wait with frequent checks
            for _ in range(100):  # Check every 0.1s for 10s total
                if MONITORING_STATE['stop_event'].is_set():
                    break
                time.sleep(0.1)

            # Periodic health check
            current_time = time.time()
            if current_time - last_health_check > health_check_interval:
                if not perform_sensor_health_check():
                    consecutive_failures += 1
                    app.logger.warning(f"Sensor health check failed ({consecutive_failures}/{max_consecutive_failures})")

                    # Try to recover after multiple failures
                    if consecutive_failures >= max_consecutive_failures:
                        app.logger.critical(f"Sensor failed {max_consecutive_failures} consecutive health checks")

                        # Remove old interrupt handler
                        if interrupt_configured:
                            try:
                                GPIO.remove_event_detect(sensor.irq_pin)
                            except:
                                pass

                        # Attempt to reinitialize
                        if initialize_sensor_with_retry(max_retries=3):
                            consecutive_failures = 0

                            # Re-setup interrupt
                            try:
                                GPIO.add_event_detect(
                                    sensor.irq_pin,
                                    GPIO.FALLING,
                                    callback=handle_sensor_interrupt,
                                    bouncetime=20
                                )
                                interrupt_configured = True
                                app.logger.info("Sensor recovered and interrupt re-configured")
                            except Exception as e:
                                app.logger.error(f"Failed to re-setup interrupt: {e}")
                                break
                        else:
                            app.logger.critical("Failed to recover sensor")
                            break
                else:
                    # Health check passed
                    consecutive_failures = 0

                last_health_check = current_time

    except Exception as e:
        app.logger.error(f"Unexpected error in monitoring loop: {e}", exc_info=True)
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = str(e)

    finally:
        app.logger.info("Cleaning up monitoring thread")

        # Remove interrupt detection first (before any GPIO operations)
        if interrupt_configured:
            try:
                GPIO.remove_event_detect(sensor.irq_pin)
                app.logger.info("GPIO interrupt removed")
            except Exception as e:
                app.logger.error(f"Error removing GPIO interrupt: {e}")

        # Clean up sensor
        with SENSOR_INIT_LOCK:
            if sensor:
                try:
                    sensor.cleanup()
                except Exception as e:
                    app.logger.error(f"Error during sensor cleanup: {e}")
                sensor = None

        # Update status
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_active'] = False
            MONITORING_STATE['status']['status_message'] = "Stopped"

        app.logger.info("Monitoring thread cleanup complete")

def monitoring_watchdog():
    """
    Watchdog thread that monitors the main monitoring thread

    This provides automatic recovery if the monitoring thread dies
    unexpectedly. It includes failure counting to prevent infinite
    restart loops.
    """
    consecutive_failures = 0
    max_failures = 3

    while not MONITORING_STATE['stop_event'].is_set():
        # Wait 60 seconds between checks
        for _ in range(60):
            if MONITORING_STATE['stop_event'].is_set():
                return
            time.sleep(1)

        with MONITORING_STATE['lock']:
            thread = MONITORING_STATE.get('thread')

            # Check if thread is alive
            if not thread or not thread.is_alive():
                # Only restart if auto-start is enabled
                if not get_config_boolean('SENSOR', 'auto_start', True):
                    continue

                consecutive_failures += 1

                # Give up after too many failures
                if consecutive_failures >= max_failures:
                    app.logger.critical(f"Monitoring thread failed {max_failures} times. Stopping watchdog.")
                    MONITORING_STATE['status']['status_message'] = "Fatal: Too many failures"
                    return

                app.logger.warning(f"Monitoring thread died (failure {consecutive_failures}/{max_failures}). Restarting...")

                # Clear stop event and start new thread
                MONITORING_STATE['stop_event'].clear()
                new_thread = threading.Thread(target=lightning_monitoring, daemon=True)
                MONITORING_STATE['thread'] = new_thread
                new_thread.start()

                # Wait a bit to see if it starts successfully
                time.sleep(5)

                if new_thread.is_alive():
                    consecutive_failures = 0  # Reset on success
                    app.logger.info("Monitoring thread restarted successfully")
            else:
                # Thread is running normally
                consecutive_failures = 0

# --- Flask Web Routes ---
@app.route('/')
def index():
    """Main dashboard page"""
    # Get current state with thread safety
    with MONITORING_STATE['lock']:
        events = list(MONITORING_STATE['events'])
        status = MONITORING_STATE['status'].copy()
        total_events = len(events)

    with ALERT_STATE["timer_lock"]:
        alert_status = {
            'warning_active': ALERT_STATE["warning_active"],
            'critical_active': ALERT_STATE["critical_active"],
            'last_warning_strike': ALERT_STATE["last_warning_strike"].strftime('%H:%M:%S')
                if ALERT_STATE["last_warning_strike"] else None,
            'last_critical_strike': ALERT_STATE["last_critical_strike"].strftime('%H:%M:%S')
                if ALERT_STATE["last_critical_strike"] else None
        }

    # Pre-format event data for template
    for event in events:
        if 'timestamp' in event:
            try:
                event['timestamp'] = datetime.fromisoformat(event['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            except:
                event['timestamp'] = 'Unknown'

        # Energy is already formatted in the event

    # Format last reading timestamp
    if status.get('last_reading'):
        try:
            status['last_reading'] = datetime.fromisoformat(status['last_reading']).strftime('%Y-%m-%d %H:%M:%S')
        except:
            status['last_reading'] = 'Unknown'

    return render_template('index.html',
        lightning_events=events,
        sensor_status=status,
        alert_state=alert_status,
        config=CONFIG,
        debug_mode=get_config_boolean('SYSTEM', 'debug', False),
        total_event_count=total_events,
        events_truncated=(total_events >= 100)
    )

@app.route('/api/status')
def api_status():
    """JSON API endpoint for system status"""
    with MONITORING_STATE['lock']:
        status = MONITORING_STATE['status'].copy()
        thread_alive = MONITORING_STATE['thread'].is_alive() if MONITORING_STATE.get('thread') else False
        event_count = len(MONITORING_STATE['events'])

    with ALERT_STATE["timer_lock"]:
        alert_status = {
            'warning_active': ALERT_STATE["warning_active"],
            'critical_active': ALERT_STATE["critical_active"]
        }

    return jsonify({
        **status,
        'alert_state': alert_status,
        'monitoring_thread_active': thread_alive,
        'version': '2.0-Production-Enhanced',
        'event_count': event_count,
        'config_valid': validate_config()
    })

@app.route('/health')
def health_check():
    """
    Health check endpoint for monitoring system health

    Returns HTTP 200 if healthy, 503 if degraded
    """
    health = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '2.0-Production-Enhanced',
        'checks': {}
    }

    # Check sensor
    with SENSOR_INIT_LOCK:
        if sensor and sensor.is_initialized:
            try:
                # Try a register read
                sensor._read_register(0x00)
                health['checks']['sensor'] = 'ok'
            except:
                health['checks']['sensor'] = 'error'
                health['status'] = 'degraded'
        else:
            health['checks']['sensor'] = 'not_initialized'
            health['status'] = 'degraded'

    # Check thread
    with MONITORING_STATE['lock']:
        thread = MONITORING_STATE.get('thread')
        if thread and thread.is_alive():
            health['checks']['monitoring_thread'] = 'running'
        else:
            health['checks']['monitoring_thread'] = 'stopped'
            if get_config_boolean('SENSOR', 'auto_start', True):
                health['status'] = 'degraded'

    # Check configuration
    health['checks']['config'] = 'valid' if validate_config() else 'invalid'
    if health['checks']['config'] == 'invalid':
        health['status'] = 'degraded'

    # Check sensor health from status
    with MONITORING_STATE['lock']:
        if not MONITORING_STATE['status'].get('sensor_healthy', True):
            health['checks']['sensor_health'] = 'unhealthy'
            health['status'] = 'degraded'
        else:
            health['checks']['sensor_health'] = 'healthy'

    return jsonify(health), 200 if health['status'] == 'healthy' else 503

@app.route('/metrics')
def metrics():
    """Prometheus-compatible metrics endpoint for external monitoring"""
    with MONITORING_STATE['lock']:
        event_count = len(MONITORING_STATE['events'])
        sensor_active = 1 if MONITORING_STATE['status']['sensor_active'] else 0
        sensor_healthy = 1 if MONITORING_STATE['status']['sensor_healthy'] else 0
        noise_level = {'Normal': 0, 'High': 1, 'Critical': 2}.get(
            MONITORING_STATE['status']['noise_mode'], 0
        )
        interrupt_storm = 1 if MONITORING_STATE['interrupt_storm_detected'] else 0

    with ALERT_STATE["timer_lock"]:
        warning_active = 1 if ALERT_STATE["warning_active"] else 0
        critical_active = 1 if ALERT_STATE["critical_active"] else 0
        active_timer_count = len([t for t in ALERT_STATE["active_timers"] if t.is_alive()])
