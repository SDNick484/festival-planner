"""GET /api/activity?since=&entity_type=&entity_id=&source=&limit=."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import jsonify

from festival_planner.api._common import INT64_MAX, Query, Router
from festival_planner.db import get_db
from festival_planner.service import activity


def _parse_since(query: Query) -> datetime | None:
    """An ISO-8601 date or datetime, as UTC (a naive value is taken as UTC)."""
    raw = query.text("since")
    if raw is None or "since" in query.errors:
        return None
    try:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)  # can overflow near year 1 or 9999
    except (ValueError, OverflowError):
        query.errors["since"] = "must be an ISO-8601 date or datetime"
        return None


def register(router: Router) -> None:
    @router.route("/activity", "GET")
    def list_activity():
        query = Query({"since", "entity_type", "entity_id", "source", "limit"})
        filters = {
            "since": _parse_since(query),
            "entity_type": query.text("entity_type"),
            "entity_id": query.integer("entity_id", lo=1, hi=INT64_MAX),
            "source": query.text("source"),
        }
        limit = query.integer("limit")
        if limit is not None:
            filters["limit"] = limit
        query.done()
        return jsonify(activity.list_activity(get_db(), **filters))
