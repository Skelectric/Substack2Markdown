import argparse
import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep
from datetime import datetime
import re
import hashlib

from bs4 import BeautifulSoup
import html2text
import markdown
import requests
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium import webdriver
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from urllib.parse import urlparse, urljoin
from config import EMAIL, PASSWORD

USE_PREMIUM: bool = False  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.citriniresearch.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = "/Users/philkir/substacks"  # Name of the directory we'll save the .md essay files
BASE_HTML_DIR: str = "/Users/philkir/substacks/html"  # Name of the directory we'll save the .html essay files
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')  # Parse the URL to get the netloc, and split on '.'
    return parts[1] if parts[0] == 'www' else parts[0]  # Return the main part of the domain, while ignoring 'www' if
    # present


def parse_date_to_iso(date_str: str) -> str:
    """
    Parse various date formats from Substack and convert to YYYY-MM-DD format.
    Returns the original string if parsing fails.
    """
    if not date_str or date_str == "Date not found":
        return ""
    
    # Common Substack date formats to try
    date_formats = [
        "%B %d, %Y",      # "January 15, 2024"
        "%b %d, %Y",      # "Jan 15, 2024"
        "%d %B %Y",       # "15 January 2024"
        "%d %b %Y",       # "15 Jan 2024"
        "%Y-%m-%d",       # "2024-01-15"
        "%m/%d/%Y",       # "01/15/2024"
        "%d/%m/%Y",       # "15/01/2024"
        "%B %d",          # "January 15" (assume current year)
        "%b %d",          # "Jan 15" (assume current year)
    ]
    
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(date_str.strip(), fmt)
            # If no year in format, assume current year
            if parsed_date.year == 1900:  # Default year when not specified
                parsed_date = parsed_date.replace(year=datetime.now().year)
            return parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            continue
    
    # If all parsing fails, try to extract year, month, day using regex
    year_match = re.search(r'\b(20\d{2})\b', date_str)
    month_match = re.search(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b', date_str.lower())
    day_match = re.search(r'\b(\d{1,2})\b', date_str)
    
    if year_match and month_match and day_match:
        try:
            year = int(year_match.group(1))
            month_name = month_match.group(1)
            day = int(day_match.group(1))
            
            # Convert month name to number
            month_map = {
                'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
                'march': 3, 'mar': 3, 'april': 4, 'apr': 4,
                'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
                'august': 8, 'aug': 8, 'september': 9, 'sep': 9,
                'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
                'december': 12, 'dec': 12
            }
            month = month_map.get(month_name.lower(), 1)
            
            parsed_date = datetime(year, month, day)
            return parsed_date.strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            pass
    
    # If all else fails, return the original string
    return date_str


def generate_html_file(author_name: str) -> None:
    """
    Generates a HTML file for the given author.
    """
    if not os.path.exists(BASE_HTML_DIR):
        os.makedirs(BASE_HTML_DIR)

    # Read JSON data
    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    # Convert JSON data to a JSON string for embedding
    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(HTML_TEMPLATE, 'r', encoding='utf-8') as file:
        html_template = file.read()

    # Insert the JSON string into the script tag in the HTML template
    html_with_data = html_template.replace('<!-- AUTHOR_NAME -->', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

    # Write the modified HTML to a new file
    html_output_path = os.path.join(BASE_HTML_DIR, f'{author_name}.html')
    with open(html_output_path, 'w', encoding='utf-8') as file:
        file.write(html_with_author)


class BaseSubstackScraper(ABC):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url

        self.writer_name: str = extract_main_part(base_substack_url)
        md_save_dir: str = f"{md_save_dir}/{self.writer_name}"

        self.md_save_dir: str = md_save_dir
        self.html_save_dir: str = f"{html_save_dir}/{self.writer_name}"

        if not os.path.exists(md_save_dir):
            os.makedirs(md_save_dir)
            print(f"Created md directory {md_save_dir}")
        if not os.path.exists(self.html_save_dir):
            os.makedirs(self.html_save_dir)
            print(f"Created html directory {self.html_save_dir}")

        self.keywords: List[str] = ["about", "archive", "podcast"]
        self.post_urls: List[str] = self.get_all_post_urls()

    def get_all_post_urls(self) -> List[str]:
        """
        Attempts to fetch URLs from sitemap.xml, falling back to feed.xml if necessary.
        """
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def fetch_urls_from_sitemap(self) -> List[str]:
        """
        Fetches URLs from sitemap.xml.
        """
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
        response = requests.get(sitemap_url)

        if not response.ok:
            print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        return urls

    def fetch_urls_from_feed(self) -> List[str]:
        """
        Fetches URLs from feed.xml.
        """
        print('Falling back to feed.xml. This will only contain up to the 22 most recent posts.')
        feed_url = f"{self.base_substack_url}feed.xml"
        response = requests.get(feed_url)

        if not response.ok:
            print(f'Error fetching feed at {feed_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = []
        for item in root.findall('.//item'):
            link = item.find('link')
            if link is not None and link.text:
                urls.append(link.text)

        return urls

    @staticmethod
    def filter_urls(urls: List[str], keywords: List[str]) -> List[str]:
        """
        This method filters out URLs that contain certain keywords
        """
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def html_to_md(html_content: str) -> str:
        """
        This method converts HTML to Markdown
        """
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
        """
        This method saves content to a file. Can be used to save HTML or Markdown
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        if os.path.exists(filepath):
            print(f"File already exists: {filepath}")
            return

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def md_to_html(md_content: str) -> str:
        """
        This method converts Markdown to HTML
        """
        return markdown.markdown(md_content, extensions=['extra'])

    def create_images_directory(self) -> str:
        """
        Creates an images directory alongside the markdown files
        """
        images_dir = os.path.join(self.md_save_dir, "images")
        if not os.path.exists(images_dir):
            os.makedirs(images_dir)
            print(f"Created images directory: {images_dir}")
        return images_dir

    def extract_image_urls_from_markdown(self, markdown_content: str) -> List[str]:
        """
        Extracts all image URLs from markdown content
        """
        # Pattern to match markdown image syntax: ![alt text](url)
        # This pattern specifically looks for ![...](url) which is markdown image syntax
        image_pattern = r'!\[[^\]]*\]\((https?://[^\s\)]+)\)'
        urls = re.findall(image_pattern, markdown_content)
        return urls

    def download_image(self, image_url: str, images_dir: str) -> Optional[str]:
        """
        Downloads an image from URL and saves it locally
        Returns the local file path if successful, None if failed
        """
        try:
            # Create a unique filename based on URL hash
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:8]
            
            # Get file extension from URL or default to .jpg
            parsed_url = urlparse(image_url)
            path = parsed_url.path
            if '.' in path:
                ext = os.path.splitext(path)[1]
                if not ext or len(ext) > 5:  # Invalid or too long extension
                    ext = '.jpg'
            else:
                ext = '.jpg'
            
            filename = f"{url_hash}{ext}"
            local_path = os.path.join(images_dir, filename)
            
            # Skip if file already exists
            if os.path.exists(local_path):
                return local_path
            
            # Download the image
            response = requests.get(image_url, stream=True, timeout=30)
            response.raise_for_status()
            
            # Save the image
            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return local_path
            
        except Exception as e:
            print(f"Failed to download image {image_url}: {e}")
            return None

    def replace_image_urls_in_markdown(self, markdown_content: str, images_dir: str) -> str:
        """
        Replaces image URLs in markdown content with relative local paths
        """
        image_urls = self.extract_image_urls_from_markdown(markdown_content)
        
        for image_url in image_urls:
            local_path = self.download_image(image_url, images_dir)
            if local_path:
                # Calculate relative path from markdown file to image
                relative_path = os.path.relpath(local_path, self.md_save_dir)
                # Ensure forward slashes for markdown compatibility
                relative_path = relative_path.replace("\\", "/")
                
                # Replace the URL in markdown content
                markdown_content = markdown_content.replace(image_url, relative_path)
        
        return markdown_content


    def save_to_html_file(self, filepath: str, content: str) -> None:
        """
        This method saves HTML content to a file with a link to an external CSS file.
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        # Calculate the relative path from the HTML file to the CSS file
        html_dir = os.path.dirname(filepath)
        css_path = os.path.relpath("./assets/css/essay-styles.css", html_dir)
        css_path = css_path.replace("\\", "/")  # Ensure forward slashes for web paths

        html_content = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Markdown Content</title>
                <link rel="stylesheet" href="{css_path}">
            </head>
            <body>
                <main class="markdown-content">
                {content}
                </main>
            </body>
            </html>
        """

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(html_content)

    @staticmethod
    def get_filename_from_url(url: str, filetype: str = ".md", date: str = "") -> str:
        """
        Gets the filename from the URL (the ending) with optional date prefix
        """
        if not isinstance(url, str):
            raise ValueError("url must be a string")

        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")

        if not filetype.startswith("."):
            filetype = f".{filetype}"

        base_filename = url.split("/")[-1]
        
        # If date is provided, prepend it to the filename
        if date:
            parsed_date = parse_date_to_iso(date)
            if parsed_date and parsed_date != date:  # Only use if parsing was successful
                return f"{parsed_date}_{base_filename}{filetype}"
        
        return base_filename + filetype

    @staticmethod
    def combine_metadata_and_content(title: str, subtitle: str, date: str, like_count: str, content) -> str:
        """
        Combines the title, subtitle, and content into a single string with Markdown format
        """
        if not isinstance(title, str):
            raise ValueError("title must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        metadata = f"# {title}\n\n"
        if subtitle:
            metadata += f"## {subtitle}\n\n"
        metadata += f"**{date}**\n\n"
        metadata += f"**Likes:** {like_count}\n\n"

        return metadata + content

    def extract_post_data(self, soup: BeautifulSoup) -> Tuple[str, str, str, str, str]:
        """
        Converts substack post soup to markdown, returns metadata and content
        """
        title = soup.select_one("h1.post-title, h2").text.strip()  # When a video is present, the title is demoted to h2

        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        
        date_element = soup.find(
            "div",
            class_="pencraft pc-reset color-pub-secondary-text-hGQ02T line-height-20-t4M0El font-meta-MWBumP size-11-NuY2Zx weight-medium-fw81nC transform-uppercase-yKDgcq reset-IxiVJZ meta-EgzBVA"
        )
        date = date_element.text.strip() if date_element else "Date not found"

        like_count_element = soup.select_one("a.post-ufi-button .label")
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )

        content = str(soup.select_one("div.available-content"))
        md = self.html_to_md(content)
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)
        return title, subtitle, like_count, date, md_content

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
        """
        Saves essays data to a JSON file for a specific author.
        """
        data_dir = os.path.join(JSON_DATA_DIR)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        json_path = os.path.join(data_dir, f'{self.writer_name}.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as file:
                existing_data = json.load(file)
            essays_data = existing_data + [data for data in essays_data if data not in existing_data]
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(essays_data, f, ensure_ascii=False, indent=4)

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """
        Iterates over all posts and saves them as markdown and html files
        """
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        
        # Create images directory
        images_dir = self.create_images_directory()
        
        for url in tqdm(self.post_urls, total=total):
            try:
                # First get the post data to extract the date
                soup = self.get_url_soup(url)
                if soup is None:
                    total += 1
                    continue
                
                title, subtitle, like_count, date, md = self.extract_post_data(soup)
                
                # Download images and replace URLs in markdown
                md = self.replace_image_urls_in_markdown(md, images_dir)
                
                # Generate filenames with date prefix
                md_filename = self.get_filename_from_url(url, filetype=".md", date=date)
                html_filename = self.get_filename_from_url(url, filetype=".html", date=date)
                md_filepath = os.path.join(self.md_save_dir, md_filename)
                html_filepath = os.path.join(self.html_save_dir, html_filename)

                if not os.path.exists(md_filepath):
                    self.save_to_file(md_filepath, md)

                    # Convert markdown to HTML and save
                    html_content = self.md_to_html(md)
                    self.save_to_html_file(html_filepath, html_content)

                    essays_data.append({
                        "title": title,
                        "subtitle": subtitle,
                        "like_count": like_count,
                        "date": date,
                        "file_link": md_filepath,
                        "html_link": html_filepath
                    })
                else:
                    print(f"File already exists: {md_filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
            count += 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(author_name=self.writer_name)


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Gets soup from URL using requests
        """
        try:
            page = requests.get(url, headers=None)
            soup = BeautifulSoup(page.content, "html.parser")
            if soup.find("h2", class_="paywall-title"):
                print(f"Skipping premium article: {url}")
                return None
            return soup
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
            self,
            base_substack_url: str,
            md_save_dir: str,
            html_save_dir: str,
            headless: bool = False,
            chrome_path: str = '',
            chrome_driver_path: str = '',
            user_agent: str = ''
    ) -> None:
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

        options = ChromeOptions()
        if headless:
            options.add_argument("--headless")
        if chrome_path:
            options.binary_location = chrome_path
        if user_agent:
            options.add_argument(f'user-agent={user_agent}')  # Pass this if running headless and blocked by captcha

        # Add Brave-specific options
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-debugging-port=9222")

        if chrome_driver_path:
            service = Service(executable_path=chrome_driver_path)
        else:
            # Try to use the Homebrew-installed ChromeDriver first
            homebrew_chromedriver = "/opt/homebrew/bin/chromedriver"
            if os.path.exists(homebrew_chromedriver):
                service = Service(executable_path=homebrew_chromedriver)
            else:
                service = Service(ChromeDriverManager().install())

        self.driver = webdriver.Chrome(service=service, options=options)
        self.login()

    def login(self) -> None:
        """
        This method logs into Substack using Selenium
        """
        print("Starting login process...")
        self.driver.get("https://substack.com/sign-in")
        sleep(5)

        try:
            signin_with_password = self.driver.find_element(
                By.XPATH, "//a[@class='login-option substack-login__login-option']"
            )
            signin_with_password.click()
            sleep(3)
        except Exception as e:
            print(f"Could not find sign-in with password option: {e}")
            # Try alternative selectors
            try:
                signin_with_password = self.driver.find_element(By.XPATH, "//button[contains(text(), 'Sign in with password')]")
                signin_with_password.click()
                sleep(3)
            except:
                print("Proceeding with current page...")

        # Email and password
        email = self.driver.find_element(By.NAME, "email")
        password = self.driver.find_element(By.NAME, "password")
        email.clear()
        email.send_keys(EMAIL)
        password.clear()
        password.send_keys(PASSWORD)

        # Find the submit button and click it.
        submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/button")
        submit.click()
        print("Login submitted, waiting for verification...")
        sleep(5)  # Wait for initial response
        
        # Wait for potential captcha completion and manual intervention
        print("⏳ Waiting for login process to complete...")
        print("If you see a captcha, popup, or need to click login, please handle it now.")
        
        # Wait for successful login
        max_wait = 60  # Maximum wait time in seconds
        wait_time = 0
        login_successful = False
        
        while wait_time < max_wait:
            try:
                current_url = self.driver.current_url
                print(f"Current URL: {current_url}")
                
                # Check if we've left the sign-in page
                if "substack.com" in current_url and "sign-in" not in current_url:
                    print("✓ Login successful")
                    login_successful = True
                    break
                
                # Check for error messages
                error_elements = self.driver.find_elements(By.XPATH, "//*[contains(@class, 'error') or contains(text(), 'Invalid')]")
                if error_elements:
                    print(f"✗ Login error: {[e.text for e in error_elements if e.text]}")
                    
            except Exception as e:
                print(f"Error checking login status: {e}")
            
            sleep(10)
            wait_time += 10
            
        if not login_successful:
            print("⚠ Login verification timeout - proceeding anyway")

        if self.is_login_failed():
            raise Exception(
                "Warning: Login unsuccessful. Please check your email and password, or your account status.\n"
                "Use the non-premium scraper for the non-paid posts. \n"
                "If running headless, run non-headlessly to see if blocked by Captcha."
            )
        
        print("Login process completed.")

    def close_popups(self) -> None:
        """
        Close any popups or modals that might be blocking the interface
        """
        try:
            # Look for close buttons (X buttons) in various common locations
            close_selectors = [
                "//button[contains(@class, 'close')]",
                "//button[contains(@aria-label, 'close')]",
                "//button[contains(@aria-label, 'Close')]",
                "//*[contains(@class, 'close-button')]",
                "//*[contains(@class, 'modal-close')]",
                "//button[text()='×']",
                "//button[text()='✕']",
                "//*[@role='button' and contains(@class, 'close')]"
            ]
            
            for selector in close_selectors:
                close_buttons = self.driver.find_elements(By.XPATH, selector)
                for button in close_buttons:
                    if button.is_displayed() and button.is_enabled():
                        print("🔘 Closing popup/modal...")
                        button.click()
                        sleep(2)
                        return
                        
        except Exception as e:
            print(f"Error closing popups: {e}")

    def click_login_if_needed(self) -> None:
        """
        Click login button if it's visible and we're not already logged in
        """
        try:
            # Look for login buttons in various common locations
            login_selectors = [
                "//button[contains(text(), 'Login')]",
                "//a[contains(text(), 'Login')]",
                "//button[contains(text(), 'Sign in')]",
                "//a[contains(text(), 'Sign in')]",
                "//*[contains(text(), 'Already a paid subscriber? Sign in')]"
            ]
            
            for selector in login_selectors:
                login_buttons = self.driver.find_elements(By.XPATH, selector)
                for button in login_buttons:
                    if button.is_displayed() and button.is_enabled():
                        print("🔘 Clicking login button...")
                        button.click()
                        sleep(3)
                        return
                        
        except Exception as e:
            print(f"Error clicking login button: {e}")


    def is_login_failed(self) -> bool:
        """
        Check for the presence of the 'error-container' to indicate a failed login attempt.
        """
        error_container = self.driver.find_elements(By.ID, 'error-container')
        return len(error_container) > 0 and error_container[0].is_displayed()

    def get_url_soup(self, url: str) -> BeautifulSoup:
        """
        Gets soup from URL using logged in selenium driver
        """
        try:
            print(f"Loading premium content from: {url}")
            self.driver.get(url)
            
            # Wait for the page to load
            sleep(1)

            self.click_login_if_needed()
            
            # Additional wait to ensure content is fully loaded
            sleep(1)
            
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            
            return soup
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a Substack site.")
    parser.add_argument(
        "-u", "--url", type=str, help="The base URL of the Substack site to scrape."
    )
    parser.add_argument(
        "-d", "--directory", type=str, help="The directory to save scraped posts."
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=0,
        help="The number of posts to scrape. If 0 or not provided, all posts will be scraped.",
    )
    parser.add_argument(
        "-p",
        "--premium",
        action="store_true",
        help="Include -p in command to use the Premium Substack Scraper with selenium.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Include -h in command to run browser in headless mode when using the Premium Substack "
        "Scraper.",
    )
    parser.add_argument(
        "--chrome-path",
        type=str,
        default="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        help='Optional: The path to the Chrome/Brave browser executable. Defaults to Brave Browser on macOS.',
    )
    parser.add_argument(
        "--chrome-driver-path",
        type=str,
        default="/opt/homebrew/bin/chromedriver",
        help='Optional: The path to the Chrome WebDriver executable. Defaults to Homebrew installation.',
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="",
        help="Optional: Specify a custom user agent for selenium browser automation. Useful for "
        "passing captcha in headless mode",
    )
    parser.add_argument(
        "--html-directory",
        type=str,
        help="The directory to save scraped posts as HTML files.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.directory is None:
        args.directory = BASE_MD_DIR

    if args.html_directory is None:
        args.html_directory = BASE_HTML_DIR

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(
                args.url,
                headless=args.headless,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                chrome_path=args.chrome_path,
                chrome_driver_path=args.chrome_driver_path,
                user_agent=args.user_agent
            )
        else:
            scraper = SubstackScraper(
                args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )
        scraper.scrape_posts(args.number)

    else:  # Use the hardcoded values at the top of the file
        if USE_PREMIUM or args.premium:
            scraper = PremiumSubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                chrome_path=args.chrome_path,
                chrome_driver_path=args.chrome_driver_path,
                user_agent=args.user_agent
            )
        else:
            scraper = SubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()
