#!/usr/bin/env python3
"""
DogWOW Auto-Poster: Instagram + Facebook ueber die Meta Graph API.

Adaptiert vom gptagency-Publisher (shorts-werkstatt/ig_publisher.py), erweitert um
Bild-KARUSSELLS (das Original konnte nur Video-Reels):
  - IG-Karussell: je Bild ein Item-Container (is_carousel_item) -> CAROUSEL-Container
    mit children -> media_publish
  - IG-Reel:      /media (REELS, video_url) -> Polling FINISHED -> media_publish
  - FB-Karussell: je Bild unveroeffentlichtes Foto (published=false) -> /feed mit
    attached_media (Page-Token)
  - FB-Reel:      /video_reels start -> upload via file_url-Header -> finish

Slots: Jobs heissen dayXX-a (Karussell) und dayXX-b (Reel). --slot a|b|auto
waehlt den naechsten offenen Job des Slots; auto entscheidet nach UTC-Stunde
(<17 Uhr UTC = Slot a, sonst b). Fortschritt in state.json (Quelle der Wahrheit,
wird vom Workflow zurueckcommittet).

Token: Long-Lived User Token (nie ablaufend) mit instagram_basic,
instagram_content_publish, pages_show_list, pages_read_engagement,
pages_manage_posts, business_management als META_TOKEN (Secret) oder
~/.dogwow_meta_token.

  python3 publisher.py --discover
  python3 publisher.py --dry-run --slot a
  python3 publisher.py --run --slot auto
"""
import json, os, sys, time, argparse, datetime, urllib.request, urllib.parse, urllib.error

GRAPH = "https://graph.facebook.com/v21.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
JOBS = os.path.join(HERE, "jobs.json")
RAW = "https://raw.githubusercontent.com/igorkuzenko/dogwow-posts-tmp/main/"

IG_HANDLE = "dogwowapp"
PAGE_NAME = "Dogwowapp"


def get_token():
    tok = os.environ.get("META_TOKEN")
    if tok:
        return tok.strip()
    tokfile = os.path.expanduser("~/.dogwow_meta_token")
    if os.path.exists(tokfile):
        return open(tokfile).read().strip()
    sys.exit("FEHLER: Kein Token (META_TOKEN oder ~/.dogwow_meta_token).")


def api(path, params=None, method="GET", data=None, token=None):
    tok = token or get_token()
    params = dict(params or {})
    params["access_token"] = tok
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"API-FEHLER {e.code} bei {method} {path}: {detail}")


def account():
    pages = api("me/accounts", {"fields": "name,access_token,instagram_business_account{username,id}", "limit": 50})
    for p in pages.get("data", []):
        iga = p.get("instagram_business_account")
        if iga and iga.get("username") == IG_HANDLE:
            return {"ig_id": iga["id"], "page_id": p["id"],
                    "page_token": p["access_token"], "page_name": p["name"]}
    sys.exit(f"FEHLER: IG-Konto @{IG_HANDLE} nicht in me/accounts gefunden.")


def wait_finished(container_id, label, tries=80):
    for _ in range(tries):
        st = api(container_id, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"Container-Fehler bei {label}: {st.get('status')}")
        time.sleep(5)
    raise RuntimeError(f"Timeout beim Verarbeiten von {label}")


def publish_ig_carousel(ig_id, job):
    children = []
    for path in job["media"]:
        item = api(f"{ig_id}/media", method="POST", data={
            "image_url": RAW + path, "is_carousel_item": "true"})
        children.append(item["id"])
    parent = api(f"{ig_id}/media", method="POST", data={
        "media_type": "CAROUSEL", "children": ",".join(children),
        "caption": job["caption"]})
    wait_finished(parent["id"], job["id"])
    pub = api(f"{ig_id}/media_publish", method="POST", data={"creation_id": parent["id"]})
    return pub["id"]


def publish_ig_reel(ig_id, job):
    data = {"media_type": "REELS", "video_url": RAW + job["media"][0],
            "caption": job["caption"], "share_to_feed": "true"}
    if job.get("cover"):
        data["cover_url"] = RAW + job["cover"]
    cre = api(f"{ig_id}/media", method="POST", data=data)
    wait_finished(cre["id"], job["id"])
    pub = api(f"{ig_id}/media_publish", method="POST", data={"creation_id": cre["id"]})
    return pub["id"]


def publish_fb_carousel(page_id, page_token, job):
    media_ids = []
    for path in job["media"]:
        ph = api(f"{page_id}/photos", method="POST", token=page_token,
                 data={"url": RAW + path, "published": "false"})
        media_ids.append(ph["id"])
    data = {"message": job["caption"]}
    for i, mid in enumerate(media_ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
    post = api(f"{page_id}/feed", method="POST", token=page_token, data=data)
    return post["id"]


def publish_fb_reel(page_id, page_token, job):
    start = api(f"{page_id}/video_reels", {"upload_phase": "start"}, method="POST", token=page_token)
    vid = start["video_id"]
    req = urllib.request.Request(start["upload_url"], method="POST")
    req.add_header("Authorization", f"OAuth {page_token}")
    req.add_header("file_url", RAW + job["media"][0])
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()
    api(f"{page_id}/video_reels", {
        "upload_phase": "finish", "video_id": vid,
        "video_state": "PUBLISHED", "description": job["caption"],
    }, method="POST", token=page_token)
    return vid


def pick_slot(arg):
    if arg in ("a", "b"):
        return arg
    return "a" if datetime.datetime.now(datetime.timezone.utc).hour < 17 else "b"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--slot", default="auto", choices=["a", "b", "auto"])
    a = ap.parse_args()

    if a.discover:
        info = account()
        print(f"@{IG_HANDLE}: ig={info['ig_id']} page={info['page_id']} ({info['page_name']})")
        return

    slot = pick_slot(a.slot)
    jobs = json.load(open(JOBS))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"posted": []}
    done = set(state["posted"])
    due = [j for j in jobs if j["id"].endswith("-" + slot) and j["id"] not in done]
    if not due:
        print(f"Slot {slot}: nichts mehr offen ({len(done)}/{len(jobs)} gepostet).")
        return
    job = due[0]

    if not a.run:
        print(f"[DRY] Slot {slot}: als Naechstes {job['id']} ({job['type']}, {len(job['media'])} Medien)")
        print("      Caption:", job["caption"][:100].replace("\n", " / "))
        return

    info = account()
    if job["type"] == "carousel":
        mid = publish_ig_carousel(info["ig_id"], job)
        print(f"[OK-IG] {job['id']} -> media {mid}")
        try:
            fid = publish_fb_carousel(info["page_id"], info["page_token"], job)
            print(f"[OK-FB] {job['id']} -> post {fid}")
        except Exception as e:
            print(f"[WARN-FB] {job['id']}: {e}")
    else:
        mid = publish_ig_reel(info["ig_id"], job)
        print(f"[OK-IG] {job['id']} -> media {mid}")
        try:
            fid = publish_fb_reel(info["page_id"], info["page_token"], job)
            print(f"[OK-FB] {job['id']} -> reel {fid}")
        except Exception as e:
            print(f"[WARN-FB] {job['id']}: {e}")

    state["posted"].append(job["id"])
    json.dump(state, open(STATE, "w"), indent=1)
    print(f"Fortschritt: {len(state['posted'])}/{len(jobs)}")


if __name__ == "__main__":
    main()
