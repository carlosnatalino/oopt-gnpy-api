#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path
from statistics import mean
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from gnpyapi.core import API_VERSION, app
from tests.utils.input import read_json_file

TEST_REQ_DIR = Path(__file__).parent / 'data' / 'req'
TEST_RES_DIR = Path(__file__).parent / 'data' / 'res'
SESSIONS = f'{API_VERSION}/sessions'
SESSION = f'{API_VERSION}/session'
TOKEN_HEADER = 'X-Session-Token'
DIRECT_PATH = ['trx Alice', 'roadm Alice', 'east edfa in Alice to Bob', 'fiber (Alice → Bob)-',
               'west edfa in Bob to Alice', 'roadm Bob', 'trx Bob']


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def legacy_input():
    data = read_json_file(TEST_REQ_DIR / 'legacy' / 'planning_demand_example.json')
    return {'gnpy-api:topology': data['gnpy-api:topology'], 'gnpy-api:equipment': data['gnpy-api:equipment']}


@pytest.fixture
def session(client, legacy_input):
    """A client that sends the token of a freshly opened session with every request."""
    client.headers[TOKEN_HEADER] = client.post(SESSIONS, json=legacy_input).json()['session-token']
    yield client
    client.delete(SESSION)


def _element_url(uid):
    return f'{SESSION}/elements/{quote(uid, safe="")}'


def test_open_session_from_yang_and_legacy(client, legacy_input):
    yang = read_json_file(TEST_REQ_DIR / 'planning_demand_example.json')
    del yang['gnpy-api:api']['gnpy-path-computation:services']

    for body in (yang, legacy_input):
        response = client.post(SESSIONS, json=body)
        assert response.status_code == 201
        summary = response.json()
        token = summary.pop('session-token')
        assert summary['element-types'] == {'Transceiver': 5, 'Roadm': 5, 'Fiber': 10, 'Edfa': 18}
        assert client.get(SESSION, headers={TOKEN_HEADER: token}).json() == summary
        assert client.delete(SESSION, headers={TOKEN_HEADER: token}).status_code == 204


def test_open_session_requires_topology_and_equipment(client, legacy_input):
    assert client.post(SESSIONS, json={'gnpy-api:equipment': legacy_input['gnpy-api:equipment']}).status_code == 400
    assert client.post(SESSIONS, json={'gnpy-api:topology': legacy_input['gnpy-api:topology']}).status_code == 400


def test_sessions_are_independent(client, legacy_input):
    first = {TOKEN_HEADER: client.post(SESSIONS, json=legacy_input).json()['session-token']}
    second = {TOKEN_HEADER: client.post(SESSIONS, json=legacy_input).json()['session-token']}

    client.patch(_element_url('fiber (Alice → Bob)-'), headers=first, json={'loss_coef': 0.3})

    assert client.get(_element_url('fiber (Alice → Bob)-'), headers=first).json()['attributes']['loss_coef'] == 0.3
    assert client.get(_element_url('fiber (Alice → Bob)-'), headers=second).json()['attributes']['loss_coef'] == 0.2
    for headers in (first, second):
        client.delete(SESSION, headers=headers)


@pytest.mark.parametrize('headers', [{}, {TOKEN_HEADER: ''}, {TOKEN_HEADER: 'not-a-token'}])
def test_calls_need_a_valid_token(client, headers):
    for method, url in [('GET', SESSION), ('DELETE', SESSION), ('POST', f'{SESSION}/redesign'),
                        ('GET', f'{SESSION}/elements'), ('GET', _element_url('trx Alice'))]:
        response = client.request(method, url, headers=headers)
        assert response.status_code == 401
        assert response.headers['www-authenticate'] == TOKEN_HEADER


def test_closed_session_rejects_its_token(session):
    assert session.delete(SESSION).status_code == 204

    assert session.get(SESSION).status_code == 401
    assert session.delete(SESSION).status_code == 401


def test_tokens_are_never_listed(session):
    token = session.headers[TOKEN_HEADER]

    assert session.get(SESSIONS).status_code == 405
    assert token not in session.get(SESSION).text
    assert token not in session.get(f'{SESSION}/elements').text
    assert token not in session.get(SESSION, headers={TOKEN_HEADER: token[:-1]}).text


def test_unknown_element(session):
    assert session.get(_element_url('nope')).status_code == 404
    assert session.patch(_element_url('nope'), json={'loss_coef': 0.3}).status_code == 404


