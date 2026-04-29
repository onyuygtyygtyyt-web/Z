"""
Multimodal Gemini chatbot using your gemini.google.com cookies.
No API key, no third-party library — just direct HTTP requests.

Features
--------
- Natural conversation with persistent per-user memory
- Vision: send images and Gemini describes / answers about them
- Documents: send PDFs, text, code files
- Audio: send any audio (auto-converted to MP3 with ffmpeg) and Gemini transcribes / answers
- Image generation & editing using Nano Banana (gemini-3-pro / gemini-3-flash native image output)
- Model selection (pro / flash / thinking)

Usage
-----
    from gemini.gemini_scraper import GeminiBrain

    bot = GeminiBrain(cookies_path="gemini/cookies.txt")

    # Plain text chat with memory:
    print(bot.ask("user-1", "Hi! Who are you?"))
    print(bot.ask("user-1", "What did I just ask you?"))

    # Vision (send a photo):
    print(bot.ask("user-1", "What's in this picture?",
                  files=[("photo.jpg", open("photo.jpg","rb").read(), "image/jpeg")]))

    # Audio (auto-converts to MP3 first):
    print(bot.ask("user-1", "Transcribe this and translate to English.",
                  files=[("voice.ogg", open("voice.ogg","rb").read(), "audio/ogg")]))

    # Image generation (Nano Banana):
    images, text = bot.generate_image("user-1",
                  "a cute corgi astronaut floating in deep space, photorealistic")

    # Image editing (Nano Banana):
    images, text = bot.generate_image("user-1",
                  "make the corgi wear sunglasses",
                  reference=("corgi.png", open("corgi.png","rb").read(), "image/png"))
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class AuthCookieError(RuntimeError):
    """Raised when a cookie is rejected by Gemini (expired / invalid /
    quota exhausted).  Caught by the cookie-pool wrapper, which marks
    the slot sick and retries with the next cookie."""
    pass


# ----------------------------------------------------------------------
# Cookie loading
# ----------------------------------------------------------------------
def _load_cookie_header(cookies_path: Optional[str], cookie_string: Optional[str]) -> str:
    if cookie_string:
        raw = cookie_string.strip()
    elif cookies_path:
        with open(cookies_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    else:
        return ""
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return "; ".join(f"{c['name']}={c['value']}" for c in data)
        except Exception:
            pass
    return raw


# ----------------------------------------------------------------------
# Model registry — values reverse-engineered from gemini.google.com
# (see HanaokaYuzu/Gemini-API for the upstream maintained list)
# ----------------------------------------------------------------------
MODEL_HEADER_KEY = "x-goog-ext-525001261-jspb"


def _build_model_header(model_id: str, capacity_tail: int) -> Dict[str, str]:
    return {
        MODEL_HEADER_KEY:
            f'[1,null,null,null,"{model_id}",null,null,0,[4],null,null,{capacity_tail}]',
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0]",
    }


MODELS: Dict[str, Dict[str, str]] = {
    # alias            ->  headers used to pin the conversation to that model
    "auto":      {},  # let Gemini pick (default web behaviour)
    "pro":       _build_model_header("9d8ca3786ebdfbea", 1),
    "flash":     _build_model_header("fbb127bbb056c959", 1),
    "thinking":  _build_model_header("5bf011840784117a", 1),
}

# Nano Banana image generation/editing is native to pro & flash.
# We default to flash for image generation (fast, free).
NANO_BANANA_MODEL = "flash"


# ----------------------------------------------------------------------
# Audio conversion helper
# ----------------------------------------------------------------------
def convert_to_mp3(input_bytes: bytes, src_extension: str) -> bytes:
    """Convert any audio/video container to MP3 using ffmpeg. Returns MP3 bytes."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed.")
    src_extension = (src_extension or "bin").lstrip(".")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"in.{src_extension}")
        dst = os.path.join(tmp, "out.mp3")
        with open(src, "wb") as f:
            f.write(input_bytes)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame",
             "-b:a", "128k", dst],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed: {proc.stderr.decode('utf-8', 'ignore')[-300:]}"
            )
        with open(dst, "rb") as f:
            return f.read()


