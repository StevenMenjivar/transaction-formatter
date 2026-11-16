from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Settings:
    app_name: str = "Transaction Normalizer"
    max_file_bytes: int = int(os.getenv("MAX_FILE_BYTES", 10 * 1024 * 1024))
    max_transactions: int = int(os.getenv("MAX_TRANSACTIONS", 50_000))
    max_depth: int = int(os.getenv("MAX_DEPTH", 10))
    timezone: str = os.getenv("DEFAULT_TIMEZONE", "UTC")
    date_order: str = os.getenv("DEFAULT_DATE_ORDER", "DMY").upper()
    fuzzy_threshold: float = float(os.getenv("FUZZY_THRESHOLD", "0.91"))

settings = Settings()
