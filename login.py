import pytz
import pyotp
import requests

from datetime import time, datetime
from kiteconnect import KiteConnect
from urllib.parse import urlparse, parse_qs

import secrets_store
from app_logging import get_logger

log = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
SAFE_CUTOFF = time(7, 30)

def needs_login() -> bool:
    last_login_str = secrets_store.read_session().get("KITE_LOGIN_TIME")
    if not last_login_str:
        log.info("no previous login recorded")
        return True

    last_login = datetime.fromisoformat(last_login_str)
    now = datetime.now(IST)

    if last_login.date() < now.date() or last_login.time() < SAFE_CUTOFF:
        log.info("previous login was before today's cutoff [%s]", last_login)
        return True

    log.info("already logged in [%s]", last_login)
    return False

def login():
    credentials = secrets_store.require_credentials()
    api_key = credentials["KITE_API_KEY"]
    api_secret = credentials["KITE_API_SECRET"]

    session = requests.Session()
    login_resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": credentials["USER_ID"], "password": credentials["PASSWORD"]},
    ).json()

    request_id = login_resp["data"]["request_id"]
    totp_code = pyotp.TOTP(credentials["TOTP_SECRET"]).now()
    session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": credentials["USER_ID"],
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
        },
    )

    kite = KiteConnect(api_key=api_key)
    redirect_resp = session.get(kite.login_url(), allow_redirects=False)

    while redirect_resp.is_redirect or redirect_resp.is_permanent_redirect:
        next_url = redirect_resp.headers["location"]
        if "localhost" in next_url:
            break
        redirect_resp = session.get(next_url, allow_redirects=False)

    request_token = parse_qs(urlparse(next_url).query)["request_token"][0]
    data = kite.generate_session(request_token, api_secret)

    login_time = datetime.now(IST).isoformat()
    secrets_store.write_session(
        KITE_ACCESS_TOKEN=data["access_token"],
        KITE_LOGIN_TIME=login_time,
    )
    log.info("authenticated for the day [%s]", login_time)

def get_kite() -> KiteConnect:
    if needs_login():
        login()
    kite = KiteConnect(api_key=secrets_store.get_credential("KITE_API_KEY"))
    kite.set_access_token(secrets_store.read_session().get("KITE_ACCESS_TOKEN"))
    return kite
