from gpiozero import LED
from gpiozero import Button
import time

""" 
This file exists to test that the LED and the Button are set up properly

The Red LED should be set to GPIO pin 16

The Green LED should be set to GPIO pin 17

The Button should be set to GPIO pin 20
"""

# Setting up the which LED is set to what
Red = LED(16)
Green = LED(17)
button = Button(20)



while True:

    # LED Testing
    Red.on()
    Green.on()
    time.sleep(1)
    Red.off()
    Green.off()
    time.sleep(1)

    # Button Test
    if button.is_pressed:
        print("Button is pressed")
        time.sleep(0.5)


