#!/usr/bin/env python3
"""
TikTok-Autoposter: Content Posting API (Direct Post), config-getrieben.

Gegenstueck zum meta-autoposter (publisher.py) fuer TikTok. Postet Videos per
PULL_FROM_URL — die Medien MUESSEN unter einer im Developer-Portal verifizierten
Domain liegen (DogWOW: dogwow.app/reels/... proxied per Netlify aufs Kampagnen-Repo).

App: "BTech Publisher" (BTechnology GmbH), mehrkontenfaehig — jedes TikTok-Konto
autorisiert die App einmal per OAuth und bekommt einen eigenen Token-Satz.

Dateien (neben diesem Script bzw. per --dir):
  tiktok_config.json   {client_key, redirect_uri, videos_base, accounts:{name:{token_file}}}
  tiktok_jobs.json     [{id, account, video, caption, not_before, privacy}]
  tiktok_state.json    {"posted": [...]}
  <token_file>         {access_token, refresh_token, expires_at, ...} chmod 600

Ablauf einmalig pro Konto:
  1) python3 tiktok_publisher.py --auth-url
     -> URL im Browser oeffnen, mit dem Ziel-Konto bestaetigen.
  2) Auf der Callback-Seite den Code kopieren, dann:
     pbpaste | tr -d '[:space:]' > ~/.tiktok_auth_code
  3) python3 tiktok_publisher.py --exchange --account dogwow
     -> schreibt den Token-Satz; ab dann laeuft alles automatisch (Refresh inklusive).

Betrieb:
  python3 tiktok_publisher.py --whoami --account dogwow
  python3 tiktok_publisher.py --dry-run
  python3 tiktok_publisher.py --run            # naechster faelliger Job

Wichtig:
  - Vor dem App-Audit sind nur private Posts moeglich (privacy SELF_ONLY),
    max. 5 autorisierte Nutzer/24h. Nach dem Audit PUBLIC_TO_EVERYONE.
  - Access-Token gilt 24 h, Refresh-Token 1 Jahr -> jeder Lauf refresht bei Bedarf.
  - Musik aus der TikTok-Bibliothek kann die API NICHT anhaengen (nur eigener Ton).
"""
import argparse, json, os, sys, time, datetime, urllib.parse, urllib.request, urllib.error

AUTH = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = "user.info.basic,video.publish,video.upload"


def load(name, default=None, dirpath=None):
    p = os.path.join(dirpath or HERE, name)
    if not os.path.exists(p):
        if default is None:
            sys.exit(f"FEHLER: {name} fehlt.")
        return default
    return json.load(open(p))


def secret(cfg=None):
    cands = [(cfg or {}).get("secret_file"), "~/.tiktok_app_secret"]
    for p in [c for c in cands if c]:
        f = os.path.expanduser(p)
        if os.path.exists(f):
            return open(f).read().strip()
    env = os.environ.get("TIKTOK_CLIENT_SECRET")
    if env:
        return env.strip()
    sys.exit("FEHLER: Kein Client Secret (~/.tiktok_app_secret oder TIKTOK_CLIENT_SECRET).")


def post_json(path, payload, token=None, form=False):
    url = f"{API}{path}"
    if form:
        body = urllib.parse.urlencode(payload).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json; charset=UTF-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TikTok-API {e.code} bei {path}: {e.read().decode()[:300]}")


# ---------- OAuth ----------
def auth_url(cfg):
    q = urllib.parse.urlencode({
        "client_key": cfg["client_key"], "scope": SCOPES, "response_type": "code",
        "redirect_uri": cfg["redirect_uri"], "state": "btechpub"})
    return f"{AUTH}?{q}"


def token_path(cfg, account):
    acc = cfg["accounts"].get(account)
    if not acc:
        sys.exit(f"FEHLER: Konto '{account}' nicht in tiktok_config.json.")
    return os.path.expanduser(acc["token_file"])


def save_tokens(path, data):
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 86400)) - 120
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(path, 0o600)


def exchange(cfg, account, code_file="~/.tiktok_auth_code"):
    f = os.path.expanduser(code_file)
    if not os.path.exists(f):
        sys.exit(f"FEHLER: {code_file} fehlt. Code von der Callback-Seite kopieren und "
                 f"`pbpaste | tr -d '[:space:]' > {code_file}` ausfuehren.")
    code = urllib.parse.unquote(open(f).read().strip())
    res = post_json("/oauth/token/", {
        "client_key": cfg["client_key"], "client_secret": secret(cfg), "code": code,
        "grant_type": "authorization_code", "redirect_uri": cfg["redirect_uri"]}, form=True)
    if "access_token" not in res:
        sys.exit(f"FEHLER beim Token-Tausch: {json.dumps(res)[:300]}")
    save_tokens(token_path(cfg, account), res)
    os.remove(f)
    print(f"OK: Token fuer '{account}' gespeichert (open_id {res.get('open_id', '?')[:8]}...).")


