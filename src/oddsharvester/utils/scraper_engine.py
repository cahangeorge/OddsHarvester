from enum import Enum


class ScraperEngine(str, Enum):
    """Supported scraper execution engines."""

    PLAYWRIGHT = "playwright"
    AUTO = "auto"
    SCRAPLING_HTTP = "scrapling-http"
    SCRAPLING_STEALTH = "scrapling-stealth"


SCRAPLING_ENGINES = {ScraperEngine.AUTO.value, ScraperEngine.SCRAPLING_HTTP.value, ScraperEngine.SCRAPLING_STEALTH.value}
