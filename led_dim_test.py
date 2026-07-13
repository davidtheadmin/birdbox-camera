import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.OUT)

pwm = GPIO.PWM(17, 1000)   # 1000 Hz
pwm.start(100)             # start at full brightness

try:
    while True:
        duty = int(input("Brightness 0-100: "))
        pwm.ChangeDutyCycle(duty)
except KeyboardInterrupt:
    pwm.stop()
    GPIO.cleanup()
