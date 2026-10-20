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
NETWORKS = f'{API_VERSION}/networks'
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
def network_id(client, legacy_input):
    network_id = client.post(NETWORKS, json=legacy_input).json()['network-id']
    yield network_id
    client.delete(f'{NETWORKS}/{network_id}')


def _element_url(network_id, uid):
    return f'{NETWORKS}/{network_id}/elements/{quote(uid, safe="")}'


def test_create_network_from_yang_and_legacy(client, legacy_input):
    yang = read_json_file(TEST_REQ_DIR / 'planning_demand_example.json')
    del yang['gnpy-api:api']['gnpy-path-computation:services']

    for body in (yang, legacy_input):
        response = client.post(NETWORKS, json=body)
        assert response.status_code == 201
        summary = response.json()
        assert response.headers['location'] == f'{NETWORKS}/{summary["network-id"]}'
        assert summary['element-types'] == {'Transceiver': 5, 'Roadm': 5, 'Fiber': 10, 'Edfa': 18}
        assert client.get(f'{NETWORKS}/{summary["network-id"]}').json() == summary
        assert summary in client.get(NETWORKS).json()['networks']
        assert client.delete(f'{NETWORKS}/{summary["network-id"]}').status_code == 204


def test_create_network_requires_topology_and_equipment(client, legacy_input):
    assert client.post(NETWORKS, json={'gnpy-api:equipment': legacy_input['gnpy-api:equipment']}).status_code == 400
    assert client.post(NETWORKS, json={'gnpy-api:topology': legacy_input['gnpy-api:topology']}).status_code == 400


def test_unknown_network_and_element(client, network_id):
    assert client.get(f'{NETWORKS}/nope').status_code == 404
    assert client.delete(f'{NETWORKS}/nope').status_code == 404
    assert client.post(f'{NETWORKS}/nope/redesign').status_code == 404
    assert client.get(_element_url(network_id, 'nope')).status_code == 404


def test_list_and_describe_elements(client, network_id):
    fibers = client.get(f'{NETWORKS}/{network_id}/elements', params={'type': 'Fiber'}).json()['elements']
    assert len(fibers) == 10
    assert all(fiber['type'] == 'Fiber' and fiber['failed'] is False for fiber in fibers)

    # uids with spaces, arrows and slashes survive the round trip through the URL
    fiber = client.get(_element_url(network_id, 'fiber (Alice → Bob)-')).json()
    assert fiber == {'uid': 'fiber (Alice → Bob)-', 'type': 'Fiber', 'type-variety': 'SSMF',
                     'attributes': {'loss_coef': 0.2, 'att_in': 0.0}, 'length-km': 75.0, 'failed': False}


def test_update_elements_is_atomic(client, network_id):
    url = f'{NETWORKS}/{network_id}/elements'

    rejected = client.patch(url, json={'fiber (Alice → Bob)-': {'loss_coef': 0.3}, 'roadm Bob': {'failed': True}})
    assert rejected.status_code == 400
    assert client.get(_element_url(network_id, 'fiber (Alice → Bob)-')).json()['attributes']['loss_coef'] == 0.2

    accepted = client.patch(url, json={'fiber (Alice → Bob)-': {'loss_coef': 0.3, 'failed': True},
                                       'roadm Bob': {'target_pch_out_db': -18}})
    assert accepted.status_code == 200
    assert [el['uid'] for el in accepted.json()['elements']] == ['fiber (Alice → Bob)-', 'roadm Bob']
    assert client.get(f'{NETWORKS}/{network_id}').json()['failed-fibers'] == ['fiber (Alice → Bob)-']


@pytest.mark.parametrize('attributes', [
    {'loss_coef': -0.1}, {'loss_coef': True}, {'loss_coef': '0.2'}, {'length': 10.0}, {'failed': 'yes'},
])
def test_update_element_rejects_invalid_values(client, network_id, attributes):
    response = client.patch(_element_url(network_id, 'fiber (Alice → Bob)-'), json=attributes)
    assert response.status_code == 400


