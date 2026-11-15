import os, sqlite3, math, hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()
YT_KEY = os.getenv("YT_KEY")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")   # لتيك توك (من حسابك في Apify)
DB = "trends.db"

# الشرق الأوسط والدول العربية (الجلب التلقائي كل 30 دقيقة، يستهلك نحو 18 وحدة في كل مرة)
ME_REGIONS = ["SA", "AE", "EG", "JO", "LB", "IQ", "KW", "QA", "BH", "OM",
              "YE", "PS", "LY", "SD", "MA", "DZ", "TN", "TR"]

# العالم (يُستخدم عند اختيار «العالم» في الصفحة فقط، يستهلك نحو 100 وحدة في كل مرة)
WORLD_REGIONS = (
    "AE AR AT AU AZ BA BD BE BG BH BO BR BY CA CH CL CO CR CY CZ DE DK DO DZ EC EE EG ES "
    "FI FR GB GE GH GR GT HK HN HR HU ID IE IL IN IQ IS IT JM JO JP KE KR KW KZ LB LI LK "
    "LT LU LV LY MA ME MK MT MX MY NG NI NL NO NP NZ OM PA PE PG PH PK PL PR PT PY QA RO "
    "RS RU SA SE SG SI SK SN SV TH TN TR TW TZ UA UG US UY VE VN YE ZA ZW"
).split()


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS trends(
      id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, url TEXT UNIQUE,
      title TEXT, owner TEXT, image_url TEXT, published_at TEXT,
      views INT, likes INT, comments INT, regions TEXT, score REAL,
      source TEXT, first_seen TEXT)""")
    c.commit(); c.close()

def trend_score(t):
    try: pub = datetime.fromisoformat(t["published_at"].replace("Z", "+00:00"))
    except Exception: pub = datetime.now(timezone.utc)
    hours = max((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 0)
    eng = (math.log10(1 + t["views"]) + 2 * math.log10(1 + t["likes"])
           + 3 * math.log10(1 + t["comments"]))
    return round(eng / (hours + 2) ** 1.2, 4)

def save(t):
    t["score"] = trend_score(t)
    t["first_seen"] = datetime.now(timezone.utc).isoformat()
    c = db()
    row = c.execute("SELECT id, regions FROM trends WHERE url=?", (t["url"],)).fetchone()
    if row:   # موجود: حدّث الأرقام وادمج الدول فقط
        old = set(filter(None, (row["regions"] or "").split(",")))
        new_r = set(filter(None, t["regions"].split(",")))
        c.execute("UPDATE trends SET views=?,likes=?,comments=?,score=?,regions=? WHERE id=?",
                  (t["views"], t["likes"], t["comments"], t["score"],
                   ",".join(sorted(old | new_r)), row["id"]))
        is_new = False
    else:
        c.execute("""INSERT INTO trends(platform,url,title,owner,image_url,published_at,
          views,likes,comments,regions,score,source,first_seen) VALUES
          (:platform,:url,:title,:owner,:image_url,:published_at,:views,:likes,
           :comments,:regions,:score,:source,:first_seen)""", t)
        is_new = True
    c.commit(); c.close()
    return is_new

def yt(path, params):
    params["key"] = YT_KEY
    r = requests.get(f"https://www.googleapis.com/youtube/v3/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_region(region, mode):
    """يجلب ترندات دولة واحدة. إذا فشلت الدولة نتخطاها ولا يتوقف الباقي."""
    try:
        if mode == "search":   # 100 وحدة لكل بحث، استخدمه بحذر
            since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            found = yt("search", {"part": "id", "type": "video", "order": "viewCount",
                                  "publishedAfter": since, "regionCode": region, "maxResults": 10})
            ids = [i["id"]["videoId"] for i in found["items"]]
            if not ids:
                return region, []
            return region, yt("videos", {"part": "snippet,statistics", "id": ",".join(ids)})["items"]
        # وحدة واحدة فقط
        return region, yt("videos", {"part": "snippet,statistics", "chart": "mostPopular",
                                     "regionCode": region, "maxResults": 10})["items"]
    except Exception as e:
        print("YouTube skip", region, "->", e)
        return region, []

def collect_youtube(mode="chart", regions=None):
    if not YT_KEY:
        print("YT_KEY غير موجود: تأكدي من ملف .env (YT_KEY=مفتاحك) ومن وجوده بجانب main.py")
        return 0
    regions = regions or ME_REGIONS
    # نجلب الدول بالتوازي لتسريع العملية، ثم نحفظ واحداً واحداً
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda r: fetch_region(r, mode), regions))
    added = 0
    for region, items in results:
        for v in items:
            s, st = v["snippet"], v.get("statistics", {})
            added += save({
                "platform": "youtube", "url": f"https://www.youtube.com/watch?v={v['id']}",
                "title": s["title"], "owner": s["channelTitle"],
                "image_url": s["thumbnails"]["high"]["url"], "published_at": s["publishedAt"],
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)), "regions": region, "source": "auto"})
    return added

def download_thumb(url):
    """يحمّل صورة الغلاف إلى مجلد thumbs، لأن روابط صور تيك توك تنتهي صلاحيتها بعد ساعات"""
    if not url:
        return ""
    try:
        name = hashlib.md5(url.encode()).hexdigest() + ".jpg"
        path = os.path.join("thumbs", name)
        if not os.path.exists(path):
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        return "http://127.0.0.1:8000/thumbs/" + name
    except Exception as e:
        print("thumb failed:", e)
        return url

def collect_tiktok(tags, per_tag=10):
    """يطلب من Apify أفضل فيديوهات الهاشتاقات ثم يحفظها بنفس الشكل الموحد"""
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN غير موجود في ملف .env")
    r = requests.post(
        "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json={"hashtags": tags, "resultsPerPage": per_tag},
        timeout=290)
    if r.status_code not in (200, 201):
        # نعرض الرمز فقط ولا نطبع الرابط لأنه يحتوي التوكن
        raise RuntimeError(f"Apify رد بالرمز {r.status_code}: {r.text[:200]}")
    added = 0
    for v in r.json():
        url = v.get("webVideoUrl")
        if not url:          # عناصر الأخطاء لا فيها رابط فيديو
            continue
        author = (v.get("authorMeta") or {}).get("name") or ""
        cover = (v.get("videoMeta") or {}).get("coverUrl") or ""
        added += save({
            "platform": "tiktok", "url": url,
            "title": (v.get("text") or "فيديو تيك توك")[:200],
            "owner": author, "image_url": download_thumb(cover),
            "published_at": v.get("createTimeISO") or "",
            "views": int(v.get("playCount") or 0), "likes": int(v.get("diggCount") or 0),
            "comments": int(v.get("commentCount") or 0), "regions": "", "source": "auto"})
    return added

def cycle(mode="chart"):
    print("[cycle] ترندات جديدة:", collect_youtube(mode, ME_REGIONS))

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
os.makedirs("thumbs", exist_ok=True)
app.mount("/thumbs", StaticFiles(directory="thumbs"), name="thumbs")
sched = BackgroundScheduler()

@app.on_event("startup")
def start():
    init_db()
    sched.add_job(cycle, "interval", minutes=30, next_run_time=datetime.now())
    sched.start()

@app.get("/")
def home():
    return {"app": "Trend Mind", "try": ["/trends", "/docs"]}

@app.get("/trends")
def trends(sort: str = "score", limit: int = 30, region: str = "", platform: str = ""):
    """sort: score (الأعلى نقاطاً) | new (الأحدث إضافة) | spread (الأكثر انتشاراً بين الدول)"""
    order = {
        "new": "first_seen DESC",
        "spread": "(LENGTH(regions) - LENGTH(REPLACE(regions, ',', ''))) DESC, score DESC",
    }.get(sort, "score DESC")
    sql, args, where = "SELECT * FROM trends", [], []
    if region:
        where.append("',' || regions || ',' LIKE ?")
        args.append(f"%,{region.upper()},%")
    if platform:
        where.append("platform = ?")
        args.append(platform)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ?"
    args.append(limit)
    c = db(); rows = c.execute(sql, args).fetchall(); c.close()
    return [dict(r) for r in rows]

@app.post("/collect-now")
def collect_now(mode: str = "chart", scope: str = "me"):
    """scope: me (الشرق الأوسط) | world (العالم)"""
    regions = WORLD_REGIONS if scope == "world" else ME_REGIONS
    added = collect_youtube(mode, regions)
    print(f"[collect-now] {scope}: ترندات جديدة: {added}")
    return {"ok": True, "added": added, "countries": len(regions)}

@app.post("/collect-tiktok")
def collect_tiktok_now(tags: str = "fyp", per_tag: int = 10):
    """tags: هاشتاقات مفصولة بفاصلة، مثل: fyp,تقنية,اليمن (حد أقصى 5)"""
    tag_list = [t.strip().lstrip("#") for t in tags.replace("،", ",").split(",") if t.strip()][:5]
    per_tag = max(1, min(per_tag, 20))      # نحدد العدد حفاظاً على رصيد Apify
    try:
        added = collect_tiktok(tag_list, per_tag)
    except Exception as e:
        print("TikTok error:", e)
        return JSONResponse(status_code=502, content={"detail": str(e)})
    print(f"[collect-tiktok] {tag_list}: ترندات جديدة: {added}")
    return {"ok": True, "added": added, "tags": tag_list}

class Link(BaseModel):
    platform: str
    url: str
    title: str
    owner: str
    image_url: str = ""
    views: int = 0
    likes: int = 0
    comments: int = 0

@app.post("/trends/link")
def add_link(l: Link):
    t = l.model_dump()
    t.update(published_at=datetime.now(timezone.utc).isoformat(), regions="", source="manual")
    return {"added": save(t)}
