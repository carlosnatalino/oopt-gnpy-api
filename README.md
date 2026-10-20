# GNPy API
[![Install via pip](https://img.shields.io/pypi/v/gnpy-api)](https://pypi.org/project/gnpy-api/)
[![Python versions](https://img.shields.io/pypi/pyversions/gnpy-api)](https://pypi.org/project/gnpy-api/)
[![Gerrit](https://img.shields.io/badge/patches-via%20Gerrit-blue)](https://review.gerrithub.io/q/project:Telecominfraproject/oopt-gnpy-api)

FastAPI service
---------------
This repository extends GNPy with a FastAPI-based HTTP service, allowing more flexible control and interaction with its simulation engine. The service can be used to integrate GNPy into software-defined networking (SDN) architectures or other external applications.

[GNPy](https://github.com/Telecominfraproject/oopt-gnpy) is an open-source Python-based library that models and evaluates the performance of optical networks. It is widely used for path computation, QoT (Quality of Transmission) estimation, and network planning.

## 🚀 Installation
### Build from Source - Option 1
Clone the repository and install the package:
```bash
python3 setup.py install
```
or if you want to install it in a docker container:
```bash
docker build ./ -t gnpy-api
```

### PyPI - Option 2
```bash
pip install gnpy-api
```

## Quick Start

## 🧪 Usage - CLI
Start the FastAPI server:
```bash
python ./samples/rest_example.py
```
OpenAPI docs are available at `http://localhost:8080/docs`.

Send example data to the API:
```bash
curl --location 'http://localhost:8080/api/v1.0/path-request' --header 'Content-Type: application/json' --data @gnpyapi/exampledata/planning_demand_example.json
```
The example server runs over HTTP. Use a reverse proxy or uvicorn TLS settings for HTTPS.

## 🧪 Usage - Docker
```bash
docker run --rm -p 8080:8080 gnpy-api
```

## 🔁 Sessions
`/path-request` designs the network again on every call. Digital twins and other SDN
controllers can instead open a session that keeps a designed network in the service, and
work on it step by step. Calls find the session through a token, sent in a header:
```bash
# load and design once; the response carries the session-token
curl -X POST 'http://localhost:8080/api/v1.0/sessions' --header 'Content-Type: application/json' \
     --data '{"gnpy-api:topology": {...}, "gnpy-api:equipment": {...}}'
# then compute paths, propagate, change parameters, fail fibers, redesign
curl -X POST 'http://localhost:8080/api/v1.0/session/paths' --header "X-Session-Token: $TOKEN" \
     --header 'Content-Type: application/json' --data '{"source": "trx Alice", "destination": "trx Bob"}'
# and end the session when done
curl -X DELETE 'http://localhost:8080/api/v1.0/session' --header "X-Session-Token: $TOKEN"
```
See [docs/session-api.md](docs/session-api.md) for every endpoint. `tests/twinlight/` shows
how the [TwinLight](https://github.com/carlosnatalino/TwinLight) digital twin would use them
in place of importing GNPy.

## 🔄 Compatibility

Different versions of this interface extension are compatible with specific versions of [GNPy](https://github.com/Telecominfraproject/oopt-gnpy). Please refer to the table below to ensure that you are using a supported version combination.



| Interface Version | Compatible GNPy Version | Notes                             |
|-------------------|-------------------------|-----------------------------------|
| `v1.0.x`          | `2.14`                  | FastAPI service, OpenAPI support  |
| `v0.2.x`          | `2.14`                  | YANG model validation             |
| `v0.1.x`          | `>=2.12.1`              | Initial release                   |

⚠️ If you use an incompatible combination, some features may not work correctly or may produce unexpected errors.

To check your GNPy version, run:

```bash
pip show gnpy
```

## 📚 Documentation
Refer to the official [GNPy documentation](https://github.com/Telecominfraproject/oopt-gnpy/tree/master/docs) for information about network modeling and simulation capabilities.

API documentation is available in the docs folder.
