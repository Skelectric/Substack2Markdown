import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Substack credentials - loaded from environment variables
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

# Validate that required environment variables are set
if not EMAIL:
    raise ValueError("EMAIL environment variable is not set")
if not PASSWORD:
    raise ValueError("PASSWORD environment variable is not set")

# Remote server configuration - loaded from environment variables
REMOTE_SERVER = os.getenv("REMOTE_SERVER", "192.168.104.209")
REMOTE_USER = os.getenv("REMOTE_USER", "ubuntu")
REMOTE_BASE_DIR = os.getenv("REMOTE_BASE_DIR", "/home/ubuntu/substacks")
REMOTE_HTML_DIR = os.getenv("REMOTE_HTML_DIR", "/home/ubuntu/substacks/html")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "~/.ssh/id_ed25519")