def test_list_and_describe_elements(session):
    fibers = session.get(f'{SESSION}/elements', params={'type': 'Fiber'}).json()['elements']
    assert len(fibers) == 10
    assert all(fiber['type'] == 'Fiber' and fiber['failed'] is False for fiber in fibers)

    # uids with spaces, arrows and slashes survive the round trip through the URL
    fiber = session.get(_element_url('fiber (Alice → Bob)-')).json()
    assert fiber == {'uid': 'fiber (Alice → Bob)-', 'type': 'Fiber', 'type-variety': 'SSMF',
                     'attributes': {'loss_coef': 0.2, 'att_in': 0.0}, 'length-km': 75.0, 'failed': False}


def test_update_elements_is_atomic(session):
    url = f'{SESSION}/elements'

    rejected = session.patch(url, json={'fiber (Alice → Bob)-': {'loss_coef': 0.3}, 'roadm Bob': {'failed': True}})
    assert rejected.status_code == 400
    assert session.get(_element_url('fiber (Alice → Bob)-')).json()['attributes']['loss_coef'] == 0.2

    accepted = session.patch(url, json={'fiber (Alice → Bob)-': {'loss_coef': 0.3, 'failed': True},
                                        'roadm Bob': {'target_pch_out_db': -18}})
    assert accepted.status_code == 200
    assert [el['uid'] for el in accepted.json()['elements']] == ['fiber (Alice → Bob)-', 'roadm Bob']
    assert session.get(SESSION).json()['failed-fibers'] == ['fiber (Alice → Bob)-']


@pytest.mark.parametrize('attributes', [
    {'loss_coef': -0.1}, {'loss_coef': True}, {'loss_coef': '0.2'}, {'length': 10.0}, {'failed': 'yes'},
])
def test_update_element_rejects_invalid_values(session, attributes):
    response = session.patch(_element_url('fiber (Alice → Bob)-'), json=attributes)
    assert response.status_code == 400


def test_paths_avoid_failed_fibers(session):
    url = f'{SESSION}/paths'
    request = {'source': 'trx Alice', 'destination': 'trx Bob', 'k': 5}

    paths = session.post(url, json=request).json()['paths']
    assert [path['uids'] for path in paths][0] == DIRECT_PATH
    assert [path['total-fiber-km'] for path in paths] == sorted(path['total-fiber-km'] for path in paths)

    session.patch(_element_url('fiber (Alice → Dan)-'), json={'failed': True})
    session.patch(_element_url('fiber (Alice → Bob)-'), json={'failed': True})
    blocked = session.post(url, json=request).json()
    assert blocked['paths'] == [] and blocked['blocking-reason'] == 'NO_PATH'


def test_paths_need_transceiver_endpoints(session):
    response = session.post(f'{SESSION}/paths', json={'source': 'roadm Alice', 'destination': 'trx Bob'})
    assert response.status_code == 400


def test_propagate_reports_worst_channel_and_path(session):
    response = session.post(f'{SESSION}/propagate', json={'path': DIRECT_PATH, 'include-channels': True})
    result = response.json()

    assert response.status_code == 200
    assert result['status'] == 'ok' and result['failed-elements'] == []
    assert [el['uid'] for el in result['elements']] == DIRECT_PATH
    assert result['total-fiber-km'] == 75.0
    assert len(result['channels']) == result['channel-count']
    assert result['worst-channel'] == min(result['channels'], key=lambda channel: channel['gsnr-db'])
    worst = result['worst-channel']
    assert worst['gsnr-db'] <= worst['osnr-ase-db']
    assert worst['receiver-gsnr-db'] < worst['gsnr-db']
    assert worst['osnr-ase-01nm-db'] == pytest.approx(worst['osnr-ase-db'] + 10 * 0.4082399653)
    assert worst['chromatic-dispersion-ps-nm'] == pytest.approx(75 * 16.7, rel=1e-3)


def test_propagate_spectrum_overrides_equipment_si(session):
    url = f'{SESSION}/propagate'
    default = session.post(url, json={'path': DIRECT_PATH}).json()
    noisy_tx = session.post(url, json={'path': DIRECT_PATH, 'spectrum': {'tx-osnr': 20.0}}).json()
    narrow = session.post(url, json={'path': DIRECT_PATH, 'spectrum': {'f-min': 193.0e12, 'f-max': 193.2e12}}).json()

    assert 'channels' not in default
    # transmitter OSNR is a receiver penalty, as in GNPy's planning: the line GSNR is untouched
    assert noisy_tx['worst-channel']['gsnr-db'] == default['worst-channel']['gsnr-db']
    # the equipment SI of this example sets tx_osnr to 40 dB
    assert noisy_tx['worst-channel']['receiver-gsnr-db'] < 20.0 < default['worst-channel']['receiver-gsnr-db']
    assert narrow['channel-count'] == 4