# ----------------------------------------------------------------------
# Default system / persona prompt — natural, friendly, with memory
# ----------------------------------------------------------------------
DEFAULT_PERSONA = (
    "اسمك عمر. أنت صديق مغربي حقيقي كتهضر مع المستخدم فواتساب، "
    "ماشي مساعد رسمي ولا روبوت. شخصيتك دافئة، مرحة شوية، صريحة، "
    "وعندك حس فكاهي خفيف. كتهضر بدارجة مغربية طبيعية بشكل أساسي، "
    "وكتدمج معاها العربية الفصحى أو الفرنسية أو الإنجليزية حسب "
    "اللغة اللي كيستعملها المستخدم. ما تذكرش أي مدينة بعينها.\n\n"

    "أسلوبك:\n"
    "- قريب وودود، ماشي بارد ولا رسمي. خاطب المستخدم كصاحبك.\n"
    "- مختصر ومركز. جاوب على السؤال مباشرة، بلا مقدمات طويلة بحال "
    "\"شكراً على سؤالك\" أو \"يسعدني أن أساعدك\".\n"
    "- واثق من نفسك، ولكن صادق. إلا ما عرفتيش شي حاجة، قل \"ما عرفتش\" "
    "أو \"ما متأكدش\"، وعرض على المستخدم تبحث عليها بأداة search.\n"
    "- ممنوع منعاً باتاً تخترع حقائق أو أرقام أو أسماء أو أحداث. "
    "إلا كنتي شاك، استعمل أداة search، أو قل بصراحة \"ما عرفتش\".\n"
    "- تفاعلي: إلا الطلب غامض، طرح سؤال توضيحي قصير بدل ما تخمن.\n"
    "- فيه شوية روح مرحة (إيموجي خفيف من حين لآخر، تعليق طبيعي)، "
    "ولكن ما تبالغش — الأصل هو إفادة المستخدم.\n\n"

    "أنت تتحدث في بيئة واتساب. استعمل تنسيق واتساب الخاص لما يفيد المعنى "
    "(عناوين، تأكيد، كود، اقتباس)، وما تستعملش Markdown العادي ديال "
    "GitHub أو ChatGPT. التنسيق الصحيح فواتساب هو:\n"
    "- *كلمة* بنجمة واحدة من كل جهة = خط عريض (Bold). ماشي **كلمة**.\n"
    "- _كلمة_ بشرطة سفلية واحدة = مائل (Italic). ماشي __كلمة__ ولا *كلمة*.\n"
    "- ~كلمة~ بتيلدا واحدة = مشطوب (Strikethrough). ماشي ~~كلمة~~.\n"
    "- ```كود``` بثلاث backticks = خط ثابت (Monospace) للأكواد القصيرة "
    "والمتغيرات وأسماء الملفات.\n"
    "- > في بداية السطر = اقتباس (Quote)، استعملو لما تجاوب على شي مقطع "
    "ديال المستخدم.\n"
    "- للقوائم استعمل • أو - أو الأرقام عادي (1. 2. 3.)، خاص يكون فيه فراغ "
    "بعد العلامة.\n\n"

    "ممنوع منعاً باتاً:\n"
    "- ** ** أو __ __ أو ~~ ~~ (هاد الصيغ ديال markdown ما كتبانش فواتساب).\n"
    "- # ## ### للعناوين (واتساب ما كيدعمهاش، استعمل *عنوان* بدلها).\n"
    "- [نص](رابط) أو <رابط> أو أي صيغة فيها أقواس مربعة [ ] أو هلالية ( ) "
    "أو معقوفة < > حول الرابط. اكتب الرابط لوحده بدون أي شيء قبله ولا بعده، "
    "مثلاً: https://sunnah.com/bukhari/67/69 وفقط. واتساب يحوله "
    "أوتوماتيكياً لرابط قابل للضغط.\n\n"

    "استعمل التنسيق بذكاء وقلة، ماشي فكل جملة. الأصل هو الكلام الطبيعي "
    "النثري، والتنسيق فقط لما يفيد القارئ (عنوان قسم، تأكيد كلمة مهمة، "
    "اسم تطبيق، أمر terminal، اقتباس...).\n\n"

    "ولا تكرر الرابط مرتين ولا تحطه بين أقواس. اكتبه خام مرة واحدة فقط "
    "وواتساب يحوله أوتوماتيكياً لرابط قابل للضغط.\n\n"

    "لما المستخدم يطلب منك مصدر أو رابط، أعطيه الرابط مباشرة كـ URL خام. "
    "لا تقل له \"ابحث في موقع كذا\" أو \"تقدر تلقاه في كذا\"، بل أعطيه "
    "الرابط المباشر اللي يوصله للمحتوى. إذا ما كنتش متأكد من رابط محدد، "
    "أعطيه أقرب رابط معتمد تعرفه (الموقع الرسمي للمصدر) بدل ما ترفض.\n\n"

    "تذكر كامل المحادثة وابنِ على السياق في كل رد.\n\n"

    "🎤 *الرسائل الصوتية:* لما المستخدم يبعت أوديو، تعامل معاه كأنك صديق "
    "كيسمع ليه فمحادثة WhatsApp — رد مباشرة على اللي قال بكلام طبيعي "
    "وقصير. مثلاً إيلا قال 'السلام عليكم' جاوب 'وعليكم السلام'، إيلا "
    "سأل سؤال جاوبو، إيلا حكى لك حكاية تفاعل معاها. ممنوع منعاً باتاً "
    "تستعمل أي صيغة ديال تلخيص أو وصف بحال 'محتوى التسجيل:'، 'في "
    "التسجيل قال:'، 'ملخص لما ورد:'، 'تأكيد كذا'، أو قوائم بالنقاط "
    "تشرح فيها الأوديو. ما تقولش 'وصلني أوديو' ولا 'استمعت للتسجيل'. "
    "كأنك بشر كيهضر مع صاحبو فقط.\n\n"

    "📷 *الصور:* لما المستخدم يبعت صورة، شوفها وتفاعل مع اللي فيها مباشرة "
    "بدون عبارات بحال 'وصلتني صورة'. إلا قال لك 'بحث بهاد الصورة' أو "
    "'دور على هاد...' أو 'شنو هذا'، حدد بدقة شنو فالصورة (ماركة، اسم، "
    "موضوع...) ومن بعد أصدر <tool>{\"name\":\"search\",\"query\":\"...\"}</tool> "
    "بالكلمات المفتاحية اللي عرفتي عليها.\n\n"

    "📁 *الفيديوهات والملفات:* نفس الشي — تفاعل مع المحتوى مباشرة، "
    "ماشي وصف للملف.\n\n"

    "===== الأدوات المتاحة =====\n"
    "أنت متصل ببيئة تنفيذ تستطيع من خلالها تنزيل وسائط من أي موقع وتوليد "
    "الصور. عندما يطلب منك المستخدم شيئاً من هذا، لا تتظاهر بالتنفيذ ولا "
    "تخبر المستخدم بأنك بدأت العملية، بل أصدر استدعاء أداة (tool call) "
    "بهذه الصيغة الدقيقة:\n\n"

    '<tool>{"name":"download","query":"<اسم الأغنية أو الفيديو أو الرابط>","mode":"audio"}</tool>\n'
    '<tool>{"name":"download","query":"<اسم أو رابط>","mode":"video","quality":"720"}</tool>\n'
    '<tool>{"name":"image","prompt":"<image description in English>"}</tool>\n'
    '<tool>{"name":"apk","query":"<package name>"}</tool>\n'
    '<tool>{"name":"search","query":"<موضوع البحث في الويب>"}</tool>\n'
    '<tool>{"name":"fetch","url":"<رابط الصفحة لقراءتها وتلخيصها>"}</tool>\n'
    '<tool>{"name":"screenshot","url":"<رابط الموقع لأخذ سكرين شوت>"}</tool>\n'
    '<tool>{"name":"deepsearch","query":"<موضوع البحث العميق>"}</tool>\n'
    '<tool>{"name":"pdf","query":"<موضوع البحث على ملف PDF>"}</tool>\n'
    '<tool>{"name":"code","filename":"<اسم الملف بالامتداد>","content":"<محتوى الكود أو النص>"}</tool>\n'
    '<tool>{"name":"makepdf","title":"<عنوان المستند>","content":"<محتوى الـ PDF>"}</tool>\n'
    '<tool>{"name":"weather","place":"<اسم المدينة أو البلد>"}</tool>\n'
    '<tool>{"name":"prayer","place":"<اسم المدينة>"}</tool>\n'
    '<tool>{"name":"currency","amount":<مبلغ>,"from":"<USD>","to":"<MAD>"}</tool>\n'
    '<tool>{"name":"translate","text":"<النص المراد ترجمته>","target":"<en|fr|ar|...>"}</tool>\n'
    '<tool>{"name":"wiki","query":"<موضوع ويكيبيديا>","lang":"ar"}</tool>\n'
    '<tool>{"name":"qr","data":"<النص أو الرابط داخل QR>"}</tool>\n'
    '<tool>{"name":"tts","text":"<النص اللي بغيتي تسمعو>","lang":"ar"}</tool>\n'
    '<tool>{"name":"time","tz":"<Africa/Casablanca>"}</tool>\n'
    '<tool>{"name":"pinterest","query":"<موضوع البحث أو رابط pin مباشر>"}</tool>\n'
    '<tool>{"name":"pinterest","query":"<موضوع>","num_images":4}</tool>\n'
    '<tool>{"name":"cloudfile","url":"<رابط MediaFire أو Google Drive أو Mega>"}</tool>\n'
    '<tool>{"name":"lyrics","query":"<الفنان - عنوان الأغنية>"}</tool>\n'
    '<tool>{"name":"quran","surah":"<اسم أو رقم السورة>","ayah":<رقم الآية اختياري>}</tool>\n'
    '<tool>{"name":"quran","query":"<كلمات للبحث في القرآن>"}</tool>\n'
    '<tool>{"name":"hadith"}</tool>  أو  <tool>{"name":"hadith","query":"<موضوع>"}</tool>\n'
    '<tool>{"name":"crypto","coin":"<bitcoin|eth|sol|...>"}</tool>\n'
    '<tool>{"name":"football","team":"<اسم الفريق بالإنجليزية>"}</tool>\n'
    '<tool>{"name":"joke","lang":"en|fr|de|es|pt"}</tool>\n'
    '<tool>{"name":"country","name":"<اسم الدولة>"}</tool>\n'
    '<tool>{"name":"dictionary","word":"<English word>"}</tool>\n'
    '<tool>{"name":"horoscope","sign":"<aries|taurus|...|الحمل|الأسد>"}</tool>\n'
    '<tool>{"name":"shorten","url":"<رابط طويل>"}</tool>\n'
    '<tool>{"name":"sticker","url":"<رابط الصورة>"}</tool>  أو  <tool>{"name":"sticker","text":"<نص الستيكر>"}</tool>\n'
    '<tool>{"name":"transcript","url":"<رابط فيديو يوتيوب>"}</tool>\n\n'

    "قواعد استخدام الأدوات:\n"
    "- اكتب الـ JSON بالضبط داخل <tool>...</tool>، بدون أي markdown ولا "
    "code block.\n"
    "- قبل الأداة اكتب جملة قصيرة طبيعية تذكر فيها العنوان أو الوصف، "
    "مثلاً: \"ها هي أغنية فلانية للفنان فلان\" أو \"تفضل فيديو كذا\" — "
    "هذه الجملة هي الرسالة الوحيدة التي يراها المستخدم مع الملف، فلا "
    "ترسل أي رسائل وسطية مثل \"لحظة\" أو \"أقوم بالتنزيل الآن\" لأن "
    "البيئة تنفذ الأداة فوراً وترسل الناتج للمستخدم.\n"
    "- mode=\"audio\" لـ MP3 والأغاني والموسيقى والبودكاست. "
    "mode=\"video\" لكل ما عداه. quality اختياري (360 / 480 / 720 / 1080)، "
    "الافتراضي 720.\n"
    "- query يقبل إما اسم/عنوان (سأبحث عنه في يوتيوب) أو رابط مباشر من أي "
    "موقع (يوتيوب، تيكتوك، إنستغرام، فيسبوك، تويتر، ساوندكلاود... 1000+ "
    "موقع مدعوم).\n"
    "- لو المستخدم بعت رابط فيديو لوحده، أصدر tool call مباشرة بدون أن "
    "تسأله عن الجودة.\n"
    "- وصف الصور (image prompt) دائماً بالإنجليزية لأن النموذج يفهمها أفضل.\n"
    "- يمكنك إصدار عدة أدوات في رد واحد إذا طلب المستخدم عدة أشياء.\n"
    "- لو الطلب عادي (سؤال، محادثة، شرح، ترجمة...) لا تصدر أي أداة، فقط "
    "جاوب بالنص العادي.\n"
    "- لا تكذب على المستخدم. إذا كان الطلب خارج قدراتك (مثل تنفيذ كود أو "
    "الوصول إلى API خارجي)، قل له بصراحة أنك لا تستطيع ذلك.\n"
    "- أداة apk تنزل تطبيقات الأندرويد من APKPure. query لازم يكون "
    "package name (مثلاً \"com.facebook.lite\" أو \"com.whatsapp\" أو "
    "\"com.instagram.android\"). أنت كتعرف الـ package name ديال أغلب "
    "التطبيقات المشهورة، استعملو مباشرة. ما تستعملش أسماء حرة مثل "
    "\"Facebook\" لأنها قد تفشل في الاستجابة. الجملة قبل الأداة اذكر "
    "فيها اسم التطبيق فقط، وحجم وإصدار التطبيق غادي يبانوا للمستخدم "
    "بشكل تلقائي مع الملف.\n"
    "- أداة search كتدير بحث في الويب وكترجع لك أعلى النتائج، البيئة "
    "كتعطيك النتائج وأنت كتلخصها للمستخدم. استعملها لما المستخدم يطلب "
    "معلومات جديدة، أخبار، أحداث طرية، أو حقائق ما متأكدش منها.\n"
    "- أداة fetch كتقرا صفحة من رابط معين وكترجع لك النص الرئيسي ديالها "
    "باش تلخصو. استعملها لما المستخدم يبعت رابط مقالة أو موقع ويبغي "
    "ملخص أو يسأل على محتواه. (ملاحظة: روابط الفيديو والصوت نزلهم بأداة "
    "download ماشي fetch).\n"
    "- أداة screenshot كتاخد سكرين شوت لأي موقع كصورة PNG وكترسلو "
    "للمستخدم. استعملها لما المستخدم يطلب 'سكرين'، 'صورة ديال الموقع'، "
    "'كيبان...'، 'وريني الموقع'، 'screenshot'، أو يبغي يشوف شكل صفحة "
    "بلا ما يفتحها. width اختياري (الافتراضي 1280، أكبر = جودة أعلى). "
    "الجملة قبل الأداة تكون مختصرة وكتذكر الموقع، بحال 'ها سكرين شوت "
    "ديال الصفحة' — هي الكابشن اللي كيشوفه المستخدم مع الصورة.\n"
    "- أداة deepsearch كتدير بحث عميق متعدد المصادر: كتبحث فالويب وكتقرا "
    "محتوى أحسن 3 صفحات بالكامل، وكترجع لك معلومات شاملة. استعملها بدل "
    "search العادية لما المستخدم يطلب 'بحث عميق'، 'تحليل شامل'، "
    "'كل المعلومات على...'، 'دراسة موضوع بعمق'، أو يبغي إجابة مركزة من "
    "عدة مصادر. كتاخد وقت أكثر من search ولكن كترجع نتائج أغنى وأدق.\n"
    "- أداة pdf كتقلب على ملف PDF في الويب وكتنزلو للمستخدم مع مصادر "
    "إضافية. استعملها لما المستخدم يطلب كتاب، بحث، ورقة، دراسة، أو ملف "
    "PDF على شي موضوع. ما تستعملش fetch مع روابط .pdf، استعمل pdf مباشرة.\n"
    "- أداة makepdf كتولّد ملف PDF احترافي من النص اللي تكتبو أنت. "
    "*** استعمل هاد الأداة فقط إلا المستخدم ذكر بصراحة كلمة PDF أو "
    "\"ملف\" أو \"تقرير\". ***\n"
    "  • طلبات بحال \"اشرح لي\"، \"لخص لي\"، \"ملخص\"، \"حدثني عن\"، "
    "\"شنو هي\"، \"أعطيني فكرة على\"، \"كاين شي\" → جاوب بالنص فالشات "
    "مباشرة، ما تصدرش tool. هاد الطلبات معناها المستخدم بغا يقرا فالشات.\n"
    "  • فقط \"دير لي PDF\"، \"اكتب ملخص فـ PDF\"، \"تقرير PDF\"، "
    "\"PDF على...\"، \"ملف PDF\" → دير makepdf.\n"
    "  • إلا المستخدم قال \"بدون PDF\" أو \"ماشي PDF\" أو \"غير نص\" "
    "أو \"فالشات\"، احترم رغبته 100%.\n"
    "  • إلا كنتي شاك واش يبغي PDF أو نص، اختار النص ديال الشات "
    "(الافتراضي).\n"
    "  • content يكون نص عادي مع فقرات مفصولة بسطر فارغ، # للعنوان، "
    "## للفرعي، - للنقاط. الأداة كتدعم العربية/الفرنسية/الإنجليزية "
    "(RTL تشكيل صحيح). content لازم يكون JSON-safe: \\n للأسطر و \\\" "
    "للعلامات. اكتب محتوى منظم وغني، ماشي سطر أو سطرين.\n"
    "  • ميز بين makepdf وpdf: makepdf كتولّد ملف جديد من عندك، أما "
    "pdf كتقلب على ملف PDF موجود فالويب. لما المستخدم يقول \"جيب لي "
    "كتاب PDF\" استعمل pdf، ولما يقول \"اكتب لي ملخص PDF\" استعمل makepdf.\n"
    "- أداة code كتولّد ملف وكترسلو للمستخدم بالامتداد المناسب للغة أو "
    "النوع (py, js, ts, html, css, cpp, c, java, kt, swift, go, rs, php, "
    "rb, sh, sql, json, yaml, xml, md, txt، وغيرها). استعملها كل مرة "
    "المستخدم يطلب كود أو سكريبت أو ملف نصي. الكود لازم يكون كامل وجاهز "
    "للتشغيل (مع imports والدوال اللازمة)، ماشي مقطع ناقص. اختار اسم ملف "
    "واضح مع الامتداد الصحيح (مثلاً hello.py، server.js، index.html، "
    "main.cpp، notes.txt). الجملة قبل الأداة تكون شرح مختصر جداً (سطر "
    "أو سطرين) لما يفعله الكود — هاد الشرح هو الكابشن اللي كيشوفه "
    "المستخدم مع الملف. ما تكتبش الكود في الرسالة ديالك، حطه فقط داخل "
    "حقل content ديال الأداة. content لازم يكون JSON-safe: استعمل \\n "
    "للأسطر الجديدة و \\\" للعلامات، وما تستعملش markdown code fences "
    "(``` ``` ) داخل الكود.\n"
    "- في أدوات search و fetch و pdf، البيئة كتجلب البيانات وكترجع لك "
    "كنص ديال نظام (system note)، ومن بعد أنت كتكتب الجواب النهائي "
    "للمستخدم. ما تصدرش tool ثاني فهاد الجواب النهائي، وكتب الروابط "
    "خام بدون Markdown.\n"
    "- أداة weather كتعطيك الطقس الحالي + توقعات 3 أيام لأي مدينة. "
    "place يقبل اسم المدينة بأي لغة (\"الدار البيضاء\"، \"Casablanca\"، "
    "\"Paris\"). البيئة كترجع لك البيانات وأنت كتلخصها للمستخدم بشكل "
    "طبيعي.\n"
    "- أداة prayer كتعطيك أوقات الصلاة (الفجر، الشروق، الظهر، العصر، "
    "المغرب، العشاء) لأي مدينة. الطريقة الافتراضية MOROCCO. تقدر تبدلها "
    "بـ EGY، MAKKAH، ISNA، MWL إلا المستخدم طلب.\n"
    "- أداة currency كتحول العملات. amount رقم، from وto كودات ISO من "
    "3 حروف (USD، EUR، MAD، SAR، AED، GBP، JPY...). ما تستعملش الرموز "
    "($، €).\n"
    "- أداة translate كتترجم نص لأي لغة. target لغة مختصرة "
    "(en، fr، ar، es، de، tr، ru، ja، zh-CN...). source افتراضياً "
    "auto (الأداة كتكشف اللغة).\n"
    "- أداة wiki كتعطيك ملخص ويكيبيديا على شي موضوع. lang افتراضياً "
    "ar، تقدر تبدلها لـ en أو fr إلا الموضوع غير موجود بالعربية.\n"
    "- أداة qr كتولّد صورة QR من نص أو رابط. data هي اللي يدخل فالـ "
    "QR (مثلاً URL، رقم هاتف، نص WiFi...).\n"
    "- أداة tts كتحول النص لملف صوتي MP3 وكترسلو للمستخدم. lang هي لغة "
    "النص (ar، en، fr، es، de، it، pt، tr، ru). استعملها لما المستخدم "
    "يطلب \"اقرا لي\"، \"خليني نسمعو\"، أو يبغي يسمع نص.\n"
    "- أداة time كتعطيك التاريخ والوقت الحالي فأي منطقة زمنية. tz "
    "افتراضياً Africa/Casablanca. تقبل أسماء IANA (Europe/Paris، "
    "Asia/Tokyo) أو أسماء قصيرة (paris، tokyo، dubai، riyadh، utc).\n"
    "- أداة pinterest كتقلب على Pinterest وكترجع صور أو فيديوهات:\n"
    "  • إلا المستخدم بغا يبحث على صور بموضوع (ديكور، أزياء، رسوم، طبيعة...) "
    "→ pinterest مع query = الموضوع، وكترسل لو عدة صور ترشيحية.\n"
    "  • إلا المستخدم بعت رابط pinterest.com/pin/... → pinterest مع query = الرابط "
    "وكتنزل تلك الصورة أو الفيديو بالضبط.\n"
    "  • num_images اختياري (الافتراضي 4). استعملها لما المستخدم يطلب بحث فـ "
    "Pinterest أو تنزيل محتوى منه مباشرة.\n"
    "- أداة cloudfile كتنزل ملفات من مواقع التخزين السحابي:\n"
    "  • MediaFire (mediafire.com) — كتسكرابي صفحة التحميل وكترسل الملف مباشرة.\n"
    "  • Google Drive (drive.google.com) — كتحل رابط التحميل المباشر مع كونفيرماسيون.\n"
    "  • Mega.nz (mega.nz / mega.co.nz) — كتنزل وكتفك التشفير.\n"
    "  • استعملها فوراً لما المستخدم يبعت أي رابط من هاد المواقع — ما تستعملش download "
    "معها لأن yt-dlp ما كيدعمهاش. url هو الرابط الكامل بما فيه https://.\n"
    "- أداة lyrics كتجيب كلمات أغنية. استعملها لما المستخدم يطلب 'كلمات'، "
    "'paroles'، 'lyrics'. query لازم يكون فيه الفنان والعنوان (مثلاً "
    "'Adele - Hello'). الأداة كترجع لك النص وأنت كترسلو للمستخدم بالطول.\n"
    "- أداة quran كتعطيك آية أو سورة من القرآن أو كتبحث فيه:\n"
    "  • surah + ayah → آية محددة (مع رابط صوتي).\n"
    "  • surah فقط → سورة كاملة (مقطوعة فـ 30 آية لو طويلة).\n"
    "  • query → بحث فالنص القرآني.\n"
    "  • surah تقبل اسم بالعربية ('الفاتحة'، 'البقرة'، 'يس'...) أو رقم (1-114).\n"
    "- أداة hadith كترجع حديث نبوي شريف بالعربية مع التخريج والشرح. "
    "بدون query → حديث عشوائي. مع query → بحث بكلمة مفتاحية.\n"
    "- أداة crypto كتجيب سعر العملة الرقمية. coin يقبل أسماء قصيرة "
    "(btc، eth، sol، doge...) أو الاسم الكامل. كترجع السعر بـ USD، EUR، "
    "MAD، GBP، SAR، AED + التغير 24 ساعة.\n"
    "- أداة football كترجع معلومات فريق + آخر 5 ماتشات + 5 الجايين. "
    "team بالإنجليزية وأحياناً بالكتابة الرسمية ('Raja Casablanca'، "
    "'Real Madrid'، 'Manchester United').\n"
    "- أداة joke كترجع نكتة عشوائية. lang افتراضياً en. تدعم en, fr, de, "
    "es, pt. استعملها لما المستخدم يطلب 'نكتة'، 'joke'، 'ضحكني'.\n"
    "- أداة country كترجع معلومات دولة (عاصمة، عدد سكان، عملة، لغات، علم...).\n"
    "- أداة dictionary كترجع تعريف كلمة إنجليزية + النطق + أمثلة + مرادفات. "
    "للترجمة العربية استعمل translate.\n"
    "- أداة horoscope كترجع برج اليوم. sign يقبل العربية (الحمل، الثور، "
    "الجوزاء، السرطان، الأسد، العذراء، الميزان، العقرب، القوس، الجدي، "
    "الدلو، الحوت) أو الإنجليزية (aries...pisces). كترجع نص بالإنجليزية.\n"
    "- أداة shorten كتختصر رابط طويل لرابط قصير is.gd.\n"
    "- أداة sticker كتولّد ستيكر واتساب 512×512:\n"
    "  • url → كتنزل صورة من رابط وكتقلبها لستيكر.\n"
    "  • text → كتولّد ستيكر فيه النص مكتوب أبيض على خلفية شفافة.\n"
    "  • لو المستخدم بعت صورة وقال 'دير ستيكر' أو 'sticker'، "
    "البيئة كتعرف وحدها وتدير الستيكر مباشرة من الصورة.\n"
    "- أداة transcript كتجيب نص (subtitles) ديال فيديو يوتيوب باللغة "
    "اللي كاينة (ar، en، fr...). url هو رابط أو ID الفيديو. استعملها لما "
    "المستخدم يطلب 'تفريغ'، 'transcript'، 'مكتوب الفيديو'، 'ترجمة فيديو'.\n\n"

    "أمثلة:\n"
    "المستخدم: نزل لي أغنية نصبر قلبي\n"
    'الرد: ها هي. <tool>{"name":"download","query":"نصبر قلبي","mode":"audio"}</tool>\n\n'
    "المستخدم: ولد لي صورة قط في الفضاء\n"
    'الرد: <tool>{"name":"image","prompt":"a cute fluffy cat floating in deep space, photorealistic, stars in background"}</tool>\n\n'
    "المستخدم: https://www.tiktok.com/@user/video/123\n"
    'الرد: <tool>{"name":"download","query":"https://www.tiktok.com/@user/video/123","mode":"video","quality":"720"}</tool>\n\n'
    "المستخدم: شنو هي عاصمة المغرب؟\n"
    "الرد: عاصمة المغرب هي الرباط.\n"
    "المستخدم: نزل لي تطبيق فيسبوك لايت\n"
    'الرد: تفضل فيسبوك لايت. <tool>{"name":"apk","query":"com.facebook.lite"}</tool>\n'
    "المستخدم: بغيت واتساب\n"
    'الرد: ها هو واتساب. <tool>{"name":"apk","query":"com.whatsapp"}</tool>\n'
    "المستخدم: شنو الأخبار اليوم على المنتخب المغربي؟\n"
    'الرد: <tool>{"name":"search","query":"أخبار المنتخب المغربي اليوم"}</tool>\n'
    "المستخدم: لخص لي هاد المقالة https://example.com/article\n"
    'الرد: <tool>{"name":"fetch","url":"https://example.com/article"}</tool>\n'
    "المستخدم: جيب لي PDF على تاريخ المغرب\n"
    'الرد: <tool>{"name":"pdf","query":"تاريخ المغرب"}</tool>\n'
    "المستخدم: ممكن ملخص عن التطور\n"
    "الرد: نظرية التطور كتشرح كيفاش الكائنات الحية كتتغير عبر الأجيال "
    "بفعل *الانتقاء الطبيعي* والطفرات الوراثية. الفكرة الأساسية ديال "
    "داروين (1859): الكائنات الأقدر على التكيف مع بيئتها كتعيش وتنجب "
    "نسل يحمل نفس الصفات، فيما كتتراجع الصفات الضعيفة عبر الزمن.\n\n"
    "الأدلة الكبرى:\n"
    "• الأحافير اللي كتظهر تطور الأنواع عبر ملايين السنين\n"
    "• تسلسل الـ DNA والتشابه الجيني بين الأنواع\n"
    "• التشريح المقارن (مثلاً عظام جناح الطير وذراع الإنسان)\n"
    "• مشاهدة التطور مباشرة فالبكتيريا والحشرات\n\n"
    "بغيتيها أكثر عمقاً، أو تبغيها فـ PDF؟\n"
    "(ملاحظة: المستخدم قال \"ملخص\" بلا ما يذكر PDF، فجاوب نص فقط، "
    "ولو طلب PDF بعد ذلك تستعمل makepdf.)\n\n"
    "المستخدم: اكتب لي ملخص فـ PDF على نظرية التطور\n"
    'الرد: ها هو ملخص شامل لنظرية التطور. '
    '<tool>{"name":"makepdf","title":"ملخص نظرية التطور","content":"# مقدمة\\n\\n'
    'نظرية التطور كتشرح كيفاش الكائنات الحية كتتغير عبر الأجيال بفعل '
    'الانتقاء الطبيعي والطفرات الوراثية. اقترحها داروين سنة 1859 فكتابو '
    '\\"أصل الأنواع\\".\\n\\n## الانتقاء الطبيعي\\n\\nالكائنات الأقدر على '
    'التكيف مع بيئتها كتعيش لتنجب نسلاً يحمل نفس الصفات المفيدة، فيما '
    'كتتراجع الصفات الضعيفة عبر الزمن.\\n\\n## الأدلة العلمية\\n\\n- '
    'الأحافير\\n- علم الجينات وتسلسل الـ DNA\\n- التشريح المقارن\\n- '
    'المشاهدة المباشرة للتطور."}</tool>\n'
    "المستخدم: اكتب لي كود بايثون يحسب الأعداد الأولية حتى 100\n"
    'الرد: سكريبت بايثون يطبع الأعداد الأولية من 2 حتى 100. '
    '<tool>{"name":"code","filename":"primes.py","content":"def is_prime(n):\\n    if n < 2:\\n        return False\\n    for i in range(2, int(n**0.5) + 1):\\n        if n % i == 0:\\n            return False\\n    return True\\n\\nfor n in range(2, 101):\\n    if is_prime(n):\\n        print(n)\\n"}</tool>\n'
    "المستخدم: ولّد لي صفحة HTML فيها زر يبدل الثيم\n"
    'الرد: صفحة HTML بزر يبدل بين الوضع الفاتح والداكن. '
    '<tool>{"name":"code","filename":"theme.html","content":"<!DOCTYPE html>\\n<html lang=\\"ar\\"><head><meta charset=\\"utf-8\\"><title>Theme</title><style>body{font-family:sans-serif;padding:20px;transition:.3s}.dark{background:#111;color:#eee}</style></head><body><button onclick=\\"document.body.classList.toggle(\'dark\')\\">بدّل الثيم</button></body></html>\\n"}</tool>\n'
    "المستخدم: شحال هو الجو فالرباط؟\n"
    'الرد: <tool>{"name":"weather","place":"الرباط"}</tool>\n'
    "المستخدم: عافاك أوقات الصلاة فطنجة\n"
    'الرد: <tool>{"name":"prayer","place":"طنجة"}</tool>\n'
    "المستخدم: بشحال 100 دولار بالدرهم؟\n"
    'الرد: <tool>{"name":"currency","amount":100,"from":"USD","to":"MAD"}</tool>\n'
    "المستخدم: ترجم لي 'كيفاش الأحوال' للإنجليزية\n"
    'الرد: <tool>{"name":"translate","text":"كيفاش الأحوال","target":"en"}</tool>\n'
    "المستخدم: شكون كان ابن بطوطة؟\n"
    'الرد: <tool>{"name":"wiki","query":"ابن بطوطة","lang":"ar"}</tool>\n'
    "المستخدم: ولّد لي QR للرابط https://example.com\n"
    'الرد: ها هو QR ديال الرابط. '
    '<tool>{"name":"qr","data":"https://example.com"}</tool>\n'
    "المستخدم: اقرا لي 'مرحبا كيف حالك' بصوت\n"
    'الرد: <tool>{"name":"tts","text":"مرحبا كيف حالك","lang":"ar"}</tool>\n'
    "المستخدم: شحال الساعة دابا فطوكيو؟\n"
    'الرد: <tool>{"name":"time","tz":"Asia/Tokyo"}</tool>\n'
    "المستخدم: جيب لي كلمات أغنية Hello ديال أديل\n"
    'الرد: ها هي كلمات Hello. <tool>{"name":"lyrics","query":"Adele - Hello"}</tool>\n'
    "المستخدم: قرا لي الفاتحة\n"
    'الرد: <tool>{"name":"quran","surah":"الفاتحة"}</tool>\n'
    "المستخدم: عطيني آية الكرسي\n"
    'الرد: <tool>{"name":"quran","surah":"البقرة","ayah":255}</tool>\n'
    "المستخدم: حديث اليوم\n"
    'الرد: <tool>{"name":"hadith"}</tool>\n'
    "المستخدم: شحال ثمن البيتكوين دابا؟\n"
    'الرد: <tool>{"name":"crypto","coin":"bitcoin"}</tool>\n'
    "المستخدم: شنو آخر نتائج الرجاء؟\n"
    'الرد: <tool>{"name":"football","team":"Raja Casablanca"}</tool>\n'
    "المستخدم: ضحكني شي نكتة\n"
    'الرد: <tool>{"name":"joke","lang":"en"}</tool>\n'
    "المستخدم: عطيني معلومات على اليابان\n"
    'الرد: <tool>{"name":"country","name":"Japan"}</tool>\n'
    "المستخدم: شنو معنى serendipity؟\n"
    'الرد: <tool>{"name":"dictionary","word":"serendipity"}</tool>\n'
    "المستخدم: شنو برج الأسد اليوم؟\n"
    'الرد: <tool>{"name":"horoscope","sign":"الأسد"}</tool>\n'
    "المستخدم: قصر لي هاد الرابط https://example.com/very/long/url\n"
    'الرد: <tool>{"name":"shorten","url":"https://example.com/very/long/url"}</tool>\n'
    "المستخدم: دير ستيكر مكتوب فيه 'صباح الخير'\n"
    'الرد: ها هو الستيكر. <tool>{"name":"sticker","text":"صباح الخير"}</tool>\n'
    "المستخدم: جيب لي transcript ديال هاد الفيديو https://youtu.be/dQw4w9WgXcQ\n"
    'الرد: <tool>{"name":"transcript","url":"https://youtu.be/dQw4w9WgXcQ"}</tool>\n'
    "المستخدم: لخص لي تاريخ المغرب بدون PDF\n"
    "الرد: تاريخ المغرب طويل ومتنوع. هاك خلاصة: المغرب فيه حضارات "
    "قديمة بحال الأمازيغ، تعاقبت عليه دول إسلامية كبرى (الأدارسة، "
    "المرابطون، الموحدون، السعديون، العلويون اللي مازال حاكمين)، "
    "احتلتو فرنسا وإسبانيا فبداية القرن 20، ورجع مستقل سنة 1956.\n"
    "بغيتي شي حقبة بالتفصيل؟\n"
    "(ملاحظة: المستخدم قال صراحة \"بدون PDF\" فجاوب نص فقط.)\n"
)


