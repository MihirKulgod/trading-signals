import os
import pytz
import pyotp
import requests

from dotenv import load_dotenv
from datetime import time, datetime
from kiteconnect import KiteConnect
from urllib.parse import urlparse, parse_qs

IST = pytz.timezone("Asia/Kolkata")
SAFE_CUTOFF = time(7, 30)

def needs_login() -> bool:
    load_dotenv()
    last_login_str = os.getenv("KITE_LOGIN_TIME")
    if not last_login_str:
        print("No previous login recorded.")
        return True

    last_login = datetime.fromisoformat(last_login_str)
    now = datetime.now(IST)

    if last_login.date() < now.date() or last_login.time() < SAFE_CUTOFF:
        print(f"Previous login was prior to cutoff time. [{last_login}]")
        return True

    print(f"Already logged in. [{last_login}]")
    return False

def login():
    try:
        session = requests.Session()
        load_dotenv()

        api_key = os.getenv("KITE_API_KEY")
        api_secret = os.getenv("KITE_API_SECRET")
        user_id = os.getenv("USER_ID")
        password = os.getenv("PASSWORD")
        totp_secret = os.getenv("TOTP_SECRET")

        login_resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password}
        ).json()

        request_id = login_resp["data"]["request_id"]

        totp_code = pyotp.TOTP(totp_secret).now()
        session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp"
            }
        )
        kite = KiteConnect(api_key=api_key)
        redirect_resp = session.get(kite.login_url(), allow_redirects=False)

        while redirect_resp.is_redirect or redirect_resp.is_permanent_redirect:
            next_url = redirect_resp.headers["location"]
            if "localhost" in next_url:
                break
            redirect_resp = session.get(next_url, allow_redirects=False)

        parsed = urlparse(next_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        data = kite.generate_session(request_token, api_secret)
        access_token = data["access_token"]

        login_time = datetime.now(IST).isoformat()
        print(f"Successfully authenticated for the day! [{login_time}]")

        with open(".env", "w") as f:
            f.write(f"KITE_API_KEY={api_key}\n")
            f.write(f"KITE_API_SECRET={api_secret}\n")
            f.write(f"KITE_ACCESS_TOKEN={access_token}\n")
            f.write(f"USER_ID={user_id}\n")
            f.write(f"PASSWORD={password}\n")
            f.write(f"TOTP_SECRET={totp_secret}\n")
            f.write(f"KITE_LOGIN_TIME={login_time}")
    except Exception as e:
        print(f"Login attempt failed:\n{e}")

def get_kite() -> KiteConnect:
    if needs_login():
        login()
    load_dotenv(override=True)
    kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
    kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN"))
    return kite