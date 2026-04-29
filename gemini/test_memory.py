"""Test 1: natural conversation with memory."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini.gemini_scraper import GeminiBrain

bot = GeminiBrain(cookies_path=os.path.join(os.path.dirname(__file__), "cookies.txt"))
u = "memory-test"

t0 = time.time()
print("Q1: My name is Omar and my favourite color is green.")
print("A1:", bot.ask(u, "My name is Omar and my favourite color is green.").strip()[:300])
print(f"({time.time()-t0:.1f}s)")

t0 = time.time()
print("\nQ2: What did I tell you about myself?")
print("A2:", bot.ask(u, "What did I tell you about myself?").strip()[:300])
print(f"({time.time()-t0:.1f}s)")
