from gpiozero import Button
from signal import pause
import time

# Use the GPIO pin you want to test.
# Use BCM numbering (e.g., 22 for GPIO22).
TEST_PIN = 22

print(f"--- GPIO Test Script ---")
print(f"Testing GPIO pin: {TEST_PIN}")

try:
    # A Button is a simple way to test a GPIO input and interrupts.
    # The pull_up=True setting is important.
    button = Button(TEST_PIN, pull_up=True)

    print(f"\n[SUCCESS] Successfully initialized GPIO {TEST_PIN}.")
    print("The pin is ready for input.")

    def button_pressed():
        print(f"[{time.strftime('%H:%M:%S')}] EVENT: Button PRESSED (edge detected)")

    def button_released():
        print(f"[{time.strftime('%H:%M:%S')}] EVENT: Button RELEASED")

    # These lines attach the functions to the interrupt events.
    # This is similar to what your lightning script is failing to do.
    button.when_pressed = button_pressed
    button.when_released = button_released

    print("\nMonitoring for events for 30 seconds...")
    print("You can test this by manually connecting GPIO 22 to a GND pin.")
    pause()

except Exception as e:
    print(f"\n[ERROR] An error occurred: {e}")
    print("\n--- Troubleshooting ---")
    print("1. Did you run this with 'sudo'?")
    print("2. Is the user in the 'gpio' group? (Run 'groups' to check)")
    print("3. Is another program or service using this pin?")

