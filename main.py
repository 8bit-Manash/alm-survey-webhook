import os
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── CONFIG FROM ENV VARS ─────────────────────────────────
ALM_BASE_URL       = "https://learningmanager.adobe.com/primeapi/v2"
ALM_OAUTH_URL      = "https://learningmanager.adobe.com/oauth/token/refresh"
CLIENT_ID          = os.environ.get("ALM_CLIENT_ID")
CLIENT_SECRET      = os.environ.get("ALM_CLIENT_SECRET")
REFRESH_TOKEN      = os.environ.get("ALM_REFRESH_TOKEN")
SURVEY_COURSE_NAME = "Feedback Survey"
# ──────────────────────────────────────────────────────────


# ─── STEP 1: GET FRESH ACCESS TOKEN ──────────────────────
def get_access_token():
    try:
        print(f"[OAuth] Refreshing token...")
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
        print(f"[OAuth] Response: {res.status_code}")
        if "access_token" not in data:
            raise Exception(f"Token refresh failed: {data}")
        print(f"[OAuth] ✅ Access token refreshed successfully.")
        return data["access_token"]
    except Exception as e:
        raise Exception(f"OAuth error: {str(e)}")


# ─── STEP 2: FETCH LP NAME FROM ALM API ──────────────────
def get_lp_name(token, lp_id):
    try:
        res = requests.get(
            f"{ALM_BASE_URL}/learningObjects/{lp_id}",
            headers={
                "Authorization": f"oauth {token}",
                "Accept": "application/vnd.api+json"
            }
        )
        data = res.json()
        metadata = data.get("data", {}).get("attributes", {}).get("localizedMetadata", [])
        name = metadata[0].get("name", "") if metadata else ""
        print(f"[Webhook] LP Name fetched: {name}")
        return name
    except Exception as e:
        print(f"[Webhook] Failed to get LP name: {e}")
        return ""


# ─── STEP 3: CREATE SURVEY COURSE IN ALM ─────────────────
def create_survey_course(token, lp_name):
    headers = {
        "Authorization": f"oauth {token}",
        "Content-Type":  "application/vnd.api+json",
        "Accept":        "application/vnd.api+json"
    }

    # Decide survey type from LP name
    lp_lower = lp_name.lower()
    is_assessment = "assessment" in lp_lower or "certification" in lp_lower
    survey_type = "assessment" if is_assessment else "course"

    # Create course payload — correct ALM API format
    course_payload = {
        "data": {
            "type": "learningObject",
            "attributes": {
                "localizedMetadata": [
                    {
                        "description": f"PostHog {survey_type} feedback survey for: {lp_name}",
                        "locale":      "en-US",
                        "name":        SURVEY_COURSE_NAME
                    }
                ],
                "loType":                  "course",
                "state":                   "Published",
                "isSubLoOrderEnforced":    False,
                "isExternal":              False
            }
        }
    }

    print(f"[Webhook] Creating survey course for LP: {lp_name}")
    res = requests.post(
        f"{ALM_BASE_URL}/learningObjects",
        json=course_payload,
        headers=headers
    )

    print(f"[Webhook] Course creation response: {res.status_code} | {res.text[:500]}")

    if res.status_code not in [200, 201]:
        raise Exception(f"Course creation failed: {res.status_code} | {res.text}")

    course_data = res.json()
    course_id   = course_data["data"]["id"]
    print(f"[Webhook] ✅ Survey course created: {course_id}")
    return course_id


# ─── STEP 4: ATTACH SURVEY COURSE TO LP AS LAST ITEM ─────
def attach_course_to_lp(token, lp_id, course_id):
    headers = {
        "Authorization": f"oauth {token}",
        "Content-Type":  "application/vnd.api+json",
        "Accept":        "application/vnd.api+json"
    }

    payload = {
        "data": [
            {
                "id":   course_id,
                "type": "learningObject",
                "attributes": {
                    "isMandatory": True,
                    "order":       9999
                }
            }
        ]
    }

    print(f"[Webhook] Attaching course {course_id} to LP {lp_id}...")
    res = requests.post(
        f"{ALM_BASE_URL}/learningObjects/{lp_id}/subLOs",
        json=payload,
        headers=headers
    )

    print(f"[Webhook] Attach response: {res.status_code} | {res.text[:500]}")

    if res.status_code not in [200, 201]:
        raise Exception(f"Attach to LP failed: {res.status_code} | {res.text}")

    print(f"[Webhook] ✅ Survey course attached to LP {lp_id}.")


# ─── HEALTH CHECK ─────────────────────────────────────────
@app.get("/")
def health():
    return {
        "status":  "ok",
        "service": "ALM Survey Webhook Receiver"
    }


# ─── MAIN WEBHOOK ENDPOINT ────────────────────────────────
@app.post("/alm-webhook")
async def alm_webhook(request: Request):
    try:
        body = await request.json()
        print(f"[Webhook] Received payload")

        # ALM sends events as array
        events = body.get("events", [])
        if not events:
            print(f"[Webhook] No events. Skipping.")
            return {"status": "skipped", "reason": "no events"}

        # Process first event
        event      = events[0]
        event_name = event.get("eventName", "")
        data       = event.get("data", {})
        lo_id      = data.get("loId", "")
        lo_type    = data.get("loType", "")

        print(f"[Webhook] Event={event_name} | Type={lo_type} | ID={lo_id}")

        # Only process Learning Path publish events
        if lo_type != "learningProgram":
            print(f"[Webhook] Not a Learning Path. Skipping.")
            return {"status": "skipped", "reason": "not a learning path"}

        if not lo_id:
            print(f"[Webhook] Missing LP ID. Skipping.")
            return {"status": "skipped", "reason": "missing lo_id"}

        # Get fresh access token
        token = get_access_token()

        # Fetch LP name
        lp_name = get_lp_name(token, lo_id)
        if not lp_name:
            lp_name = "Learning Path"

        # Create survey course
        course_id = create_survey_course(token, lp_name)

        # Attach to LP as last mandatory item
        attach_course_to_lp(token, lo_id, course_id)

        return {
            "status":    "success",
            "lp_id":     lo_id,
            "lp_name":   lp_name,
            "course_id": course_id,
            "message":   f"✅ Survey course created and attached to: {lp_name}"
        }

    except Exception as e:
        print(f"[Webhook] ❌ Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
