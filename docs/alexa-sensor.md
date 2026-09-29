# Alexa path B: virtual contact sensor

Official, stable alternative to `alexa_remote_control.sh`. Trades dynamic
speech for auth that does not rot.

**Everything in this document is unverified.** It was written from API
knowledge, not from a working deployment. Amazon's console UI in particular
moves around; treat the click-paths as directional.

## The trade, stated plainly

| | path A: `alexa_remote_control.sh` | path B: contact sensor |
|---|---|---|
| Speech | `"Call from Mom"` — arbitrary, from astdb | static per sensor, baked into a Routine |
| Auth | session cookie, MFA-derived | LWA OAuth refresh token |
| Breaks | ~annually | when you revoke the link |
| Off-box deps | none | AWS account + Lambda + developer account |
| Adding a person | `orata-cnam.sh add` | new sensor + new Routine, by hand in the app |
| Runtime on the Pi | ~1000 lines of third-party bash | one curl to a documented endpoint |

Path B's hot path is *more* bare-metal. Its setup is much heavier, and it is
the setup that conflicts with "no tech debt" — you acquire a second
deployment target in AWS with its own lifecycle.

## Naming model: pick one

Sensor IDs map per-number in astdb, so both models are the same code.

**Per-person.** One sensor + one Routine per named caller. Exact
announcements. N GUI chores; the astdb name book stops being the single
source of truth for who is who.

    orata-cnam.sh sensor-add 15551234567 orata-mom

**Tiered.** Three sensors — `orata-family`, `orata-known`, `orata-unknown`.
Three Routines, built once. "Call from family." Loses the specific name to
Alexa but keeps `orata-cnam.sh` authoritative, and ntfy still shows the exact
name on your phone.

Tiered is the one I would ship. Per-person announcements are the project's
stated point, but the GUI-chore-per-contact cost lands on every future
change, and ntfy already carries the precise name.

## Setup

### 1. Smart Home skill

developer.amazon.com -> Alexa Skills Kit -> Create Skill -> **Smart Home**,
payload v3. Note the **Skill ID**. Leave the endpoint blank for now.

### 2. Lambda

AWS Lambda, Python 3.12, **us-east-1** for a NA account (eu-west-1 for EU,
us-west-2 for FE — the region is constrained by your Alexa account region).
Add trigger: Alexa Smart Home, paste the Skill ID. Copy the function ARN back
into the skill's Default Endpoint.

The handler answers three things. It is never in the call path.

```python
import json, os, time, urllib.parse, urllib.request

SENSORS = ["orata-family", "orata-known", "orata-unknown"]

def _hdr(name, ns="Alexa", cid=None):
    h = {"namespace": ns, "name": name, "payloadVersion": "3",
         "messageId": str(time.time())}
    if cid:
        h["correlationToken"] = cid
    return h

def handler(event, context):
    hdr = event["directive"]["header"]
    ns, name = hdr["namespace"], hdr["name"]

    if ns == "Alexa.Discovery" and name == "Discover":
        endpoints = [{
            "endpointId": s,
            "manufacturerName": "orata",
            "friendlyName": s.replace("orata-", "orata ").title(),
            "description": "orata virtual call sensor",
            "displayCategories": ["CONTACT_SENSOR"],
            "capabilities": [
                {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                {"type": "AlexaInterface", "interface": "Alexa.ContactSensor",
                 "version": "3",
                 "properties": {"supported": [{"name": "detectionState"}],
                                "proactivelyReported": True, "retrievable": True}},
                {"type": "AlexaInterface", "interface": "Alexa.EndpointHealth",
                 "version": "3",
                 "properties": {"supported": [{"name": "connectivity"}],
                                "proactivelyReported": True, "retrievable": True}},
            ],
        } for s in SENSORS]
        return {"event": {"header": _hdr("Discover.Response", "Alexa.Discovery"),
                          "payload": {"endpoints": endpoints}}}

    if ns == "Alexa.Authorization" and name == "AcceptGrant":
        code = event["directive"]["payload"]["grant"]["code"]
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": os.environ["LWA_CLIENT_ID"],
            "client_secret": os.environ["LWA_CLIENT_SECRET"],
        }).encode()
        req = urllib.request.Request("https://api.amazon.com/auth/o2/token",
                                     data=body)
        tok = json.load(urllib.request.urlopen(req))
        # THE REFRESH TOKEN YOU NEED IS ON THE NEXT LINE, IN CLOUDWATCH.
        # Copy it into announce.conf, then delete this print and redeploy.
        print("ORATA_LWA_REFRESH_TOKEN=" + tok["refresh_token"])
        return {"event": {"header": _hdr("AcceptGrant.Response",
                                         "Alexa.Authorization"),
                          "payload": {}}}

    if ns == "Alexa" and name == "ReportState":
        ep = event["directive"]["endpoint"]["endpointId"]
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.00Z", time.gmtime())
        return {"event": {"header": _hdr("StateReport", cid=hdr.get("correlationToken")),
                          "endpoint": {"endpointId": ep}, "payload": {}},
                "context": {"properties": [
                    {"namespace": "Alexa.ContactSensor", "name": "detectionState",
                     "value": "NOT_DETECTED", "timeOfSample": ts,
                     "uncertaintyInMilliseconds": 0},
                    {"namespace": "Alexa.EndpointHealth", "name": "connectivity",
                     "value": {"value": "OK"}, "timeOfSample": ts,
                     "uncertaintyInMilliseconds": 0}]}}

    return {"event": {"header": _hdr("ErrorResponse"),
                      "payload": {"type": "INVALID_DIRECTIVE",
                                  "message": name}}}
```

