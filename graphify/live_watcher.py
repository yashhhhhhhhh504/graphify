from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS, IMAGE_EXTENSIONS, PAPER_EXTENSIONS
from graphify.live_state import LiveState, log
from graphify.live_extract import apply_inbox_delta, apply_code_delta, apply_semantic_delta

_ALL_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS | PAPER_EXTENSIONS | IMAGE_EXTENSIONS
_NON_CODE_EXTENSIONS = DOC_EXTENSIONS | PAPER_EXTENSIONS | IMAGE_EXTENSIONS
_INBOX_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS | {".pdf"}


def start_watcher(
    state: LiveState,
    watch_path: Path,
    *,
    debounce: float,
    semantic: bool,
    semantic_model: str,
) -> threading.Thread:
    try:
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver
        from watchdog.events import FileSystemEventHandler
    except ImportError as exc:
        raise ImportError(
            "watchdog is required for live mode. Install with: pip install 'graphifyy[live]'"
        ) from exc

    changed: set[Path] = set()
    changed_lock = threading.Lock()
    trigger_ts = [0.0]
    pending = [False]
    inbox_path = watch_path / "inbox"

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix.lower() not in _ALL_EXTENSIONS:
                return
            if any(part.startswith(".") for part in path.parts):
                return
            if "graphify-out" in path.parts:
                return
            with changed_lock:
                changed.add(path)
                trigger_ts[0] = time.monotonic()
                pending[0] = True

    # PollingObserver on macOS because FSEvents has known issues with some setups
    observer = PollingObserver() if sys.platform == "darwin" else Observer()
    observer.schedule(Handler(), str(watch_path), recursive=True)
    observer.start()

    def loop() -> None:
        nonlocal observer
        log(f"watcher started on {watch_path.resolve()} (debounce {debounce}s, semantic={semantic})")
        log(f"inbox ready → {inbox_path}")
        while True:
            time.sleep(0.5)

            if not observer.is_alive():
                log("warning: file watcher died — restarting observer")
                try:
                    observer.stop()
                    observer = PollingObserver() if sys.platform == "darwin" else Observer()
                    observer.schedule(Handler(), str(watch_path), recursive=True)
                    observer.start()
                    log("observer restarted")
                except Exception as exc:
                    log(f"could not restart observer: {exc}")

            with changed_lock:
                if not pending[0] or (time.monotonic() - trigger_ts[0]) < debounce:
                    continue
                batch = list(changed)
                changed.clear()
                pending[0] = False

            inbox = [p for p in batch if inbox_path in p.parents and p.suffix.lower() in _INBOX_EXTENSIONS]
            rest = [p for p in batch if p not in inbox]
            code = [p for p in rest if p.suffix.lower() in CODE_EXTENSIONS]
            non_code = [p for p in rest if p.suffix.lower() in _NON_CODE_EXTENSIONS]

            log(f"{len(batch)} change(s): {len(inbox)} inbox, {len(code)} code, {len(non_code)} non-code")

            try:
                if inbox:
                    apply_inbox_delta(state, watch_path, inbox)
                if code:
                    apply_code_delta(state, watch_path, code)
                if non_code:
                    if semantic:
                        apply_semantic_delta(state, watch_path, non_code, model=semantic_model)
                    else:
                        flag = watch_path / "graphify-out" / "needs_update"
                        flag.parent.mkdir(parents=True, exist_ok=True)
                        flag.write_text("1", encoding="utf-8")
                        log("non-code changes outside inbox — restart with --semantic to process")
            except Exception as exc:
                log(f"update loop error: {exc!r}")

    thread = threading.Thread(target=loop, daemon=True, name="graphify-live-watcher")
    thread.start()
    return thread
