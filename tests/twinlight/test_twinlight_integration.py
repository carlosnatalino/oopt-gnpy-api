#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""TwinLight's GNPy workload, driven through gnpy-api by the mock ``RemoteGnpyBackend``.

The reference for "what TwinLight does today" is ``_in_process_*`` below: a copy of the
code TwinLight runs in its own process (``physics/gnpy_adapter.py`` and
``TapiContext._get_path_via_gnpy``). Parity tests require the remote backend to return
exactly the same numbers, so the twin can drop its gnpy dependency without changing any
baseline it reports.
"""

import asyncio
import copy
import json
from pathlib import Path

import gnpy
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from gnpy.core.elements import Fiber, Roadm
from gnpy.core.info import create_input_spectral_information
from gnpy.tools.json_io import load_equipment, network_from_json
from gnpy.tools.worker_utils import designed_network
from gnpy.topology.request import PathRequest, compute_constrained_path

from gnpyapi.core import app
from tests.twinlight.remote_gnpy_backend import (MODULATION_TABLE, OpmBaseline, ParamValidationError,
                                                 RemoteGnpyBackend, fetch_example, list_examples)
from tests.utils.input import read_json_file

GNPY_EXAMPLE_DIR = Path(gnpy.__file__).parent / 'example-data'
MESH_EXAMPLE = Path(__file__).parent.parent / 'data' / 'req' / 'legacy' / 'planning_demand_example.json'
# The files twinlight-fetch-examples provisions for the bundled scenarios.
TWINLIGHT_EXAMPLES = ['CORONET_CONUS_Topology.json', 'edfa_example_network.json', 'eqpt_config.json']


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope='module')
def examples(client):
    return {name: fetch_example(client, name) for name in TWINLIGHT_EXAMPLES}


@pytest.fixture
def edfa_backend(client, examples):
    backend = RemoteGnpyBackend(client, examples['edfa_example_network.json'], examples['eqpt_config.json'])
    yield backend
    backend.close()


@pytest.fixture(scope='module')
def coronet_backend(client, examples):
    backend = RemoteGnpyBackend(client, examples['CORONET_CONUS_Topology.json'], examples['eqpt_config.json'])
    yield backend
    backend.close()


@pytest.fixture
def mesh_backend(client):
    data = read_json_file(MESH_EXAMPLE)
    backend = RemoteGnpyBackend(client, data['gnpy-api:topology'], data['gnpy-api:equipment'])
    yield backend
    backend.close()


def _in_process_network(topology_name):
    """TwinLight's ``gnpy_adapter.build_gnpy_network``."""
    equipment = load_equipment(str(GNPY_EXAMPLE_DIR / 'eqpt_config.json'))
    network = network_from_json(json.loads((GNPY_EXAMPLE_DIR / topology_name).read_text()), equipment)
    network, _, _ = designed_network(equipment, network)
    return network


def _in_process_path(network, src_uid, dst_uid):
    """TwinLight's ``TapiContext._get_path_via_gnpy``."""
    req = PathRequest(source=src_uid, destination=dst_uid, nodes_list=[dst_uid], loose_list=['STRICT'])
    return [el.uid for el in compute_constrained_path(network, req)]


