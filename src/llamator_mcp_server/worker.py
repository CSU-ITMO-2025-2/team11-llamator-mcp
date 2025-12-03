from __future__ import annotations

from arq.worker import run_worker

from llamator_mcp_server.worker_settings import WorkerSettings


def main() -> None:
    """
    Точка входа для запуска ARQ worker-а (без использования arq CLI).

    :return: None
    """
    run_worker(WorkerSettings)
