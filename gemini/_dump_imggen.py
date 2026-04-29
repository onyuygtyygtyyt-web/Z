"""Dump the raw response from an image-generation call to disk for inspection."""
import json, os, re, sys
from urllib.parse import urlencode
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini.gemini_scraper import GeminiScraper, MODELS, NANO_BANANA_MODEL

s = GeminiScraper(cookies_path=os.path.join(os.path.dirname(__file__), "cookies.txt"))
s._ensure_tokens()

prompt_data = ["Generate an image: a cute corgi astronaut floating in deep space"]
inner = [prompt_data, ["en-US"], None]
outer = [None, json.dumps(inner, separators=(",", ":"))]
body = urlencode({"f.req": json.dumps(outer, separators=(",", ":")), "at": s._snlm0e})

headers = {
    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    "cookie": s._cookie_header,
    "user-agent": s.USER_AGENT,
    "origin": "https://gemini.google.com",
    "referer": "https://gemini.google.com/",
}
headers.update(MODELS[NANO_BANANA_MODEL])

url = s.GENERATE_URL_TMPL.format(bl=s._bl, reqid=s._next_reqid())
r = s._session.post(url, headers=headers, data=body, timeout=120)

with open("/tmp/raw.txt", "w") as f:
    f.write(r.text)

print(f"Saved /tmp/raw.txt ({len(r.text)} bytes)")
urls = sorted(set(re.findall(r'https?://[^"\\\s,\]]*googleusercontent[^"\\\s,\]]*', r.text)))
print(f"googleusercontent URLs found: {len(urls)}")
for u in urls[:10]:
    print(" ", u[:180])
