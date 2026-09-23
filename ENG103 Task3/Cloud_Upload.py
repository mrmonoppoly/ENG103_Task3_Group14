import os
from time import sleep
from gpiozero import Button
from signal import pause
from datetime import datetime
from MAX30102.py import start_monitoring

button = Button(20)

ACCESS_TOKEN = "sl.u.AGyr4mvetkILWzBzHqKhqKmuEg6xUbhEA9IKuXHAFqST8AVwx3Qh_UcN8TO487zN-5_J8MMC555r0IcWP37DhKFVwcgHYI3nHNgwl9GnI5OwZZJ4To-dy78SySlHPWy0Cq9Ul3x0yfjl3qaxfAyaXf544Z9T5iAY3r5NqvM_hTy8bTj1A2JUn6vPyNjg6KxIyquQtJqckVQZgB52lm_d5qq9eyQvN4aOvD5z2452FlwCG739MBa-90KC90d1lW-5B_wR7Fm5ZtGr3vfImST1ALZ4JdwtDhKZtdDSdN7-1ANKM0Wai1aqnbu0BRVf5me9ERIN4OFlOZ-kSpi31pAux5bYQjFGjWRAz6iMkY-DXE-tHL7CuIPAa9ZfkbsbZvWv7fafYkoP9ZTL95TK3nTvMvu4HejAmp9kIjhVCC-UXCsCDB2Q-aG6XIdpk7I1gmbKL_uFlS_RPkUP8jGmFsvgNIqE4U_OIpCyzD8RhsY_iPTgk4ddYNbmX3LwnNiERjrp0ysmje-CN5is9latbWLnEvmWtYls9VaOKmeWipM4MrEMNAJmogyIpozaq2AuVVjsgz61am7KoNsgl23Hjrag-hBfaXjWOkhNTTrnGmQYbP-8uON3gi8J2MLNJyIKdibpRuMRJQhGrsk5xCB2zmgz1b5jCPrnSY2vZag--QcKN7OrrjvxIcWnpbqtqw6nETmOxRITFEgRN7TjAF8wVptU78RU6uXcWZZoUxHjbyXisRNQU4afWakyxnspjrNSvNNaynGy59jKFoA8lMW7aozUdyz-pq2UT5A5Zz6veSVv53Wrfwz_i94QBjrYmHcCi2ji7iUMMYVEFXqCvyd3ye3x313U2GSIpMDOOwWlTXEnt5Y-vO6nMwQ_geqwCH14XWNdk5zQf3BHnLdIBkLPl8y7zbzpNVCuLVsCazykI7QA_KVdAEQDh3aP9aH3xvZ-O1wg4xFk-vHq4fV-le9VtGqil1j_io-s_buy5S1ITdScibYLe4cXZ8lTCsijtscBW4fAEmdPH15zJpNMcpSqn5gxUWK7zCFdMOY30y8FroQVf5xcaq-G7oNukkVPA6zdws5fm1T8em-yMyu3OOsakVLbH2IMbwivghVa5fzfk7AqajYJd30lTq2-0SOJLHDSFNkXNsIb870mSIZIQQJW_Lc6HJdfIFHJ5QzFpyw-k_7hFjQYXaRmS2rj-wN9UbWfQbiocWjdEs1LbWaE7HtaV5v0cn8buIkxMEPc7FmY1HE_dGbTt1H6RTJK7urznzLSJsY_3yLLI_zpNoejIUJirwKFamaAANh-HX3565unqJ-qjON3esO55tp4k6KkjfqCI260C8d1-vtO9dHXcAiUa5XuQRDPXbAtnFjbAwPv011DLmR1LeNy7e4cukdHteeKLbPjvn7tGqhJf_Uz7W9cACbeSpbK_kZBQrqXB3t6Jukmwv_HMw"
LOG_FILE = "Saved_data.txt"
DROPBOX_FILE = "Saved_data.txt"


def button_pressed_action():
    raw_time = datetime.now()
    time = raw_time.strftime("%Y-%m-%D %H:%M:%S")
    with open("Saved_data.txt", "w") as file:
        file.write("Collected at ", time)
        file.write("Insert for actual data")


def on_button_release():
    try:
        dbx = dropbox.DROPBOX(ACCESS_TOKEN)
        with open("Saved_data.txt", "rb") as f:
            dbx.files_upload(f.read(), DROPBOX_FILE, mode = dropbox.files.WriteMode("overwrite"))
        print("Cloud sync complete")
    except Exception as e:
        print("Cloud sync failed")


button.when_pressed = button_pressed_action
button.when_released = on_button_release

pause()
    