def test_receiver_snr_matches_path_request(session):
    planning = read_json_file(TEST_REQ_DIR / 'legacy' / 'planning_demand_example.json')
    metrics = read_json_file(TEST_RES_DIR / 'planning_demand_res.json')['response'][0]['path-properties']['path-metric']
    expected = {metric['metric-type']: metric['accumulative-value'] for metric in metrics}
    # request 0 uses Voyager mode 1, whose frequency range sets the comb GNPy propagates
    voyager = next(trx for trx in planning['gnpy-api:equipment']['Transceiver'] if trx['type_variety'] == 'Voyager')
    spectrum = {'f-min': voyager['frequency']['min'], 'f-max': voyager['frequency']['max']}

    result = session.post(f'{SESSION}/propagate',
                          json={'path': DIRECT_PATH, 'spectrum': spectrum, 'include-channels': True}).json()

    assert round(mean(ch['receiver-gsnr-db'] for ch in result['channels']), 2) == expected['SNR-bandwidth']
    assert round(mean(ch['receiver-osnr-ase-db'] for ch in result['channels']), 2) == expected['OSNR-bandwidth']


def test_propagate_over_a_failed_fiber(session):
    session.patch(_element_url('fiber (Alice → Bob)-'), json={'failed': True})
    result = session.post(f'{SESSION}/propagate', json={'path': DIRECT_PATH}).json()

    assert result['status'] == 'link-failed'
    assert result['failed-elements'] == ['fiber (Alice → Bob)-']
    assert 'worst-channel' not in result


@pytest.mark.parametrize('body, status_code', [
    ({'path': ['trx Alice', 'nope']}, 400),
    ({'path': ['trx Alice', 'roadm Bob']}, 400),
    ({'path': ['trx Alice']}, 422),
    ({'path': DIRECT_PATH, 'spectrum': {'f-min': 193.1e12, 'f-max': 193.1e12}}, 400),
    ({'path': DIRECT_PATH, 'spectrum': {'bogus': 1}}, 422),
])
def test_propagate_rejects_invalid_requests(session, body, status_code):
    assert session.post(f'{SESSION}/propagate', json=body).status_code == status_code


def test_redesign_keeps_failures(session):
    session.patch(_element_url('fiber (Alice → Bob)-'), json={'failed': True})
    summary = session.post(f'{SESSION}/redesign').json()

    assert summary['failed-fibers'] == ['fiber (Alice → Bob)-']


def test_designed_topology(session):
    topology = session.get(f'{SESSION}/topology').json()

    assert len(topology['elements']) == 38
    assert {'from_node': 'fiber (Alice → Bob)-', 'to_node': 'west edfa in Bob to Alice'} in topology['connections']


def test_element_attributes_schema(client):
    schema = client.get(f'{API_VERSION}/element-attributes').json()

    assert schema['Fiber']['loss_coef'] == {'type': 'float', 'unit': 'dB/km', 'min': 0.0, 'max': 2.0}
    assert set(schema) == {'Fiber', 'Edfa', 'Roadm'}


def test_example_data(client):
    assert 'eqpt_config.json' in client.get(f'{API_VERSION}/example-data').json()['files']
    assert 'Edfa' in client.get(f'{API_VERSION}/example-data/eqpt_config.json').json()
    assert client.get(f'{API_VERSION}/example-data/..%2Fsetup.py').status_code == 404
    assert client.get(f'{API_VERSION}/example-data/CORONET_Global_Topology.xls').status_code == 404


def test_openapi_documents_sessions(client):
    openapi = client.get('/openapi.json').json()
    body = openapi['paths'][SESSIONS]['post']['requestBody']['content']['application/json']
    example = body['examples']['network']['value']['gnpy-api:api']

    assert set(example) == {'gnpy-network-topology:topology', 'gnpy-eqpt-config:equipment'}
    assert openapi['components']['securitySchemes']['SessionToken'] == {
        'type': 'apiKey', 'in': 'header', 'name': TOKEN_HEADER,
        'description': f'Token returned by POST {SESSIONS}'}
    assert openapi['paths'][f'{SESSION}/propagate']['post']['security'] == [{'SessionToken': []}]
