from collections import deque

import pandas as pd

from condition import build_condition, build_definitions, MarketContext
from data_processing import generate_base_window
from notifications import Notifier

class LiveEvaluator:
    def __init__(self, config: dict, instruments_data: list[dict], window_days: int,
                 notifier: Notifier, history_length: int = 2):
        self.config = config
        self.instruments_data = instruments_data
        self.window_days = window_days
        self.notifier = notifier
        self.definitions = build_definitions(config)
        built = [build_condition(c, self.definitions) for c in config["conditions"]]
        self.conditions = list({id(c): c for c in built}.values())
        self.score_history: dict[str, deque] = {
            c.id: deque(maxlen=history_length) for c in self.conditions
        }

    def run_once(self, now: pd.Timestamp) -> dict[str, float]:
        print("> Running evaluator..")
        generate_base_window(self.config, self.instruments_data, self.window_days)

        ctx = MarketContext(self.instruments_data, now)
        scores = {}
        for condition in self.conditions:
            score = condition(ctx)

            print(f"> {condition.id} : {score}")

            scores[condition.id] = score

            history = self.score_history[condition.id]
            previous = history[-1] if history else None
            history.append(score)

            if previous is not None and previous < 0 and score >= 0:
                self.notifier.notify_onset(condition.id, score, now)

        return scores
