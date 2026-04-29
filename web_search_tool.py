import sys
from duckduckgo_search import DDGS

def search(query):
    with DDGS() as ddgs:
        results = ddgs.text(query, max_results=5)
        for r in results:
            print(f"Title: {r['title']}")
            print(f"Link: {r['href']}")
            print(f"Snippet: {r['body']}")
            print("-" * 20)

if __name__ == "__main__":
    search(sys.argv[1])
