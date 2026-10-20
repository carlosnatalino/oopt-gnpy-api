# coding: utf-8
import json
from pathlib import Path

import gnpy
from fastapi import HTTPException

from gnpyapi.core import API_VERSION, app

EXAMPLE_DATA_BASE_PATH = API_VERSION + '/example-data'
_EXAMPLE_DATA_DIR = Path(gnpy.__file__).parent / 'example-data'


def _example_files():
    return {path.name: path for path in _EXAMPLE_DATA_DIR.glob('*.json')}


@app.get(EXAMPLE_DATA_BASE_PATH)
def list_example_data():
    """JSON reference files shipped with the GNPy release this service runs."""
    return {'files': sorted(_example_files())}


@app.get(EXAMPLE_DATA_BASE_PATH + '/{name}')
def get_example_data(name: str):
    path = _example_files().get(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f'No GNPy example file named {name!r}')
    return json.loads(path.read_text(encoding='utf-8'))
