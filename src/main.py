import network
import urequests
import ujson
import utime
import socket
from machine import Pin, I2C

from lcd import Lcd_i2c


# ---------------------------
# Logging
# ---------------------------

LOG_FILE = "log.txt"

def log(msg):
    ts = utime.ticks_ms()
    line = "[{} ms] {}".format(ts, msg)
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------
# Helpers
# ---------------------------

def read_config(path="/CONFIGURATION.txt"):
    with open(path, "r") as f:
        return ujson.loads(f.read())

def safe_get(dct, path, default=None):
    cur = dct
    try:
        for key in path:
            cur = cur[key]
        return cur
    except Exception:
        return default

def clamp_str(s, max_len):
    if s is None:
        s = ""
    s = str(s)
    if len(s) > max_len:
        return s[:max_len]
    return s

def is_number(x):
    try:
        float(x)
        return True
    except Exception:
        return False

def pad_right(s, width):
    s = "" if s is None else str(s)
    if len(s) > width:
        return s[:width]
    return s + (" " * (width - len(s)))

def get_time_string():
    """Return current time as HH:MM:SS string"""
    t = utime.localtime()
    return "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])

def lcd_write_lines(lcd, cols, line0, line1):
    l0 = pad_right(line0, cols)
    l1 = pad_right(line1, cols)

    lcd.set_cursor(0, 0)
    lcd.write(l0)
    lcd.set_cursor(0, 1)
    lcd.write(l1)

def log_netinfo(wlan):
    try:
        ip, mask, gw, dns = wlan.ifconfig()
        log("IFCONFIG ip={} mask={} gw={} dns={}".format(ip, mask, gw, dns))
    except Exception as e:
        log("ifconfig read failed: {}".format(e))

def dns_test(host, port=80):
    try:
        ai = socket.getaddrinfo(host, port)
        ip = ai[0][4][0]
        log("DNS OK {} -> {}".format(host, ip))
        return True
    except Exception as e:
        log("DNS FAIL {} -> {}".format(host, e))
        return False


# ---------------------------
# WiFi
# ---------------------------

def wifi_connect(wlan, ssid, password, lcd=None, cols=16, max_wait_s=20):
    if not wlan.active():
        wlan.active(True)

    if wlan.isconnected():
        return True

    log("WiFi connect() -> ssid='{}'".format(ssid))
    wlan.connect(ssid, password)

    start = utime.ticks_ms()
    while not wlan.isconnected():
        if lcd:
            try:
                lcd_write_lines(lcd, cols, "Connecting WiFi", "Please wait...")
            except Exception as e:
                log("LCD write failed (wifi loop): {}".format(e))
        if utime.ticks_diff(utime.ticks_ms(), start) > max_wait_s * 1000:
            log("WiFi connect TIMEOUT")
            return False
        utime.sleep_ms(300)

    ip = wlan.ifconfig()[0]
    log("WiFi connected, IP={}".format(ip))

    # DHCP/DNS sometimes needs a moment
    log_netinfo(wlan)
    utime.sleep(1)
    dns_test("ip-api.com", 80)
    dns_test("api.openweathermap.org", 443)

    return True

def wifi_ensure_connected(wlan, ssid, password, lcd=None, cols=16):
    if wlan.isconnected():
        return True

    for attempt in range(1, 4):
        log("WiFi reconnect attempt {}".format(attempt))
        ok = wifi_connect(wlan, ssid, password, lcd=lcd, cols=cols, max_wait_s=20)
        if ok:
            return True
        utime.sleep_ms(500)

    log("WiFi hard reset interface")
    try:
        wlan.disconnect()
    except Exception:
        pass

    utime.sleep_ms(200)
    wlan.active(False)
    utime.sleep_ms(200)
    wlan.active(True)
    utime.sleep_ms(200)

    return wifi_connect(wlan, ssid, password, lcd=lcd, cols=cols, max_wait_s=25)


# ---------------------------
# HTTP / APIs
# ---------------------------

def http_get_json(url, timeout_s=10):
    r = None
    try:
        log("HTTP GET {}".format(url))
        try:
            r = urequests.get(url, timeout=timeout_s)
        except TypeError:
            r = urequests.get(url)

        log("HTTP status {}".format(r.status_code))
        if r.status_code != 200:
            return None, "HTTP {}".format(r.status_code)

        try:
            return r.json(), None
        except Exception:
            return None, "Bad JSON"
    except Exception as e:
        return None, "NET {}".format(e)
    finally:
        try:
            if r:
                r.close()
        except Exception:
            pass

