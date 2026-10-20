#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""How TwinLight would call GNPy through gnpy-api instead of importing it.

This is a mock of ``twinlight/physics/remote_gnpy_backend.py``. It implements the
``PhysicalBackend`` Protocol of TwinLight (``physics/backend.py``) exactly where the
in-process ``GnpyBackend`` does, and covers the other places where TwinLight imports
``gnpy`` today:

==============================================  ============================================
TwinLight call site (gnpy import)               gnpy-api call
==============================================  ============================================
``gnpy_adapter.build_gnpy_network``             ``POST /sessions``
``gnpy_adapter.build_uid_map``                  ``GET /session/elements``
``gnpy_adapter.compute_path_baseline``          ``POST /session/propagate``
``element_params.read_attr / read_all``         ``GET /session/elements/{uid}``
``element_params.write_attr``                   ``PATCH /session/elements/{uid}``
``element_params.schema``                       ``GET /element-attributes``
``GnpyBackend.redesign`` (designed_network)     ``POST /session/redesign``
``GnpyBackend.is_fiber`` (isinstance Fiber)     element type from the uid map
``TapiContext.set_link_failed`` (edge removal)  ``PATCH /session/elements/{uid}`` failed
``TapiContext._get_path_via_gnpy``              ``POST /session/paths``
``egn_backend._designed_topology_json``         ``GET /session/topology``
``example_data.gnpy_example_data_dir``          ``GET /example-data/{name}``
==============================================  ============================================

Every ``/session`` call carries the token returned by ``POST /sessions`` in the
``X-Session-Token`` header. The real backend would use ``httpx.AsyncClient`` for
``compute_baseline``; this mock takes any ``httpx.Client`` so the tests can hand it
FastAPI's ``TestClient``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

API = '/api/v1.0'
SESSION_TOKEN_HEADER = 'X-Session-Token'

# TwinLight's propagation comb (gnpy_adapter.py): 88 x 50 GHz slots centred on 193.1 THz.
_F_CENTER_HZ = 193.1e12
_NB_CHANNELS = 88
_SPACING_HZ = 50e9

# Subset of TwinLight's physics/modulation.py MODULATION_TABLE: (baud rate [Hz], launch power [dBm]).
MODULATION_TABLE = {
    'DP-QPSK': (32e9, 0.0),
    'DP-16QAM': (32e9, 0.0),
    'DP-64QAM': (32e9, 0.0),
}


class ParamValidationError(ValueError):
    """Same role as TwinLight's ``physics.element_params.ParamValidationError``."""


class SessionLostError(RuntimeError):
    """The service no longer knows the token, e.g. after a restart.

    TwinLight would call :meth:`RemoteGnpyBackend.reopen`, then replay the element overrides
    and failed links it already keeps for snapshots (``TapiContext._element_overrides`` and
    ``_failed_links``), and retry.
    """


@dataclass
class OpmBaseline:
    """Copy of TwinLight's ``physics.gnpy_adapter.OpmBaseline``."""

    gsnr_db: float
    osnr_ase_db: float
    cd_ps_nm: float
    pmd_ps: float
    latency_ms: float
    total_fiber_km: float
    edfa_uids: List[str] = field(default_factory=list)
    fiber_uids: List[str] = field(default_factory=list)
    pdl_elements: List[Tuple[str, str]] = field(default_factory=list)
    status: Optional[str] = None


@dataclass
class _UidStub:
    """Stand-in for a GNPy element in ``uid_map``.

    TwinLight's ``physics.backend.element_kind_name`` already reads ``.type`` from objects
    of this class name (the EGN backend uses the same trick), and ``params.length`` in
    metres keeps ``_fiber_length_km`` and ``get_equipment_list`` working unchanged.
    """

    uid: str
    type: str
    params: Optional[SimpleNamespace] = None


