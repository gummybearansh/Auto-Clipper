"""Instagram publishing via the Instagram API with Facebook Login.

Per Meta's docs this route needs: an Instagram Creator/Business account linked to a Facebook Page, a Facebook
app, and a user token with instagram_basic + instagram_content_publish + pages_show_list (+ pages_read_engagement).

Connect flow (no Graph API Explorer):
  /auth/instagram/start  → Facebook login dialog asking for exactly those permissions
  /auth/instagram/callback → code → user token → long-lived (60d) → Page token (doesn't expire) → IG user id
Everything is stored in ~/.config/auto-clipper/instagram.json (chmod 600).

Publishing a Reel (resumable upload, no public URL needed):
  POST /{ig-user}/media (media_type=REELS, upload_type=resumable) → container + upload uri
  POST rupload uri (file bytes) → poll container status_code until FINISHED → POST /{ig-user}/media_publish
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "v27.0"
GRAPH = f"https://graph.facebook.com/{API}"
CRED = Path("~/.config/auto-clipper/instagram.json").expanduser()
SCOPES = ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
          "business_management"]


class IGError(RuntimeError):
    pass


# ------------------------------------------------------------------ creds
def load() -> dict:
    return json.loads(CRED.read_text()) if CRED.exists() else {}


def save(d: dict) -> None:
    CRED.parent.mkdir(parents=True, exist_ok=True)
    CRED.parent.chmod(0o700)
    CRED.write_text(json.dumps(d, indent=2))
    CRED.chmod(0o600)


def status() -> dict:
    c = load()
    return {"configured": bool(c.get("app_id") and c.get("app_secret")),
            "connected": bool(c.get("ig_user_id") and c.get("page_token")),
            "username": c.get("ig_username"), "page": c.get("page_name"), "connected_at": c.get("connected_at")}


# ------------------------------------------------------------------ http
def _call(method: str, url: str, params: dict | None = None, data: bytes | None = None,
          headers: dict | None = None, timeout: int = 60) -> dict:
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except Exception:
            err = {"message": str(e)}
        raise IGError(f"{err.get('message', 'error')} (code {err.get('code')}, sub {err.get('error_subcode')})") from None


def get(path: str, **params) -> dict:
    return _call("GET", f"{GRAPH}/{path}", params)


def post(path: str, **params) -> dict:
    return _call("POST", f"{GRAPH}/{path}", params, data=b"")


# ------------------------------------------------------------------ connect
def login_url(redirect_uri: str) -> str:
    c = load()
    if not c.get("app_id"):
        raise IGError("app_id/app_secret missing in ~/.config/auto-clipper/instagram.json")
    state = secrets.token_urlsafe(16)
    c["oauth_state"] = state
    save(c)
    q = {"client_id": c["app_id"], "redirect_uri": redirect_uri, "state": state, "response_type": "code",
         "auth_type": "rerequest"}
    if c.get("config_id"):           # Facebook Login for Business configuration, if the app uses one
        q["config_id"] = c["config_id"]
    else:
        q["scope"] = ",".join(SCOPES)
    return f"https://www.facebook.com/{API}/dialog/oauth?{urllib.parse.urlencode(q)}"


def finish_login(code: str, state: str, redirect_uri: str) -> dict:
    c = load()
    if not state or state != c.get("oauth_state"):
        raise IGError("login state mismatch — start the connect flow again")
    short = get("oauth/access_token", client_id=c["app_id"], client_secret=c["app_secret"],
                redirect_uri=redirect_uri, code=code)["access_token"]
    long = get("oauth/access_token", grant_type="fb_exchange_token", client_id=c["app_id"],
               client_secret=c["app_secret"], fb_exchange_token=short)["access_token"]
    c["user_token"] = long                      # keep it even if the Page lookup below fails
    c.pop("oauth_state", None)
    save(c)
    dbg = get("debug_token", input_token=long, access_token=f"{c['app_id']}|{c['app_secret']}").get("data", {})
    granted = dbg.get("scopes", [])
    targets = {g["scope"]: g.get("target_ids", []) for g in dbg.get("granular_scopes", [])}
    pages = get("me/accounts", fields="id,name,access_token,instagram_business_account{id,username}",
                access_token=long).get("data", [])
    # Pages granted to pages_show_list / pages_read_engagement can also be looked up directly by id
    if not pages:
        for pid in dict.fromkeys(targets.get("pages_show_list", []) + targets.get("pages_read_engagement", [])):
            try:
                p = get(pid, fields="id,name,access_token,instagram_business_account{id,username}",
                        access_token=long)
                if p.get("id"):
                    pages.append(p)
            except IGError:
                pass
    if not pages:
        missing = [s for s in ("pages_show_list", "pages_read_engagement", "instagram_basic",
                               "instagram_content_publish") if s not in granted]
        hint = ("Facebook didn't grant: " + ", ".join(missing) + ". Your app uses Facebook Login for Business, "
                "which only grants what's in a Login Configuration — create one and add its config_id."
                if missing else "All permissions were granted, but no Page was selected in the popup — click "
                "Connect again, choose 'Edit settings' and tick your Page and @gummybearclips.")
        raise IGError(f"No Facebook Page visible. Granted: {', '.join(granted) or 'nothing'}. {hint}")
    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        names = ", ".join(p.get("name", "?") for p in pages) or "none"
        raise IGError(f"No Facebook Page with a linked Instagram account was shared (Pages granted: {names}). "
                      "In the login popup tick your Page AND @gummybearclips, and make sure Instagram is linked "
                      "to the Page (Page → Settings → Linked accounts → Instagram).")
    want = (c.get("ig_username") or "gummybearclips").lower()
    page = next((p for p in linked if p["instagram_business_account"].get("username", "").lower() == want), linked[0])
    ig = page["instagram_business_account"]
    c.update(user_token=long, page_id=page["id"], page_name=page.get("name"), page_token=page["access_token"],
             ig_user_id=ig["id"], ig_username=ig.get("username"),
             connected_at=time.strftime("%Y-%m-%d %H:%M"))
    for k in ("short_token", "long_token", "oauth_state"):     # old/temporary values
        c.pop(k, None)
    save(c)
    return status()


# ------------------------------------------------------------------ publish
def _ctx() -> tuple[str, str]:
    c = load()
    if not (c.get("ig_user_id") and c.get("page_token")):
        raise IGError("Instagram not connected — open the clipper UI and click 'Connect Instagram'")
    return c["ig_user_id"], c["page_token"]


def publishing_limit() -> dict:
    ig, tok = _ctx()
    d = get(f"{ig}/content_publishing_limit", fields="quota_usage,config", access_token=tok).get("data", [{}])
    return d[0] if d else {}


def publish_reel(video: Path, caption: str, share_to_feed: bool = True, progress=None,
                 timeout: int = 600) -> dict:
    """Upload + publish a Reel. Returns {"media_id", "permalink"}."""
    progress = progress or (lambda s: None)
    ig, tok = _ctx()
    video = Path(video)
    size = video.stat().st_size
    progress("creating container")
    cont = post(f"{ig}/media", media_type="REELS", upload_type="resumable", caption=caption,
                share_to_feed="true" if share_to_feed else "false", access_token=tok)
    cid, uri = cont["id"], cont.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{API}/{cont['id']}"
    progress(f"uploading {size / 1e6:.0f} MB")
    r = _call("POST", uri, data=video.read_bytes(), timeout=600,
              headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    if not r.get("success", True):
        raise IGError(f"upload failed: {r}")
    t0 = time.time()
    while True:
        st = get(cid, fields="status_code,status", access_token=tok)
        code = st.get("status_code")
        progress(f"processing ({code})")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise IGError(f"Instagram couldn't process the video: {st.get('status')}")
        if time.time() - t0 > timeout:
            raise IGError("timed out waiting for Instagram to process the video")
        time.sleep(5)
    progress("publishing")
    media_id = post(f"{ig}/media_publish", creation_id=cid, access_token=tok)["id"]
    link = get(media_id, fields="permalink", access_token=tok).get("permalink", "")
    return {"media_id": media_id, "permalink": link}
