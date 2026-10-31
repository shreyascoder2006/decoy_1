"""Email phishing-signal channel: independent of the live-call audio channel."""

from app.email_check.router import chat_router, router

__all__ = ["chat_router", "router"]
