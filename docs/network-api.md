# Network sessions

`POST /api/v1.0/path-request` is stateless: every call loads the topology, designs the
network and plans the services from scratch. A digital twin needs more control. It
designs a network once, and then repeatedly:

* computes candidate paths between transceivers,
* propagates a spectrum along a chosen path and reads the QoT per channel,
* changes element parameters (fiber loss, amplifier gain, ROADM target power), with or
  without re-running the design afterwards,
* fails and restores fibers.

A **network session** keeps a designed GNPy network in the service's memory between
calls, so it supports all of the above. Every endpoint below sits under `/api/v1.0`.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/networks` | Load and design a network; returns `201` with its `network-id` |
| `GET` | `/networks` | List the sessions held by the service |
| `GET` | `/networks/{id}` | Summary: element counts, failed fibers |
| `DELETE` | `/networks/{id}` | Release the session |
| `GET` | `/networks/{id}/elements[?type=Fiber]` | Elements of the *designed* network, with mutable attributes |
| `GET` | `/networks/{id}/elements/{uid}` | One element |
| `PATCH` | `/networks/{id}/elements/{uid}` | Change attributes of one element |
| `PATCH` | `/networks/{id}/elements` | Change attributes of several elements in one atomic batch |
| `POST` | `/networks/{id}/redesign` | Re-run GNPy's design on the network as currently mutated |
| `POST` | `/networks/{id}/paths` | Up to `k` shortest paths between two transceivers |
| `POST` | `/networks/{id}/propagate` | Propagate a comb along a path and report QoT |
| `GET` | `/networks/{id}/topology` | The designed network as GNPy topology JSON |
| `GET` | `/element-attributes` | Mutable attributes per element type, with units and ranges |
| `GET` | `/example-data`, `/example-data/{name}` | JSON reference files shipped with GNPy |

Element uids may contain spaces, arrows and slashes (`fiber (Abilene → Dallas)-_(1/4)`).
Percent-encode them in the URL.

## Creating a network

The body takes the same two formats as `/path-request`, without the services: the RFC 7951
envelope (`gnpy-api:api` holding `gnpy-network-topology:topology` and
`gnpy-eqpt-config:equipment`), or the legacy GNPy JSON (`gnpy-api:topology` and
`gnpy-api:equipment`). The query parameter `no-insert-edfas=true` maps to GNPy's
`no_insert_edfas` design option.

```bash
curl -X POST 'http://localhost:8080/api/v1.0/networks' \
     -H 'Content-Type: application/json' \
     -d '{"gnpy-api:topology": {...}, "gnpy-api:equipment": {...}}'
