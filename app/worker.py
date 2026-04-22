import signal
import threading

from app.core.config import settings
from app.service.ingestion_task_service import ensure_ingestion_tables, run_ingestion_worker_forever


def main():
    ensure_ingestion_tables()
    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    run_ingestion_worker_forever(
        worker_name=settings.ingestion_worker_name or None,
        stop_event=stop_event,
        poll_seconds=settings.ingestion_worker_poll_seconds,
    )


if __name__ == "__main__":
    main()
