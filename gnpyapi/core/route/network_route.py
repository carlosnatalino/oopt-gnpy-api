# coding: utf-8
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, HTTPException, Query, Response
from gnpy.tools.convert_legacy_yang import yang_to_legacy

from gnpyapi.core import API_VERSION, app
from gnpyapi.core.exception.element_not_found_error import ElementNotFoundError
from gnpyapi.core.exception.equipment_error import EquipmentError
from gnpyapi.core.exception.network_not_found_error import NetworkNotFoundError
from gnpyapi.core.exception.param_validation_error import ParamValidationError
from gnpyapi.core.exception.path_computation_error import PathComputationError
from gnpyapi.core.exception.topology_error import TopologyError
from gnpyapi.core.model.network import PathComputationRequest, PropagationRequest
from gnpyapi.core.service import element_attributes
from gnpyapi.core.service.network_service import NetworkSession, network_store

NETWORKS_BASE_PATH = API_VERSION + '/networks'
NETWORK_PATH = NETWORKS_BASE_PATH + '/{network_id}'

_PLANNING_EXAMPLE = json.loads(
    (Path(__file__).parents[2] / 'exampledata' / 'planning_demand_example.json').read_text(encoding='utf-8')
)
_EXAMPLE = {'gnpy-api:api': {key: value for key, value in _PLANNING_EXAMPLE['gnpy-api:api'].items()
                             if key != 'gnpy-path-computation:services'}}
_EXAMPLES = {'network': {'summary': 'Topology and equipment', 'value': _EXAMPLE}}
_SCHEMA = {'type': 'object', 'example': _EXAMPLE}


@contextmanager
def _http_errors():
    """Translate service errors into HTTP status codes."""
    try:
        yield
    except (NetworkNotFoundError, ElementNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ParamValidationError, PathComputationError, TopologyError, EquipmentError) as e:
        raise HTTPException(status_code=400, detail=str(e))


def _topology_and_equipment(data: Any):
    """Accept the same envelopes as /path-request: RFC 7951 (``gnpy-api:api``) or legacy (``gnpy-api:*``)."""
    data = json.loads(data) if isinstance(data, str) else data
    if 'gnpy-api:api' in data:
        legacy_data = yang_to_legacy(data)
        topology = legacy_data.get('gnpy-network-topology:topology')
        equipment = legacy_data.get('gnpy-eqpt-config:equipment')
    else:
        topology = data.get('gnpy-api:topology')
        equipment = data.get('gnpy-api:equipment')
    if topology is None:
        raise TopologyError('No topology found in request')
    if equipment is None:
        raise EquipmentError('No equipment found in request')
    return topology, equipment


@app.post(NETWORKS_BASE_PATH, status_code=201)
def create_network(
    response: Response,
    data: Any = Body(..., openapi_examples=_EXAMPLES, json_schema_extra=_SCHEMA),
    no_insert_edfas: bool = Query(False, alias='no-insert-edfas'),
):
    """Load and design a network, and keep it in memory for the calls below."""
    with _http_errors():
        topology, equipment = _topology_and_equipment(data)
        session = network_store.add(NetworkSession.from_json(topology, equipment, no_insert_edfas))
    response.headers['Location'] = f'{NETWORKS_BASE_PATH}/{session.network_id}'
    return session.summary()


@app.get(NETWORKS_BASE_PATH)
def list_networks():
    return {'networks': [session.summary() for session in network_store.list()]}


@app.get(NETWORK_PATH)
def get_network(network_id: str):
    with _http_errors():
        return network_store.get(network_id).summary()


@app.delete(NETWORK_PATH, status_code=204)
def delete_network(network_id: str):
    with _http_errors():
        network_store.remove(network_id)
    return Response(status_code=204)


@app.get(NETWORK_PATH + '/topology')
def get_designed_topology(network_id: str):
    """The designed network as GNPy topology JSON, including inserted amplifiers and split spans."""
    with _http_errors():
        return network_store.get(network_id).to_json()


@app.get(NETWORK_PATH + '/elements')
def list_elements(network_id: str, element_type: Optional[str] = Query(None, alias='type')):
    with _http_errors():
        return {'elements': network_store.get(network_id).describe_elements(element_type)}


@app.patch(NETWORK_PATH + '/elements')
def update_elements(network_id: str, updates: Dict[str, Dict[str, Any]] = Body(
        ..., examples=[{'fiber (Alice → Bob)-': {'loss_coef': 0.25, 'failed': False}}])):
    """Apply ``{uid: {attribute: value}}`` atomically; the network is not re-designed."""
    with _http_errors():
        session = network_store.get(network_id)
        uids = session.update_elements(updates)
        return {'elements': [session.describe_element(uid) for uid in uids]}


@app.get(NETWORK_PATH + '/elements/{uid:path}')
def get_element(network_id: str, uid: str):
    with _http_errors():
        return network_store.get(network_id).describe_element(uid)


@app.patch(NETWORK_PATH + '/elements/{uid:path}')
def update_element(network_id: str, uid: str, attributes: Dict[str, Any] = Body(..., examples=[{'nf0': 6.0}])):
    with _http_errors():
        session = network_store.get(network_id)
        session.update_elements({uid: attributes})
        return session.describe_element(uid)


@app.post(NETWORK_PATH + '/redesign')
def redesign_network(network_id: str):
    """Re-run GNPy's design on the network as currently mutated."""
    with _http_errors():
        session = network_store.get(network_id)
        session.redesign()
        return session.summary()


@app.post(NETWORK_PATH + '/paths')
def compute_paths(network_id: str, request: PathComputationRequest):
    with _http_errors():
        return network_store.get(network_id).compute_paths(request.source, request.destination, request.k)


@app.post(NETWORK_PATH + '/propagate')
def propagate(network_id: str, request: PropagationRequest):
    with _http_errors():
        spectrum = request.spectrum.model_dump() if request.spectrum else None
        return network_store.get(network_id).propagate(request.path, spectrum, request.include_channels)


@app.get(API_VERSION + '/element-attributes')
def get_element_attributes():
    """Runtime-mutable attributes per element type, with unit and accepted range."""
    return element_attributes.schema()
