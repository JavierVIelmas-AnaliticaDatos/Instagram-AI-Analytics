import os
from dotenv import load_dotenv

load_dotenv()

def _get_secret(key):
    """Busca primero en st.secrets (Streamlit Cloud),
    luego en variables de entorno (.env / terminal)."""
    try:
        import streamlit as st
        value = st.secrets.get(key)
        if value:
            return value
    except (ImportError, FileNotFoundError):
        pass
    return os.getenv(key)

ACCESS_TOKEN = _get_secret("ACCESS_TOKEN")
INSTAGRAM_ACCOUNT_ID = _get_secret("INSTAGRAM_ACCOUNT_ID")

NEON_DB_URL = _get_secret("NEON_DB_URL")
if NEON_DB_URL and NEON_DB_URL.startswith("postgresql://"):
    NEON_DB_URL = NEON_DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
elif NEON_DB_URL and NEON_DB_URL.startswith("postgres://"):
    NEON_DB_URL = NEON_DB_URL.replace("postgres://", "postgresql+psycopg2://", 1)