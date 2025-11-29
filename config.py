import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

# Validate that required environment variables are set
if not EMAIL:
    raise ValueError("EMAIL environment variable is not set")
if not PASSWORD:
    raise ValueError("PASSWORD environment variable is not set")