def get_geo_by_ip():
    # Primary (documented free endpoint):
    # http://ip-api.com/json/
    data, err = http_get_json("http://ip-api.com/json/")
    if err or not data:
        log("Geo primary failed: {}".format(err))

        # Fallback by IP (diagnostic fallback; IP can change over time):
        # If DNS is the issue, this can still work.
        data2, err2 = http_get_json("http://208.95.112.1/json/")
        if err2 or not data2:
            return None, (err2 or err or "No data")
        data = data2

    lat = data.get("lat")
    lon = data.get("lon")
    city = data.get("city")
    country = data.get("countryCode") or data.get("country")

    if not is_number(lat) or not is_number(lon):
        return None, "Missing lat/lon"

    geo = {"lat": float(lat), "lon": float(lon), "city": city, "country": country}
    log("GEO OK lat={} lon={} city={} country={}".format(geo["lat"], geo["lon"], city, country))
    return geo, None

def get_weather_openweathermap(api_key, lat, lon):
    # OpenWeatherMap Current Weather endpoint:
    # https://api.openweathermap.org/data/2.5/weather?lat=...&lon=...&appid=...&units=metric&lang=cs
    url = (
        "https://api.openweathermap.org/data/2.5/weather"
        "?lat={}&lon={}&appid={}&units=metric&lang=cs"
    ).format(lat, lon, api_key)

    data, err = http_get_json(url)
    if err or not data:
        return None, err or "No data"

    main = data.get("main", {})
    wind = data.get("wind", {})
    w_arr = data.get("weather", [])

    temp = main.get("temp")
    humidity = main.get("humidity")
    pressure = main.get("pressure")
    wind_speed = wind.get("speed")

    desc = ""
    if isinstance(w_arr, list) and len(w_arr) > 0:
        desc = w_arr[0].get("description") or ""

    if not is_number(temp):
        return None, "Bad temp"

    weather = {
        "temp": float(temp),
        "humidity": int(humidity) if is_number(humidity) else None,
        "pressure": int(pressure) if is_number(pressure) else None,
        "wind": float(wind_speed) if is_number(wind_speed) else None,
        "desc": desc
    }
    log("WEATHER OK temp={} desc='{}'".format(weather["temp"], desc))
    return weather, None


# ---------------------------
# Display logic
# ---------------------------

def show_coords(lcd, cols, lat, lon, seconds=3):
    try:
        lcd.clear()
        lcd_write_lines(lcd, cols, "Coords lat/lon", "{} {}".format(round(lat, 4), round(lon, 4)))
        log("LCD: showed coords")
    except Exception as e:
        log("LCD show_coords failed: {}".format(e))
    utime.sleep(seconds)

def show_error(lcd, cols, msg, seconds=2):
    log("ERROR: {}".format(msg))
    try:
        if lcd:
            lcd.clear()
            lcd_write_lines(lcd, cols, "ERROR", clamp_str(msg, cols))
    except Exception as e:
        log("LCD show_error failed: {}".format(e))
    utime.sleep(seconds)

def show_weather_cycle(lcd, cols, geo, weather):
    city = geo.get("city") or "Unknown"
    country = geo.get("country") or ""
    loc = (city + " " + country).strip()

    temp = weather.get("temp")
    hum = weather.get("humidity")
    wind = weather.get("wind")
    pres = weather.get("pressure")
    desc = weather.get("desc") or ""

    try:
        lcd.clear()
        time_str = get_time_string()
        lcd_write_lines(lcd, cols, "Time", time_str)
    except Exception as e:
        log("LCD weather time screen failed: {}".format(e))
    utime.sleep(2)

    try:
        lcd.clear()
        lcd_write_lines(lcd, cols, clamp_str(loc, cols), "Temp: {}C".format(round(temp, 1)))
    except Exception as e:
        log("LCD weather screen1 failed: {}".format(e))
    utime.sleep(2)

    try:
        lcd.clear()
        hum_txt = "Hum: --%" if hum is None else "Hum: {}%".format(hum)
        lcd_write_lines(lcd, cols, clamp_str(desc, cols), hum_txt)
    except Exception as e:
        log("LCD weather screen2 failed: {}".format(e))
    utime.sleep(2)

    try:
        lcd.clear()
        wind_txt = "Wind: -- m/s" if wind is None else "Wind: {} m/s".format(round(wind, 1))
        pres_txt = "Pres: ----hPa" if pres is None else "Pres: {} hPa".format(pres)
        lcd_write_lines(lcd, cols, wind_txt, pres_txt)
    except Exception as e:
        log("LCD weather screen3 failed: {}".format(e))
    utime.sleep(2)


# ---------------------------
# Main
# ---------------------------

