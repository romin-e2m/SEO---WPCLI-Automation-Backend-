import logging
import logging.config
from typing import Any

# Configure logging to suppress health check logs
LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        },
        "access": {
            "format": "%(asctime)s - %(client_addr)s - %(request_line)s - %(status_code)s",
        },
    },
    "filters": {
        "health_check_filter": {
            "()": "app.logging_config.HealthCheckFilter",
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["health_check_filter"],
        },
    },
    "loggers": {
        "uvicorn": {
            "handlers": ["default"],
            "level": "INFO",
        },
        "uvicorn.access": {
            "handlers": ["access"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


class HealthCheckFilter(logging.Filter):
    """Filter out health check and status endpoint logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Suppress logs for health check and status endpoints
        if hasattr(record, "getMessage"):
            message = record.getMessage()
            if "/system/status" in message or "/health" in message:
                return False
        return True
