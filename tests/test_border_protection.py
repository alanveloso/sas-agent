"""Unit tests for injectable generic border protection."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from domain.band_profile import ProtectionFlags, cbrs_winnforum_profile
from services.border_protection import (
    BorderPoint,
    HarnessBorderGeoProvider,
    PropagationResult,
    violates_border_pfd,
    violates_canadian_border_pfd,
)


class FakeBorderProvider:
    def __init__(self) -> None:
        self.border: BorderPoint | None = BorderPoint(49.0, -122.0)
        self.loss_db = 120.0
        self.bearing_deg = 20.0
        self.gain_db = 0.0
        self.fail_location = False
        self.fail_propagation = False
        self.calls = 0

    def closest_border_point(self, *args):
        self.calls += 1
        if self.fail_location:
            raise RuntimeError("geometry unavailable")
        return self.border

    def propagation(self, *args, **kwargs):
        if self.fail_propagation:
            raise RuntimeError("terrain unavailable")
        return PropagationResult(self.loss_db, self.bearing_deg)

    def antenna_gain(self, *args):
        return self.gain_db


def installation(**overrides):
    value = {
        "latitude": 49.0,
        "longitude": -122.0,
        "height": 10.0,
        "heightType": "AGL",
        "indoorDeployment": False,
        "antennaAzimuth": 0,
        "antennaBeamwidth": 60,
        "antennaGain": 5,
    }
    value.update(overrides)
    return value


def disabled_profile():
    profile = cbrs_winnforum_profile()
    return replace(
        profile,
        protection=replace(profile.protection, border_protection=False),
        border_policy=None,
    )


def test_disabled_profile_bypasses_provider() -> None:
    provider = FakeBorderProvider()
    assert not violates_border_pfd(
        installation(),
        20,
        3_650_000_000,
        3_660_000_000,
        profile=disabled_profile(),
        provider=provider,
    )
    assert provider.calls == 0


def test_non_overlapping_frequency_and_invalid_location_bypass() -> None:
    provider = FakeBorderProvider()
    profile = cbrs_winnforum_profile()
    assert not violates_border_pfd(
        installation(),
        20,
        3_550_000_000,
        3_560_000_000,
        profile=profile,
        provider=provider,
    )
    assert not violates_border_pfd(
        {"latitude": "invalid"},
        20,
        3_650_000_000,
        3_660_000_000,
        profile=profile,
        provider=provider,
    )


def test_missing_provider_or_border_point_bypasses() -> None:
    profile = cbrs_winnforum_profile()
    with patch("services.border_protection._default_provider", return_value=None):
        assert not violates_border_pfd(
            installation(), 20, 3_650_000_000, 3_660_000_000, profile=profile
        )
    provider = FakeBorderProvider()
    provider.border = None
    assert not violates_border_pfd(
        installation(),
        20,
        3_650_000_000,
        3_660_000_000,
        profile=profile,
        provider=provider,
    )


def test_geometry_failure_bypasses_but_propagation_failure_fails_closed() -> None:
    profile = cbrs_winnforum_profile()
    provider = FakeBorderProvider()
    provider.fail_location = True
    assert not violates_border_pfd(
        installation(),
        20,
        3_650_000_000,
        3_660_000_000,
        profile=profile,
        provider=provider,
    )
    provider.fail_location = False
    provider.fail_propagation = True
    assert violates_border_pfd(
        installation(),
        20,
        3_650_000_000,
        3_660_000_000,
        profile=profile,
        provider=provider,
    )


@pytest.mark.parametrize(
    ("loss_db", "expected"),
    [(80.0, True), (160.0, False)],
)
def test_provider_driven_pfd_decision(loss_db: float, expected: bool) -> None:
    provider = FakeBorderProvider()
    provider.loss_db = loss_db
    assert (
        violates_border_pfd(
            installation(antennaGain="invalid", heightType="AMSL"),
            20,
            3_650_000_000,
            3_660_000_000,
            profile=cbrs_winnforum_profile(),
            provider=provider,
        )
        is expected
    )


def test_legacy_alias_delegates_to_generic_function() -> None:
    provider = FakeBorderProvider()
    provider.loss_db = 160
    assert not violates_canadian_border_pfd(
        installation(),
        20,
        3_650_000_000,
        3_660_000_000,
        profile=cbrs_winnforum_profile(),
        provider=provider,
    )


def test_harness_adapter_translates_external_models() -> None:
    adapter = object.__new__(HarnessBorderGeoProvider)
    adapter._utils = SimpleNamespace(
        CheckCbsdInBorderSharingZone=lambda *args: (True, 49.0, -122.0)
    )
    propagation = SimpleNamespace(
        db_loss=100.0,
        incidence_angles=SimpleNamespace(hor_cbsd=45.0),
    )
    adapter._propagation = SimpleNamespace(
        CalcItmPropagationLoss=lambda *args, **kwargs: propagation
    )
    adapter._antenna = SimpleNamespace(
        GetStandardAntennaGains=lambda *args: 3.0
    )

    border = adapter.closest_border_point(48.0, -123.0, 0, 60)
    assert border == BorderPoint(49.0, -122.0)
    result = adapter.propagation(
        48.0,
        -123.0,
        10,
        border,
        cbrs_winnforum_profile().border_policy,
        indoor=False,
        height_is_amsl=False,
    )
    assert result == PropagationResult(100.0, 45.0)
    assert adapter.antenna_gain(45, 0, 60, 5) == 3.0

    adapter._utils.CheckCbsdInBorderSharingZone = lambda *args: (
        False,
        None,
        None,
    )
    assert adapter.closest_border_point(48.0, -123.0, 0, 60) is None


def test_default_provider_handles_missing_reference_models() -> None:
    with patch(
        "services.border_protection.HarnessBorderGeoProvider",
        side_effect=ImportError,
    ):
        from services.border_protection import _default_provider

        assert _default_provider() is None
