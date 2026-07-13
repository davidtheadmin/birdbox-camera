import RPi.GPIO as GPIO
import time

PIN = 18
GPIO.setmode(GPIO.BCM)

def read_ldr():
    count = 0
    # drain the capacitor
    GPIO.setup(PIN, GPIO.OUT)
    GPIO.output(PIN, GPIO.LOW)
    time.sleep(0.1)
    # time the charge
    GPIO.setup(PIN, GPIO.IN)
    while GPIO.input(PIN) == GPIO.LOW:
        count += 1
    return count

try:
    while True:
        print(read_ldr())
        time.sleep(0.5)
except KeyboardInterrupt:
    GPIO.cleanup()
