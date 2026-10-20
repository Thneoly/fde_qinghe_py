"""Optional local .env loader for development.

Explicit environment variables always win: load_dotenv(override=False) keeps
shell/CI configuration authoritative. The .env file is gitignored; only
.env.example is committed.
"""
from pathlib import Path


def load_local_env(root):
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(Path(root) / ".env", override=False)