def _in_process_baseline(network, path_uids, modulation_format):
    """TwinLight's ``gnpy_adapter.compute_path_baseline``."""
    uid_map = {el.uid: el for el in network}
    baud_rate, tx_power_dbm = MODULATION_TABLE[modulation_format]
    si = create_input_spectral_information(f_min=193.1e12 - 44 * 50e9, f_max=193.1e12 + 44 * 50e9, roll_off=0.15,
                                           baud_rate=baud_rate, spacing=50e9, tx_osnr=100.0,
                                           tx_power=10 ** (tx_power_dbm / 10.0) * 1e-3, delta_pdb=0.0)
    path_items = [(uid, copy.deepcopy(uid_map[uid])) for uid in path_uids]
    for i, (uid, el) in enumerate(path_items):
        if isinstance(el, Roadm):
            prev_uid = path_items[i - 1][0] if i > 0 else None
            next_uid = path_items[i + 1][0] if i < len(path_items) - 1 else None
            si = el(si, degree=next_uid, from_degree=prev_uid)
        else:
            si = el(si)
    worst = int(np.argmin(si.gsnr_db))
    return OpmBaseline(
        gsnr_db=float(si.gsnr_db[worst]),
        osnr_ase_db=float(si.snr_lin_db[worst]),
        cd_ps_nm=float(si.chromatic_dispersion[worst]) * 1e3,
        pmd_ps=float(np.max(si.pmd) * 1e12),
        latency_ms=float(si.latency[0] * 1e3),
        total_fiber_km=sum(float(el.params.length) / 1000.0 for _, el in path_items if type(el).__name__ == 'Fiber'),
        edfa_uids=[uid for uid, el in path_items if type(el).__name__ == 'Edfa'],
        fiber_uids=[uid for uid, el in path_items if type(el).__name__ == 'Fiber'],
        pdl_elements=[(uid, type(el).__name__) for uid, el in path_items if type(el).__name__ in ('Roadm', 'Edfa')],
    )


def _baseline(backend, path, modulation_format='DP-QPSK'):
    return asyncio.run(backend.compute_baseline(path, modulation_format))


def test_startup_mirrors_the_in_process_uid_map(edfa_backend):
    network = _in_process_network('edfa_example_network.json')

    assert edfa_backend.available
    assert {uid: stub.type for uid, stub in edfa_backend.uid_map.items()} == \
        {el.uid: type(el).__name__ for el in network}
    for el in network:
        if isinstance(el, Fiber):
            assert edfa_backend.uid_map[el.uid].params.length == el.params.length
            assert edfa_backend.is_fiber(el.uid)


@pytest.mark.parametrize('backend_fixture, topology_name, src, dst', [
    ('edfa_backend', 'edfa_example_network.json', 'Site_A', 'Site_B'),
    ('coronet_backend', 'CORONET_CONUS_Topology.json', 'trx Abilene', 'trx Dallas'),
    ('coronet_backend', 'CORONET_CONUS_Topology.json', 'trx Seattle', 'trx Miami'),
    ('coronet_backend', 'CORONET_CONUS_Topology.json', 'trx Boston', 'trx Los_Angeles'),
])
def test_path_and_baseline_match_in_process_gnpy(request, backend_fixture, topology_name, src, dst):
    backend = request.getfixturevalue(backend_fixture)
    network = _in_process_network(topology_name)

    path = backend.compute_path(src, dst)[0]
    assert path == _in_process_path(network, src, dst)
    assert _baseline(backend, path) == _in_process_baseline(network, path, 'DP-QPSK')


def test_parameter_write_matches_in_process_write(edfa_backend):
    network = _in_process_network('edfa_example_network.json')
    path = edfa_backend.compute_path('Site_A', 'Site_B')[0]
    before = _baseline(edfa_backend, path)

    edfa_backend.write_attribute('Span1', 'loss_coef', 0.25)
    # TwinLight's element_params.write_attr: dB/km in, ndarray in dB/m on params._loss_coef
    next(el for el in network if el.uid == 'Span1').params._loss_coef = np.asarray(0.25 * 1e-3)

    after = _baseline(edfa_backend, path)
    assert edfa_backend.read_attribute('Span1', 'loss_coef') == pytest.approx(0.25)
    assert after == _in_process_baseline(network, path, 'DP-QPSK')
    assert after.gsnr_db < before.gsnr_db