def test_paths_avoid_failed_fibers(client, network_id):
    url = f'{NETWORKS}/{network_id}/paths'
    request = {'source': 'trx Alice', 'destination': 'trx Bob', 'k': 5}

    paths = client.post(url, json=request).json()['paths']
    assert [path['uids'] for path in paths][0] == DIRECT_PATH
    assert [path['total-fiber-km'] for path in paths] == sorted(path['total-fiber-km'] for path in paths)

    client.patch(_element_url(network_id, 'fiber (Alice → Dan)-'), json={'failed': True})
    client.patch(_element_url(network_id, 'fiber (Alice → Bob)-'), json={'failed': True})
    blocked = client.post(url, json=request).json()
    assert blocked['paths'] == [] and blocked['blocking-reason'] == 'NO_PATH'


def test_paths_need_transceiver_endpoints(client, network_id):
    response = client.post(f'{NETWORKS}/{network_id}/paths', json={'source': 'roadm Alice', 'destination': 'trx Bob'})
    assert response.status_code == 400


def test_propagate_reports_worst_channel_and_path(client, network_id):
    response = client.post(f'{NETWORKS}/{network_id}/propagate', json={'path': DIRECT_PATH, 'include-channels': True})
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


def test_propagate_spectrum_overrides_equipment_si(client, network_id):
    url = f'{NETWORKS}/{network_id}/propagate'
    default = client.post(url, json={'path': DIRECT_PATH}).json()
    noisy_tx = client.post(url, json={'path': DIRECT_PATH, 'spectrum': {'tx-osnr': 20.0}}).json()
    narrow = client.post(url, json={'path': DIRECT_PATH, 'spectrum': {'f-min': 193.0e12, 'f-max': 193.2e12}}).json()

    assert 'channels' not in default
    # transmitter OSNR is a receiver penalty, as in GNPy's planning: the line GSNR is untouched
    assert noisy_tx['worst-channel']['gsnr-db'] == default['worst-channel']['gsnr-db']
    # the equipment SI of this example sets tx_osnr to 40 dB
    assert noisy_tx['worst-channel']['receiver-gsnr-db'] < 20.0 < default['worst-channel']['receiver-gsnr-db']
    assert narrow['channel-count'] == 4


def test_receiver_snr_matches_path_request(client, network_id):
    planning = read_json_file(TEST_REQ_DIR / 'legacy' / 'planning_demand_example.json')
    metrics = read_json_file(TEST_RES_DIR / 'planning_demand_res.json')['response'][0]['path-properties']['path-metric']
    expected = {metric['metric-type']: metric['accumulative-value'] for metric in metrics}
    # request 0 uses Voyager mode 1, whose frequency range sets the comb GNPy propagates
    voyager = next(trx for trx in planning['gnpy-api:equipment']['Transceiver'] if trx['type_variety'] == 'Voyager')
    spectrum = {'f-min': voyager['frequency']['min'], 'f-max': voyager['frequency']['max']}

    result = client.post(f'{NETWORKS}/{network_id}/propagate',
                         json={'path': DIRECT_PATH, 'spectrum': spectrum, 'include-channels': True}).json()

    assert round(mean(ch['receiver-gsnr-db'] for ch in result['channels']), 2) == expected['SNR-bandwidth']
    assert round(mean(ch['receiver-osnr-ase-db'] for ch in result['channels']), 2) == expected['OSNR-bandwidth']


def test_propagate_over_a_failed_fiber(client, network_id):
    client.patch(_element_url(network_id, 'fiber (Alice → Bob)-'), json={'failed': True})
    result = client.post(f'{NETWORKS}/{network_id}/propagate', json={'path': DIRECT_PATH}).json()

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
def test_propagate_rejects_invalid_requests(client, network_id, body, status_code):
    assert client.post(f'{NETWORKS}/{network_id}/propagate', json=body).status_code == status_code


def test_redesign_keeps_failures(client, network_id):
    client.patch(_element_url(network_id, 'fiber (Alice → Bob)-'), json={'failed': True})
    summary = client.post(f'{NETWORKS}/{network_id}/redesign').json()

    assert summary['failed-fibers'] == ['fiber (Alice → Bob)-']


def test_designed_topology(client, network_id):
    topology = client.get(f'{NETWORKS}/{network_id}/topology').json()

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


def test_create_network_openapi_example(client):
    body = client.get('/openapi.json').json()['paths'][NETWORKS]['post']['requestBody']['content']['application/json']
    example = body['examples']['network']['value']['gnpy-api:api']

    assert set(example) == {'gnpy-network-topology:topology', 'gnpy-eqpt-config:equipment'}
