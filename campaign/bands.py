"""Score -> band. The cutoffs are settings.BAND_CUTOFFS."""
from . import settings


def band_for(score):
    """'A'..'D' for a score from 0 to SKIP_ABOVE_SCORE, else None (too high, or no score)."""
    if score is None or score < 0 or score > settings.SKIP_ABOVE_SCORE:
        return None
    for band, (low, high) in settings.BAND_CUTOFFS.items():
        if low <= score <= high:
            return band
    return None
