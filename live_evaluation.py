import pandas as pd

from condition import build_selected_conditions, MarketContext
from data_processing import generate_base_window

class LiveEvaluator:
    def __init__(self, config: dict, instruments_data: list[dict], window_days: int):
        self.config = config
        self.instruments_data = instruments_data
        self.window_days = window_days
        self.conditions = build_selected_conditions(config)
        # node id -> direct child ids, for notification rules that watch a
        # node's children (e.g. "5/6 of X's subconditions met"). Built once
        # since the condition objects themselves never change after bootstrap.
        self.children: dict[str, list[str]] = {}
        for root in self.conditions:
            for node in root.walk():
                self.children[node.id] = [c.id for c in node.sub_conditions()]
        # Every node's score, not just the top-level ones, so a dashboard can
        # show what a combination's children are doing. Replaced wholesale at
        # the end of a cycle so a reader never sees it half filled.
        self.node_scores: dict[str, float] = {}

    def run_once(self, now: pd.Timestamp, live_candles: dict | None = None) -> dict[str, float]:
        print("> Running evaluator..")
        generate_base_window(self.config, self.instruments_data, self.window_days, live_candles)

        ctx = MarketContext(self.instruments_data, now)
        scores = {}
        for condition in self.conditions:
            score = condition(ctx)
            print(f"> {condition.id} : {score}")
            scores[condition.id] = score

        self.node_scores = dict(ctx.trace)
        return scores
