# -*- coding: utf-8 -*-

"""Stateful network sessions: a designed GNPy network kept in memory between requests.

A session is what a digital twin needs from GNPy at runtime: design the network once,
then repeatedly compute paths, propagate a spectrum along a path, mutate element
parameters, fail and restore fibers, and optionally re-run the design. The
stateless ``/path-request`` endpoint re-designs the network on every call and has no
way to express a parameter change that should *not* be followed by a re-design.
"""

import copy
import logging
import math
import secrets
import threading
from collections import Counter
from itertools import islice
from typing import Any, Dict, List, Optional

import numpy as np
from networkx import NetworkXNoPath, restricted_view, shortest_simple_paths
from gnpy.core.elements import Fiber, Roadm, Transceiver
from gnpy.core.exceptions import EquipmentConfigError, NetworkTopologyError
from gnpy.core.info import create_input_spectral_information
from gnpy.tools.json_io import load_eqpt_topo_from_json, network_to_json
from gnpy.tools.worker_utils import designed_network

from gnpyapi.core.exception.element_not_found_error import ElementNotFoundError
from gnpyapi.core.exception.equipment_error import EquipmentError
from gnpyapi.core.exception.param_validation_error import ParamValidationError
from gnpyapi.core.exception.path_computation_error import PathComputationError
from gnpyapi.core.exception.session_not_found_error import SessionNotFoundError
from gnpyapi.core.exception.topology_error import TopologyError
from gnpyapi.core.service import element_attributes

_logger = logging.getLogger(__name__)

# Spectrum fields a client may set, mapped to the equipment SI attribute used when omitted.
_SPECTRUM_DEFAULTS = {
    'f_min': 'f_min',
    'f_max': 'f_max',
    'spacing': 'spacing',
    'baud_rate': 'baud_rate',
    'roll_off': 'roll_off',
    'tx_osnr': 'tx_osnr',
    'tx_power_dbm': 'power_dbm',
}


def _fiber_km(element) -> float:
    """GNPy normalises ``params.length`` to metres whatever unit the topology used."""
    return float(element.params.length) / 1000.0


