# تشغيل البوت على Hugging Face Spaces (Docker)

## 1. إنشاء Space جديد

افتح <https://huggingface.co/spaces/new> واختر:

| الإعداد | القيمة |
|---|---|
| Owner | حسابك |
| Space name | `omar-whatsapp-bot` (أو أي اسم) |
| License | اختياري |
| **SDK** | **Docker** ← مهم |
| Template | **Blank** |
| Hardware | CPU Basic — مجاني |
| Visibility | **Private** بشدة (راه الكوكيز فيه) |

## 2. ملف `README.md` على HF

أول ما يتخلق الـ Space، عوّض `README.md` ديالو بهاد المحتوى (الـ YAML الأول مهم باش HF يعرف المنفذ):

```markdown
---
title: Omar WhatsApp Bot
emoji: 🤖
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

WhatsApp + Gemini bot.
```

## 3. رفع الكود

نسخ كل الملفات ديال هاد المشروع (بما فيها `Dockerfile`، `start.sh`، `requirements.txt`، `package.json`، `index.js`، `server.py`، `gemini/`، `downloaders/`، `extras.py`، إلخ) للـ Space — إما عبر:

* **Git** (مستحسن):
  ```bash
  git clone https://huggingface.co/spaces/<USER>/omar-whatsapp-bot
  cd omar-whatsapp-bot
  cp -r /path/to/this/repo/* .
  git add . && git commit -m "deploy" && git push
  ```
* **واجهة الموقع**: Files → "Add file → Upload files" (مناسب لرفع سريع).

> **انتبه:** ما تبعث `gemini/cookies.txt` ولا `auth_info/` ولا `node_modules/` — هاد الإعدادات محمية فـ `.dockerignore`.

## 4. ضبط المتغيرات البيئية فـ HF

من Settings → **Variables and secrets**، زيد:

| المفتاح | النوع | القيمة |
|---|---|---|
| `PHONE_NUMBER` | Secret | رقم الهاتف ديال الجلسة (مثلاً `212688898322`) |
| `DEVELOPER_NUMBER` | Variable | `212688898322` |
| `ADMIN_TOKEN` | Secret | كلمة سر طويلة عشوائية (اختياري لكن مستحسن) |

> الـ ADMIN_TOKEN كاتزاد كهيدر `X-Admin-Token` عند طلبات `/admin/*`. كاتحمي إندبوينتس الكوكيز من أي طلب من برّا الكونتاينر (أصلاً مغلقين من الخارج، ولكن طبقة ثانية ما كتضرش).

## 5. (اختياري ولكن مستحسن) إضافة Persistent Storage

Settings → **Persistent storage** → اختر "Small" ($5/شهر). كاتمونتي ديركت على `/data`:
* `/data/auth/` ← جلسة WhatsApp تبقى مع الـ restart
* `/data/cookies/` ← كوكيز Gemini تبقى

بدونها، كل ما يـ restart الـ Space (كل 48h فالـ free tier) خاصك تعاود تربط الـ WhatsApp وتعاود تصيفط الكوكيز عبر `/cookie add`.

## 6. أول إقلاع — ربط WhatsApp

1. الـ Space يبدأ يبني (بحوالي 5-10 دقايق أول مرة).
2. ملي يبدا يخدم، افتح **Logs** ديال الـ Space.
3. غادي تشوف فالـ logs:
   ```
   Pair with this code on your phone: ABCD-1234
   ```
4. فهاتفك → WhatsApp → الإعدادات → الأجهزة المرتبطة → **ربط جهاز** → **ربط بكود الهاتف بدلاً من ذلك** → دخل الكود.
5. ملي ينربط، غادي تشوف "WhatsApp bot connected successfully!" فالـ logs.

## 7. أول إقلاع — صيفط الكوكيز ديال Gemini

من رقم المطور (212688898322 — اللي ضبطتي فـ DEVELOPER_NUMBER):

1. صيفط للبوت ملف `cookies.txt` (نسخة export من إضافة "Cookie-Editor" مثلاً) مع caption:
   ```
   /cookie add
   ```
2. البوت غادي يجاوب: "تمت إضافة الكوكي فالخانة 1 — اختبار ناجح مع Gemini 🟢".
3. كرر العملية مع 9 حسابات ديال Gemini مختلفة (ولا قد ما عندك). كل ما زاد العدد، قل الضغط على حساب واحد.

## 8. التحكم الكامل من الشات

كل أوامر الكوكيز خاصة بالمطور (الرقم 212688898322 — الباقي يجاوبهم البوت بـ "هاد الأمر خاص بالمطور"):

| الأمر | الوظيفة |
|---|---|
| `/cookie list` | عرض كل الخانات وحالتها (🟢 صحية / 🔴 مريضة) |
| `/cookie add` | أضف الكوكي للخانة الفارغة الأولى (مع ملف مرفق) |
| `/cookie add 3` | أضف الكوكي للخانة 3 بالضبط (مع ملف مرفق) |
| `/cookie del 3` | امسح الكوكي ديال الخانة 3 |
| `/cookie test` | اختبر كل الخانات مع Gemini |
| `/cookie test 3` | اختبر الخانة 3 فقط |

## 9. كيفاش كيخدم التدوير (Rotation)

* عند كل طلب لـ Gemini، البوت كياخد الكوكي اللي بعد فالـ pool (round-robin).
* إلا الكوكي رفض الطلب (401/403/429)، البوت كيعلم الخانة كـ "مريضة" لمدة ساعة، وكيعاود مع الخانة اللي بعد، وهكذا.
* بعد ساعة، الخانة كترجع تلقائياً.
* بهاد الطريقة، 10 حسابات كيقدمو ~10x ديال الـ rate-limit مقابل حساب واحد.

## 10. مشاكل شائعة

| المشكلة | الحل |
|---|---|
| الـ Space نعس → البوت قطع | فالـ free tier هاد عادي. ترقّى لـ CPU Basic مدفوع باش يبقى 24/7. |
| "No cookies configured" | صيفط `/cookie add` مع ملف cookies.txt من رقم المطور. |
| كل الكوكيز 🔴 sick | احتمال انتهت صلاحيتها — استخرج كوكيز جدد من Gemini وعاود `/cookie add`. |
| ما كنشوفش pairing code | تأكد من PHONE_NUMBER فالـ Secrets، شوف Logs مرة أخرى بعد restart. |

## 11. تشغيل محلي للاختبار قبل HF

```bash
docker build -t omar-bot .
docker run --rm -it -p 7860:7860 \
    -e PHONE_NUMBER=212688898322 \
    -e DEVELOPER_NUMBER=212688898322 \
    -v "$PWD/data:/data" \
    omar-bot
```

افتح <http://localhost:7860> → غادي تشوف صفحة "Container is alive".
شوف logs ديال الـ container باش تخرج pairing code، وكمل نفس خطوات HF.
