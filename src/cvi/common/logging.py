"""Structured JSON logging (one JSON object per line; ready for Loki/ELK/CloudWatch)."""

import logging
import sys
import time

import orjson


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in getattr(record, "ctx", {}).items():
            doc[k] = v
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(doc, default=str).decode()


def setup_logging(service: str, level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return logging.getLogger(service)


def log(logger: logging.Logger, level: int, msg: str, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
