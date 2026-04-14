"""Strategy-only test build disables local Qwen inference."""


class LocalQwenAdapter:
    def __init__(self):
        self.is_ready = False

    def suggest(self, features, params):
        return None

