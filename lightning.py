#!/usr/bin/env python3
"""
CJMCU-3935 Lightning Detector Flask Application v2.0
Event-Driven & Hardened Architecture
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

import requests
import RPi.GPIO as GPIO
import spidev
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash

# --- Alert Level Enumeration ---
class AlertLevel(Enum):
    WARNING = "warning"
    CRITICAL = "critical"
    ALL_CLEAR = "all_clear"

# --- Global Configuration and State ---
CONFIG = configparser.ConfigParser()

# Main monitoring state - thread-safe dictionary to hold all shared state
MONITORING_STATE = {
    "lock": threading.Lock(),
    "stop_event": threading.Event(),
    "events": deque(maxlen=100),
    "status": {
        'last_reading': None,
        'sensor_active': False,
        'status_message': 'Not started',
        'indoor_mode': False,
        'noise_mode': 'Normal' # Can be Normal, High, or Critical
    },
    "thread": None,
    "noise_events": deque(maxlen=50), # For counting transient disturber events
    "noise_revert_timer": None,
}

# Enhanced alert state management
ALERT_STATE = {
    "warning_timer": None,
    "critical_timer": None,
    "warning_active": False,
    "critical_active": False,
    "last_warning_strike": None,
    "last_critical_strike": None,
    "timer_lock": threading.Lock()
}

# Dedicated lock for sensor initialization to ensure it is atomic.
SENSOR_INIT_LOCK = threading.Lock()
sensor = None # Global sensor object

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = 'lightning-detector-hardened-v20-secret-key'

# --- Sensor Driver Class (v2.0) ---
class AS3935LightningDetector:
    """Hardened Driver for CJMCU-3935 (AS3935) Lightning Detector"""
    # Register addresses
    REG_AFE_GAIN = 0x00
    REG_PWD = 0x00
    REG_MIXED_MODE = 0x01
    REG_SREJ = 0x02
    REG_LCO_FDIV = 0x03
    REG_MASK_DIST = 0x03
    REG_DISP_LCO = 0x08

    # Interrupt reasons
    INT_NH = 0x01      # Noise level too high
    INT_D = 0x04       # Disturber detected
    INT_L = 0x08       # Lightning detected

    def __init__(self, spi_bus=0, spi_device=0, irq_pin=2):
        self.spi = None
        self.irq_pin = irq_pin
        self.is_initialized = False
        self.original_noise_floor = 0x02

        try:
            self.spi = spidev.SpiDev()
            self.spi.open(spi_bus, spi_device)
            self.spi.max_speed_hz = 2000000
            self.spi.mode = 0b01

            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.irq_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            self.power_up()
            self.is_initialized = True
        except Exception as e:
            self.cleanup()
            raise e

    def _write_register(self, reg, value):
        if self.spi:
            self.spi.xfer2([reg, value])

    def _read_register(self, reg):
        if self.spi:
            result = self.spi.xfer2([reg | 0x40, 0x00])
            return result[1]
        return 0

    def power_up(self):
        """Initializes and calibrates the sensor with indoor/outdoor support."""
        self._write_register(self.REG_PWD, 0x96)
        time.sleep(0.003)
        self._write_register(self.REG_LCO_FDIV, 0b10010111)
        self._write_register(self.REG_DISP_LCO, 0x80)
        time.sleep(0.002)
        self._write_register(self.REG_DISP_LCO, 0x00)
        time.sleep(0.002)

        is_indoor = get_config_boolean('SENSOR', 'indoor', False)
        sensitivity = CONFIG.get('SENSOR', 'sensitivity', fallback='medium')

        # AFE Gain settings from datasheet for indoor/outdoor
        afe_gain = 0b00010010 if is_indoor else 0b00100100

        s_rej = {'low': 0x03, 'medium': 0x02, 'high': 0x01}.get(sensitivity, 0x02)
        nf_lev = {'low': 0x04, 'medium': 0x02, 'high': 0x01}.get(sensitivity, 0x02)
        wdth = {'low': 0x03, 'medium': 0x02, 'high': 0x01}.get(sensitivity, 0x02)

        self.original_noise_floor = nf_lev
        reg01_value = (nf_lev << 4) | wdth

        self._write_register(self.REG_AFE_GAIN, afe_gain)
        self._write_register(self.REG_MIXED_MODE, reg01_value)
        self._write_register(self.REG_SREJ, (s_rej << 4) | (self._read_register(self.REG_SREJ) & 0x0F))

        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['indoor_mode'] = is_indoor
        app.logger.info(f"Sensor powered up. Mode: {'Indoor' if is_indoor else 'Outdoor'}, Sensitivity: {sensitivity}.")

    def set_noise_floor(self, level):
        """Sets the noise floor level, preserving the watchdog threshold."""
        if not (0x00 <= level <= 0x07):
            app.logger.error(f"Invalid noise floor level: {level}. Must be 0-7.")
            return
        try:
            current_reg_val = self._read_register(self.REG_MIXED_MODE)
            preserved_wdth = current_reg_val & 0x0F
            new_reg_val = (level << 4) | preserved_wdth
            self._write_register(self.REG_MIXED_MODE, new_reg_val)
            app.logger.info(f"Sensor noise floor dynamically set to level {level}. Register 0x01 is now {new_reg_val:#04x}.")
        except IOError as e:
            app.logger.error(f"SPI Error setting noise floor: {e}")

    def get_interrupt_reason(self):
        return self._read_register(0x03) & 0x0F

    def get_lightning_distance(self):
        return self._read_register(0x07) & 0x3F

    def get_lightning_energy(self):
        lsb = self._read_register(0x04)
        msb = self._read_register(0x05)
        mmsb = self._read_register(0x06) & 0x1F
        return (mmsb << 16) | (msb << 8) | lsb

    def cleanup(self):
        """Safe cleanup only affecting this application's resources."""
        try:
            if self.spi:
                self.spi.close()
                self.spi = None
            if self.is_initialized and self.irq_pin is not None:
                try:
                    # Only cleanup the specific pin we're using
                    GPIO.remove_event_detect(self.irq_pin)
                    GPIO.setup(self.irq_pin, GPIO.IN)  # Reset to input
                except Exception as e:
                    app.logger.debug(f"GPIO cleanup for pin {self.irq_pin}: {e}")
            app.logger.info(f"Cleaned up GPIO pin {self.irq_pin} and SPI resources.")
        except Exception as e:
            app.logger.error(f"Error during hardware cleanup: {e}")

