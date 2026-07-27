#!/usr/bin/env python3
"""
Meta-Autoposter: Instagram + Facebook ueber die Meta Graph API, config-getrieben.

Postet Bild-KARUSSELLS und REELS auf ein IG-Business-Konto und die verknuepfte
FB-Seite. Entstanden aus dem gptagency-Shorts-Poster, generalisiert im
DogWOW-Projekt (Juli 2026). Kein natives IG-Scheduling in der API, deshalb
laeuft das Ding per Cron (GitHub Actions) und postet je Lauf den naechsten
faelligen Job.

Dateien (liegen neben publisher.py, Pfade in config.json aenderbar):
  config.json   Kampagnen-Konfiguration (siehe examples/config.json)
  jobs.json     Geordnete Post-Liste (siehe examples/jobs.json)
  state.json    Fortschritt {"posted": ["job-ids"]}; Quelle der Wahrheit,
                im Cloud-Betrieb vom Workflow zurueckcommitten lassen

Job-Typen:
  carousel  media = Liste von Bildpfaden (max 10! IG-Limit) -> IG-Karussell
            (is_carousel_item-Container -> CAROUSEL-Container -> publish)
            + FB-Feed-Post mit attached_media (unveroeffentlichte Fotos)
  reel      media = [ein Videopfad] -> IG-Reel (REELS-Container, Polling auf
            FINISHED, publish) + FB-Page-Reel (start -> file_url-Upload -> finish)
  image     media = [ein Bildpfad] -> IG-Einzelbild + FB-Foto-Post

Slots: Job-IDs enden auf -a/-b/... (frei waehlbar). --slot X nimmt den naechsten
offenen Job dieses Suffixes; --slot auto mappt die aktuelle UTC-Stunde ueber
config.slots (z.B. {"a": [0,17], "b": [17,24]}); ohne Slot-Konzept einfach
Job-IDs ohne Suffix und --slot any.

Medien MUESSEN oeffentlich per HTTPS erreichbar sein (Meta laedt server-seitig).
Bewaehrt: PUBLIC GitHub-Repo, raw.githubusercontent-URLs; config.raw_base +
Jobpfad ergibt die URL. Das Medien-Repo muss bis Kampagnenende public bleiben.

Token: Long-Lived User Token mit instagram_basic, instagram_content_publish,
pages_show_list, pages_read_engagement, pages_manage_posts, business_management.
Quelle: Env-Var (config.token_env, Default META_TOKEN) oder Datei
(config.token_file). Beschaffung/Verlaengerung: siehe README.

  python3 publisher.py --discover              # Konten anzeigen
  python3 publisher.py --dry-run --slot a      # Vorschau
  python3 publisher.py --run --slot auto       # posten
"""
import json, os, sys, time, random, argparse, datetime, urllib.request, urllib.parse, urllib.error

GRAPH = "https://graph.facebook.com/v21.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        sys.exit("FEHLER: config.json fehlt (Vorlage: examples/config.json).")
    cfg = json.load(open(path))
    cfg.setdefault("token_env", "META_TOKEN")
    cfg.setdefault("token_file", "")
    cfg.setdefault("jobs", "jobs.json")
    cfg.setdefault("state", "state.json")
    cfg.setdefault("slots", {"a": [0, 17], "b": [17, 24]})
    cfg.setdefault("fb_enabled", True)
    cfg.setdefault("ig_music", "off")   # "set" (config.ig_audio_set) EMPFOHLEN | "trending" | "off"
    cfg.setdefault("ig_audio_set", [])  # Liste kuratierter audio_ids fuer ig_music="set"
    for k in ("ig_handle", "raw_base"):
        if not cfg.get(k):
            sys.exit(f"FEHLER: config.json braucht '{k}'.")
    return cfg


def get_token(cfg):
    tok = os.environ.get(cfg["token_env"])
    if tok:
        return tok.strip()
    if cfg["token_file"]:
        f = os.path.expanduser(cfg["token_file"])
        if os.path.exists(f):
            return open(f).read().strip()
    sys.exit(f"FEHLER: Kein Token ({cfg['token_env']} oder {cfg['token_file']}).")


class Api:
    def __init__(self, cfg):
        self.token = get_token(cfg)

    def __call__(self, path, params=None, method="GET", data=None, token=None):
        params = dict(params or {})
        params["access_token"] = token or self.token
        url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API-FEHLER {e.code} bei {method} {path}: {e.read().decode()}")


