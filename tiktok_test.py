import os, json, requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('APIFY_TOKEN')
if not TOKEN:
    raise SystemExit('APIFY_TOKEN غير موجود في ملف .env')

# نطلب 3 فيديوهات فقط من هاشتاق واحد لتوفير الرصيد
r = requests.post(
    'https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items',
    params={'token': TOKEN},
    json={'hashtags': ['fyp'], 'resultsPerPage': 3},
    timeout=290,
)
print('رمز الرد:', r.status_code)
if r.status_code not in (200, 201):
    print(r.text[:300])
    raise SystemExit

rows = r.json()
print('عدد النتائج:', len(rows))
for v in rows:
    print('-' * 40)
    print('النص    :', (v.get('text') or '')[:70])
    print('الحساب  :', (v.get('authorMeta') or {}).get('name'))
    print('التاريخ :', v.get('createTimeISO'))
    print('المشاهدات:', v.get('playCount'), '| الإعجابات:', v.get('diggCount'))
    print('الغلاف  :', ((v.get('videoMeta') or {}).get('coverUrl') or '')[:60])
    print('الرابط  :', v.get('webVideoUrl'))

if rows:
    print('\nكل أسماء الحقول في أول نتيجة:')
    print(list(rows[0].keys()))
