"""Log estruturado: cada evento vai para o arquivo JSONL e para a tabela system_events."""

from __future__ import annotations

import json
import logging
import re
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any

from sqlalchemy.orm import Session

from nikki.db import SystemEvent, utcnow
from nikki.settings import DATA_DIR

_SECRET_KEYS = re.compile(r"(token|secret|auth|password|authorization|code_verifier|refresh)", re.I)
_logger: logging.Logger | None = None


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = logging.getLogger("nikki")
        _logger.setLevel(logging.INFO)
        if not _logger.handlers:
            import os

            if os.environ.get("NIKKI_ENV") != "test":  # testes não poluem o log de produção
                (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
                fh = RotatingFileHandler(DATA_DIR / "logs" / "nikki.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(message)s"))
                _logger.addHandler(fh)
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(message)s"))
            _logger.addHandler(sh)
    return _logger


def redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("***" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


def log_event(
    s: Session | None,
    event: str,
    *,
    level: str = "info",
    correlation_id: str | None = None,
    promotion_id: int | None = None,
    job_id: int | None = None,
    **data: Any,
) -> None:
    clean = redact(data)
    record = {
        "ts": utcnow().isoformat(timespec="seconds") + "Z",
        "level": level,
        "event": event,
        "cid": correlation_id,
        "promotion_id": promotion_id,
        "job_id": job_id,
        **clean,
    }
    get_logger().log(getattr(logging, level.upper(), logging.INFO), json.dumps(record, ensure_ascii=False, default=str))
    if s is not None:
        s.add(
            SystemEvent(
                level=level,
                event=event,
                correlation_id=correlation_id,
                promotion_id=promotion_id,
                job_id=job_id,
                data=json.loads(json.dumps(clean, default=str)) if clean else None,
            )
        )
