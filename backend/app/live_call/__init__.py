"""Live-call audio deepfake detection channel.

This package is an independent ingress: it accepts PCM audio streamed from the
Chrome extension and relays it to a streaming deepfake-detection provider. It
does not share transport with any chat-message pipeline, because the detector
needs original audio rather than a transcript.
"""

from app.live_call.router import router

__all__ = ["router"]
