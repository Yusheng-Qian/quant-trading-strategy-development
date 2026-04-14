"""Strategy-only test build uses a no-op AI parameter model."""

PARAM_BOUNDS = {
    "grid_N_mult": (0.8, 1.2),
    "spacing_mult_mult": (0.8, 1.2),
    "flow_bias": (-1.0, 1.0),
}


def clamp(value, lower, upper):
    return max(lower, min(upper, value))


class AIParamModel:
    def __init__(self):
        self.weight = 0.0

    def predict(self, features):
        return None

    def record_outcome(self, *args, **kwargs):
        return None

