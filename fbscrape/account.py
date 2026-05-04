from dataclasses import dataclass, field, asdict
from datetime import datetime
import sqlite3
import json

from .models import JSONTrait
from .utils import utc


@dataclass
class Account(JSONTrait):
    password: str
    email: str | None = None
    username: str | None = None
    email_password: str | None = None
    phone_number: str | None = None
    active: bool = False
    locks: dict[str, datetime] = field(default_factory=dict)  # queue: datetime
    scroll_count_per_endpoint_total: dict[str, int] = field(default_factory=dict)  # queue: requests
    cookies: list[dict] = field(default_factory=list)  # Playwright cookie format
    twofa_id: str | None = None
    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    fingerprints: dict[str, str] = field(default_factory=dict)  # os -> serialized fingerprint JSON
    error_msg: str | None = None
    last_used: datetime | None = None
    in_use: bool = False
    scroll_count_overall_24h: int = 0

    def __post_init__(self):
        if self.email is None and self.phone_number is None:
            raise ValueError("Account must have either email or phone_number")

    @property
    def identifier(self) -> str:
        """Returns the account identifier (email or phone_number)"""
        return self.email if self.email else self.phone_number

    @property
    def display_name(self) -> str:
        """Returns username if available, otherwise identifier (for logging)"""
        return self.username if self.username else self.identifier

    @staticmethod
    def from_rs(rs: sqlite3.Row):
        doc = dict(rs)
        # Remove internal fields
        doc.pop("_tx", None)
        doc["locks"] = {k: utc.from_iso(v) for k, v in json.loads(doc["locks"]).items()}
        doc["scroll_count_per_endpoint_total"] = {k: v for k, v in json.loads(doc["scroll_count_per_endpoint_total"]).items() if isinstance(v, int)}
        doc["cookies"] = json.loads(doc["cookies"])
        doc["fingerprints"] = json.loads(doc["fingerprints"])
        doc["active"] = bool(doc["active"])
        doc["in_use"] = bool(doc["in_use"])
        doc["last_used"] = utc.from_iso(doc["last_used"]) if doc["last_used"] else None
        return Account(**doc)

    def to_rs(self):
        rs = asdict(self)
        rs["locks"] = json.dumps(rs["locks"], default=lambda x: x.isoformat())
        rs["scroll_count_per_endpoint_total"] = json.dumps(rs["scroll_count_per_endpoint_total"])
        rs["cookies"] = json.dumps(rs["cookies"])
        rs["fingerprints"] = json.dumps(rs["fingerprints"])
        rs["last_used"] = rs["last_used"].isoformat() if rs["last_used"] else None
        return rs