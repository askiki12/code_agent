"""Background worker bridging run_task to the Textual UI thread."""
from __future__ import annotations

import threading


class AgentWorker:
    def __init__(self, app, session, *, on_delta, on_tool, on_done, on_ask=None) -> None:
        self.app = app
        self.session = session
        self._on_delta = on_delta
        self._on_tool = on_tool
        self._on_done = on_done
        self._on_ask = on_ask
        self._thread: threading.Thread | None = None

    def start(self, task: str) -> None:
        self._thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, task: str) -> None:
        try:
            if self._on_ask is not None:
                self.session.ask = self._ask
            result = self.session.run_task(task, on_delta=self._delta, on_tool=self._tool)
        except Exception as e:  # noqa: BLE001
            from code_agent.agent import RunResult
            result = RunResult(final_text="", iterations=0, finished=False, reason=f"worker crash: {type(e).__name__}: {e}")
        self.app.call_from_thread(lambda: self._on_done(result))

    def _delta(self, chunk: str) -> None:
        self.app.call_from_thread(lambda: self._on_delta(chunk))

    def _tool(self, name, res) -> None:
        self.app.call_from_thread(lambda: self._on_tool(name, res))

    def _ask(self, prompt: str) -> str:
        ev = threading.Event()
        holder = {"answer": ""}

        def responder(answer: str) -> None:
            holder["answer"] = answer
            ev.set()

        if self._on_ask is not None:
            on_ask = self._on_ask
            self.app.call_from_thread(lambda: on_ask(prompt, responder))
            ev.wait(timeout=600)
        return holder["answer"]
