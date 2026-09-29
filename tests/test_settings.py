from __future__ import annotations

import pytest

from BatSpectroGen import settings_for_profile


def test_low_memory_profile_is_conservative() -> None:
    settings = settings_for_profile("low-memory")
    assert settings.workers == 1
    assert settings.image_width == 1600
    assert settings.generate_power_spectrum is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"segment_duration": 0},
        {"workers": 0},
        {"fmin_khz": 50, "fmax_khz": 20},
        {"n_fft": 1000},
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        settings_for_profile("balanced", **overrides)