def account(api, cfg):
    pages = api("me/accounts", {"fields": "name,access_token,instagram_business_account{username,id}", "limit": 100})
    for p in pages.get("data", []):
        iga = p.get("instagram_business_account")
        if iga and iga.get("username") == cfg["ig_handle"]:
            return {"ig_id": iga["id"], "page_id": p["id"],
                    "page_token": p["access_token"], "page_name": p["name"]}
    sys.exit(f"FEHLER: IG-Konto @{cfg['ig_handle']} nicht in me/accounts. "
             "App-Asset-Freigabe pruefen (README: Re-Auth mit 'alle Assets').")


def wait_finished(api, container_id, label, tries=80):
    for _ in range(tries):
        st = api(container_id, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"Container-Fehler bei {label}: {st.get('status')}")
        time.sleep(5)
    raise RuntimeError(f"Timeout beim Verarbeiten von {label}")


# ---------- Instagram ----------
def ig_carousel(api, ig_id, job, url):
    if len(job["media"]) > 10:
        raise RuntimeError(f"{job['id']}: IG-Karussell-Limit ist 10 Bilder ({len(job['media'])} uebergeben).")
    children = []
    for path in job["media"]:
        item = api(f"{ig_id}/media", method="POST",
                   data={"image_url": url(path), "is_carousel_item": "true"})
        children.append(item["id"])
    parent = api(f"{ig_id}/media", method="POST", data={
        "media_type": "CAROUSEL", "children": ",".join(children), "caption": job["caption"]})
    wait_finished(api, parent["id"], job["id"])
    return api(f"{ig_id}/media_publish", method="POST", data={"creation_id": parent["id"]})["id"]


def set_audio_id(cfg, used):
    """Zufaelligen Track aus config.ig_audio_set (Liste von audio_ids), der in den
    letzten 6 Reels nicht lief. EMPFOHLEN gegenueber trending: der Trending-Top
    passt oft nicht zum Vibe. Set vorab kuratieren + je Track testen (Container
    muss FINISHED erreichen), sonst 2207082 beim Posten."""
    aset = (cfg or {}).get("ig_audio_set") or []
    if not aset:
        return None
    recent = set(used[-6:])
    pool = [a for a in aset if a not in recent] or aset
    aid = random.choice(pool)
    print(f"[MUSIK] Set-Track {aid}")
    return aid


def trending_audio_id(api, ig_id, used):
    """Ersten Trending-Track waehlen, der in den letzten 10 Reels nicht lief
    (Rotation gegen Wiederholung; None = ohne Musik weiter).
    ACHTUNG: Trending-Top ist oft themenfremd — lieber config.ig_audio_set nutzen."""
    try:
        res = api("ig_audio", {"audio_type": "music", "ig_user_id": ig_id})
        tracks = res.get("audio") or []
        recent = set(used[-10:])
        pick = next((t for t in tracks if t["audio_id"] not in recent), tracks[0] if tracks else None)
        if pick:
            print(f"[MUSIK] {pick.get('title')} - {pick.get('display_artist')} ({pick['audio_id']})")
            return pick["audio_id"]
    except Exception as e:
        print(f"[WARN-MUSIK] Trending-Abruf fehlgeschlagen: {e}")
    return None


def ig_reel(api, ig_id, job, url, cfg=None, state=None):
    data = {"media_type": "REELS", "video_url": url(job["media"][0]),
            "caption": job["caption"], "share_to_feed": "true"}
    if job.get("cover"):
        data["cover_url"] = url(job["cover"])       # eigenes Vorschaubild (9:16)
    elif job.get("thumb_offset") is not None:
        data["thumb_offset"] = str(job["thumb_offset"])  # Frame bei ms-Offset
    # Musik: config ig_music="trending" ODER Job-Feld audio_id.
    # WICHTIG: Das Video braucht eine Audiospur (notfalls stille AAC-Spur einbauen),
    # sonst scheitert der Container mit 2207082!
    used = (state or {}).setdefault("used_audio", [])
    aid = job.get("audio_id")
    trending_pick = False
    if not aid and cfg and cfg.get("ig_music") == "set":
        aid = set_audio_id(cfg, used)          # kuratiertes Set (empfohlen)
        trending_pick = aid is not None
    elif not aid and cfg and cfg.get("ig_music") == "trending":
        aid = trending_audio_id(api, ig_id, used)
        trending_pick = aid is not None
    if aid:
        data["audio_configuration"] = json.dumps(
            {"audio_id": aid,
             "audio_volume": int((cfg or {}).get("ig_audio_volume", 100)),
             "video_volume": int((cfg or {}).get("ig_video_volume", 0))})
    try:
        cre = api(f"{ig_id}/media", method="POST", data=data)
        wait_finished(api, cre["id"], job["id"])
    except Exception as e:
        if not aid:
            raise
        print(f"[WARN-MUSIK] Container mit Musik fehlgeschlagen ({e}), Retry ohne Musik.")
        data.pop("audio_configuration", None)
        cre = api(f"{ig_id}/media", method="POST", data=data)
        wait_finished(api, cre["id"], job["id"])
    pub = api(f"{ig_id}/media_publish", method="POST", data={"creation_id": cre["id"]})["id"]
    if trending_pick and "audio_configuration" in data:
        used.append(aid)
    return pub