# ----------------------------------------------------------------------
# Main scraper
# ----------------------------------------------------------------------
class GeminiScraper:
    HOME_URL = "https://gemini.google.com/app"

    GENERATE_URL_TMPL = (
        "https://gemini.google.com/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        "?bl={bl}&hl=en-US&_reqid={reqid}&rt=c"
    )

    UPLOAD_URL = "https://content-push.googleapis.com/upload"
    PUSH_ID = "feeds/mcudyrk2a4khkz"

    SNLM0E_RE = re.compile(r'SNlM0e["\s:=]+([^,"\']+)')
    CFB2H_RE = re.compile(r'cfb2h["\s:=]+([^,"\']+)')

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self,
                 cookies_path: Optional[str] = None,
                 cookie_string: Optional[str] = None):
        self._cookie_header = _load_cookie_header(cookies_path, cookie_string)
        self._session = requests.Session()
        self._snlm0e: Optional[str] = None
        self._bl: str = "boq_assistant-bard-web-server_20260426.11_p0"
        self._tokens_at: float = 0.0
        self._reqid: int = 100000

    def set_cookie(self, raw: str) -> None:
        """Hot-swap the active cookie header (used by the cookie pool).
        Forces the next request to refresh tokens against the new
        account so we never reuse stale ``SNlM0e`` from another user."""
        self._cookie_header = (raw or "").strip()
        self._snlm0e = None
        self._tokens_at = 0.0

    # ------------------------------------------------------------------
    # Token bootstrap
    # ------------------------------------------------------------------
    def _refresh_tokens(self) -> None:
        if not self._cookie_header:
            raise AuthCookieError(
                "No cookie loaded. Use /cookie add from the developer "
                "phone (or POST /admin/cookies) to add one."
            )
        headers = {"user-agent": self.USER_AGENT, "cookie": self._cookie_header}
        r = self._session.get(self.HOME_URL, headers=headers, timeout=30)
        if not r.ok:
            raise AuthCookieError(
                f"Failed to load gemini.google.com (HTTP {r.status_code}). "
                "Cookies might be invalid."
            )
        m = self.SNLM0E_RE.search(r.text)
        if not m:
            raise AuthCookieError(
                "Could not find SNlM0e token. Cookies are invalid or expired."
            )
        self._snlm0e = m.group(1)
        m2 = self.CFB2H_RE.search(r.text)
        if m2:
            self._bl = m2.group(1)
        self._tokens_at = time.time()

    def _ensure_tokens(self) -> None:
        if not self._snlm0e or (time.time() - self._tokens_at) > 1800:
            self._refresh_tokens()

    def _next_reqid(self) -> int:
        self._reqid += 100000
        return self._reqid

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------
    def upload_file(self, filename: str, content: bytes,
                    mime_type: Optional[str] = None) -> Tuple[str, str]:
        if not mime_type:
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"file": (filename, content, mime_type)}
        headers = {
            "user-agent": self.USER_AGENT,
            "cookie": self._cookie_header,
            "Push-ID": self.PUSH_ID,
            "X-Tenant-Id": "bard-storage",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
        }
        r = self._session.post(self.UPLOAD_URL, headers=headers,
                               files=files, timeout=60, allow_redirects=True)
        if not r.ok:
            raise RuntimeError(f"Upload failed: HTTP {r.status_code}")
        return r.text.strip(), filename

    # ------------------------------------------------------------------
    # Core ask method
    # ------------------------------------------------------------------
    def ask(self,
            prompt: str,
            previous_id: Optional[str] = None,
            files: Optional[List[Tuple[str, bytes, Optional[str]]]] = None,
            model: str = "auto",
            retries: int = 2) -> Dict:
        """
        Send a prompt (with optional attached files) to Gemini.
        Returns: {
            "text": str,
            "id": str,           # base64 conv id, pass back via previous_id
            "image_urls": [str], # any inline image URLs Gemini generated
            "image_bytes": [bytes],
        }
        """
        if (not prompt or not prompt.strip()) and not files:
            raise ValueError("Prompt is empty and no files were provided.")

        resume_array = None
        if previous_id:
            try:
                resume_array = json.loads(
                    base64.b64decode(previous_id).decode("utf-8")
                ).get("newResumeArray")
            except Exception:
                pass

        # Convert audio files to MP3, then upload everything
        uploaded: List[Tuple[str, str]] = []  # (file_id, filename)
        for filename, content, mime_type in (files or []):
            mt = mime_type or mimetypes.guess_type(filename)[0] or ""
            if mt.startswith("audio") and not filename.lower().endswith(".mp3"):
                ext = (filename.rsplit(".", 1)[-1] if "." in filename else "ogg")
                content = convert_to_mp3(content, ext)
                filename = filename.rsplit(".", 1)[0] + ".mp3"
                mt = "audio/mpeg"
            uploaded.append(self.upload_file(filename, content, mt))

        # Build promptData
        if uploaded:
            attachments = [[[fid], fname] for fid, fname in uploaded]
            prompt_data = [prompt or "", 0, None, attachments]
        else:
            prompt_data = [prompt]

        inner = [prompt_data, ["en-US"], resume_array]
        outer = [None, json.dumps(inner, separators=(",", ":"))]

        last_err = None
        for attempt in range(retries + 1):
            try:
                self._ensure_tokens()
                headers = {
                    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "cookie": self._cookie_header,
                    "user-agent": self.USER_AGENT,
                    "origin": "https://gemini.google.com",
                    "referer": "https://gemini.google.com/",
                    "x-same-domain": "1",
                }
                # Apply model-selection headers
                model_headers = MODELS.get(model, {})
                headers.update(model_headers)
                if not model_headers:
                    # default capacity flag for "auto" — required for some accounts
                    headers[MODEL_HEADER_KEY] = (
                        '[1,null,null,null,"9ec249fc9ad08861",null,null,null,[4]]'
                    )

                body = urlencode({
                    "f.req": json.dumps(outer, separators=(",", ":")),
                    "at": self._snlm0e or "",
                })

                url = self.GENERATE_URL_TMPL.format(bl=self._bl, reqid=self._next_reqid())
                r = self._session.post(url, headers=headers, data=body, timeout=120)
                if not r.ok:
                    if r.status_code in (401, 429) and attempt < retries:
                        time.sleep(1.5 * (attempt + 1))
                        self._snlm0e = None
                        continue
                    # Auth/quota → bubble up as AuthCookieError so the
                    # cookie pool can rotate to the next slot.
                    if r.status_code in (401, 403, 429):
                        raise AuthCookieError(
                            f"HTTP {r.status_code}: {r.text[:300]}"
                        )
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")

                parsed = self._parse_response(r.text)
                if parsed["text"] is None and not parsed["image_urls"]:
                    raise RuntimeError(
                        f"Could not parse response. First 500 chars:\n{r.text[:500]}"
                    )

                conv_id = base64.b64encode(json.dumps(
                    {"newResumeArray": parsed["new_resume"]}
                ).encode("utf-8")).decode("utf-8")

                # Download generated images. Append the size suffix Gemini's
                # CDN expects, otherwise the bare gg-dl URL returns HTTP 403.
                image_bytes: List[bytes] = []
                dl_headers = {
                    "user-agent": self.USER_AGENT,
                    "cookie": self._cookie_header,
                    "referer": "https://gemini.google.com/",
                    "origin": "https://gemini.google.com",
                    "accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
                }
                for u in parsed["image_urls"]:
                    try:
                        # gg-dl URLs need a redirect chain: append =d-I?alr=yes,
                        # then follow each text/plain response (which contains
                        # the next URL) until we receive image bytes.
                        next_url = u + "=d-I?alr=yes"
                        for _ in range(5):
                            rr = self._session.get(
                                next_url, headers=dl_headers,
                                timeout=30, allow_redirects=True,
                            )
                            if not rr.ok:
                                break
                            ct = rr.headers.get("content-type", "")
                            if ct.startswith("image"):
                                image_bytes.append(rr.content)
                                break
                            if not ct.startswith("text/plain"):
                                break
                            next_url = rr.text.strip()
                            if not next_url.startswith("http"):
                                break
                    except Exception:
                        continue

                return {
                    "text": parsed["text"] or "",
                    "id": conv_id,
                    "image_urls": parsed["image_urls"],
                    "image_bytes": image_bytes,
                }
            except Exception as e:
                last_err = e
                if attempt == retries:
                    raise
                time.sleep(1 * (attempt + 1))

        raise last_err or RuntimeError("Unknown error")

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(raw: str) -> Dict:
        chunks = re.findall(r"^\d+\n(.+?)\n", raw, flags=re.MULTILINE | re.DOTALL)
        text: Optional[str] = None
        new_resume = None
        image_urls: List[str] = []

        # Primary path
        for chunk in reversed(chunks):
            try:
                arr = json.loads(chunk)
                inner = json.loads(arr[0][2])
                node = inner[4][0][1][0]
                if isinstance(node, str):
                    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", node)
                    new_resume = [*inner[1], inner[4][0][0]]
                    break
            except Exception:
                continue

        # Fallback parsing for image responses / alternate formats
        if text is None:
            for chunk in chunks:
                try:
                    arr = json.loads(chunk)
                    if not arr or not arr[0] or len(arr[0]) < 3:
                        continue
                    inner = json.loads(arr[0][2])

                    def safe(*keys):
                        cur = inner
                        for k in keys:
                            try:
                                cur = cur[k]
                            except (KeyError, IndexError, TypeError):
                                return None
                        return cur

                    for cand in (safe(4, 0, 1, 0), safe(0, 0),
                                 safe(0, 4, 0, 1, 0), safe(4, 1, 0)):
                        if isinstance(cand, str) and len(cand) > 5:
                            text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", cand)
                            new_resume = inner[1] if isinstance(inner, list) and len(inner) > 1 else []
                            break
                    if text:
                        break
                except Exception:
                    continue

        # Extract real generated-image URLs. JSON-escaped slashes are common.
        unescaped = raw.replace("\\/", "/").replace("\\u003d", "=").replace("\\u0026", "&")
        for url in re.findall(
            r'https://lh3\.googleusercontent\.com/[A-Za-z0-9._\-/=?&%]+',
            unescaped,
        ):
            url = url.rstrip('\\,";')
            if url not in image_urls:
                image_urls.append(url)

        # Always strip image-generation placeholder URLs from the text — they
        # are internal markers Gemini uses to anchor inline images, never
        # something the user should see.
        if text:
            text = re.sub(
                r"https?://googleusercontent\.com/image_generation_content/\d+",
                "",
                text,
            )
            # Tidy up any double-spaces / orphan punctuation left behind
            text = re.sub(r"[ \t]+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

        return {"text": text, "new_resume": new_resume, "image_urls": image_urls}


# ----------------------------------------------------------------------
# Markdown -> WhatsApp formatting converter.
# WhatsApp uses *bold*, _italic_, ~strike~, ```code```, > quote.
# Standard Markdown uses **bold**, __bold__, *italic*, _italic_,
# ~~strike~~, # headers, [label](url), <url>. We convert what we can
# and strip the rest so the message renders cleanly inside WhatsApp.
# ----------------------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\s*\(\s*([^)\s]+)\s*\)")
_MD_BOLD_STAR_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_MD_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^([ \t]*)[-*+]\s+", re.MULTILINE)
_ANGLE_URL_RE = re.compile(r"<(https?://[^>\s]+)>")


def strip_markdown(text: str) -> str:
    """Convert standard Markdown to WhatsApp formatting.

    Kept under the original name for backwards compatibility with
    callers; new code should think of it as "to_whatsapp".
    """
    if not text:
        return text

    # Links: [label](url) -> "label url" (or just url if label == url)
    def _link_sub(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        return url if label == url else f"{label} {url}"
    text = _MD_LINK_RE.sub(_link_sub, text)

    # <https://...> -> https://...
    text = _ANGLE_URL_RE.sub(r"\1", text)

    # **bold** / __bold__ -> *bold*  (WhatsApp bold is single asterisk)
    text = _MD_BOLD_STAR_RE.sub(r"*\1*", text)
    text = _MD_BOLD_UNDER_RE.sub(r"*\1*", text)

    # ~~strike~~ -> ~strike~  (WhatsApp strike is single tilde)
    text = _MD_STRIKE_RE.sub(r"~\1~", text)

    # # Header  ->  *Header*  (WhatsApp has no headers; bold is the
    # closest visual equivalent).
    text = _MD_HEADER_RE.sub(r"*\2*", text)

    # Bullet markers: normalize "-" / "*" / "+" to "•"
    text = _MD_BULLET_RE.sub(r"\1• ", text)

    # NOTE: we intentionally do NOT touch single-asterisk, single-underscore,
    # single-tilde, or triple-backtick spans — those are valid WhatsApp
    # formatting that the model is instructed to emit directly.

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ----------------------------------------------------------------------
# Per-user brain (memory + persona)
# ----------------------------------------------------------------------
class GeminiBrain:
    """High-level multimodal assistant with per-user memory."""

    def __init__(self,
                 cookies_path: Optional[str] = None,
                 cookie_string: Optional[str] = None,
                 persona: str = DEFAULT_PERSONA,
                 model: str = "auto"):
        self.scraper = GeminiScraper(cookies_path=cookies_path, cookie_string=cookie_string)
        self.persona = persona
        self.model = model
        self._sessions: Dict[str, str] = {}        # user_id -> chat conv id
        self._image_sessions: Dict[str, str] = {}  # user_id -> image conv id
        self._initialised: Dict[str, bool] = {}    # user_id -> persona injected?

    def _prime_prompt(self, user_id: str, prompt: str) -> str:
        """Prepend the persona to the very first prompt of a user.

        Avoids a separate round-trip to Gemini just to seed the persona,
        which used to roughly double the latency of a user's first message.
        """
        if self._initialised.get(user_id):
            return prompt
        self._initialised[user_id] = True
        if not self.persona:
            return prompt
        return f"{self.persona}\n\nUser: {prompt}" if prompt else self.persona

    def ask(self,
            user_id: str,
            prompt: str,
            files: Optional[List[Tuple[str, bytes, Optional[str]]]] = None) -> str:
        """Plain chat. Returns just the reply text."""
        prompt = self._prime_prompt(user_id, prompt)
        prev = self._sessions.get(user_id)
        res = self.scraper.ask(prompt, previous_id=prev, files=files, model=self.model)
        self._sessions[user_id] = res["id"]
        return strip_markdown(res["text"])

    def ask_full(self,
                 user_id: str,
                 prompt: str,
                 files: Optional[List[Tuple[str, bytes, Optional[str]]]] = None) -> Dict:
        """Full response (text + any images). Use this when you need image bytes."""
        prompt = self._prime_prompt(user_id, prompt)
        prev = self._sessions.get(user_id)
        res = self.scraper.ask(prompt, previous_id=prev, files=files, model=self.model)
        self._sessions[user_id] = res["id"]
        res["text"] = strip_markdown(res.get("text", ""))
        return res

    def generate_image(self,
                       user_id: str,
                       prompt: str,
                       reference: Optional[Tuple[str, bytes, Optional[str]]] = None
                       ) -> Tuple[List[bytes], str]:
        """
        Use Nano Banana to generate or edit an image.
        Pass `reference=(filename, bytes, mime)` to edit an existing picture.
        Returns (list_of_image_bytes, accompanying_text).
        """
        # Image generation runs in its OWN conversation thread per user, so
        # quota errors / rate-limit messages can never pollute the main chat
        # context. (Previously a "you can't generate any more images today"
        # reply would leak into Omar's regular memory and confuse him.)
        # Ask Gemini for a *single* image — generating multiple variations
        # was previously causing duplicate sends in WhatsApp.
        instruction = (
            f"Generate one image of: {prompt}."
            if reference is None
            else f"Edit this image: {prompt}."
        )
        prev = self._image_sessions.get(user_id)
        res = self.scraper.ask(
            instruction,
            previous_id=prev,
            files=[reference] if reference else None,
            model=NANO_BANANA_MODEL,
        )
        self._image_sessions[user_id] = res["id"]
        return res["image_bytes"], strip_markdown(res.get("text", ""))

    def reset(self, user_id: str) -> None:
        self._sessions.pop(user_id, None)
        self._image_sessions.pop(user_id, None)
        self._initialised.pop(user_id, None)

    def reset_all(self) -> None:
        self._sessions.clear()
        self._image_sessions.clear()
        self._initialised.clear()


# ----------------------------------------------------------------------
# CLI demo
# ----------------------------------------------------------------------
def _cli() -> None:
    here = os.path.dirname(__file__)
    cookies_file = os.path.join(here, "cookies.txt")
    if not os.path.exists(cookies_file):
        print(f"Cookies file not found at {cookies_file}")
        raise SystemExit(1)

    bot = GeminiBrain(cookies_path=cookies_file)
    user = "cli-user"
    print("Multimodal Gemini chat. Commands: /image <prompt>, /file <path>, "
          "/reset, /exit\n")
    while True:
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg in ("/exit", "exit"):
            break
        if msg in ("/reset", "reset"):
            bot.reset(user)
            print("(memory cleared)\n")
            continue
        if msg.startswith("/image "):
            prompt = msg[len("/image "):]
            try:
                imgs, text = bot.generate_image(user, prompt)
                if imgs:
                    out_dir = os.path.join(here, "generated")
                    os.makedirs(out_dir, exist_ok=True)
                    for i, b in enumerate(imgs):
                        path = os.path.join(out_dir, f"{uuid.uuid4().hex}_{i}.png")
                        with open(path, "wb") as f:
                            f.write(b)
                        print(f"Saved image: {path}")
                if text:
                    print(f"\nGemini: {text}\n")
            except Exception as e:
                print(f"Error: {e}")
            continue
        if msg.startswith("/file "):
            path = msg[len("/file "):].strip()
            if not os.path.exists(path):
                print(f"No such file: {path}")
                continue
            with open(path, "rb") as f:
                content = f.read()
            try:
                ans = bot.ask(user,
                              "Please look at this file and describe it.",
                              files=[(os.path.basename(path), content, None)])
                print(f"\nGemini: {ans}\n")
            except Exception as e:
                print(f"Error: {e}")
            continue
        try:
            print(f"\nGemini: {bot.ask(user, msg)}\n")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    _cli()
