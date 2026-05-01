import io
import json
import os
import stat
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import pyotp
import requests
from bs4 import BeautifulSoup as soup
from kiteconnect import KiteConnect


AUTH_START_TIME = "07:30"
AUTH_RETRY_SLEEP_SECONDS = 30
CHECK_INSTRUMENT_TOKEN = "408065"

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
if not (APP_ROOT / "config").exists() and (REPO_ROOT / "config").exists():
    APP_ROOT = REPO_ROOT

CONFIG_DIR = APP_ROOT / "config"
DATA_DIR = APP_ROOT / "data"
CREDS_FILE_PATH = CONFIG_DIR / "kite_credentials.json"
AUTH_FILE_PATH = CONFIG_DIR / "auth.txt"
LEVERAGE_FILE_PATH = DATA_DIR / "Zerodha_MIS_Leverage.csv"
INSTRUMENT_TOKENS_FILE_PATH = DATA_DIR / "instrument_tokens.csv"
NIFTY50_FILE_PATH = DATA_DIR / "ind_nifty50list.csv"

ZERODHA_LOGIN_URL = "https://kite.zerodha.com/api/login"
ZERODHA_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
ZERODHA_LEVERAGE_URL = "https://zerodha.com/margin-calculator/Equity/"
NSE_HOME_URL = "https://www.nseindia.com/"
NIFTY50_CSV_URLS = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NSE_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": NSE_HOME_URL,
    "Accept": "text/csv,application/csv,text/plain,*/*",
}


def secure_file_permissions(path):
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


@lru_cache(maxsize=1)
def load_creds():
    secure_file_permissions(CREDS_FILE_PATH)
    with CREDS_FILE_PATH.open("r", encoding="utf-8") as f:
        creds = json.load(f)

    required_keys = {"user_id", "password", "totp_key", "api_key", "api_secret"}
    missing_keys = sorted(required_keys - set(creds))
    if missing_keys:
        raise KeyError(f"Missing keys in {CREDS_FILE_PATH}: {', '.join(missing_keys)}")

    return creds


def make_session(headers=None):
    session = requests.Session()
    session.headers.update(headers or DEFAULT_HEADERS)
    return session


def write_auth_file(api_key, access_token):
    AUTH_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE_PATH.write_text(f"{api_key},{access_token}", encoding="utf-8")
    secure_file_permissions(AUTH_FILE_PATH)


def get_request_token_from_url(url):
    return parse_qs(urlparse(url).query).get("request_token", [None])[0]


def resolve_request_token(session, login_url, max_redirects=5):
    current_url = login_url

    for _ in range(max_redirects):
        request_token = get_request_token_from_url(current_url)
        if request_token:
            return request_token

        response = session.get(current_url, allow_redirects=False, timeout=20)
        redirect_url = response.headers.get("Location")
        if not redirect_url:
            request_token = get_request_token_from_url(response.url)
            if request_token:
                return request_token
            raise ValueError(f"Could not find request_token in redirect URL: {response.url}")

        current_url = urljoin(current_url, redirect_url)

    raise ValueError(f"Could not find request_token after {max_redirects} redirects")


def fetch_request_token(creds, max_attempts=10):
    for attempt in range(max_attempts):
        try:
            session = make_session()
            response = session.post(
                ZERODHA_LOGIN_URL,
                data={"user_id": creds["user_id"], "password": creds["password"]},
                timeout=20,
            )
            response.raise_for_status()

            request_id = response.json()["data"]["request_id"]
            twofa_response = session.post(
                ZERODHA_TWOFA_URL,
                data={
                    "user_id": creds["user_id"],
                    "request_id": request_id,
                    "twofa_value": pyotp.TOTP(creds["totp_key"]).now(),
                    "twofa_type": "totp",
                },
                timeout=20,
            )
            twofa_response.raise_for_status()

            kite = KiteConnect(api_key=creds["api_key"])
            request_token = resolve_request_token(session, kite.login_url())
            print("Successful login with request token")
            return request_token
        except Exception as e:
            print("main_token_request_error", e)
            if attempt == max_attempts - 1:
                raise
            time.sleep(180)


def refresh_auth_file():
    creds = load_creds()
    request_token = fetch_request_token(creds)

    kite = KiteConnect(api_key=creds["api_key"])
    data = kite.generate_session(request_token, creds["api_secret"])
    access_token = data["access_token"]
    write_auth_file(creds["api_key"], access_token)

    kite.set_access_token(access_token)
    return kite


