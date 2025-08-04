#!/usr/bin/env python3
"""Test GPIO access for Lightning Detector"""
import sys
import os

print("Testing GPIO access...")
print(f"Running as user: {os.getuid()} (UID), {os.getgid()} (GID)")
print(f"Groups: {os.getgroups()}")

try:
    import RPi.GPIO as GPIO
    print("✓ RPi.GPIO module imported successfully")
    
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    
    # Test setting up a pin
    test_pin = 2
    GPIO.setup(test_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"✓ Successfully configured GPIO pin {test_pin}")
    
    # Test edge detection
    GPIO.add_event_detect(test_pin, GPIO.FALLING, bouncetime=20)
    print(f"✓ Successfully added edge detection to pin {test_pin}")
    
    GPIO.remove_event_detect(test_pin)
    GPIO.cleanup()
    print("✓ GPIO cleanup successful")
    
    print("\n✅ All GPIO tests passed!")
    
except Exception as e:
    print(f"\n❌ GPIO test failed: {e}")
    print("\nTroubleshooting:")
    print("1. Make sure you've rebooted after running setup.sh")
    print("2. Ensure you're in the gpio group: groups $USER")
    print("3. Check /dev/gpiomem permissions: ls -la /dev/gpiomem")
    print("4. Try running with: sudo python3 test_gpio.py (for testing only)")
    sys.exit(1)
