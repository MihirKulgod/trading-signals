import pandas as pd

class Notifier:
    def __init__(self):
        self.notified_today: set[str] = set()

    def notify_onset(self, condition_id: str, score: float, now: pd.Timestamp) -> None:
        if condition_id in self.notified_today:
            return
        self.notified_today.add(condition_id)
        print(f">>> [{now}] {condition_id} onset, score={score:.4f}")

    def reset_daily_state(self) -> None:
        self.notified_today.clear()