def verify_auth(kite):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=10)
    candles = kite.historical_data(CHECK_INSTRUMENT_TOKEN, start_date, end_date, "30minute")
    if not candles:
        raise RuntimeError("Auth verification returned no historical data")
    return True


def auth():
    while True:
        try:
            kite = refresh_auth_file()
            verify_auth(kite)
            print("Auth Done")
            return kite
        except Exception as e:
            print(e, "Auth error")
            time.sleep(AUTH_RETRY_SLEEP_SECONDS)


def parse_leverage_table(html):
    page_soup = soup(html, "html.parser")
    scrip_cells = page_soup.find_all("td", class_="scrip border-right")
    mis_cells = page_soup.find_all("td", class_="mis_multiplier border-right")
    rows = [
        (
            scrip_cell.get_text(strip=True).split(":")[0],
            mis_cell.get_text(strip=True).replace("x", ""),
        )
        for scrip_cell, mis_cell in zip(scrip_cells, mis_cells)
    ]
    if rows:
        return pd.DataFrame(rows, columns=["Scrip", "MIS Leverage"]).drop_duplicates()

    for table in page_soup.find_all("table"):
        table_rows = table.find_all("tr")
        if not table_rows:
            continue

        headers = [
            cell.get_text(strip=True).lower()
            for cell in table_rows[0].find_all(["th", "td"])
        ]
        scrip_index = next((i for i, header in enumerate(headers) if "scrip" in header), None)
        mis_index = next(
            (
                i
                for i, header in enumerate(headers)
                if "mis" in header and ("leverage" in header or "multiplier" in header)
            ),
            None,
        )
        if scrip_index is None or mis_index is None:
            continue

        parsed_rows = []
        for row in table_rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(scrip_index, mis_index):
                continue

            parsed_rows.append(
                (
                    cells[scrip_index].get_text(strip=True).split(":")[0],
                    cells[mis_index].get_text(strip=True).replace("x", ""),
                )
            )

        if parsed_rows:
            return pd.DataFrame(parsed_rows, columns=["Scrip", "MIS Leverage"]).drop_duplicates()

    return pd.DataFrame(columns=["Scrip", "MIS Leverage"])


def get_leverage(output_path=LEVERAGE_FILE_PATH):
    try:
        response = make_session(DEFAULT_HEADERS).get(ZERODHA_LEVERAGE_URL, timeout=30)
        response.raise_for_status()

        symbol_leverages = parse_leverage_table(response.text)
        if symbol_leverages.empty:
            raise RuntimeError("No leverage rows found")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        symbol_leverages.to_csv(output_path, index=False)
        print("Leverages_updated")
        return symbol_leverages
    except Exception as e:
        print(e, "Leverage error")
        return pd.DataFrame(columns=["Scrip", "MIS Leverage"])


def gettoken(kite, output_path=INSTRUMENT_TOKENS_FILE_PATH):
    try:
        df = pd.DataFrame(kite.instruments())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        print("Tokens_updated")
        return df
    except Exception as e:
        print(e, "Token fetch error")
        return pd.DataFrame()


def getnifty50(output_path=NIFTY50_FILE_PATH):
    session = make_session(NSE_HEADERS)

    try:
        session.get(NSE_HOME_URL, timeout=10)
    except Exception as e:
        print(e, "NSE session warmup error")

    for csv_url in NIFTY50_CSV_URLS:
        try:
            response = session.get(csv_url, timeout=20)
            response.raise_for_status()
            nifty50 = pd.read_csv(io.StringIO(response.text))

            if nifty50.empty or "Symbol" not in nifty50.columns:
                raise RuntimeError(f"Unexpected Nifty50 CSV format from {csv_url}")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            nifty50.to_csv(output_path, index=False)
            print("Nifty50 updated")
            return nifty50
        except Exception as e:
            print(e, "Nifty50 fetch error")

    return pd.DataFrame()


def getcash(kite):
    try:
        margins = kite.margins()
        equity_available = margins["equity"]["available"]
        cash = float(equity_available.get("cash", 0)) + float(
            equity_available.get("intraday_payin", 0)
        )
        print(cash)
        return cash
    except Exception as e:
        print(e, "Zerodha cash error")
        return 0


if __name__ == "__main__":
    print("Start Auth")
    while True:
        now_sec = time.localtime().tm_sec
        time.sleep(60 - now_sec)
        now = datetime.now()

        if now.strftime("%H:%M") == AUTH_START_TIME:
            print(now)
            kite_client = auth()
            #get_leverage()
            gettoken(kite_client)
            getcash(kite_client)
            getnifty50()
            print(datetime.now())
            print("====xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx======")
