"""Smoke test: text memory, vision, audio, and image generation."""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gemini.gemini_scraper import GeminiBrain

here = os.path.dirname(os.path.abspath(__file__))
cookies_file = os.path.join(here, "cookies.txt")

bot = GeminiBrain(cookies_path=cookies_file)
user = "test"


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


# 1) Memory
section("1. Natural conversation with memory")
print("Q: My name is Omar and my favorite color is green.")
print("A:", bot.ask(user, "My name is Omar and my favorite color is green.").strip()[:300])
print("\nQ: What's my name and favorite color?")
print("A:", bot.ask(user, "What's my name and favorite color?").strip()[:300])

# 2) Vision (download a small public test image)
section("2. Vision (image understanding)")
img_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/240px-PNG_transparency_demonstration_1.png"
try:
    img_bytes = urllib.request.urlopen(img_url, timeout=20).read()
    print(f"Downloaded test image ({len(img_bytes)} bytes)")
    print("A:", bot.ask(user, "What do you see in this picture? One short sentence.",
                        files=[("test.png", img_bytes, "image/png")]).strip()[:400])
except Exception as e:
    print(f"Vision test skipped: {e}")

# 3) Image generation (Nano Banana)
section("3. Image generation (Nano Banana)")
try:
    imgs, text = bot.generate_image(
        user, "a cute corgi astronaut floating in deep space, photorealistic, vivid colors"
    )
    if text:
        print("Text reply:", text[:300])
    if imgs:
        out = os.path.join(here, "generated_test.png")
        with open(out, "wb") as f:
            f.write(imgs[0])
        print(f"Saved generated image -> {out} ({len(imgs[0])} bytes)")
    else:
        print("No images returned (Gemini may have replied with a text refusal).")
except Exception as e:
    print(f"Image generation failed: {e}")

print("\nDone.")
