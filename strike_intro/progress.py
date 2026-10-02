"""Atomic progress.json for a study run; the live Streamlit page polls it."""
import datetime
import json
import os


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class RunProgress:
    def __init__(self, run_dir: str):
        self.path = os.path.join(run_dir, "progress.json")
        os.makedirs(run_dir, exist_ok=True)
        self.state = {"stages": {}, "data": {}, "updated_at": None}

    def _flush(self):
        self.state["updated_at"] = _now()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, default=float)
        os.replace(tmp, self.path)

    def stage(self, name: str, done: int, total: int, note: str = ""):
        self.state["stages"][name] = {"done": done, "total": total, "note": note}
        self._flush()

    def put(self, key: str, value):
        self.state["data"][key] = value
        self._flush()


def read_progress(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, "progress.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"stages": {}, "data": {}, "updated_at": None}