def access_token(cfg, account):
    p = token_path(cfg, account)
    if not os.path.exists(p):
        sys.exit(f"FEHLER: Kein Token fuer '{account}'. Erst --auth-url + --exchange.")
    tok = json.load(open(p))
    if tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    res = post_json("/oauth/token/", {
        "client_key": cfg["client_key"], "client_secret": secret(cfg),
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, form=True)
    if "access_token" not in res:
        sys.exit(f"FEHLER beim Refresh: {json.dumps(res)[:300]}")
    res.setdefault("refresh_token", tok["refresh_token"])
    save_tokens(p, res)
    print("[TOKEN] erneuert")
    return res["access_token"]


# ---------- Posting ----------
def creator_info(token):
    return post_json("/post/publish/creator_info/query/", {}, token=token).get("data", {})


def publish(cfg, token, job):
    url = cfg["videos_base"].rstrip("/") + "/" + job["video"].lstrip("/")
    info = creator_info(token)
    allowed = info.get("privacy_level_options") or []
    want = job.get("privacy", cfg.get("default_privacy", "SELF_ONLY"))
    if want not in allowed:
        print(f"[WARN] privacy {want} nicht erlaubt (erlaubt: {allowed}), nehme {allowed[0]}")
        want = allowed[0]
    payload = {
        "post_info": {
            "title": job["caption"][:2200],
            "privacy_level": want,
            "disable_duet": False, "disable_comment": False, "disable_stitch": False,
            "video_cover_timestamp_ms": job.get("cover_ms", 1200),
        },
        "source_info": {"source": "PULL_FROM_URL", "video_url": url},
    }
    res = post_json("/post/publish/video/init/", payload, token=token)
    pid = (res.get("data") or {}).get("publish_id")
    if not pid:
        raise RuntimeError(f"Kein publish_id: {json.dumps(res)[:300]}")
    for _ in range(60):
        time.sleep(5)
        st = post_json("/post/publish/status/fetch/", {"publish_id": pid}, token=token)
        s = (st.get("data") or {}).get("status")
        if s in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return pid, s
        if s == "FAILED":
            raise RuntimeError(f"Publish FAILED: {json.dumps(st)[:300]}")
    raise RuntimeError(f"Timeout beim Publish ({pid})")


def due_job(cfg, jobs, state):
    now = datetime.datetime.now(datetime.timezone.utc)
    done = set(state["posted"])
    for j in jobs:
        if j["id"] in done:
            continue
        nb = j.get("not_before")
        if nb and now < datetime.datetime.fromisoformat(nb.replace("Z", "+00:00")):
            continue
        return j
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE)
    ap.add_argument("--auth-url", action="store_true")
    ap.add_argument("--exchange", action="store_true")
    ap.add_argument("--whoami", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--account", default="dogwow")
    a = ap.parse_args()

    cfg = load("tiktok_config.json", dirpath=a.dir)
    if a.auth_url:
        print(auth_url(cfg)); return
    if a.exchange:
        exchange(cfg, a.account); return
    if a.whoami:
        info = creator_info(access_token(cfg, a.account))
        print(json.dumps({k: info.get(k) for k in
              ("creator_username", "creator_nickname", "privacy_level_options",
               "max_video_post_duration_sec")}, indent=1)); return

    jobs = load("tiktok_jobs.json", [], dirpath=a.dir)
    sp = os.path.join(a.dir, "tiktok_state.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {"posted": []}
    job = due_job(cfg, jobs, state)
    if not job:
        print(f"Nichts faellig ({len(state['posted'])}/{len(jobs)} gepostet)."); return
    if not a.run:
        print(f"[DRY] {job['id']} -> {cfg['videos_base'].rstrip('/')}/{job['video']}")
        print("      ", job["caption"][:100]); return
    token = access_token(cfg, job.get("account", a.account))
    pid, status = publish(cfg, token, job)
    print(f"[OK-TIKTOK] {job['id']} -> {pid} ({status})")
    state["posted"].append(job["id"])
    json.dump(state, open(sp, "w"), indent=1)
    print(f"Fortschritt: {len(state['posted'])}/{len(jobs)}")


if __name__ == "__main__":
    main()
