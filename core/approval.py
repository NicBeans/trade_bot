"""Approval queue for supervised mode decisions."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    id: str
    action: str          # "grid_reset", "coin_switch", etc.
    details: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    approved: bool | None = None  # None = pending, True/False = decided
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class ApprovalQueue:
    def __init__(self):
        self._requests: dict[str, ApprovalRequest] = {}
        self._counter = 0

    def create_request(self, action: str, details: str) -> ApprovalRequest:
        self._counter += 1
        req = ApprovalRequest(
            id=f"approval-{self._counter}",
            action=action,
            details=details,
        )
        self._requests[req.id] = req
        logger.info("Approval request created: %s — %s", req.id, action)
        return req

    async def wait_for_decision(self, req: ApprovalRequest, timeout: float = 3600) -> bool:
        """Wait for approval/rejection. Returns True if approved, False if rejected or timed out."""
        try:
            await asyncio.wait_for(req._event.wait(), timeout=timeout)
            return req.approved is True
        except asyncio.TimeoutError:
            logger.warning("Approval request %s timed out", req.id)
            req.approved = False
            return False

    def approve(self, request_id: str) -> bool:
        req = self._requests.get(request_id)
        if not req or req.approved is not None:
            return False
        req.approved = True
        req._event.set()
        logger.info("Approval request %s: APPROVED", request_id)
        return True

    def reject(self, request_id: str) -> bool:
        req = self._requests.get(request_id)
        if not req or req.approved is not None:
            return False
        req.approved = False
        req._event.set()
        logger.info("Approval request %s: REJECTED", request_id)
        return True

    def get_pending(self) -> list[ApprovalRequest]:
        return [r for r in self._requests.values() if r.approved is None]

    def get_all(self) -> list[ApprovalRequest]:
        return list(self._requests.values())