class NetworkSession:
    """A designed GNPy network plus the runtime state a twin layers on top of it."""

    def __init__(self, equipment: dict, network, no_insert_edfas: bool = False):
        self.equipment = equipment
        self.network = network
        self.no_insert_edfas = no_insert_edfas
        self.failed_fibers = set()
        self.uid_map = {el.uid: el for el in network}
        # Requests run in FastAPI's thread pool; parameter writes and re-design mutate
        # the element objects that propagation deep-copies.
        self.lock = threading.RLock()

    @classmethod
    def from_json(cls, topology: dict, equipment: dict, no_insert_edfas: bool = False) -> 'NetworkSession':
        """Load and design a network from GNPy legacy JSON documents."""
        try:
            equipment, network = load_eqpt_topo_from_json(equipment, topology)
            network, _, _ = designed_network(equipment, network, no_insert_edfas=no_insert_edfas)
        except EquipmentConfigError as e:
            _logger.error(f"An equipment error occurred: {e}")
            raise EquipmentError(str(e))
        except NetworkTopologyError as e:
            _logger.error(f"A topology error occurred: {e}")
            raise TopologyError(str(e))
        return cls(equipment, network, no_insert_edfas)

    # -- inventory ---------------------------------------------------------

    def summary(self) -> dict:
        with self.lock:
            return {
                'network-name': self.network.graph.get('network_name'),
                'element-count': len(self.uid_map),
                'element-types': dict(Counter(type(el).__name__ for el in self.uid_map.values())),
                'failed-fibers': sorted(self.failed_fibers),
                'no-insert-edfas': self.no_insert_edfas,
            }

    def element(self, uid: str):
        try:
            return self.uid_map[uid]
        except KeyError:
            raise ElementNotFoundError(f'Unknown element uid {uid!r}')

    def describe_element(self, uid: str) -> dict:
        with self.lock:
            element = self.element(uid)
            description = {
                'uid': uid,
                'type': type(element).__name__,
                # GNPy transceivers carry no type_variety
                'type-variety': getattr(element, 'type_variety', None),
                'attributes': element_attributes.read_all(element),
            }
            if isinstance(element, Fiber):
                description['length-km'] = _fiber_km(element)
                description['failed'] = uid in self.failed_fibers
            return description

    def describe_elements(self, element_type: Optional[str] = None) -> List[dict]:
        with self.lock:
            return [self.describe_element(uid) for uid, el in self.uid_map.items()
                    if element_type is None or type(el).__name__ == element_type]

    def to_json(self) -> dict:
        """The designed network as a GNPy topology document (amplifiers and split spans included)."""
        with self.lock:
            return network_to_json(self.network)

    # -- runtime mutation --------------------------------------------------

    def update_elements(self, updates: Dict[str, Dict[str, Any]]) -> List[str]:
        """Apply ``{uid: {attribute: value}}``; ``failed`` (bool) fails or restores a fiber.

        Every update is validated before any is written, so a rejected batch leaves the
        network untouched. The network is not re-designed: call :meth:`redesign` for that.
        """
        with self.lock:
            for uid, attributes in updates.items():
                element = self.element(uid)
                for attr, value in attributes.items():
                    if attr == 'failed':
                        if not isinstance(element, Fiber):
                            raise ParamValidationError(f'{uid!r}: only fibers can be failed')
                        if not isinstance(value, bool):
                            raise ParamValidationError(f'{uid!r}.failed must be a boolean')
                    else:
                        element_attributes.validate_attr(element, attr, value)
            for uid, attributes in updates.items():
                for attr, value in attributes.items():
                    if attr != 'failed':
                        element_attributes.write_attr(self.uid_map[uid], attr, value)
                    elif value:
                        self.failed_fibers.add(uid)
                    else:
                        self.failed_fibers.discard(uid)
            return list(updates)

    def redesign(self):
        """Re-run GNPy's design on the mutated network (re-equalises ROADMs and amplifier operating points)."""
        with self.lock:
            # gnpy.core.network.span_loss caches each span's loss in ``design_span_loss`` at the first
            # design; drop it, otherwise a mutated loss_coef or att_in never reaches the amplifier gains.
            for element in self.network:
                if hasattr(element, 'design_span_loss'):
                    del element.design_span_loss
            self.network, _, _ = designed_network(self.equipment, self.network, no_insert_edfas=self.no_insert_edfas)
            self.uid_map = {el.uid: el for el in self.network}
            self.failed_fibers &= set(self.uid_map)

    # -- path computation --------------------------------------------------

    def _transceiver(self, uid: str):
        element = self.uid_map.get(uid)
        if not isinstance(element, Transceiver):
            raise PathComputationError(f'{uid!r} is not a transceiver')
        return element

    def compute_paths(self, source: str, destination: str, k: int = 1) -> dict:
        """Return up to ``k`` shortest paths (weighted by fiber length) that avoid failed fibers.

        With no include constraint, ``gnpy.topology.request.compute_constrained_path`` reduces
        to the first of these paths, so ``k=1`` matches GNPy's own path computation.
        """
        with self.lock:
            src = self._transceiver(source)
            dst = self._transceiver(destination)
            usable = restricted_view(self.network, [self.uid_map[uid] for uid in self.failed_fibers], [])
            try:
                paths = list(islice(shortest_simple_paths(usable, src, dst, weight='weight'), k))
            except NetworkXNoPath:
                paths = []
            return {
                'source': source,
                'destination': destination,
                'paths': [{'uids': [el.uid for el in path],
                           'total-fiber-km': sum(_fiber_km(el) for el in path if isinstance(el, Fiber))}
                          for path in paths],
                'blocking-reason': None if paths else 'NO_PATH',
            }

    # -- propagation -------------------------------------------------------

    def _path_copy(self, path: List[str]) -> list:
        """Validate the hops of ``path`` and deep-copy its elements; propagation mutates them."""
        elements = []
        for uid in path:
            element = self.uid_map.get(uid)
            if element is None:
                raise PathComputationError(f'Unknown element uid {uid!r} in path')
            if elements and not self.network.has_edge(elements[-1], element):
                raise PathComputationError(f'No connection from {elements[-1].uid!r} to {uid!r} in path')
            elements.append(element)
        return [copy.deepcopy(el) for el in elements]

    def _input_spectrum(self, spectrum: Optional[dict]):
        """Flat-power comb; any field left out falls back to the equipment's default SI."""
        spectrum = {k: v for k, v in (spectrum or {}).items() if v is not None}
        si_default = self.equipment['SI']['default']
        params = {field: spectrum.get(field, getattr(si_default, attr)) for field, attr in _SPECTRUM_DEFAULTS.items()}
        if params['f_max'] - params['f_min'] < params['spacing']:
            raise PathComputationError('Spectrum must hold at least one channel between f-min and f-max')
        tx_power = 10 ** (params.pop('tx_power_dbm') / 10) * 1e-3
        return create_input_spectral_information(tx_power=tx_power, delta_pdb=spectrum.get('delta_pdb', 0.0),
                                                 **params)

    def propagate(self, path: List[str], spectrum: Optional[dict] = None, include_channels: bool = False) -> dict:
        """Propagate a flat comb element by element along ``path`` and report the received QoT.

        ``gsnr-db`` and ``osnr-ase-db`` are the line values carried by the spectrum. When the path
        ends at a transceiver, ``receiver-gsnr-db`` and ``receiver-osnr-ase-db`` also include the
        transmitter OSNR and the ROADM add/drop OSNR, as reported by ``/path-request``.

        Unit conversions follow GNPy's ``Transceiver._calc_*`` methods: accumulated CD is in
        s/m (x1e3 -> ps/nm), PMD in s (x1e12 -> ps), PDL in dB and latency in s (x1e3 -> ms).
        """
        with self.lock:
            elements = self._path_copy(path)
            failed = [uid for uid in path if uid in self.failed_fibers]
            si = self._input_spectrum(spectrum)
        description = {
            'status': 'link-failed' if failed else 'ok',
            'failed-elements': failed,
            'elements': [{'uid': el.uid, 'type': type(el).__name__} for el in elements],
            'total-fiber-km': sum(_fiber_km(el) for el in elements if isinstance(el, Fiber)),
        }
        if failed:
            # No light reaches the receiver through a cut fiber.
            return description

        roadm_crossings = []
        for i, element in enumerate(elements):
            if isinstance(element, Roadm):
                from_degree = elements[i - 1].uid if i > 0 else None
                to_degree = elements[i + 1].uid if i < len(elements) - 1 else None
                si = element(si, degree=to_degree, from_degree=from_degree)
                if from_degree and to_degree:
                    roadm_crossings.append((element, from_degree, to_degree))
            else:
                si = element(si)

        receiver = elements[-1] if isinstance(elements[-1], Transceiver) else None
        if receiver is not None:
            # Like gnpy.topology.request.propagate: transmitter and ROADM add/drop OSNR are
            # penalties on the receiver's SNR, not part of the propagated spectrum. They are looked
            # up on the received channels, since amplifiers drop channels outside their band.
            roadm_osnr = [roadm.get_impairment('roadm-osnr', si.frequency, from_degree, to_degree)
                          for roadm, from_degree, to_degree in roadm_crossings]
            receiver.update_snr(si.tx_osnr, *roadm_osnr)
        channels = self._channels(si, receiver)
        worst = int(np.argmin(si.gsnr_db))
        description.update({
            'channel-count': len(channels),
            'worst-channel': channels[worst],
            'max-pmd-ps': float(np.max(si.pmd) * 1e12),
            'latency-ms': float(si.latency[0] * 1e3),
        })
        if include_channels:
            description['channels'] = channels
        return description

    @staticmethod
    def _channels(si, receiver=None) -> List[dict]:
        osnr_01nm = si.snr_lin_db + 10 * np.log10(si.baud_rate / 12.5e9)
        channels = [{
            'channel-number': int(si.channel_number[i]),
            'frequency-hz': float(si.frequency[i]),
            'baud-rate-hz': float(si.baud_rate[i]),
            'signal-power-dbm': float(si.signal_dbm[i]),
            'gsnr-db': float(si.gsnr_db[i]),
            'osnr-ase-db': float(si.snr_lin_db[i]),
            'osnr-ase-01nm-db': float(osnr_01nm[i]),
            'snr-nli-db': float(si.snr_nli_db[i]) if math.isfinite(si.snr_nli_db[i]) else None,
            'chromatic-dispersion-ps-nm': float(si.chromatic_dispersion[i] * 1e3),
            'pmd-ps': float(si.pmd[i] * 1e12),
            'pdl-db': float(si.pdl[i]),
            'latency-ms': float(si.latency[i] * 1e3),
        } for i in range(si.number_of_channels)]
        if receiver is not None:
            for i, channel in enumerate(channels):
                channel['receiver-gsnr-db'] = float(receiver.snr[i])
                channel['receiver-osnr-ase-db'] = float(receiver.osnr_ase[i])
        return channels


class SessionStore:
    """In-memory registry of network sessions, keyed by an unguessable token.

    The token is the only credential for a session: it is never listed, and error messages
    do not echo it, so it cannot leak into responses or logs.
    """

    def __init__(self):
        self._sessions: Dict[str, NetworkSession] = {}
        self._lock = threading.Lock()

    def add(self, session: NetworkSession) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = session
        return token

    def get(self, token: Optional[str]) -> NetworkSession:
        with self._lock:
            session = self._sessions.get(token) if token else None
        if session is None:
            raise SessionNotFoundError('Missing, unknown or closed session token')
        return session

    def remove(self, token: Optional[str]):
        with self._lock:
            if not token or self._sessions.pop(token, None) is None:
                raise SessionNotFoundError('Missing, unknown or closed session token')


session_store = SessionStore()
