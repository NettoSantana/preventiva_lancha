import os, time, hashlib, requests

BASE_URL = "https://gps.brasilsatgps.com.br"
ACCOUNT  = os.getenv("BRASILSAT_ACCOUNT", "nettosantana@icloud.com")
PASSWORD = os.getenv("BRASILSAT_PASSWORD", "1234567")
IMEI     = os.getenv("BRASILSAT_IMEI", "355468593059041")

def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def get_token():
    now = int(time.time())
    signature = md5(md5(PASSWORD) + str(now))
    url = f"{BASE_URL}/api/authorization"
    r = requests.get(url, params={"time": now, "account": ACCOUNT, "signature": signature}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"Auth falhou: {j}")
    return j["record"]["access_token"], j["record"]["expires_in"]

def track(access_token: str, imei: str):
    url = f"{BASE_URL}/api/track"
    r = requests.get(url, params={"access_token": access_token, "imeis": imei}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"Track falhou: {j}")
    return j["record"][0]

if __name__ == "__main__":
    token, exp = get_token()
    data = track(token, IMEI)

    resumo = {
        "imei": data.get("imei"),
        "accstatus": data.get("accstatus"),
        "acctime_s": data.get("acctime"),
        "externalpower_v": data.get("externalpower"),
        "servertime": data.get("servertime"),
    }
    print(resumo)
