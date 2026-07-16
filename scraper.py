import requests
from bs4 import BeautifulSoup
import re
import os
from telegram import Bot
import asyncio

# ========== CONFIGURATION ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# ========== SCRAPING FUNCTION ==========
def scrape_download_links(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    
    # Get movie title
    title_tag = soup.find("h1", class_="page-title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    else:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Movie"
    
    # Find all content and look for DOWNLOAD LINKS section
    all_text = soup.get_text()
    
    # Find position of "DOWNLOAD LINKS"
    download_marker = ": DOWNLOAD LINKS :"
    skip_marker = ": Single Episode x264 Links :"
    
    # Get the HTML around download section
    # Strategy: Find all heading tags that contain download links
    download_links = []
    
    # Find all h2, h3, h4, h5 tags that contain links
    for heading in soup.find_all(['h2', 'h3', 'h4', 'h5']):
        # Check if this heading contains "DOWNLOAD LINKS"
        if heading and download_marker.lower() in heading.get_text().lower():
            # Found the download links section - process all following siblings until next heading
            current = heading.find_next_sibling()
            
            # Flag to stop when we hit the skip marker
            stop_scanning = False
            
            while current and not stop_scanning:
                # Check if we hit the skip marker
                if current.name in ['h2', 'h3', 'h4', 'h5']:
                    if skip_marker.lower() in current.get_text().lower():
                        stop_scanning = True
                        break
                    # Also stop if we hit another major heading without skip marker
                    # (to avoid going too far)
                    if "download" not in current.get_text().lower():
                        break
                
                # Find links within this element
                if current.name == 'a':
                    # Direct link
                    href = current.get('href', '')
                    label = current.get_text(strip=True)
                    if href and ('http' in href or '//' in href):
                        download_links.append({
                            'label': label,
                            'url': href
                        })
                else:
                    # Find all links inside this element
                    for link in current.find_all('a', href=True):
                        href = link.get('href', '')
                        label = link.get_text(strip=True)
                        # Skip empty or javascript links
                        if href and href.startswith(('http', '//', '#')) and not href.startswith('javascript:'):
                            # Skip if it's just "#" or empty
                            if href != '#':
                                download_links.append({
                                    'label': label if label else href,
                                    'url': href
                                })
                
                # Check if current element itself contains the skip marker
                if current.string and skip_marker.lower() in current.string.lower():
                    stop_scanning = True
                    break
                
                # Move to next sibling
                current = current.find_next_sibling()
    
    # If no links found via heading method, try alternative approach
    if not download_links:
        # Find all links that look like download links (contain 480p, 720p, 1080p, 4K, etc.)
        quality_patterns = re.compile(r'(480p|720p|1080p|4K|2160p|SDR|HDR|DV|HEVC|x264|x265)', re.IGNORECASE)
        
        # Get all text and find sections
        for heading in soup.find_all(['h2', 'h3', 'h4', 'h5']):
            heading_text = heading.get_text().lower()
            if 'download' in heading_text and 'link' in heading_text:
                # This is a download section
                current = heading.find_next_sibling()
                while current and current.name not in ['h2', 'h3', 'h4', 'h5']:
                    for link in current.find_all('a', href=True):
                        href = link.get('href', '')
                        label = link.get_text(strip=True)
                        if href and href.startswith(('http', '//')):
                            if quality_patterns.search(label) or quality_patterns.search(href):
                                download_links.append({
                                    'label': label if label else href,
                                    'url': href
                                })
                    current = current.find_next_sibling()
    
    # Remove duplicates
    seen = set()
    unique_links = []
    for link in download_links:
        if link['url'] not in seen:
            seen.add(link['url'])
            unique_links.append(link)
    
    return {
        'title': title,
        'url': url,
        'links': unique_links
    }

# ========== TELEGRAM MESSAGE FORMATTING ==========
def format_telegram_message(data):
    title = data['title']
    url = data['url']
    links = data['links']
    
    message = f"🎬 *{title}*\n"
    message += f"🔗 {url}\n\n"
    message += "📥 *Download Links:*\n\n"
    
    if not links:
        message += "❌ No download links found."
        return message
    
    for idx, link in enumerate(links, 1):
        label = link['label'][:80] if link['label'] else "Link"
        message += f"{idx}. [{label}]({link['url']})\n"
    
    message += f"\n📊 Total Links: {len(links)}"
    return message

# ========== TELEGRAM SENDER ==========
async def send_to_telegram(message):
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        print("✅ Message sent to Telegram!")
        return True
    except Exception as e:
        print(f"❌ Failed to send to Telegram: {e}")
        return False

# ========== MAIN FUNCTION ==========
def main():
    # URL to scrape (you can change this or pass as argument)
    url = input("Enter movie URL: ").strip()
    
    if not url:
        print("❌ Please enter a valid URL")
        return
    
    print(f"\n🔍 Scraping: {url}")
    
    try:
        # Scrape the page
        data = scrape_download_links(url)
        
        print(f"\n📌 Title: {data['title']}")
        print(f"📊 Found {len(data['links'])} download links")
        
        # Print links to console
        for idx, link in enumerate(data['links'], 1):
            print(f"  {idx}. {link['label'][:60]} -> {link['url'][:80]}...")
        
        # Format message
        message = format_telegram_message(data)
        print("\n" + "="*50)
        print(message)
        print("="*50)
        
        # Send to Telegram
        if TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN" and TELEGRAM_CHAT_ID != "YOUR_CHAT_ID":
            asyncio.run(send_to_telegram(message))
        else:
            print("\n⚠️ Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables")
            print("   Or replace them with your actual values in the code.")
        
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
