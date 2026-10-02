"""Project Agent v1 interface (Spec: "Project Agent v1" - wiki
maintenance only; health score/nudges/GitHub are After v1).
"""
from __future__ import annotations

from shared.models.interface import TextModelClient
from shared.wiki.interface import VaultClient

from agents.project_agent.agent import (
    ApplyResult,
    ProjectAgent,
    apply_thread_items_to_initiative,
    rewrite_status,
)


async def run_project_agent_once(vault: VaultClient, worker_client: TextModelClient) -> list[ApplyResult]:
    return await ProjectAgent(vault, worker_client).run_once()


__all__ = [
    "ApplyResult",
    "ProjectAgent",
    "apply_thread_items_to_initiative",
    "rewrite_status",
    "run_project_agent_once",
]
