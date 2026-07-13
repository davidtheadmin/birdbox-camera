import RPi.GPIO as GPIO
import time

LDR_PIN = 18
IR_PIN = 17

LEVEL_MIN = 800     # this bright or brighter -> LEDs fully off
LEVEL_MAX = 4000    # this dark or darker -> LEDs at MAX_BRIGHT
MAX_BRIGHT = 80     # cap duty cycle, tune on the camera image
SMOOTH = 0.3        # 0-1, higher = reacts faster, lower = smoother

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(IR_PIN, GPIO.OUT)
pwm = GPIO.PWM(IR_PIN, 1000)
pwm.start(0)

def read_ldr():
    count = 0
    GPIO.setup(LDR_PIN, GPIO.OUT)
    GPIO.output(LDR_PIN, GPIO.LOW)
    time.sleep(0.3)
    GPIO.setup(LDR_PIN, GPIO.IN)
    while GPIO.input(LDR_PIN) == GPIO.LOW and count < 100000:
        count += 1
    return count

def level_to_brightness(level):
    if level <= LEVEL_MIN:
        return 0
    if level >= LEVEL_MAX:
        return MAX_BRIGHT
    return MAX_BRIGHT * (level - LEVEL_MIN) / (LEVEL_MAX - LEVEL_MIN)

current = 0.0
try:
    while True:
        level = read_ldr()
        target = level_to_brightness(level)
        current += (target - current) * SMOOTH
        pwm.ChangeDutyCycle(current)
        print(f"level: {level:5d}  ->  brightness: {current:5.1f}%")
except KeyboardInterrupt:
    pwm.stop()
    GPIO.cleanup()