# --- Helper functions for robust config reading ---
def get_config_int(section, key, fallback):
    """Safely get an integer from the config."""
    try: return CONFIG.getint(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback
def get_config_float(section, key, fallback):
    """Safely get a float from the config."""
    try: return CONFIG.getfloat(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback
def get_config_boolean(section, key, fallback):
    """Safely get a boolean from the config."""
    try: return CONFIG.getboolean(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

# --- Alert System & Slack Functions ---
def schedule_all_clear_message(alert_level):
    """Schedules an all-clear message. Timer cancellation is thread-safe."""
    delay_minutes = get_config_int('ALERTS', 'all_clear_timer', 15)

    def send_all_clear():
        # Check if monitoring is still active
        if MONITORING_STATE['stop_event'].is_set():
            return

        with ALERT_STATE["timer_lock"]:
            now = datetime.now()
            if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_active"]:
                if ALERT_STATE["last_warning_strike"] and (now - ALERT_STATE["last_warning_strike"]) >= timedelta(minutes=delay_minutes):
                    send_slack_notification(f"🟢 All Clear: No lightning detected within {get_config_int('ALERTS', 'warning_distance', 30)}km for {delay_minutes} minutes.", alert_level=AlertLevel.ALL_CLEAR, previous_level=AlertLevel.WARNING)
                    ALERT_STATE["warning_active"] = False
                    ALERT_STATE["warning_timer"] = None
            elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_active"]:
                if ALERT_STATE["last_critical_strike"] and (now - ALERT_STATE["last_critical_strike"]) >= timedelta(minutes=delay_minutes):
                    send_slack_notification(f"🟢 All Clear: No lightning detected within {get_config_int('ALERTS', 'critical_distance', 10)}km for {delay_minutes} minutes.", alert_level=AlertLevel.ALL_CLEAR, previous_level=AlertLevel.CRITICAL)
                    ALERT_STATE["critical_active"] = False
                    ALERT_STATE["critical_timer"] = None

    with ALERT_STATE["timer_lock"]:
        if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_timer"]: ALERT_STATE["warning_timer"].cancel()
        elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_timer"]: ALERT_STATE["critical_timer"].cancel()
        timer = threading.Timer(delay_minutes * 60, send_all_clear)
        timer.daemon = True
        timer.start()
        if alert_level == AlertLevel.WARNING: ALERT_STATE["warning_timer"] = timer
        elif alert_level == AlertLevel.CRITICAL: ALERT_STATE["critical_timer"] = timer

def check_alert_conditions(distance, energy):
    with ALERT_STATE["timer_lock"]:
        now, should_send_alert, alert_level = datetime.now(), False, None
        energy_threshold = get_config_int('ALERTS', 'energy_threshold', 100000)
        critical_distance = get_config_int('ALERTS', 'critical_distance', 10)
        warning_distance = get_config_int('ALERTS', 'warning_distance', 30)
        if energy < energy_threshold: return {"send_alert": False, "level": None}
        if distance <= critical_distance:
            ALERT_STATE["last_critical_strike"] = now
            if not ALERT_STATE["critical_active"]:
                ALERT_STATE["critical_active"], should_send_alert, alert_level, ALERT_STATE["warning_active"] = True, True, AlertLevel.CRITICAL, False
            schedule_all_clear_message(AlertLevel.CRITICAL)
        elif distance <= warning_distance and not ALERT_STATE["critical_active"]:
            ALERT_STATE["last_warning_strike"] = now
            if not ALERT_STATE["warning_active"]:
                ALERT_STATE["warning_active"], should_send_alert, alert_level = True, True, AlertLevel.WARNING
            schedule_all_clear_message(AlertLevel.WARNING)
    return {"send_alert": should_send_alert, "level": alert_level}

def send_slack_notification(message, distance=None, energy=None, alert_level=None, previous_level=None):
    if not get_config_boolean('SLACK', 'enabled', False): return
    bot_token = CONFIG.get('SLACK', 'bot_token', fallback='')
    channel = CONFIG.get('SLACK', 'channel', fallback='#alerts')
    if not bot_token:
        app.logger.warning("Slack is enabled, but Bot Token is not configured.")
        return
    url = 'https://slack.com/api/chat.postMessage'
    if alert_level == AlertLevel.CRITICAL: color, emoji, urgency = "#ff0000", ":rotating_light:", "CRITICAL"
    elif alert_level == AlertLevel.WARNING: color, emoji, urgency = "#ff9900", ":warning:", "WARNING"
    elif alert_level == AlertLevel.ALL_CLEAR: color, emoji, urgency = "#00ff00", ":white_check_mark:", "ALL CLEAR"
    else: color, emoji, urgency = "#ffcc00", ":zap:", "INFO"
    blocks = []
    if alert_level in [AlertLevel.WARNING, AlertLevel.CRITICAL]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{urgency} LIGHTNING ALERT* {emoji}\n{message}"}})
        if distance is not None and energy is not None: blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": f"*Distance:*\n{distance} km"}, {"type": "mrkdwn", "text": f"*Energy Level:*\n{energy:,}"}, {"type": "mrkdwn", "text": f"*Alert Level:*\n{urgency}"}, {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now().strftime('%H:%M:%S')}"}]})
        context_text = ":exclamation: *Very close strike. Take shelter.*" if alert_level == AlertLevel.CRITICAL else ":cloud_with_lightning: *Activity in the area. Be prepared.*"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]})
    elif alert_level == AlertLevel.ALL_CLEAR:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{urgency}*\n{message}"}})
        previous_urgency = "WARNING" if previous_level == AlertLevel.WARNING else "CRITICAL"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f":information_source: No strikes in {previous_urgency.lower()} zone for {get_config_int('ALERTS', 'all_clear_timer', 15)} min."}]})
    else: blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} {message}"}})
    payload = {'channel': channel, 'text': message, 'blocks': blocks, 'icon_emoji': emoji}
    if alert_level in [AlertLevel.CRITICAL, AlertLevel.WARNING, AlertLevel.ALL_CLEAR]: payload['attachments'] = [{'color': color, 'fallback': message}]
    headers = {'Authorization': f'Bearer {bot_token}', 'Content-Type': 'application/json'}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()
        if not result.get('ok'): app.logger.error(f"Slack API error: {result.get('error', 'Unknown error')}")
    except requests.exceptions.RequestException as e: app.logger.error(f"Slack notification failed (RequestException): {e}")
    except Exception as e: app.logger.error(f"Unexpected error sending Slack notification: {e}")

