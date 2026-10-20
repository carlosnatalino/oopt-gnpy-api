#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

import pytest

from gnpyapi.core.exception.param_validation_error import ParamValidationError
from gnpyapi.core.service.network_service import NetworkSession
from tests.utils.input import read_json_file

TEST_REQ_DIR_LEGACY = Path(__file__).parent.parent / 'data' / 'req' / 'legacy'


@pytest.fixture
def session():
    data = read_json_file(TEST_REQ_DIR_LEGACY / 'planning_demand_example.json')
    return NetworkSession.from_json(data['gnpy-api:topology'], data['gnpy-api:equipment'])


@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_non_finite_values_are_rejected(session, value):
    with pytest.raises(ParamValidationError):
        session.update_elements({'fiber (Alice → Bob)-': {'loss_coef': value}})


def test_redesign_without_changes_is_idempotent(session):
    path = session.compute_paths('trx Alice', 'trx Bob')['paths'][0]['uids']
    before = session.propagate(path, include_channels=True)

    session.redesign()

    assert session.propagate(path, include_channels=True) == before
