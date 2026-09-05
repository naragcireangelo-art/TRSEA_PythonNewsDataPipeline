import time
import requests
import feedparser_rs
from bs4 import BeautifulSoup



# Better headers to bypass 403 Forbidden on Inquirer
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

def get_lead_paragraph(article_url):
    """Fetches the full webpage and extracts the main opening lead statement."""
    try:
        res = requests.get(article_url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        page_soup = BeautifulSoup(res.text, "html.parser")
        
        all_paras = page_soup.find_all("p")
        valid_paras = [
            p.get_text().strip() for p in all_paras if len(p.get_text().strip()) > 40
        ]
        
        return valid_paras[0] if valid_paras else "No valid text body found."
    except Exception as err:
        return f"Could not scrape article body: {err}"

def rss_Gather(country):
    output_text = f"Target Country: {country}\n\n"

    match country:
        case "Philippines":
            feed_urls = [
                "https://currentph.com/feed/","https://www.philstar.com/rss/headlines",
                "https://data.gmanews.tv/gno/rss/news/feed.xml","https://www.inquirer.net/fullfeed"
            ]
        case _:
            feed_urls = []

    for url in feed_urls:
        output_text += f"=" * 50 + "\n"
        output_text += f"FETCHING FEED: {url}\n"
        output_text += f"=" * 50 + "\n"
        
        try:
            feed = feedparser_rs.parse(url)
            output_text += f"Channel Title: {getattr(feed.channel, 'title', 'Unknown')}\n\n"
            
            for entry in feed.entries[:5]:  # Top 5 news items
                raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
                clean_teaser = BeautifulSoup(raw_summary, "html.parser").get_text().strip()
                
                lead_statement = get_lead_paragraph(entry.link)
                
                output_text += f"HEADLINE: {entry.title}\n"
                output_text += f"LINK:     {entry.link}\n"
                output_text += f"PUBLISHED: {getattr(entry, 'published', 'N/A')}\n"
                output_text += f"TEASER:   {clean_teaser if clean_teaser else 'N/A'}\n"
                output_text += f"LEAD STMT: {lead_statement}\n"
                output_text += "-" * 50 + "\n\n"
                
        except Exception as e:
            output_text += f"Failed to process feed {url}: {e}\n"
    return output_text




"""
# Main Loop
#while True:
    #print("Fetching news feeds...")
def Gather_runner():
    polingDelay = 600
    content = rss_Gather("Philippines")
    with open("input.txt", "w", encoding="utf-8") as file:
        file.write(content)
    #print(f"Updated input.txt! Sleeping for {polingDelay} seconds...\n")
    time.sleep(polingDelay)
"""

def Gather_runner():
    print("Gathering RSS feeds...")
    content = rss_Gather("Philippines")
    with open("input.txt", "w", encoding="utf-8") as file:
        file.write(content)
    print("Done! Saved to input.txt")

if __name__ == "__main__":
    Gather_runner()

