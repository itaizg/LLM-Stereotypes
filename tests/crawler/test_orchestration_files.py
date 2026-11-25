"""The n8n workflow export and compose files hold no secrets and pin images (T011)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2] / "orchestration"
WORKFLOW = ROOT / "n8n" / "crawler-run.workflow.json"
COMPOSE = [ROOT / "n8n" / "docker-compose.yaml"]
LANGFUSE = ROOT / "langfuse" / "docker-compose.yaml"
if LANGFUSE.exists():
    COMPOSE.append(LANGFUSE)


def workflow() -> dict[str, Any]:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def reachable(start: str, connections: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        for branch in connections.get(name, {}).get("main", []):
            for target in branch:
                if target["node"] not in seen:
                    seen.add(target["node"])
                    todo.append(target["node"])
    return seen


def test_the_workflow_is_inactive_and_holds_no_credential_values() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    data = workflow()
    assert data["active"] is False
    assert not re.search(r"Bearer\s+[A-Za-z0-9._-]{8,}", text)
    for node in data["nodes"]:
        for ref in node.get("credentials", {}).values():
            assert set(ref) == {"id", "name"}
            assert ref["id"] == ref["name"].strip() and ref["name"].startswith(
                "crawler-"
            )


def test_every_http_request_targets_the_trigger() -> None:
    urls = [
        n["parameters"]["url"]
        for n in workflow()["nodes"]
        if n["type"] == "n8n-nodes-base.httpRequest"
    ]
    assert urls
    assert all(
        u.lstrip("=").startswith("http://host.docker.internal:8765/") for u in urls
    )


def test_every_outcome_but_completed_and_skipped_reaches_notify() -> None:
    data = workflow()
    connections = data["connections"]
    # Start-response branches: running, busy (skipped), refused, anything else.
    branches = connections["Route start response"]["main"]
    notified = [any(t["node"] == "Notify" for t in b) for b in branches]
    assert notified == [False, False, True, True]
    assert [t["node"] for t in branches[1]] == ["End (skipped)"]
    # Poll loop: a finished run that is not "completed" goes to Notify.
    outcome = connections["Route outcome"]["main"]
    assert [t["node"] for t in outcome[0]] == ["End (completed)"]
    assert [t["node"] for t in outcome[1]] == ["Notify"]
    assert "Notify" in reachable("Schedule", connections)
    assert "Notify" in reachable("Webhook", connections)


def test_the_schedule_is_two_in_the_morning() -> None:
    schedule = next(n for n in workflow()["nodes"] if n["name"] == "Schedule")
    rule = schedule["parameters"]["rule"]["interval"][0]
    assert rule["expression"] == "0 2 * * *"


@pytest.mark.parametrize("path", COMPOSE, ids=lambda p: p.parent.name)
def test_compose_files_pin_images_and_use_named_volumes(path: Path) -> None:
    compose = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        image = service["image"]
        tag = image.rsplit(":", 1)[1] if ":" in image.split("/")[-1] else ""
        assert tag and tag != "latest", f"{name} must pin an image tag"
        for volume in service.get("volumes", []):
            source = str(volume).split(":")[0]
            assert not source.startswith((".", "/", "~")), f"{name}: bind mount"
            assert source in compose.get("volumes", {})
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: port not on loopback"
