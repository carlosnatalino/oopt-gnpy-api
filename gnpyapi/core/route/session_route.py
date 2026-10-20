# coding: utf-8
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, Depends, HTTPException, Query, Response, Security
from fastapi.security import APIKeyHeader
from gnpy.tools.convert_legacy_yang import yang_to_legacy

from gnpyapi.core import API_VERSION, app
from gnpyapi.core.exception.element_not_found_error import ElementNotFoundError
from gnpyapi.core.exception.equipment_error import EquipmentError
from gnpyapi.core.exception.param_validation_error import ParamValidationError
from gnpyapi.core.exception.path_computation_error import PathComputationError
from gnpyapi.core.exception.session_not_found_error import SessionNotFoundError
from gnpyapi.core.exception.topology_error import TopologyError
from gnpyapi.core.model.network import PathComputationRequest, PropagationRequest
from gnpyapi.core.service import element_attributes
from gnpyapi.core.service.network_service import NetworkSession, session_store

SESSIONS_PATH = API_VERSION + '/sessions'
SESSION_PATH = API_VERSION + '/session'
SESSION_TOKEN_HEADER = 'X-Session-Token'

_PLANNING_EXAMPLE = json.loads(
    (Path(__file__).parents[2] / 'exampledata' / 'planning_demand_example.json').read_text(encoding='utf-8')
)
_EXAMPLE = {'gnpy-api:api': {key: value for key, value in _PLANNING_EXAMPLE['gnpy-api:api'].items()
                             if key != 'gnpy-path-computation:services'}}
_EXAMPLES = {'network': {'summary': 'Topology and equipment', 'value': _EXAMPLE}}
_SCHEMA = {'type': 'object', 'example': _EXAMPLE}

# Declared as a security scheme so the OpenAPI page offers to set the token once for all calls.
_session_token = APIKeyHeader(name=SESSION_TOKEN_HEADER, scheme_name='SessionToken', auto_error=False,
                              description='Token returned by POST ' + SESSIONS_PATH)


@contextmanager
def _http_errors():
    """Translate service errors into HTTP status codes."""
    try:
        yield
    except SessionNotFoundError as e:
        # 401 rather than 404: the client has to open a new session, not fix a resource name
        raise HTTPException(status_code=401, detail=str(e), headers={'WWW-Authenticate': SESSION_TOKEN_HEADER})
    except ElementNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ParamValidationError, PathComputationError, TopologyError, EquipmentError) as e:
        raise HTTPException(status_code=400, detail=str(e))


def _current_session(token: Optional[str] = Security(_session_token)) -> NetworkSession:
    with _http_errors():
        return session_store.get(token)


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


@app.post(SESSIONS_PATH, status_code=201)
def open_session(
    data: Any = Body(..., openapi_examples=_EXAMPLES, json_schema_extra=_SCHEMA),
    no_insert_edfas: bool = Query(False, alias='no-insert-edfas'),
):
    """Load and design a network, and return the token that selects it in the calls below."""
    with _http_errors():
        topology, equipment = _topology_and_equipment(data)
        session = NetworkSession.from_json(topology, equipment, no_insert_edfas)
    token = session_store.add(session)
    return {'session-token': token, **session.summary()}


@app.get(SESSION_PATH)
def get_session(session: NetworkSession = Depends(_current_session)):
    return session.summary()


@app.delete(SESSION_PATH, status_code=204)
def close_session(token: Optional[str] = Security(_session_token)):
    """End the session: the network is released and the token stops working."""
    with _http_errors():
        session_store.remove(token)
    return Response(status_code=204)


@app.get(SESSION_PATH + '/topology')
def get_designed_topology(session: NetworkSession = Depends(_current_session)):
    """The designed network as GNPy topology JSON, including inserted amplifiers and split spans."""
    return session.to_json()


@app.get(SESSION_PATH + '/elements')
def list_elements(session: NetworkSession = Depends(_current_session),
                  element_type: Optional[str] = Query(None, alias='type')):
    return {'elements': session.describe_elements(element_type)}


@app.patch(SESSION_PATH + '/elements')
def update_elements(session: NetworkSession = Depends(_current_session), updates: Dict[str, Dict[str, Any]] = Body(
        ..., examples=[{'fiber (Alice → Bob)-': {'loss_coef': 0.25, 'failed': False}}])):
    """Apply ``{uid: {attribute: value}}`` atomically; the network is not re-designed."""
    with _http_errors():
        uids = session.update_elements(updates)
        return {'elements': [session.describe_element(uid) for uid in uids]}


@app.get(SESSION_PATH + '/elements/{uid:path}')
def get_element(uid: str, session: NetworkSession = Depends(_current_session)):
    with _http_errors():
        return session.describe_element(uid)


@app.patch(SESSION_PATH + '/elements/{uid:path}')
def update_element(uid: str, session: NetworkSession = Depends(_current_session),
                   attributes: Dict[str, Any] = Body(..., examples=[{'nf0': 6.0}])):
    with _http_errors():
        session.update_elements({uid: attributes})
        return session.describe_element(uid)


@app.post(SESSION_PATH + '/redesign')
def redesign_network(session: NetworkSession = Depends(_current_session)):
    """Re-run GNPy's design on the network as currently mutated."""
    session.redesign()
    return session.summary()


@app.post(SESSION_PATH + '/paths')
def compute_paths(request: PathComputationRequest, session: NetworkSession = Depends(_current_session)):
    with _http_errors():
        return session.compute_paths(request.source, request.destination, request.k)


@app.post(SESSION_PATH + '/propagate')
def propagate(request: PropagationRequest, session: NetworkSession = Depends(_current_session)):
    with _http_errors():
        spectrum = request.spectrum.model_dump() if request.spectrum else None
        return session.propagate(request.path, spectrum, request.include_channels)


@app.get(API_VERSION + '/element-attributes')
def get_element_attributes():
    """Runtime-mutable attributes per element type, with unit and accepted range."""
    return element_attributes.schema()