def cleanup_alert_timers():
    with ALERT_STATE["timer_lock"]:
        if ALERT_STATE["warning_timer"]: ALERT_STATE["warning_timer"].cancel()
        if ALERT_STATE["critical_timer"]: ALERT_STATE["critical_timer"].cancel()
        ALERT_STATE["warning_timer"] = None
        ALERT_STATE["critical_timer"] = None
        ALERT_STATE["warning_active"] = False
        ALERT_STATE["critical_active"] = False
    app.logger.info("Alert timers cleaned up.")

# --- DYNAMIC NOISE HANDLING (v2.0 REFINED LOGIC) ---
def revert_noise_floor(level_to_revert):
    """Reverts the sensor's noise floor to its original or previous value."""
    with SENSOR_INIT_LOCK:
        if sensor:
            with MONITORING_STATE['lock']:
                current_mode = MONITORING_STATE['status']['noise_mode']
                if current_mode == level_to_revert:
                    app.logger.info(f"Reverting noise floor from {current_mode} to Normal.")
                    sensor.set_noise_floor(sensor.original_noise_floor)
                    MONITORING_STATE['status']['noise_mode'] = 'Normal'
                    MONITORING_STATE['noise_events'].clear()
                    if MONITORING_STATE.get('noise_revert_timer'):
                        MONITORING_STATE['noise_revert_timer'].cancel()
                    MONITORING_STATE['noise_revert_timer'] = None

