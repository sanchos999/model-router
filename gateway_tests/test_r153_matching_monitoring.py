import pytest
from gateway.control.admin_api import _match_norm

def test_matching_normalization_keeps_versions_and_removes_prefix():
    assert _match_norm('CB/the model 5') == _match_norm('the model 5')
    assert _match_norm('the model 4.7') != _match_norm('the model 5')

def test_matching_routes_require_existing_canonical():
    assert _match_norm('Cluade Opus 5') != _match_norm('the model 5')
