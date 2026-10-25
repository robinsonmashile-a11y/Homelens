"""Deal badge: gap % sets the tier; 'excellent_deal' additionally needs solid evidence."""
from __future__ import annotations

import pytest

from scraper.engine import SIZE_MAX, _deal_badge


def badge(gap, conf='high', method='price_per_m2', size=80.0, erf=None):
    return _deal_badge(gap, conf, method, size, erf)


@pytest.mark.parametrize('gap,expected', [
    (-60, 'excellent_deal'), (-40, 'excellent_deal'),          # >= 40% below, solid evidence
    (-39.9, 'good_deal'), (-10, 'good_deal'),
    (-9.9, 'fair'), (0, 'fair'), (9.9, 'fair'),
    (10, 'above_market'), (24.9, 'above_market'), (25, 'overpriced'),
])
def test_tiers_by_gap(gap, expected):
    assert badge(gap) == expected


def test_no_gap_no_badge():
    assert _deal_badge(None) is None


@pytest.mark.parametrize('conf', ['low', 'low_blended', 'very_low', None])
def test_thin_evidence_never_earns_excellent(conf):
    assert badge(-70, conf=conf) == 'good_deal'


@pytest.mark.parametrize('conf', ['medium', 'medium-high', 'high'])
def test_medium_or_better_confidence_can_be_excellent(conf):
    assert badge(-70, conf=conf) == 'excellent_deal'


def test_implausible_floor_size_blocks_excellent_on_price_per_m2():
    assert badge(-70, size=SIZE_MAX + 1) == 'good_deal'          # a plot, not a floor
    assert badge(-70, size=1204.0) == 'good_deal'
    assert badge(-70, size=300.0, erf=300.0) == 'good_deal'      # floor == plot size
    assert badge(-70, size=None) == 'good_deal'
    assert badge(-70, size=120.0, erf=600.0) == 'excellent_deal'  # a real house on a bigger plot


def test_size_is_irrelevant_for_price_only_comparisons():
    assert badge(-70, method='price_only', size=None) == 'excellent_deal'
    assert badge(-70, method='price_only', size=2000.0) == 'excellent_deal'


def test_the_gap_alone_still_decides_non_excellent_tiers():
    assert badge(-25, conf='low', size=5000.0) == 'good_deal'    # evidence rules only touch Excellent
    assert badge(5, conf='low') == 'fair'