def handle_disturber_event():
    """Handles transient disturber events by counting them over time."""
    if not get_config_boolean('NOISE_HANDLING', 'enabled', False): return
    now = datetime.now()
    threshold = get_config_int('NOISE_HANDLING', 'event_threshold', 15)
    window = timedelta(seconds=get_config_int('NOISE_HANDLING', 'time_window_seconds', 120))
    revert_delay = get_config_int('NOISE_HANDLING', 'revert_delay_minutes', 10) * 60

    with MONITORING_STATE['lock']:
        MONITORING_STATE['noise_events'].append(now)
        while MONITORING_STATE['noise_events'] and (now - MONITORING_STATE['noise_events'][0]) > window:
            MONITORING_STATE['noise_events'].popleft()

        if len(MONITORING_STATE['noise_events']) >= threshold and MONITORING_STATE['status']['noise_mode'] != 'Critical':
            if MONITORING_STATE.get('noise_revert_timer'): MONITORING_STATE['noise_revert_timer'].cancel()

            if MONITORING_STATE['status']['noise_mode'] != 'High':
                with SENSOR_INIT_LOCK:
                    if sensor:
                        app.logger.warning(f"Disturber threshold exceeded ({len(MONITORING_STATE['noise_events'])} events). Elevating noise floor to High.")
                        sensor.set_noise_floor(get_config_int('NOISE_HANDLING', 'raised_noise_floor_level', 5))
                        MONITORING_STATE['status']['noise_mode'] = 'High'

            timer = threading.Timer(revert_delay, revert_noise_floor, args=['High'])
            timer.daemon = True; timer.start()
            MONITORING_STATE['noise_revert_timer'] = timer

def handle_noise_high_event():
    """Handles persistent noise events immediately by setting noise floor to max."""
    if not get_config_boolean('NOISE_HANDLING', 'enabled', False): return
    with MONITORING_STATE['lock']:
        if MONITORING_STATE['status']['noise_mode'] == 'Critical': return # Already at max
        if MONITORING_STATE.get('noise_revert_timer'): MONITORING_STATE['noise_revert_timer'].cancel()

        with SENSOR_INIT_LOCK:
            if sensor:
                app.logger.critical("Persistent high noise detected (INT_NH). Elevating noise floor to Critical.")
                sensor.set_noise_floor(7) # Set to max
                MONITORING_STATE['status']['noise_mode'] = 'Critical'

        revert_delay = get_config_int('NOISE_HANDLING', 'revert_delay_minutes', 10) * 60
        timer = threading.Timer(revert_delay, revert_noise_floor, args=['Critical'])
        timer.daemon = True; timer.start()
        MONITORING_STATE['noise_revert_timer'] = timer