def ig_image(api, ig_id, job, url):
    cre = api(f"{ig_id}/media", method="POST",
              data={"image_url": url(job["media"][0]), "caption": job["caption"]})
    wait_finished(api, cre["id"], job["id"], tries=24)
    return api(f"{ig_id}/media_publish", method="POST", data={"creation_id": cre["id"]})["id"]


# ---------- Facebook (best-effort) ----------
def fb_carousel(api, page_id, page_token, job, url):
    ids = [api(f"{page_id}/photos", method="POST", token=page_token,
               data={"url": url(p), "published": "false"})["id"] for p in job["media"]]
    data = {"message": job["caption"]}
    for i, mid in enumerate(ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
    return api(f"{page_id}/feed", method="POST", token=page_token, data=data)["id"]


def fb_reel(api, page_id, page_token, job, url):
    start = api(f"{page_id}/video_reels", {"upload_phase": "start"}, method="POST", token=page_token)
    req = urllib.request.Request(start["upload_url"], method="POST")
    req.add_header("Authorization", f"OAuth {page_token}")
    req.add_header("file_url", url(job["media"][0]))
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()
    api(f"{page_id}/video_reels", {
        "upload_phase": "finish", "video_id": start["video_id"],
        "video_state": "PUBLISHED", "description": job["caption"]},
        method="POST", token=page_token)
    return start["video_id"]


def fb_image(api, page_id, page_token, job, url):
    return api(f"{page_id}/photos", method="POST", token=page_token,
               data={"url": url(job["media"][0]), "message": job["caption"]})["id"]


IG = {"carousel": ig_carousel, "reel": ig_reel, "image": ig_image}
FB = {"carousel": fb_carousel, "reel": fb_reel, "image": fb_image}


def pick_slot(cfg, arg):
    if arg != "auto":
        return arg
    h = datetime.datetime.now(datetime.timezone.utc).hour
    for name, (lo, hi) in cfg["slots"].items():
        if lo <= h < hi:
            return name
    return "any"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--slot", default="auto", help="a|b|...|any|auto")
    a = ap.parse_args()

    cfg = load_config()
    api = Api(cfg)

    if a.discover:
        pages = api("me/accounts", {"fields": "name,instagram_business_account{username,id}", "limit": 100})
        for p in pages.get("data", []):
            iga = p.get("instagram_business_account")
            if iga:
                print(f"  {p['name']!r:40} IG @{iga['username']}  ig={iga['id']}  page={p['id']}")
        return

    slot = pick_slot(cfg, a.slot)
    jobs = json.load(open(os.path.join(HERE, cfg["jobs"])))
    state_path = os.path.join(HERE, cfg["state"])
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"posted": []}
    done = set(state["posted"])
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    def ready(j):
        nb = j.get("not_before")
        if nb and now_utc < datetime.datetime.fromisoformat(nb.replace("Z", "+00:00")):
            return False
        return True
    due = [j for j in jobs if j["id"] not in done and ready(j) and
           (slot == "any" or j["id"].endswith("-" + slot))]
    if not due:
        print(f"Slot {slot}: nichts mehr offen ({len(done)}/{len(jobs)} gepostet).")
        return
    job = due[0]
    url = lambda p: cfg["raw_base"].rstrip("/") + "/" + p

    if not a.run:
        print(f"[DRY] Slot {slot}: {job['id']} ({job['type']}, {len(job['media'])} Medien)")
        print("      Caption:", job["caption"][:100].replace(chr(10), " / "))
        return

    info = account(api, cfg)
    if job["type"] == "reel":
        mid = ig_reel(api, info["ig_id"], job, url, cfg, state)
    else:
        mid = IG[job["type"]](api, info["ig_id"], job, url)
    print(f"[OK-IG] {job['id']} -> {mid}")
    if job.get("fb", cfg["fb_enabled"]):
        try:
            fid = FB[job["type"]](api, info["page_id"], info["page_token"], job, url)
            print(f"[OK-FB] {job['id']} -> {fid}")
        except Exception as e:
            print(f"[WARN-FB] {job['id']}: {e}")

    state["posted"].append(job["id"])
    json.dump(state, open(state_path, "w"), indent=1)
    print(f"Fortschritt: {len(state['posted'])}/{len(jobs)}")


if __name__ == "__main__":
    main()
