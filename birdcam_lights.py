from gpiozero import LED, LightSensor
from signal import pause

# GPIO17 drives the transistor switch controlling the 6 IR LEDs
ir_lights = LED(17)

# GPIO18 reads the LDR + capacitor light sensor
# threshold: how bright is "light" vs "dark" (0.0-1.0 scale, tune if needed)
# queue_len: averages several readings together to smooth out noisy transitions
sensor = LightSensor(18, queue_len=5, threshold=0.1, charge_time_limit = 1)

# React to light level changes as they happen
sensor.when_dark = ir_lights.on
sensor.when_light = ir_lights.off

# Set the correct starting state immediately, rather than waiting
# for the next light/dark transition to happen
if sensor.value > sensor.threshold:
    ir_lights.off()
else:
    ir_lights.on()

# Keep the script running so it keeps reacting to sensor events
pause()