# --- CORE MONITORING LOGIC (v2.0 EVENT-DRIVEN) ---
def handle_sensor_interrupt(channel):
    """Callback function triggered by the IRQ pin FALLING edge."""
    time.sleep(0.002)  # Datasheet recommends 2ms delay after IRQ to read registers

    # Check if we're shutting down first
    if MONITORING_STATE['stop_event'].is_set():
        return

    with SENSOR_INIT_LOCK:
        if not sensor or not hasattr(sensor, 'is_initialized') or not sensor.is_initialized:
            return

        try:
            interrupt_reason = sensor.get_interrupt_reason()

            if interrupt_reason == sensor.INT_L:
                distance = sensor.get_lightning_distance()
                energy = sensor.get_lightning_energy()
                alert_result = check_alert_conditions(distance, energy)
                event = {'timestamp': datetime.now().isoformat(), 'distance': distance, 'energy': energy, 'alert_sent': alert_result.get('send_alert', False), 'alert_level': alert_result.get('level').value if alert_result.get('level') else None}
                with MONITORING_STATE['lock']: MONITORING_STATE['events'].append(event)
                app.logger.info(f"⚡ Lightning detected: {distance}km, energy: {energy}")
                if alert_result.get('send_alert'):
                    level = alert_result.get('level')
                    if level == AlertLevel.CRITICAL: send_slack_notification(f"🚨 CRITICAL: Lightning strike detected! Distance: {distance}km", distance, energy, level)
                    elif level == AlertLevel.WARNING: send_slack_notification(f"⚠️ WARNING: Lightning detected. Distance: {distance}km", distance, energy, level)

            elif interrupt_reason == sensor.INT_D:
                app.logger.debug("Disturber detected")
                handle_disturber_event()

            elif interrupt_reason == sensor.INT_NH:
                app.logger.warning("Noise level too high interrupt")
                handle_noise_high_event()

            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['last_reading'] = datetime.now().isoformat()

        except IOError as e:
            app.logger.error(f"SPI/IOError during interrupt handling: {e}. Sensor may need reset.")
        except Exception as e:
            app.logger.error(f"Unhandled exception in interrupt handler: {e}", exc_info=True)

def lightning_monitoring():
    """Main monitoring thread setup. Now event-driven and robust."""
    global sensor
    MAX_SENSOR_RETRIES = 5; retry_count = 0

    while not MONITORING_STATE['stop_event'].is_set():
        with SENSOR_INIT_LOCK:
            if sensor is None:
                if retry_count >= MAX_SENSOR_RETRIES:
                    app.logger.critical(f"Sensor init failed after {MAX_SENSOR_RETRIES} attempts. Thread stopping.")
                    with MONITORING_STATE['lock']: MONITORING_STATE['status']['status_message'] = f"Fatal: Max sensor retries."
                    return
                try:
                    sensor = AS3935LightningDetector(
                        spi_bus=get_config_int('SENSOR', 'spi_bus', 0),
                        spi_device=get_config_int('SENSOR', 'spi_device', 0),
                        irq_pin=get_config_int('SENSOR', 'irq_pin', 2)
                    )
                    with MONITORING_STATE['lock']:
                        MONITORING_STATE['status']['sensor_active'] = True
                        MONITORING_STATE['status']['status_message'] = "Monitoring (Event-Driven)"
                    app.logger.info("Event-driven lightning sensor initialized successfully.")
                    retry_count = 0
                    break # Exit init loop on success
                except Exception as e:
                    retry_count += 1
                    with MONITORING_STATE['lock']:
                        MONITORING_STATE['status']['sensor_active'] = False
                        MONITORING_STATE['status']['status_message'] = f"Sensor init failed (attempt {retry_count})."
                    app.logger.error(f"Sensor init failed (attempt {retry_count}/{MAX_SENSOR_RETRIES}): {e}. Retrying.")
                    MONITORING_STATE['stop_event'].wait(30)

    if sensor is None: return # Failed to initialize

    try:
        GPIO.add_event_detect(sensor.irq_pin, GPIO.FALLING, callback=handle_sensor_interrupt, bouncetime=20)
        app.logger.info("GPIO event detection started. Waiting for interrupts.")
        MONITORING_STATE['stop_event'].wait()
        app.logger.info("Stop event received, shutting down monitoring thread.")
    except Exception as e:
        app.logger.error(f"Fatal error in monitoring setup: {e}", exc_info=True)
    finally:
        with SENSOR_INIT_LOCK:
            if sensor:
                try:
                    GPIO.remove_event_detect(sensor.irq_pin)
                    app.logger.info("GPIO event detection stopped.")
                except Exception: pass
                sensor.cleanup()
            sensor = None
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_active'] = False
            MONITORING_STATE['status']['status_message'] = "Stopped"

