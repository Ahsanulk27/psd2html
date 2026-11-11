"""Load settings from .env."""

import os

from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv()
    # The Anthropic SDK reads ANTHROPIC_API_KEY; accept CLAUDE_API_KEY too.
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]
