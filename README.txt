
ENG 103 Task 3 Group 14 Implementation

Setup on the Raspberry Pi

Run the following:
- sudo raspi-config                 # Interface Options -> I2C -> Enable, then reboot
- sudo apt install -y python3-smbus i2c-tools
- pip3 install smbus2 dropbox --break-system-packages
- pip3 install adafruit-circuitpython-ht16k33 --break-system-packages (if using the display)
- i2cdetect -y 1 


 Run these commands in the command prompt to set up the necessary keys for Main.py

    Run this for the auth code but put the app key in place of "YOUR_APP_KEY"
    https://www.dropbox.com/oauth2/authorize?client_id=[APP_KEY]&response_type=code&token_access_type=offline

    Then run this in the terminal replacing YOUR_CODE with the authorization code, APP_KEY with the dropbox app key and YOUR_APP_SECRET with the  dropbox app secret
    curl https://api.dropbox.com/oauth2/token -d code=[AUTH CODE] -d grant_type=authorization_code -d client_id=[APP KEY] -d client_secret=[APP KEY] | python3 -c "import sys,json; d=json.load(sys.stdin); print(list(d.keys())); print(d.get('refresh_token'))"

    Then run these command in the terminal using the refresh token provided above
    export DROPBOX_REFRESH_TOKEN="REFRESH_TOKEN"
    export DROPBOX_APP_KEY="APP KEY"
    export DROPBOX_APP_SECRET="APP SECRET"

    This will feed the required info needed to export information to dropbox


Raspberry Pi Wiring Setup

The MAX30102 requires the following connections:
 - The VIN connected to Pin 1 (3.3V)
 - The GND connected to Pin 6 (GND)
 - The SDA connected to Pin 3 (GPIO 2)
 - The SCL connected to Pin 5 (GPIO 3)

The 4 Digit Display requires the following connections (optional):
 - The VIN connected to Pin 17 (3.3V)
 - The GND connected to Pin 20 (GND)
 - The SDA connected to Pin 3 (GPIO2)
 - The SCL connected to Pin 5 (GPIO3)

Due to the Display and MAX30102 using the same pins plug both pins into a pin board then place additional
jumper wires in the same row and connect them to their respective component

 The rest of the Pins should remain unused

 The Red LED requires the following connections:
 - The Long Node connected to Pin 36 (GPIO 16) connected via a 330Ω resistor
 - The Short Node connected to a Ground Pin
 
 The Green LED requires the following connections:
 - The Long Node connected to Pin 11 (GPIO 17) connected via a 330Ω resistor
 - The Short Node connected to a Ground Pin

 The Button requires the following connections:
 - Either pin connected to Pin 38 (GPIO 20)
 - The other pin connected to a Ground Pin

LED meaning
 - Green: BPM is within 50–100 and SpO2 is 92% or higher
 - Red: BPM is outside 50–100 or SpO2 is below 92%
 - Both off: No usable reading (no finger on the sensor, or still warming up)

Testing:

Run i2cdetect -y 1 before each use of Main.py or any other file that uses an I2C connection to make sure 0x57 and 
0x70(if using the display) show up, if they are both connected but one is not getting powered it will return an error


Run python Button_test.py  to test if the LEDs and Button have been connected correctly
and will start the flashing the Red and Green LEDs, the command propt will print 
"Button is pressed" when the button has been pressed.

Run python MAX30102.py to ensure the pins are set up correctly and the I2C is working properly
If its not feeding out results run i2cdetect -y 1 and check if 57 shows up and its recieving a 
connection, if its not then check the pin connection and that its getting enough power to run
