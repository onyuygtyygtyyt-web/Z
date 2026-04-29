"""Test 2: vision."""
import os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini.gemini_scraper import GeminiBrain

bot = GeminiBrain(cookies_path=os.path.join(os.path.dirname(__file__), "cookies.txt"))
u = "vision-test"

img_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/240px-PNG_transparency_demonstration_1.png"
img = urllib.request.urlopen(img_url, timeout=20).read()
print(f"Got image: {len(img)} bytes")
t = time.time()
ans = bot.ask(u, "What do you see in this picture? One short sentence.",
              files=[("dice.png", img, "image/png")])
print(f"({time.time()-t:.1f}s) A:", ans.strip()[:400])