def test_config_plane_errors_map_to_twinlight_exceptions(edfa_backend):
    with pytest.raises(ParamValidationError):
        edfa_backend.write_attribute('Span1', 'loss_coef', 5.0)
    with pytest.raises(ParamValidationError):
        edfa_backend.write_attribute('Edfa1', 'loss_coef', 0.2)
    with pytest.raises(ParamValidationError):
        edfa_backend.read_attribute('Span1', 'gain_target')
    with pytest.raises(KeyError):
        edfa_backend.read_all_attributes('no such element')

    assert edfa_backend.read_attribute('Span1', 'loss_coef') == pytest.approx(0.2)
    assert set(edfa_backend.supported_attributes()) == {'Fiber', 'Edfa', 'Roadm'}
    assert set(edfa_backend.read_all_attributes('Edfa1')) == {'gain_target', 'tilt_target', 'out_voa', 'nf0'}


def test_link_failure_reroutes_and_marks_baseline(mesh_backend):
    direct = mesh_backend.compute_path('trx Alice', 'trx Bob')[0]
    assert 'fiber (Alice → Bob)-' in direct

    mesh_backend.notify_fiber_failed('fiber (Alice → Bob)-', True)
    detour = mesh_backend.compute_path('trx Alice', 'trx Bob')[0]
    assert 'fiber (Alice → Bob)-' not in detour
    assert 'roadm Carol' in detour
    # an admitted service keeps its allocated path: OPM reports link-failed instead of rerouting
    assert _baseline(mesh_backend, direct).status == 'link-failed'
    assert _baseline(mesh_backend, detour).status is None

    mesh_backend.notify_fiber_failed('fiber (Alice → Bob)-', False)
    assert mesh_backend.compute_path('trx Alice', 'trx Bob')[0] == direct


def test_k_shortest_candidates_for_rmsa(mesh_backend):
    candidates = mesh_backend.compute_path('trx Alice', 'trx Bob', k=3)

    assert len(candidates) == 2
    assert candidates[0] == mesh_backend.compute_path('trx Alice', 'trx Bob')[0]
    assert all(path[0] == 'trx Alice' and path[-1] == 'trx Bob' for path in candidates)


def test_redesign_reequalises_after_a_loss_change(edfa_backend):
    path = edfa_backend.compute_path('Site_A', 'Site_B')[0]
    nominal = _baseline(edfa_backend, path)

    edfa_backend.write_attribute('Span1', 'loss_coef', 0.3)
    degraded = _baseline(edfa_backend, path)
    edfa_backend.redesign()
    redesigned = _baseline(edfa_backend, path)

    assert degraded.gsnr_db < redesigned.gsnr_db < nominal.gsnr_db
    assert set(edfa_backend.uid_map) == {'Site_A', 'Span1', 'Edfa1', 'Site_B'}


def test_designed_topology_feeds_the_egn_backend(coronet_backend):
    designed = coronet_backend.designed_topology()

    assert {el['uid'] for el in designed['elements']} == set(coronet_backend.uid_map)
    # GNPy split long spans and inserted amplifiers: the EGN backend needs exactly this placement
    assert any(el['uid'].endswith('-_(1/4)') for el in designed['elements'] if el['type'] == 'Fiber')
    assert sum(el['type'] == 'Edfa' for el in designed['elements']) > 0
    assert designed['connections']


def test_fetch_examples_replaces_the_installed_gnpy_copy(client):
    assert set(TWINLIGHT_EXAMPLES) <= set(list_examples(client))
    assert fetch_example(client, 'eqpt_config.json') == \
        json.loads((GNPY_EXAMPLE_DIR / 'eqpt_config.json').read_text(encoding='utf-8'))


def test_unreachable_service_falls_back_to_mock_opm(examples):
    down = httpx.Client(base_url='http://gnpy-api', transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    backend = RemoteGnpyBackend(down, examples['edfa_example_network.json'], examples['eqpt_config.json'])

    assert not backend.available
    assert _baseline(backend, ['Site_A', 'Span1', 'Edfa1', 'Site_B']) is None


def test_close_releases_the_session(client, examples):
    backend = RemoteGnpyBackend(client, examples['edfa_example_network.json'], examples['eqpt_config.json'])
    network_id = backend.network_id
    backend.close()

    assert client.get(f'/api/v1.0/networks/{network_id}').status_code == 404
