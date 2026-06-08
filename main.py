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
ALM_OAUTH_URL      = "https://learningmanager.adobe.com/oauth/token"
CLIENT_ID          = os.environ.get("ALM_CLIENT_ID")
CLIENT_SECRET      = os.environ.get("ALM_CLIENT_SECRET")
REFRESH_TOKEN      = os.environ.get("ALM_REFRESH_TOKEN")
SURVEY_COURSE_NAME = "Feedback Survey"
# ──────────────────────────────────────────────────────────


# ─── STEP 1: GET FRESH ACCESS TOKEN ──────────────────────
# Called fresh on every webhook — no stored token, no expiry issue
def get_access_token():
    try:
        res = requests.post(ALM_OAUTH_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET
        })
        data = res.json()
        if "access_token" not in data:
            raise Exception(f"Token refresh failed: {data}")
        print(f"[Webhook] ✅ Access token refreshed.")
        return data["access_token"]
    except Exception as e:
        raise Exception(f"OAuth error: {str(e)}")


# ─── STEP 2: FETCH LP NAME FROM ALM API ──────────────────
def get_lp_name(token, lp_id):
    try:
        res = requests.get(
            f"{ALM_BASE_URL}/learningObjects/{lp_id}",
            headers={"Authorization": f"oauth {token}"}
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
def create_survey_course(token, lp_name, lp_id):
    headers = {
        "Authorization": f"oauth {token}",
        "Content-Type":  "application/vnd.api+json"
    }

    # Placeholder URL — survey fires via ALM JS injection
    # LP name passed so JS knows which PostHog survey to fire
    encoded_lp   = requests.utils.quote(lp_name)
    launcher_url = f"https://learningmanager.adobe.com?lp_name={encoded_lp}"

    # Create course
    course_payload = {
        "data": {
            "type": "learningObject",
            "attributes": {
                "localizedMetadata": [{
                    "locale":      "en-US",
                    "name":        SURVEY_COURSE_NAME,
                    "description": "Feedback survey for this Learning Path."
                }],
                "loType": "course",
                "state":  "Published"
            }
        }
    }

    res = requests.post(
        f"{ALM_BASE_URL}/learningObjects",
        json=course_payload,
        headers=headers
    )

    if res.status_code not in [200, 201]:
        raise Exception(f"Course creation failed: {res.status_code} | {res.text}")

    course_id = res.json()["data"]["id"]
    print(f"[Webhook] ✅ Survey course created: {course_id}")

    # Add activity module to course
    module_payload = {
        "data": {
            "type": "resource",
            "attributes": {
                "localizedMetadata": [{
                    "locale": "en-US",
                    "name":   SURVEY_COURSE_NAME
                }],
                "moduleType":         "activity",
                "activityType":       "url",
                "activityUrl":        launcher_url,
                "completionCriteria": "LEARNER_MARKED"
            }
        }
    }

    res = requests.post(
        f"{ALM_BASE_URL}/learningObjects/{course_id}/resources",
        json=module_payload,
        headers=headers
    )

    if res.status_code not in [200, 201]:
        raise Exception(f"Module creation failed: {res.status_code} | {res.text}")

    print(f"[Webhook] ✅ Activity module added to survey course.")
    return course_id


# ─── STEP 4: ATTACH SURVEY COURSE TO LP AS LAST ITEM ─────
def attach_course_to_lp(token, lp_id, course_id):
    headers = {
        "Authorization": f"oauth {token}",
        "Content-Type":  "application/vnd.api+json"
    }

    payload = {
        "data": [{
            "id":   course_id,
            "type": "learningObject",
            "attributes": {
                "isMandatory": True,
                "order":       9999
            }
        }]
    }

    res = requests.post(
        f"{ALM_BASE_URL}/learningObjects/{lp_id}/subLOs",
        json=payload,
        headers=headers
    )

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
        print(f"[Webhook] FULL RAW PAYLOAD:\n{json.dumps(body, indent=2)}")

        # ALM sends events as array
        events = body.get("events", [])
        if not events:
            print(f"[Webhook] No events in payload. Skipping.")
            return {"status": "skipped", "reason": "no events"}

        # Process first event
        event      = events[0]
        event_name = event.get("eventName", "")
        data       = event.get("data", {})
        lo_id      = data.get("loId", "")
        lo_type    = data.get("loType", "")

        print(f"[Webhook] Event={event_name} | Type={lo_type} | ID={lo_id}")

        # Only process Learning Path events
        if lo_type != "learningProgram":
            print(f"[Webhook] Not a Learning Path. Skipping.")
            return {"status": "skipped", "reason": "not a learning path"}

        if not lo_id:
            print(f"[Webhook] Missing LP ID. Skipping.")
            return {"status": "skipped", "reason": "missing lo_id"}

        # Get fresh access token — no stored token, no expiry issue
        token = get_access_token()

        # Fetch LP name from ALM API using loId
        lp_name = get_lp_name(token, lo_id)
        if not lp_name:
            print(f"[Webhook] Could not fetch LP name. Using fallback.")
            lp_name = "Learning Path"

        # Create survey course with LP name embedded in URL
        course_id = create_survey_course(token, lp_name, lo_id)

        # Attach survey course to LP as last mandatory item
        attach_course_to_lp(token, lo_id, course_id)

        return {
            "status":    "success",
            "lp_id":     lo_id,
            "lp_name":   lp_name,
            "course_id": course_id,
            "message":   f"Survey course created and attached to: {lp_name}"
        }

    except Exception as e:
        print(f"[Webhook] ❌ Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
