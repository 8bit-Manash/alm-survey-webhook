import os
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Allow ALM domain to call this endpoint
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://learningmanager.adobe.com"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ─── CONFIG FROM ENV VARS ─────────────────────────────────
ALM_OAUTH_URL = "https://learningmanager.adobe.com/oauth/token/refresh"
CLIENT_ID     = os.environ.get("ALM_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ALM_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("ALM_REFRESH_TOKEN")
# ──────────────────────────────────────────────────────────


# ─── GET FRESH ACCESS TOKEN ───────────────────────────────
def get_access_token():
    try:
        res = requests.post(
            ALM_OAUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
            }
        )
        data = res.json()
        if "access_token" not in data:
            raise Exception(f"Token refresh failed: {data}")
        return data["access_token"]
    except Exception as e:
        raise Exception(f"OAuth error: {str(e)}")


# ─── HEALTH CHECK ─────────────────────────────────────────
@app.get("/")
def health():
    return {
        "status":  "ok",
        "service": "ALM Token Service"
    }


# ─── TOKEN ENDPOINT ───────────────────────────────────────
# Called by ALM JS injection to get fresh access token
# Token lives server-side — never exposed in browser JS
@app.get("/get-token")
def get_token():
    try:
        token = get_access_token()
        return {"access_token": token}
    except Exception as e:
        print(f"[Token] ❌ Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
