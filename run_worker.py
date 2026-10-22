#!/usr/bin/env python3
"""CLI entry point for the job processing worker.

Usage:
    ./venv/bin/python run_worker.py

With custom options:
    ./venv/bin/python run_worker.py --poll-interval 60 --concurrent 3

Run multiple workers for parallel processing:
    ./venv/bin/python run_worker.py --worker-id worker-1 --concurrent 2 &
    ./venv/bin/python run_worker.py --worker-id worker-2 --concurrent 2 &

Secrets:
    API keys are loaded from Docker secret files (/run/secrets/),
    environment variables, or macOS Keychain (local dev fallback).
"""

import argparse
import asyncio
import signal

from dotenv import load_dotenv

load_dotenv()

from api.services.secrets import bootstrap_secrets

bootstrap_secrets()

import json

from api.services.config_path import resolve_config_path
from api.services.database import close_db, init_db
from api.services.llm import close_llm_client, get_llm_client
from api.services.worker import JobWorker, WorkerConfig


def load_worker_defaults() -> dict:
    """Load worker defaults from config file."""
    # Same file the API's Settings page writes — not the packaged default (#440).
    config_path = resolve_config_path()
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
            return config.get("worker", {})
    return {}


async def main(args):
    """Run the job processing worker."""
    # Initialize database
    await init_db()

    # Initialize LLM client
    get_llm_client()

    # Load defaults from config file
    defaults = load_worker_defaults()

    # Create worker config (CLI args override config file defaults)
    # Use None as sentinel to detect if CLI arg was provided
    config = WorkerConfig(
        poll_interval=(
            args.poll_interval if args.poll_interval is not None else defaults.get("poll_interval_seconds", 5)
        ),
        heartbeat_interval=(
            args.heartbeat_interval
            if args.heartbeat_interval is not None
            else defaults.get("heartbeat_interval_seconds", 60)
        ),
        max_retries=args.max_retries if args.max_retries is not None else 3,
        max_concurrent_jobs=(
            args.concurrent if args.concurrent is not None else defaults.get("max_concurrent_jobs", 3)
        ),
        worker_id=args.worker_id,
    )

    # Create and start worker
    worker = JobWorker(config)

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def shutdown_handler():
        print("\n[Worker] Shutdown signal received, stopping...")
        asyncio.create_task(worker.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    try:
        worker_id = config.worker_id
        print(f"[{worker_id}] Starting Cardigan job worker...")
        print(f"[{worker_id}] Poll interval: {config.poll_interval}s")
        print(f"[{worker_id}] Heartbeat interval: {config.heartbeat_interval}s")
        print(f"[{worker_id}] Concurrent jobs: {config.max_concurrent_jobs}")
        print(f"[{worker_id}] Max retries: {config.max_retries}")
        print(f"[{worker_id}] Press Ctrl+C to stop")
        print()

        await worker.start()
    finally:
        # Cleanup
        await close_llm_client()
        await close_db()
        print("[Worker] Shutdown complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Cardigan job processing worker")
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help="Seconds between queue polling (default: from config file, fallback: 5)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=None,
        help="Seconds between heartbeat updates (default: from config file, fallback: 60)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Maximum retry attempts for failed jobs (default: 3)",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=None,
        help="Maximum concurrent jobs to process (default: from config file, fallback: 3)",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=None,
        help="Unique worker identifier (default: worker-{pid})",
    )

    args = parser.parse_args()
    asyncio.run(main(args))