# --- FLASK ROUTES (v2.0) ---
@app.route('/')
def index():
    with MONITORING_STATE['lock']:
        events = list(MONITORING_STATE['events'])
        status = MONITORING_STATE['status'].copy()
    with ALERT_STATE["timer_lock"]:
        alert_status = {
            'warning_active': ALERT_STATE["warning_active"],
            'critical_active': ALERT_STATE["critical_active"],
            'last_warning_strike': ALERT_STATE["last_warning_strike"].strftime('%H:%M:%S') if ALERT_STATE["last_warning_strike"] else None,
            'last_critical_strike': ALERT_STATE["last_critical_strike"].strftime('%H:%M:%S') if ALERT_STATE["last_critical_strike"] else None
        }

    # FIX: Pre-format event data before sending to the template to prevent Jinja2 errors.
    for event in events:
        event['timestamp'] = datetime.fromisoformat(event['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        # This formats the number with commas (e.g., 100000 -> "100,000") and adds it to the event dict
        event['energy_formatted'] = f"{event.get('energy', 0):,}"

    if status.get('last_reading'):
        status['last_reading'] = datetime.fromisoformat(status['last_reading']).strftime('%Y-%m-%d %H:%M:%S')

    return render_template('index.html',
        lightning_events=events,
        sensor_status=status,
        alert_state=alert_status,
        config=CONFIG,
        debug_mode=get_config_boolean('SYSTEM', 'debug', False))

@app.route('/api/status')
def api_status():
    with MONITORING_STATE['lock']:
        status = MONITORING_STATE['status'].copy()
        thread_alive = MONITORING_STATE['thread'].is_alive() if MONITORING_STATE.get('thread') else False
    with ALERT_STATE["timer_lock"]:
        alert_status = {'warning_active': ALERT_STATE["warning_active"], 'critical_active': ALERT_STATE["critical_active"]}
    return jsonify({**status, 'alert_state': alert_status, 'monitoring_thread_active': thread_alive, 'version': '2.0'})

@app.route('/config')
def config_page():
    return render_template('config.html', config=CONFIG, debug_mode=get_config_boolean('SYSTEM', 'debug', False))

@app.route('/save_config', methods=['POST'])
def save_config_route():
    try:
        for key, value in request.form.items():
            if '_' in key:
                section, option = key.split('_', 1)
                if CONFIG.has_section(section): CONFIG.set(section, option, value)
        # Handle checkboxes which are not present in form data if unchecked
        CONFIG.set('SLACK', 'enabled', 'true' if 'SLACK_enabled' in request.form else 'false')
        CONFIG.set('SENSOR', 'auto_start', 'true' if 'SENSOR_auto_start' in request.form else 'false')
        CONFIG.set('SENSOR', 'indoor', 'true' if 'SENSOR_indoor' in request.form else 'false')
        CONFIG.set('NOISE_HANDLING', 'enabled', 'true' if 'NOISE_HANDLING_enabled' in request.form else 'false')
        with open('config.ini', 'w') as configfile: CONFIG.write(configfile)
        flash('Configuration saved successfully! A restart is required for changes to take effect.', 'success')
    except Exception as e: flash(f'Error saving configuration: {str(e)}', 'error')
    return redirect(url_for('config_page'))

@app.route('/start_monitoring')
def start_monitoring_route():
    with MONITORING_STATE['lock']:
        thread = MONITORING_STATE.get("thread")
        if thread and thread.is_alive():
            flash('Monitoring is already running.', 'info')
        else:
            MONITORING_STATE['stop_event'].clear()
            new_thread = threading.Thread(target=lightning_monitoring, daemon=True)
            MONITORING_STATE['thread'] = new_thread
            new_thread.start()
            flash('Lightning monitoring started!', 'success')
            app.logger.info("Monitoring started via web UI.")
    return redirect(url_for('index'))

@app.route('/stop_monitoring')
def stop_monitoring_route():
    MONITORING_STATE['stop_event'].set()
    cleanup_alert_timers()
    with MONITORING_STATE['lock']:
        if MONITORING_STATE.get('noise_revert_timer'):
            MONITORING_STATE['noise_revert_timer'].cancel()
    flash('Stop signal sent to monitoring thread.', 'info')
    app.logger.info("Monitoring stop requested via web UI.")
    return redirect(url_for('index'))

@app.route('/reset_alerts')
def reset_alerts():
    cleanup_alert_timers()
    with MONITORING_STATE['lock']:
        if MONITORING_STATE.get('noise_revert_timer'):
            MONITORING_STATE['noise_revert_timer'].cancel()
            revert_noise_floor(MONITORING_STATE['status']['noise_mode'])
    flash('All alert states and noise mitigation modes have been reset.', 'success')
    app.logger.info("Alerts and noise modes manually reset via web interface.")
    return redirect(url_for('index'))

@app.route('/test_alerts/<type>')
def test_alerts(type):
    """Test alert functionality"""
    if not get_config_boolean('SYSTEM', 'debug', False):
        flash('Test alerts only available in debug mode', 'error')
        return redirect(url_for('index'))

    if type == 'warning':
        # Simulate a warning alert
        test_event = {
            'timestamp': datetime.now().isoformat(),
            'distance': 25,
            'energy': 150000,
            'alert_sent': True,
            'alert_level': 'warning'
        }
        with MONITORING_STATE['lock']:
            MONITORING_STATE['events'].append(test_event)
        send_slack_notification(
            "⚠️ TEST WARNING: Lightning detected. Distance: 25km",
            25, 150000, AlertLevel.WARNING
        )
        flash('Test warning alert sent', 'success')

    elif type == 'critical':
        # Simulate a critical alert
        test_event = {
            'timestamp': datetime.now().isoformat(),
            'distance': 8,
            'energy': 250000,
            'alert_sent': True,
            'alert_level': 'critical'
        }
        with MONITORING_STATE['lock']:
            MONITORING_STATE['events'].append(test_event)
        send_slack_notification(
            "🚨 TEST CRITICAL: Lightning strike detected! Distance: 8km",
            8, 250000, AlertLevel.CRITICAL
        )
        flash('Test critical alert sent', 'success')

    return redirect(url_for('index'))

@app.route('/test_slack')
def test_slack():
    """Test Slack connection"""
    try:
        send_slack_notification("🧪 Test message from Lightning Detector v2.0")
        flash('Test message sent to Slack successfully', 'success')
    except Exception as e:
        flash(f'Failed to send test message: {str(e)}', 'error')
    return redirect(url_for('config_page'))

# --- MAIN EXECUTION ---
def load_and_configure():
    CONFIG_FILE = 'config.ini'
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: Configuration file '{CONFIG_FILE}' not found. Exiting.")
        exit(1)
    CONFIG.read(CONFIG_FILE)
    required_sections = ['SYSTEM', 'SENSOR', 'SLACK', 'ALERTS', 'LOGGING', 'NOISE_HANDLING']
    for section in required_sections:
        if not CONFIG.has_section(section):
            CONFIG.add_section(section)
    log_cfg = CONFIG['LOGGING']
    handler = RotatingFileHandler('lightning_detector.log', maxBytes=get_config_int('LOGGING', 'max_file_size', 10) * 1024 * 1024, backupCount=get_config_int('LOGGING', 'backup_count', 5))
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    app.logger.addHandler(handler)
    app.logger.setLevel(log_cfg.get('level', 'INFO').upper())
    app.logger.info("Lightning Detector v2.0 starting up.")

def start_monitoring_on_boot():
    if get_config_boolean('SENSOR', 'auto_start', True):
        with MONITORING_STATE['lock']:
            if not (MONITORING_STATE.get('thread') and MONITORING_STATE['thread'].is_alive()):
                MONITORING_STATE['stop_event'].clear()
                thread = threading.Thread(target=lightning_monitoring, daemon=True)
                MONITORING_STATE['thread'] = thread
                thread.start()
                app.logger.info("Auto-starting lightning monitoring.")

def cleanup_on_exit():
    print("\nShutting down...")
    app.logger.info("Shutdown initiated. Cleaning up resources.")
    MONITORING_STATE['stop_event'].set()
    cleanup_alert_timers()
    with MONITORING_STATE['lock']:
        if MONITORING_STATE.get('noise_revert_timer'):
            MONITORING_STATE['noise_revert_timer'].cancel()
    thread = MONITORING_STATE.get('thread')
    if thread and thread.is_alive():
        thread.join(timeout=5)
    print("Cleanup complete.")

if __name__ == '__main__':
    load_and_configure()
    atexit.register(cleanup_on_exit)
    start_monitoring_on_boot()
    is_debug_mode = get_config_boolean('SYSTEM', 'debug', False)
    if is_debug_mode:
        app.logger.warning("Application is running in DEBUG mode. Do not use in production.")
    app.run(host='0.0.0.0', port=5000, debug=is_debug_mode)
