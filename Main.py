import os
from time import sleep
from gpiozero import Button
from gpiozero import LED
from datetime import datetime
from MAX30102 import start_monitoring, stop_monitoring

"""
 Run these commands in the command prompt to set up the necessary keys

    Run this for the auth code but put the app key in place of "YOUR_APP_KEY"
    https://www.dropbox.com/oauth2/authorize?client_id= YOUR_APP_KEY &response_type=code&token_access_type=offline

    Then run this in the terminal replacing YOUR_CODE with the authorization code and YOUR_APP_SECRET with the  dropbox app secret
    curl https://api.dropbox.com/oauth2/token \
     -d code=YOUR_CODE \
     -d grant_type=authorization_code \
     -u YOUR_APP_KEY:YOUR_APP_SECRET

    Then run this command in the terminal using the refresh token provided above
    export DROPBOX_REFRESH_TOKEN="REFRESH_TOKEN"

    This will feed the required info needed to export information to dropbox

"""

APP_KEY = os.environ["DROPBOX_APP_KEY"]
APP_SECRET = os.environ["DROPBOX_APP_SECRET"]
REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]

LOG_FILE = "Saved_data.txt"
DROPBOX_FILE = "/Saved_data.txt"

Red_LED = LED(16)
Green_LED = LED(17)

button = Button(20)
monitor = start_monitoring()

# Sends the saved data to the txt file
def button_pressed_action():
    timestamp = datetime.now().strftime("%Y-%m=%d $H:$M:%S")
    bpm = monitor.bpm if monitor.bpm is not None else "no reading"
    spo2 = monitor.spo2 if monitor.spo2 is not None else "no reading"

    with open(LOG_FILE, "w") as f:
        f.write(f"Collected at {timestamp}\n")
        f.write(f"BPM: {bpm}  SpO2: {spo2}\n")
    print(f"Saved: BPM={bpm}  SpO2={spo2} at {timestamp}")


# Uploads the txt file to dropbox
def on_button_release():
    try:
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=REFRESH_TOKEN,
            app_key=APP_KEY,
            app_secret=APP_SECRET,
        )
        with open(LOG_FILE, "rb") as f:
            dbx.files_upload(
                f.read(),
                DROPBOX_FILE,
                mode=dropbox.files.WriteMode("overwrite"),
            )
        print("Cloud sync successful")
    except Exception as e:
        print("Cloud sync failed")


button.when_pressed = button_pressed_action
button.when_released = on_button_release

# Starts the LED monitoring
try:
    while True:
        bpm = monitor.bpm
        spo2 = monitor.spo2

        if bpm is None and spo2 is None:
            Red_LED.off()
            Green_LED.off()
        elif bpm is not None and not (50 >= bpm >= 100):
            Red_LED.on()
            Green_LED.off()
        elif spo2 is not None and not (spo2 >= 92):
            Red_LED.on()
            Green_LED.off()
        else:
            Green_LED.on()
            Red_LED.off()
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    Red_LED.off()
    Green_LED.off()
    stop_monitoring()
    