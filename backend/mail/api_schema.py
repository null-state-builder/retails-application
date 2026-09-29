"""Wire shapes for mail endpoints that assemble conditional JSON responses."""

from __future__ import annotations

from typing import Any


def obj(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


TEXT = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
ERROR = obj({"error": TEXT, "code": TEXT}, "error", "code")
ACCOUNT = {
    "email_address": {"type": "string", "format": "email"},
    "status": {"type": "string", "enum": ["connected", "needs_reconnect", "disconnected"]},
    "last_synced_at": {"type": "string", "format": "date-time", "nullable": True},
    "last_sync_error": TEXT,
}
STATUS = obj({"configured": BOOLEAN, "connected": BOOLEAN, **ACCOUNT}, "configured", "connected")
CONNECT = obj({"url": {"type": "string", "format": "uri"}}, "url")
COMPLETE_REQUEST = obj({"handoff": TEXT}, "handoff")
COMPLETE = obj({"connected": BOOLEAN, **ACCOUNT}, "connected", *ACCOUNT)
DISCONNECT = obj({"connected": BOOLEAN}, "connected")
MESSAGE_ROW = obj(
    {
        "id": INTEGER,
        "thread_id": TEXT,
        "subject": TEXT,
        "from_name": TEXT,
        "from_email": TEXT,
        "to_emails": TEXT,
        "snippet": TEXT,
        "sent_at": {"type": "string", "format": "date-time"},
        "is_unread": BOOLEAN,
        "is_starred": BOOLEAN,
        "is_sent": BOOLEAN,
    },
    "id", "thread_id", "subject", "from_name", "from_email", "to_emails",
    "snippet", "sent_at", "is_unread", "is_starred", "is_sent",
)
MESSAGES = obj(
    {
        "connected": BOOLEAN,
        "status": TEXT,
        "email_address": {"type": "string", "format": "email"},
        "last_sync_error": TEXT,
        "messages": {"type": "array", "items": MESSAGE_ROW},
    },
    "connected", "messages",
)
READ = obj({"is_unread": BOOLEAN}, "is_unread")
UNREAD = obj(
    {"connected": BOOLEAN, "status": TEXT, "unread": INTEGER, "last_sync_error": TEXT},
    "connected", "unread",
)
SEND_REQUEST = obj(
    {"to": TEXT, "subject": TEXT, "body": TEXT, "cc": TEXT,
     "reply_to": {"oneOf": [INTEGER, TEXT]}},
    "to",
)
SENT = obj({"sent": BOOLEAN}, "sent")