`ReportState` lies — it always says NOT_DETECTED. The sensors are momentary
and stateless; nothing polls them for truth. If that bothers you, back it
with DynamoDB, but it buys nothing here.

### 3. Account linking

Login with Amazon (developer.amazon.com -> Login with Amazon) -> create a
Security Profile. Take **Client ID** and **Client Secret**.

In the skill's Account Linking page:
- Auth URI `https://www.amazon.com/ap/oa`
- Token URI `https://api.amazon.com/auth/o2/token`
- scope `alexa::skills:account_linking`
- copy the **Alexa Redirect URLs** shown there into the LWA security
  profile's Allowed Return URLs

Set `LWA_CLIENT_ID` / `LWA_CLIENT_SECRET` as Lambda environment variables
too — `AcceptGrant` needs them.

### 4. Link, and steal the refresh token

Alexa app -> More -> Skills -> Your Skills -> Dev -> your skill -> Enable,
log in. That fires `AcceptGrant`. Open CloudWatch Logs for the function and
find the `ORATA_LWA_REFRESH_TOKEN=` line. Paste it, plus the client ID and
secret, into `/etc/orata/announce.conf`. Remove the `print` and redeploy.

The refresh token is long-lived and survives access-token expiry. It dies
only if you disable the skill or revoke the LWA profile.

Then Discover Devices in the app; the sensors should appear.

### 5. Routines

One per sensor. Alexa app -> More -> Routines -> +

- When: Smart Home -> `orata family` -> **Opens**
- Action: Alexa Says -> Customized -> `Call from family.`
  (or Messaging -> Announcement, if you want it on every Echo at once)
- From: the device or group that should speak

### 6. Pi side

    sudo cp bin/orata-alexa-sensor.sh /usr/local/bin/
    sudo chmod 755 /usr/local/bin/orata-alexa-sensor.sh
    sudo mkdir -p /var/lib/orata
    sudo chown asterisk:asterisk /var/lib/orata
    sudo chmod 700 /var/lib/orata
    sed -i 's/\r$//' /usr/local/bin/orata-alexa-sensor.sh

Set `ORATA_ALEXA_MODE=sensor` in `/etc/orata/announce.conf`, then
`sudo asterisk -rx "core reload"` to pick up the new dialplan.

## Testing

Bottom-up, same discipline as the rest of the project.

    # 1. token refresh only -- expect a fresh /var/lib/orata/alexa.token
    sudo -u asterisk /usr/local/bin/orata-alexa-sensor.sh orata-unknown
    cat /var/log/orata/announce.log

`sensor OK ... DETECTED` plus speech = done. `token refresh failed` = step 4
is wrong. `ChangeReport ... rejected` = token is fine but the endpointId is
not one Discovery reported, or the gateway region is wrong.

    # 2. through the announce script
    sudo -u asterisk /usr/local/bin/orata-announce.sh 15551234567 "Mom" orata-family

    # 3. through the dialplan
    dial *99

## Failure modes specific to this path

- **Fires once, then never again.** The sensor did not reset. Look for
  `sensor WARN`. Alexa edge-triggers on NOT_DETECTED -> DETECTED.
- **Two calls in quick succession, second is silent.** The second trip
  landed inside `ORATA_SENSOR_RESET`. Lower it, but not below ~2s or Alexa
  may coalesce the transition.
- **Several seconds of lag.** Pi -> Event Gateway -> Routine -> Echo is a
  cloud round trip, typically 1-3s. Path A has the same shape. Harmless
  inside a 30s ring.
- **DND suppresses it, and volume follows device volume.** Unchanged from
  path A.
- **Token cache is stale garbage** after you re-link the skill.
  `rm /var/lib/orata/alexa.token`.