def main():
    # start fresh log
    try:
        with open(LOG_FILE, "w") as f:
            f.write("")
    except Exception:
        pass

    log("BOOT")

    try:
        log("Reading config...")
        cfg = read_config()
        log("Config read OK")
    except Exception as e:
        log("Config read error: {}".format(e))
        raise
    
    ssid = safe_get(cfg, ["wifi", "ssid"], "")
    password = safe_get(cfg, ["wifi", "password"], "")
    api_key = safe_get(cfg, ["openweathermap", "api_key"], "")

    lcd_cfg = cfg.get("lcd", {})
    i2c_id = int(lcd_cfg.get("i2c_id", 0))
    sda_pin = int(lcd_cfg.get("sda_pin", 0))
    scl_pin = int(lcd_cfg.get("scl_pin", 1))
    cols = int(lcd_cfg.get("cols", 16))
    rows = int(lcd_cfg.get("rows", 2))

    log("Config: i2c_id={} sda={} scl={} cols={} rows={}".format(i2c_id, sda_pin, scl_pin, cols, rows))

    try:
        log("Creating pins: sda={} scl={}".format(sda_pin, scl_pin))
        sda = Pin(sda_pin)
        scl = Pin(scl_pin)
        log("Pins created OK, initializing I2C...")
        i2c = I2C(i2c_id, sda=sda, scl=scl, freq=50000)
        log("I2C initialized OK")
    except Exception as e:
        log("I2C init error: {} {}".format(type(e).__name__, e))
        raise

    # I2C scan debug
    devices = []
    try:
        devices = i2c.scan()
        log("I2C scan: {}".format([hex(d) for d in devices]))
    except Exception as e:
        log("I2C scan failed: {}".format(e))

    # init LCD
    lcd = None
    try:
        lcd = Lcd_i2c(i2c, cols=cols, rows=rows)
        log("LCD init OK")
        lcd.clear()
        lcd_write_lines(lcd, cols, "BOOT OK", "I2C: " + (hex(devices[0]) if devices else "none"))
        utime.sleep(1)
    except Exception as e:
        log("LCD init FAILED: {}".format(e))
        lcd = None

    wlan = network.WLAN(network.STA_IF)

    # Boot message
    if lcd:
        try:
            lcd.clear()
            lcd_write_lines(lcd, cols, "Connecting to", "WiFi...")
        except Exception as e:
            log("LCD boot msg failed: {}".format(e))

    if not wifi_ensure_connected(wlan, ssid, password, lcd=lcd, cols=cols):
        show_error(lcd, cols, "WiFi failed", seconds=2)
        while True:
            if wifi_ensure_connected(wlan, ssid, password, lcd=lcd, cols=cols):
                break
            utime.sleep(2)

    # Geo after connect
    geo, err = get_geo_by_ip()
    if err or not geo:
        show_error(lcd, cols, err or "Geo failed", seconds=2)
        for i in range(5):
            log("Geo retry {}".format(i + 1))
            utime.sleep(2)
            geo, err = get_geo_by_ip()
            if geo:
                break

    if not geo:
        while True:
            show_error(lcd, cols, "No GEO data", seconds=2)
            wifi_ensure_connected(wlan, ssid, password, lcd=lcd, cols=cols)
            utime.sleep(2)

    show_coords(lcd, cols, geo["lat"], geo["lon"], seconds=3)

    last_weather = None
    interval_ms = 10 * 60 * 1000
    next_fetch = utime.ticks_ms()

    heartbeat = 0

    while True:
        wifi_ensure_connected(wlan, ssid, password, lcd=lcd, cols=cols)

        now = utime.ticks_ms()
        if utime.ticks_diff(now, next_fetch) >= 0:
            log("Weather fetch start")
            w, werr = get_weather_openweathermap(api_key, geo["lat"], geo["lon"])
            if werr or not w:
                log("Weather fetch failed: {}".format(werr))
                if last_weather and lcd:
                    try:
                        lcd.clear()
                        lcd_write_lines(lcd, cols, "API warn", "using cached")
                        utime.sleep(2)
                    except Exception as e:
                        log("LCD warn msg failed: {}".format(e))
                else:
                    show_error(lcd, cols, werr or "Weather fail", seconds=2)
            else:
                last_weather = w

            next_fetch = utime.ticks_add(now, interval_ms)
            log("Next fetch in 10min")

        if last_weather and lcd:
            show_weather_cycle(lcd, cols, geo, last_weather)
        else:
            heartbeat += 1
            log("Heartbeat {}".format(heartbeat))
            if lcd:
                try:
                    lcd.clear()
                    lcd_write_lines(lcd, cols, "Running...", "HB {}".format(heartbeat))
                except Exception as e:
                    log("LCD heartbeat failed: {}".format(e))
            utime.sleep(1)

        utime.sleep_ms(100)


try:
    main()
except Exception as e:
    log("FATAL: {}".format(e))
    while True:
        utime.sleep(1)
