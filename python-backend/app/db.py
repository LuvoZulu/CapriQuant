import os
import logging
from dotenv import load_dotenv
import psycopg2
from psycopg2 import OperationalError

load_dotenv()

logger = logging.getLogger(__name__)

conn = None
cursor = None


def get_connection():
    """Returns a valid connection, reconnecting if necessary."""
    global conn, cursor
    if conn is None or conn.closed != 0:
        try:
            conn = psycopg2.connect(
                dbname=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT")
            )
            cursor = conn.cursor()
            logger.info("Database connection (re)established successfully.")
        except OperationalError as e:
            logger.error(f"Failed to connect to database: {e}")
            conn = None
            cursor = None
            raise
    return conn, cursor


# Initial connection attempt
try:
    conn, cursor = get_connection()
except Exception:
    logger.warning("Initial database connection failed. Will retry on first use.")