class RemoteGnpyBackend:
    """``PhysicalBackend`` whose GNPy network lives in a gnpy-api session."""

    name = 'gnpy'

    def __init__(self, http: httpx.Client, topology: dict, equipment: dict, no_insert_edfas: bool = False):
        self._http = http
        self.available = False
        self.uid_map: Dict[str, _UidStub] = {}
        # No local DiGraph: TapiContext asks compute_path() instead of running
        # compute_constrained_path on self.network.
        self.network = None
        self._schema: Optional[dict] = None
        self._token: Optional[str] = None
        self._network = {'gnpy-api:topology': topology, 'gnpy-api:equipment': equipment}
        self._no_insert_edfas = no_insert_edfas
        try:
            self.reopen()
        except (httpx.HTTPError, ParamValidationError) as exc:
            # Same contract as GnpyBackend: never take the twin down, fall back to mock OPM.
            _logger.warning('gnpy-api network load failed (%s) - using mock data', exc)

    def reopen(self):
        """Open a session on the network as loaded from file; earlier changes are not replayed."""
        created = self._call('POST', '/sessions', params={'no-insert-edfas': self._no_insert_edfas},
                             json=self._network)
        self._token = created['session-token']
        self._refresh_uid_map()
        self.available = True

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs) -> Any:
        headers = {SESSION_TOKEN_HEADER: self._token} if self._token else {}
        response = self._http.request(method, API + path, headers=headers, **kwargs)
        if response.status_code == 401:
            raise SessionLostError(_detail(response))
        if response.status_code == 404:
            raise KeyError(_detail(response))
        if response.status_code in (400, 422):
            raise ParamValidationError(_detail(response))
        response.raise_for_status()
        return response.json() if response.content else None

    def _element_path(self, uid: str) -> str:
        # Real uids carry spaces, arrows and slashes: "fiber (Abilene → Dallas)-_(1/4)".
        return f'/session/elements/{quote(uid, safe="")}'

    def _refresh_uid_map(self):
        elements = self._call('GET', '/session/elements')['elements']
        self.uid_map = {
            el['uid']: _UidStub(el['uid'], el['type'],
                                SimpleNamespace(length=el['length-km'] * 1000.0) if 'length-km' in el else None)
            for el in elements
        }

    # -- PhysicalBackend: QoT propagation ---------------------------------

    async def compute_baseline(self, path_uids: List[str], modulation_format: str) -> Optional[OpmBaseline]:
        if not self.available:
            return None
        baud_rate, tx_power_dbm = MODULATION_TABLE[modulation_format]
        spectrum = {
            'f-min': _F_CENTER_HZ - (_NB_CHANNELS // 2) * _SPACING_HZ,
            'f-max': _F_CENTER_HZ + (_NB_CHANNELS // 2) * _SPACING_HZ,
            'spacing': _SPACING_HZ,
            'baud-rate': baud_rate,
            'roll-off': 0.15,
            'tx-osnr': 100.0,  # ideal Tx: the transmitter is never the bottleneck
            'tx-power-dbm': tx_power_dbm,
        }
        try:
            result = self._call('POST', '/session/propagate',
                                json={'path': path_uids, 'spectrum': spectrum})
        except (httpx.HTTPError, KeyError, ParamValidationError) as exc:
            _logger.warning('gnpy-api baseline computation failed: %s', exc)
            return None
        return baseline_from_propagation(result)

    # -- PhysicalBackend: /config plane -----------------------------------

    def read_attribute(self, uid: str, attr: str) -> Any:
        attributes = self.read_all_attributes(uid)
        if attr not in attributes:
            raise ParamValidationError(f'{self.uid_map[uid].type} {uid!r}: attribute {attr!r} is not exposed')
        return attributes[attr]

    def write_attribute(self, uid: str, attr: str, value: Any):
        self._call('PATCH', self._element_path(uid), json={attr: value})

    def read_all_attributes(self, uid: str) -> Dict[str, Any]:
        return self._call('GET', self._element_path(uid))['attributes']

    def supported_attributes(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if self._schema is None:
            self._schema = self._call('GET', '/element-attributes')
        return self._schema

    def redesign(self):
        if not self.available:
            return
        self._call('POST', '/session/redesign')
        self._refresh_uid_map()

    def is_fiber(self, uid: str) -> bool:
        element = self.uid_map.get(uid)
        return element is not None and element.type == 'Fiber'

    def notify_fiber_failed(self, uid: str, failed: bool):
        """TapiContext stops removing edges from a local GNPy graph; the service keeps the failure."""
        self._call('PATCH', self._element_path(uid), json={'failed': failed})

    # -- the other gnpy call sites in TwinLight ---------------------------

    def compute_path(self, src_uid: str, dst_uid: str, k: int = 1) -> List[List[str]]:
        """Replaces ``TapiContext._get_path_via_gnpy`` (and the k-shortest fallback when k > 1)."""
        result = self._call('POST', '/session/paths',
                            json={'source': src_uid, 'destination': dst_uid, 'k': k})
        return [path['uids'] for path in result['paths']]

    def designed_topology(self) -> dict:
        """Replaces ``egn_backend._designed_topology_json``: GNPy's amplifier placement, as JSON."""
        return self._call('GET', '/session/topology')

    def close(self):
        if self._token is not None:
            self._call('DELETE', '/session')
            self._token = None
            self.available = False


def baseline_from_propagation(result: dict) -> OpmBaseline:
    """Map a ``/propagate`` response onto TwinLight's OpmBaseline."""
    if result['status'] == 'link-failed':
        # Same sentinel TapiContext.get_or_compute_baseline builds for a path over a failed fiber.
        return OpmBaseline(gsnr_db=0.0, osnr_ase_db=0.0, cd_ps_nm=0.0, pmd_ps=0.0, latency_ms=0.0,
                           total_fiber_km=0.0, status='link-failed')
    elements = [(el['uid'], el['type']) for el in result['elements']]
    worst = result['worst-channel']
    return OpmBaseline(
        gsnr_db=worst['gsnr-db'],
        osnr_ase_db=worst['osnr-ase-db'],
        cd_ps_nm=worst['chromatic-dispersion-ps-nm'],
        pmd_ps=result['max-pmd-ps'],
        latency_ms=result['latency-ms'],
        total_fiber_km=result['total-fiber-km'],
        edfa_uids=[uid for uid, kind in elements if kind == 'Edfa'],
        fiber_uids=[uid for uid, kind in elements if kind == 'Fiber'],
        # PDL hinges in propagation order (Zarkosvky & Shtaif, Opt. Lett. 45(5):1224 (2020)):
        # ROADMs and EDFAs are discrete PDL elements, fibers contribute negligibly.
        pdl_elements=[(uid, kind) for uid, kind in elements if kind in ('Roadm', 'Edfa')],
    )


def list_examples(http: httpx.Client) -> List[str]:
    """Replaces ``twinlight-fetch-examples --list``'s look into the installed gnpy package."""
    response = http.get(f'{API}/example-data')
    response.raise_for_status()
    return response.json()['files']


def fetch_example(http: httpx.Client, name: str) -> dict:
    """Replaces copying ``gnpy/example-data/<name>`` out of the installed gnpy package."""
    response = http.get(f'{API}/example-data/{quote(name)}')
    response.raise_for_status()
    return response.json()


def _detail(response: httpx.Response) -> str:
    """FastAPI answers ``{"detail"}``; the sample server's handler answers ``{"description"}``."""
    try:
        body = response.json()
    except ValueError:
        return response.text
    return str(body.get('detail') or body.get('description') or body)
