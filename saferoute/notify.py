"""SMS delivery. `ConsoleNotifier` is for development and tests; the Africa's
Talking adapter is written from the provider's public REST docs and has NOT been
exercised against the live or sandbox API (no network in the build session)."""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger("saferoute.notify")


def normalize_ug_phone(raw: str) -> str | None:
    """Return +256XXXXXXXXX for any common Ugandan format, or None if it doesn't look valid."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("+256") and len(s) == 13:
        return s
    if s.startswith("256") and len(s) == 12:
        return "+" + s
    if s.startswith("0") and len(s) == 10:
        return "+256" + s[1:]
    if len(s) == 9 and s[0] == "7":
        return "+256" + s
    return None


class ConsoleNotifier:
    def __init__(self) -> None:
        self.outbox: list[tuple[str, str]] = []
        self.fail_for: set[str] = set()  # tests can make specific numbers fail

    def send(self, phone: str, message: str) -> tuple[bool, str]:
        if phone in self.fail_for:
            return False, "simulated failure"
        self.outbox.append((phone, message))
        log.info("SMS to %s: %s", phone, message)
        return True, "console"


def parse_at_response(body: str) -> tuple[bool, str]:
    try:
        recipients = json.loads(body)["SMSMessageData"]["Recipients"]
    except (ValueError, KeyError, TypeError):
        return False, f"unparseable response: {body[:120]}"
    if not recipients:
        return False, "no recipients in response"
    bad = [r for r in recipients if r.get("statusCode") not in (100, 101, 102)]
    if bad:
        return False, f"{bad[0].get('status', 'failed')} ({bad[0].get('statusCode')})"
    return True, recipients[0].get("messageId", "sent")


class AfricasTalkingNotifier:
    def __init__(self, username: str, api_key: str, sender_id: str | None = None, sandbox: bool = True) -> None:
        host = "api.sandbox.africastalking.com" if sandbox else "api.africastalking.com"
        self.url = f"https://{host}/version1/messaging"
        self.username, self.api_key, self.sender_id = username, api_key, sender_id

    def send(self, phone: str, message: str) -> tuple[bool, str]:
        form = {"username": self.username, "to": phone, "message": message}
        if self.sender_id:
            form["from"] = self.sender_id
        req = urllib.request.Request(
            self.url,
            data=urllib.parse.urlencode(form).encode(),
            headers={"apiKey": self.api_key, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return parse_at_response(resp.read().decode())
        except Exception as exc:  # network down, 4xx/5xx, timeout
            return False, f"{type(exc).__name__}: {exc}"


def build_notifier(cfg):
    if cfg.sms_provider == "africastalking":
        if not cfg.at_api_key:
            raise RuntimeError("AT_API_KEY is required when SAFEROUTE_SMS_PROVIDER=africastalking")
        return AfricasTalkingNotifier(cfg.at_username, cfg.at_api_key, cfg.at_sender_id, cfg.at_sandbox)
    return ConsoleNotifier()
