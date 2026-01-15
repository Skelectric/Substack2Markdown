import argparse
import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep
from datetime import datetime
import re
import hashlib
import tempfile
import subprocess

from bs4 import BeautifulSoup
import html2text
import markdown
import requests
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, urljoin
from config import EMAIL, PASSWORD, REMOTE_SERVER, REMOTE_USER, REMOTE_BASE_DIR, REMOTE_HTML_DIR, SSH_KEY_PATH

USE_PREMIUM: bool = False  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.citriniresearch.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = REMOTE_BASE_DIR  # Remote directory for .md essay files
BASE_HTML_DIR: str = REMOTE_HTML_DIR  # Remote directory for .html essay files
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts


def get_chrome_version(chrome_path: str = None) -> Optional[str]:
    """
    Detect Chrome browser version from the binary.
    Returns version string like '142.0.7444.175' or None if detection fails.
    """
    import platform
    import json
    
    # Try to get version using Chrome's --version flag
    chrome_binary = chrome_path
    if not chrome_binary:
        # Try common Chrome locations
        system = platform.system()
        if system == "Darwin":  # macOS
            chrome_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                os.path.expanduser("~/Library/Application Support/Google/Chrome/Default"),
            ]
            # Check for Chrome for Testing in cache (used by Selenium)
            cache_path = os.path.expanduser("~/.cache/selenium/chrome")
            if os.path.exists(cache_path):
                for root, dirs, files in os.walk(cache_path):
                    for d in dirs:
                        test_path = os.path.join(root, d, "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
                        if os.path.exists(test_path):
                            chrome_paths.insert(0, test_path)
        elif system == "Windows":
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        else:  # Linux
            chrome_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
        
        for path in chrome_paths:
            if os.path.exists(path):
                chrome_binary = path
                break
    
    if not chrome_binary or not os.path.exists(chrome_binary):
        return None
    
    try:
        # Try --version flag
        result = subprocess.run(
            [chrome_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            # Extract version number (e.g., "Google Chrome 142.0.7444.175" -> "142.0.7444.175")
            version_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
            if version_match:
                return version_match.group(1)
    except Exception:
        pass
    
    try:
        # Try --version --format=json (newer Chrome versions)
        result = subprocess.run(
            [chrome_binary, "--version", "--format=json"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if "browser_version" in data:
                return data["browser_version"]
    except Exception:
        pass
    
    return None


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


class RemoteFileHandler:
    """
    Handles file operations on a remote server via SSH/SCP
    """
    
    def __init__(self, server: str, user: str, base_dir: str, ssh_key_path: str = None):
        self.server = server
        self.user = user
        self.base_dir = base_dir
        self.ssh_key_path = os.path.expanduser(ssh_key_path or "~/.ssh/id_rsa")
        
        # Test connection on initialization
        if not self.test_connection():
            raise ConnectionError(f"Could not connect to remote server {self.user}@{self.server}")
    
    def test_connection(self) -> bool:
        """
        Test SSH connection to the remote server
        """
        try:
            success, output = self._run_ssh_command("echo 'Connection test successful'")
            if success:
                print(f"[OK] Successfully connected to {self.user}@{self.server}")
                return True
            else:
                print(f"[ERROR] Failed to connect to {self.user}@{self.server}: {output}")
                return False
        except Exception as e:
            print(f"[ERROR] Connection test failed: {e}")
            return False
        
    def _run_ssh_command(self, command: str, max_retries: int = 3) -> Tuple[bool, str]:
        """
        Run an SSH command on the remote server with improved error logging and connection reuse
        """
        for attempt in range(max_retries):
            try:
                # Use SSH with connection multiplexing for better performance
                ssh_cmd = [
                    "ssh", 
                    "-i", self.ssh_key_path,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10",
                    "-o", "ControlMaster=auto",
                    "-o", "ControlPath=~/.ssh/control-%r@%h:%p",
                    "-o", "ControlPersist=60",
                    f"{self.user}@{self.server}",
                    command
                ]
                
                result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
                
                if result.returncode == 0:
                    return True, result.stdout
                else:
                    # Enhanced error logging with command details
                    error_details = []
                    if result.stderr:
                        error_details.append(f"stderr: {result.stderr}")
                    if result.stdout:
                        error_details.append(f"stdout: {result.stdout}")
                    if result.returncode != 0:
                        error_details.append(f"exit code: {result.returncode}")
                    
                    # Add command details for debugging
                    error_details.append(f"command: {command}")
                    
                    error_msg = "; ".join(error_details) if error_details else "Unknown SSH error"
                    
                    # Check if this is a "file doesn't exist" case (normal behavior for test -f)
                    is_file_check = command.startswith("test -f")
                    is_exit_code_1 = result.returncode == 1
                    is_no_stderr = not result.stderr
                    
                    if is_file_check and is_exit_code_1 and is_no_stderr:
                        # This is normal "file doesn't exist" behavior, don't treat as error
                        return False, error_msg
                    
                    if attempt < max_retries - 1:
                        print(f"[ERROR] SSH command failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                        sleep(2)
                        continue
                    return False, error_msg
                    
            except subprocess.TimeoutExpired:
                if attempt < max_retries - 1:
                    print(f"[ERROR] SSH command timed out (attempt {attempt + 1}/{max_retries})")
                    sleep(2)
                    continue
                return False, "SSH command timed out"
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[ERROR] SSH command failed (attempt {attempt + 1}/{max_retries}): {str(e)}")
                    sleep(2)
                    continue
                return False, f"SSH command failed: {str(e)}"
        
        return False, "All SSH command attempts failed"
    
    def _run_scp_command(self, local_path: str, remote_path: str, max_retries: int = 3) -> Tuple[bool, str]:
        """
        Copy a file to the remote server using SCP with retry logic
        """
        for attempt in range(max_retries):
            try:
                scp_cmd = [
                    "scp",
                    "-i", self.ssh_key_path,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10",
                    "-o", "ControlMaster=auto",
                    "-o", "ControlPath=~/.ssh/control-%r@%h:%p",
                    "-o", "ControlPersist=60",
                    local_path,
                    f"{self.user}@{self.server}:{remote_path}"
                ]
                
                # print(f"[DEBUG] SCP command: {' '.join(scp_cmd)}")
                # print(f"[DEBUG] Local file exists: {os.path.exists(local_path)}")
                # print(f"[DEBUG] Local file size: {os.path.getsize(local_path) if os.path.exists(local_path) else 'N/A'}")
                
                result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
                
                # print(f"[DEBUG] SCP return code: {result.returncode}")
                # print(f"[DEBUG] SCP stdout: '{result.stdout}'")
                # print(f"[DEBUG] SCP stderr: '{result.stderr}'")
                
                if result.returncode == 0:
                    return True, result.stdout
                else:
                    # Improved error logging - show actual error details
                    error_details = []
                    if result.stderr:
                        error_details.append(f"stderr: {result.stderr}")
                    if result.stdout:
                        error_details.append(f"stdout: {result.stdout}")
                    if result.returncode != 0:
                        error_details.append(f"exit code: {result.returncode}")
                    
                    error_msg = "; ".join(error_details) if error_details else "Unknown SCP error"
                    
                    if attempt < max_retries - 1:
                        print(f"[ERROR] SCP command failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                        sleep(2)  # Wait before retry
                        continue
                    return False, error_msg
            except subprocess.TimeoutExpired:
                if attempt < max_retries - 1:
                    print(f"[ERROR] SCP command timed out (attempt {attempt + 1}/{max_retries})")
                    sleep(2)
                    continue
                return False, "SCP command timed out"
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[ERROR] SCP command failed (attempt {attempt + 1}/{max_retries}): {str(e)}")
                    sleep(2)
                    continue
                return False, f"SCP command failed: {str(e)}"
        
        return False, "All SCP command attempts failed"
    
    def test_connection(self) -> bool:
        """
        Test SSH connection to the remote server
        """
        # print(f"[DEBUG] Testing SSH connection to {self.user}@{self.server}")
        success, output = self._run_ssh_command("echo 'SSH connection test successful'")
        if success:
            print(f"[OK] SSH connection test passed: {output.strip()}")
            return True
        else:
            print(f"[ERROR] SSH connection test failed: {output}")
            return False
    
    def ensure_directory_exists(self, remote_path: str) -> bool:
        """
        Ensure a directory exists on the remote server
        """
        success, output = self._run_ssh_command(f"mkdir -p {remote_path}")
        if not success:
            print(f"Warning: Could not create directory {remote_path}: {output}")
        return success
    
    def file_exists(self, remote_path: str) -> bool:
        """
        Check if a file exists on the remote server
        """
        success, output = self._run_ssh_command(f"test -f {remote_path}")
        
        # Handle the case where test -f returns exit code 1 (file doesn't exist)
        # This is normal behavior, not an error
        if not success:
            # Check if the error is just "file doesn't exist" (exit code 1)
            if "exit code: 1" in output and "stderr:" not in output:
                # This is normal - file doesn't exist
                return False
            else:
                # This is a real error (connection issue, permission problem, etc.)
                print(f"[DEBUG] File existence check failed for {remote_path}: {output}")
                return False
        
        return success
    
    def save_file(self, content: str, remote_path: str) -> bool:
        """
        Save content to a file on the remote server
        """
        # Create the directory if it doesn't exist
        remote_dir = os.path.dirname(remote_path)
        if not self.ensure_directory_exists(remote_dir):
            return False
        
        # Write content to a temporary local file
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as temp_file:
            temp_file.write(content)
            temp_local_path = temp_file.name
        
        try:
            # Copy the file to the remote server
            success, output = self._run_scp_command(temp_local_path, remote_path)
            if not success:
                print(f"Error saving file {remote_path}: {output}")
            return success
        finally:
            # Clean up the temporary file
            os.unlink(temp_local_path)
    
    def download_file(self, remote_path: str, local_path: str) -> bool:
        """
        Download a file from the remote server
        """
        try:
            scp_cmd = [
                "scp",
                "-i", self.ssh_key_path,
                "-o", "StrictHostKeyChecking=no",
                f"{self.user}@{self.server}:{remote_path}",
                local_path
            ]
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
            return result.returncode == 0
        except Exception as e:
            print(f"Error downloading file {remote_path}: {str(e)}")
            return False


def generate_html_file(author_name: str) -> None:
    """
    Generates a HTML file for the given author.
    """
    # Create remote file handler
    remote_handler = RemoteFileHandler(REMOTE_SERVER, REMOTE_USER, REMOTE_BASE_DIR, SSH_KEY_PATH)
    
    # Ensure remote HTML directory exists
    remote_handler.ensure_directory_exists(REMOTE_HTML_DIR)

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

    # Write the modified HTML to the remote server
    html_output_path = os.path.join(REMOTE_HTML_DIR, f'{author_name}.html')
    success = remote_handler.save_file(html_with_author, html_output_path)
    if success:
        print(f"Generated HTML file: {html_output_path}")
    else:
        print(f"Failed to generate HTML file: {html_output_path}")


class BaseSubstackScraper(ABC):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url

        self.writer_name: str = extract_main_part(base_substack_url)
        md_save_dir: str = f"{md_save_dir}/{self.writer_name}"

        self.md_save_dir: str = md_save_dir
        self.html_save_dir: str = f"{html_save_dir}/{self.writer_name}"

        # Initialize remote file handler with fallback
        try:
            self.remote_handler = RemoteFileHandler(REMOTE_SERVER, REMOTE_USER, REMOTE_BASE_DIR, SSH_KEY_PATH)
            self.use_remote = True
            
            # Test connection before proceeding
            if not self.remote_handler.test_connection():
                print("[ERROR] SSH connection test failed, falling back to local mode")
                self.use_remote = False
                self.remote_handler = None
            else:
                # Ensure remote directories exist
                if not self.remote_handler.ensure_directory_exists(md_save_dir):
                    print(f"Warning: Could not create remote md directory {md_save_dir}")
                else:
                    print(f"Ensured remote md directory exists: {md_save_dir}")
                
                if not self.remote_handler.ensure_directory_exists(self.html_save_dir):
                    print(f"Warning: Could not create remote html directory {self.html_save_dir}")
                else:
                    print(f"Ensured remote html directory exists: {self.html_save_dir}")
                
        except ConnectionError as e:
            print(f"[WARNING] Remote connection failed: {e}")
            print("Falling back to local file storage...")
            self.use_remote = False
            self.remote_handler = None
            
            # Create local directories as fallback
            if not os.path.exists(md_save_dir):
                os.makedirs(md_save_dir)
                print(f"Created local md directory {md_save_dir}")
            if not os.path.exists(self.html_save_dir):
                os.makedirs(self.html_save_dir)
                print(f"Created local html directory {self.html_save_dir}")

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

    def save_to_file(self, filepath: str, content: str) -> None:
        """
        This method saves content to a file. Can be used to save HTML or Markdown
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        if self.use_remote:
            if self.remote_handler.file_exists(filepath):
                print(f"File already exists: {filepath}")
                return

            success = self.remote_handler.save_file(content, filepath)
            if success:
                print(f"Saved file: {filepath}")
            else:
                print(f"Failed to save file: {filepath}")
        else:
            # Local fallback
            if os.path.exists(filepath):
                print(f"File already exists: {filepath}")
                return

            with open(filepath, 'w', encoding='utf-8') as file:
                file.write(content)
            print(f"Saved file locally: {filepath}")

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
        if self.use_remote:
            # Convert Windows path separators to Unix for remote server
            remote_images_dir = images_dir.replace("\\", "/")
            if not self.remote_handler.ensure_directory_exists(remote_images_dir):
                print(f"Warning: Could not create remote images directory: {remote_images_dir}")
            else:
                print(f"Ensured remote images directory exists: {remote_images_dir}")
            return remote_images_dir
        else:
            if not os.path.exists(images_dir):
                os.makedirs(images_dir)
                print(f"Created local images directory: {images_dir}")
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
        Downloads an image from URL and saves it
        Returns the file path if successful, None if failed
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
            if self.use_remote:
                # For remote, use forward slashes
                file_path = f"{images_dir}/{filename}"
            else:
                # For local, use os.path.join
                file_path = os.path.join(images_dir, filename)
            
            # Skip if file already exists
            if self.use_remote:
                if self.remote_handler.file_exists(file_path):
                    return file_path
            else:
                if os.path.exists(file_path):
                    return file_path
            
            # Download the image
            try:
                # print(f"[DEBUG] Downloading image: {image_url}")
                response = requests.get(image_url, stream=True, timeout=30)
                response.raise_for_status()
                # print(f"[DEBUG] Image download successful, size: {len(response.content)} bytes")
            except requests.exceptions.RequestException as e:
                # print(f"[ERROR] Failed to download image {image_url}: {e}")
                return None
            
            if self.use_remote:
                # Save to temporary local file first, then upload
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                        for chunk in response.iter_content(chunk_size=8192):
                            temp_file.write(chunk)
                        temp_local_path = temp_file.name
                    
                    # print(f"[DEBUG] Temporary file created: {temp_local_path}")
                    
                    # Copy the image to the remote server
                    success, output = self.remote_handler._run_scp_command(temp_local_path, file_path)
                    if success:
                        print(f"[OK] Image uploaded successfully: {file_path}")
                        return file_path
                    else:
                        print(f"[ERROR] Failed to upload image {image_url}: {output}")
                        return None
                except Exception as e:
                    print(f"[ERROR] Error processing image {image_url}: {e}")
                    return None
                finally:
                    # Clean up the temporary file
                    if 'temp_local_path' in locals() and os.path.exists(temp_local_path):
                        os.unlink(temp_local_path)
            else:
                # Save directly to local file
                try:
                    with open(file_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    print(f"[OK] Image saved locally: {file_path}")
                    return file_path
                except Exception as e:
                    print(f"[ERROR] Failed to save image locally {image_url}: {e}")
                    return None
            
        except Exception as e:
            print(f"Failed to download image {image_url}: {e}")
            return None

    def replace_image_urls_in_markdown(self, markdown_content: str, images_dir: str) -> str:
        """
        Replaces image URLs in markdown content with relative remote paths
        """
        image_urls = self.extract_image_urls_from_markdown(markdown_content)
        
        for image_url in image_urls:
            remote_path = self.download_image(image_url, images_dir)
            if remote_path:
                # Calculate relative path from markdown file to image
                relative_path = os.path.relpath(remote_path, self.md_save_dir)
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

        if self.use_remote:
            success = self.remote_handler.save_file(html_content, filepath)
            if success:
                print(f"Saved HTML file: {filepath}")
            else:
                print(f"Failed to save HTML file: {filepath}")
        else:
            with open(filepath, 'w', encoding='utf-8') as file:
                file.write(html_content)
            print(f"Saved HTML file locally: {filepath}")

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
        Note: JSON data is stored locally, but file paths in the JSON point to remote locations.
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
                
                if self.use_remote:
                    # For remote, use forward slashes
                    md_filepath = f"{self.md_save_dir}/{md_filename}"
                    html_filepath = f"{self.html_save_dir}/{html_filename}"
                else:
                    # For local, use os.path.join
                    md_filepath = os.path.join(self.md_save_dir, md_filename)
                    html_filepath = os.path.join(self.html_save_dir, html_filename)

                file_exists = False
                if self.use_remote:
                    file_exists = self.remote_handler.file_exists(md_filepath)
                else:
                    file_exists = os.path.exists(md_filepath)
                
                if not file_exists:
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
        
        # Set Chrome/Brave Browser path if not specified
        import platform
        if chrome_path:
            options.binary_location = chrome_path
        else:
            # Try to find Chrome for Testing (used by Selenium Manager) first
            cache_path = os.path.expanduser("~/.cache/selenium/chrome")
            chrome_for_testing_found = False
            if os.path.exists(cache_path):
                # Look for Chrome for Testing in cache
                for root, dirs, files in os.walk(cache_path):
                    for d in dirs:
                        if platform.system() == "Darwin":  # macOS
                            test_path = os.path.join(root, d, "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
                        elif platform.system() == "Windows":
                            test_path = os.path.join(root, d, "chrome.exe")
                        else:  # Linux
                            test_path = os.path.join(root, d, "chrome")
                        
                        if os.path.exists(test_path):
                            options.binary_location = test_path
                            print(f"Found Chrome for Testing at: {test_path}")
                            chrome_for_testing_found = True
                            break
                    if chrome_for_testing_found:
                        break
            
            # If Chrome for Testing not found, try standard locations
            if not chrome_for_testing_found:
                if platform.system() == "Windows":
                    brave_paths = [
                        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
                        os.path.expanduser(r"~\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"),
                        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    ]
                    for path in brave_paths:
                        if os.path.exists(path):
                            options.binary_location = path
                            print(f"Found browser at: {path}")
                            break
                    else:
                        print("Browser not found in common locations. Using system default.")
                elif platform.system() == "Darwin":  # macOS
                    chrome_paths = [
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                    ]
                    for path in chrome_paths:
                        if os.path.exists(path):
                            options.binary_location = path
                            print(f"Found browser at: {path}")
                            break
                    else:
                        print("Chrome not found in standard locations. Selenium Manager will handle it.")
                else:  # Linux
                    chrome_paths = [
                        "/usr/bin/google-chrome",
                        "/usr/bin/chromium-browser",
                        "/usr/bin/chromium",
                    ]
                    for path in chrome_paths:
                        if os.path.exists(path):
                            options.binary_location = path
                            print(f"Found browser at: {path}")
                            break
        
        if user_agent:
            options.add_argument(f'user-agent={user_agent}')  # Pass this if running headless and blocked by captcha

        # Add Brave-specific options
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-debugging-port=9222")

        # Use webdriver-manager or Selenium Manager to automatically download ChromeDriver
        if chrome_driver_path:
            service = Service(executable_path=chrome_driver_path)
        else:
            # Detect Chrome version for better driver matching
            detected_chrome_path = chrome_path if chrome_path else (options.binary_location if options.binary_location else None)
            chrome_version = get_chrome_version(detected_chrome_path)
            if chrome_version:
                print(f"Detected Chrome version: {chrome_version}")
            
            # Try webdriver-manager first, fallback to Selenium Manager
            try:
                driver_manager = ChromeDriverManager()
                # Try to help webdriver-manager use the detected Chrome version
                if chrome_version and hasattr(driver_manager, 'driver'):
                    try:
                        if hasattr(driver_manager.driver, 'get_browser_version_from_os'):
                            driver_manager.driver.get_browser_version_from_os = lambda: chrome_version
                            print(f"Using Chrome version {chrome_version} for driver selection")
                    except (AttributeError, Exception):
                        pass  # Continue if patching fails
                
                driver_path = driver_manager.install()
                service = Service(executable_path=driver_path)
                print(f"Using ChromeDriver from webdriver-manager: {driver_path}")
            except (AttributeError, Exception) as e:
                # Fallback to Selenium Manager (built into Selenium 4.6+)
                print(f"webdriver-manager had issues ({type(e).__name__}), using Selenium Manager instead")
                service = Service()

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
        print("[WAITING] Waiting for login process to complete...")
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
                    print("[OK] Login successful")
                    login_successful = True
                    break
                
                # Check for error messages
                error_elements = self.driver.find_elements(By.XPATH, "//*[contains(@class, 'error') or contains(text(), 'Invalid')]")
                if error_elements:
                    print(f"[ERROR] Login error: {[e.text for e in error_elements if e.text]}")
                    
            except Exception as e:
                print(f"Error checking login status: {e}")
            
            sleep(10)
            wait_time += 10
            
        if not login_successful:
            print("[WARNING] Login verification timeout - proceeding anyway")

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
                        print("[ACTION] Closing popup/modal...")
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
                        print("[ACTION] Clicking login button...")
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
        default="",
        help='Optional: The path to the Chrome/Brave browser executable. If not specified, will use system default.',
    )
    parser.add_argument(
        "--chrome-driver-path",
        type=str,
        default="",
        help='Optional: The path to the Chrome WebDriver executable. If not specified, Selenium Manager will handle it automatically.',
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
