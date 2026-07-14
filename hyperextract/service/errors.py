from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceError(Exception):
    status_code: int
    code: str
    message: str
    details: list[dict] = field(default_factory=list)

    def body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }
