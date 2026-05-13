import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from backend root directory (parent of qa_agent)
backend_root = Path(__file__).parent.parent
env_file = backend_root / ".env"

if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv()

# OpenRouter OR Anthropic
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# WP site credentials (used when running QA standalone without the UI)
WP_REST_BASE_URL = os.getenv("WP_REST_BASE_URL")       # e.g. https://mysite.com
WP_USERNAME = os.getenv("WP_USERNAME")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")         # WP Application Password
WP_ADMIN_URL = os.getenv("WP_ADMIN_URL")        # WP admin panel URL (for reference/reporting)
WP_PASSWORD = os.getenv("WP_PASSWORD")           # WP login password (optional, for reference)

# Timeouts
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