```

```json
{
  "network-id": "c865d3b8-a4c8-4065-a37b-abefdd03f416",
  "network-name": null,
  "element-count": 38,
  "element-types": {"Transceiver": 5, "Roadm": 5, "Fiber": 10, "Edfa": 18},
  "failed-fibers": [],
  "no-insert-edfas": false
}
```

The element list reflects the network after design. That includes amplifiers GNPy inserted
and long spans it split into sub-spans.

## Elements and runtime changes

```json
{
  "uid": "fiber (Alice → Bob)-",
  "type": "Fiber",
  "type-variety": "SSMF",
  "attributes": {"loss_coef": 0.2, "att_in": 0.0},
  "length-km": 75.0,
  "failed": false
}
```

The mutable attributes use GNPy's own names:

| Type | Attribute | Unit | Range |
|------|-----------|------|-------|
| Fiber | `loss_coef` | dB/km | 0 to 2 |
| Fiber | `att_in` | dB | 0 to 20 |
| Edfa | `gain_target` | dB | 0 to 40 |
| Edfa | `tilt_target` | dB | -5 to 5 |
| Edfa | `out_voa` | dB | 0 to 20 |
| Edfa | `nf0` | dB | 3 to 12 |
| Roadm | `target_pch_out_db` | dBm | -25 to 0 |

A value of `null` means GNPy left the field unset. For example, `nf0` is unset on
variable-gain amplifier types.

On a fiber, the pseudo-attribute `failed` (boolean) cuts or restores the fiber. A failed
fiber is excluded from path computation. A propagation through it returns
`"status": "link-failed"`.

`PATCH /networks/{id}/elements` takes `{uid: {attribute: value}}`. Every update is
validated before any is written: a single invalid value rejects the whole batch with `400`
and leaves the network untouched.

Parameter changes do not re-run the design. To re-equalise ROADMs and amplifier operating
points against the changed parameters, call `POST /networks/{id}/redesign`. GNPy caches
each span's design loss after the first design. The redesign endpoint clears that cache,
so a changed `loss_coef` or `att_in` does reach the amplifier gains.

## Paths

`POST /networks/{id}/paths` with:

```json
{"source": "trx Alice", "destination": "trx Bob", "k": 2}
```

```json
{
  "source": "trx Alice",
  "destination": "trx Bob",
  "paths": [
    {"uids": ["trx Alice", "roadm Alice", "east edfa in Alice to Bob", "fiber (Alice → Bob)-",
              "west edfa in Bob to Alice", "roadm Bob", "trx Bob"], "total-fiber-km": 75.0},
    {"uids": ["trx Alice", "roadm Alice", "east edfa in Alice to Dan", "..."], "total-fiber-km": 223.0}
  ],
  "blocking-reason": null
}
```

Paths are ordered by GNPy's edge weight (fiber length) and avoid failed fibers. Without
include constraints, the first path is the one `compute_constrained_path` returns. When no
path exists, `paths` is empty and `blocking-reason` is `NO_PATH`.

## Propagation

`POST /networks/{id}/propagate` with:

```json
{
  "path": ["trx Alice", "roadm Alice", "...", "trx Bob"],
  "spectrum": {"f-min": 191.3e12, "f-max": 195.1e12, "spacing": 50e9, "baud-rate": 32e9,
               "roll-off": 0.15, "tx-osnr": 40, "tx-power-dbm": 0, "delta-pdb": 0},
  "include-channels": false
}
```

Every `spectrum` field is optional. An omitted field takes its value from the equipment's
`SI`. Consecutive uids of `path` must be connected in the designed network.

```json
{
  "status": "ok",
  "failed-elements": [],
  "elements": [{"uid": "trx Alice", "type": "Transceiver"}, "..."],
  "total-fiber-km": 75.0,
  "channel-count": 76,
  "worst-channel": {
    "channel-number": 57,
    "frequency-hz": 194150000000000.0,
    "baud-rate-hz": 32000000000.0,
    "signal-power-dbm": -20.02,
    "gsnr-db": 24.08,
    "osnr-ase-db": 24.68,
    "osnr-ase-01nm-db": 28.76,
    "snr-nli-db": 33.01,
    "chromatic-dispersion-ps-nm": 1252.5,
    "pmd-ps": 0.35,
    "pdl-db": 0.0,
    "latency-ms": 0.37,
    "receiver-gsnr-db": 23.4,
    "receiver-osnr-ase-db": 23.91
  },
  "max-pmd-ps": 0.35,
  "latency-ms": 0.37
}
```

The worst channel is the one with the lowest line GSNR. With `"include-channels": true`,
`channels` lists the same fields for every received channel.

Two families of SNR are reported:

* `gsnr-db`, `osnr-ase-db` and `snr-nli-db` are the line values: what the spectrum carries
  after element-by-element propagation.
* `receiver-gsnr-db` and `receiver-osnr-ase-db` are present when the path ends at a
  transceiver. They add the transmitter OSNR and the add/drop OSNR of every ROADM crossed,
  as GNPy's planning engine does. With the same spectrum, their channel average equals the
  `SNR-bandwidth` and `OSNR-bandwidth` that `/path-request` reports.

Amplifiers drop channels outside their band, so `channel-count` can be smaller than the
number of channels launched.

## Errors

| Status | When |
|--------|------|
| `400` | Invalid topology or equipment, attribute not writable or out of range, unknown or disconnected uid in a path, path endpoints that are not transceivers, empty spectrum |
| `404` | Unknown network id or element uid |
| `422` | Body does not match the request schema |

## Operational notes

* Sessions live in the memory of one service process. They do not survive a restart and
  are not shared between replicas. A client should recreate its session when it gets a
  `404` for its network id.
* Requests on one session are serialised for parameter changes and redesign. Propagation
  works on deep copies of the path elements and can run concurrently.
* There is no limit on the number of sessions yet. Clients should `DELETE` sessions they no
  longer need.
