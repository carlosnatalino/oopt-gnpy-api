# -*- coding: utf-8 -*-

"""Allow-list of runtime-mutable attributes on designed GNPy elements.

Ported from TwinLight's ``physics/element_params.py`` so that a twin moving its
GNPy calls behind this API keeps exactly the same read/write semantics. Each
spec captures:

* the user-facing value type and accepted range,
* where to *read* the current value from (a dotted attribute path),
* where to *write* the new value to (several paths when GNPy mirrors an
  operational field onto the element instance as well as its sub-object), and
* the coerce/display transforms needed when GNPy stores the value in different
  units than the user sees (``Fiber.loss_coef`` is exposed in dB/km but stored
  as an ndarray in dB/m on ``params._loss_coef``).
"""

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np
from gnpy.core.elements import Edfa, Fiber, Roadm

from gnpyapi.core.exception.param_validation_error import ParamValidationError


@dataclass(frozen=True)
class AttrSpec:
    """One writable physical-layer attribute."""

    py_type: type
    read_path: str
    write_paths: Tuple[str, ...]
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    unit: str = ''
    coerce: Callable[[Any], Any] = float
    display: Callable[[Any], Any] = float


def _walk(obj: Any, dotted: str) -> Tuple[Any, str]:
    """Return (parent_obj, leaf_attr_name) for a dotted attribute path."""
    parts = dotted.split('.')
    parent = obj
    for part in parts[:-1]:
        parent = getattr(parent, part)
    return parent, parts[-1]


def _loss_coef_display(value: Any) -> float:
    """``params._loss_coef`` is an ndarray in dB/m; show it as a dB/km float."""
    return float(np.asarray(value).flat[0]) * 1e3


def _loss_coef_coerce(value: Any) -> np.ndarray:
    """User-facing dB/km float -> internal 0-d ndarray in dB/m."""
    return np.asarray(float(value) * 1e-3)


# Bounds are deliberately loose: they reject clearly nonsensical inputs (e.g. a
# negative loss) without preventing legitimate operator experiments.
ALLOWED: Dict[type, Dict[str, AttrSpec]] = {
    Fiber: {
        'loss_coef': AttrSpec(py_type=float, read_path='params._loss_coef', write_paths=('params._loss_coef',),
                              min_value=0.0, max_value=2.0, unit='dB/km',
                              coerce=_loss_coef_coerce, display=_loss_coef_display),
        'att_in': AttrSpec(py_type=float, read_path='params.att_in', write_paths=('params.att_in',),
                           min_value=0.0, max_value=20.0, unit='dB'),
    },
    Edfa: {
        'gain_target': AttrSpec(py_type=float, read_path='operational.gain_target',
                                write_paths=('operational.gain_target', 'effective_gain'),
                                min_value=0.0, max_value=40.0, unit='dB'),
        'tilt_target': AttrSpec(py_type=float, read_path='operational.tilt_target',
                                write_paths=('operational.tilt_target', 'tilt_target'),
                                min_value=-5.0, max_value=5.0, unit='dB'),
        'out_voa': AttrSpec(py_type=float, read_path='operational.out_voa',
                            write_paths=('operational.out_voa', 'out_voa'),
                            min_value=0.0, max_value=20.0, unit='dB'),
        'nf0': AttrSpec(py_type=float, read_path='params.nf0', write_paths=('params.nf0',),
                        min_value=3.0, max_value=12.0, unit='dB'),
    },
    Roadm: {
        'target_pch_out_db': AttrSpec(py_type=float, read_path='params.target_pch_out_db',
                                      write_paths=('params.target_pch_out_db', 'target_pch_out_dbm'),
                                      min_value=-25.0, max_value=0.0, unit='dBm'),
    },
}


def specs_for(element: Any) -> Dict[str, AttrSpec]:
    """Return the specs for the element's exact class (subclasses such as RamanFiber are not covered)."""
    return ALLOWED.get(type(element), {})


def read_attr(element: Any, attr: str) -> Any:
    """Read an allow-listed attribute as a JSON-friendly value (None when GNPy left it unset)."""
    spec = specs_for(element).get(attr)
    if spec is None:
        raise ParamValidationError(f'{type(element).__name__} {element.uid!r}: attribute {attr!r} is not exposed')
    parent, leaf = _walk(element, spec.read_path)
    raw = getattr(parent, leaf)
    if raw is None:
        # Equipment-library amplifiers may leave optional fields unset (e.g. nf0 on a
        # variable-gain type that uses nf_min/nf_max instead).
        return None
    return spec.display(raw)


def read_all(element: Any) -> Dict[str, Any]:
    """Return every allow-listed attribute of the element with its current value."""
    return {attr: read_attr(element, attr) for attr in specs_for(element)}


def validate_attr(element: Any, attr: str, value: Any) -> AttrSpec:
    """Check that ``value`` may be written to ``attr`` without writing it."""
    specs = specs_for(element)
    spec = specs.get(attr)
    name = f'{type(element).__name__} {element.uid!r}'
    if spec is None:
        raise ParamValidationError(f'{name}: attribute {attr!r} is not writable. Allowed: {sorted(specs)!r}')
    # bools are ints, so reject them explicitly or True would silently become 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParamValidationError(f'{name}.{attr}: expected {spec.py_type.__name__}, got {type(value).__name__}')
    if not math.isfinite(value):
        raise ParamValidationError(f'{name}.{attr}: {value} is not a finite number')
    if spec.min_value is not None and value < spec.min_value:
        raise ParamValidationError(f'{name}.{attr}={value} below minimum {spec.min_value} {spec.unit}')
    if spec.max_value is not None and value > spec.max_value:
        raise ParamValidationError(f'{name}.{attr}={value} above maximum {spec.max_value} {spec.unit}')
    return spec


def write_attr(element: Any, attr: str, value: Any):
    """Validate ``value`` and write it through to every path of the spec."""
    spec = validate_attr(element, attr, value)
    coerced = spec.coerce(value)
    for path in spec.write_paths:
        parent, leaf = _walk(element, path)
        setattr(parent, leaf, coerced)


def schema() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Machine-readable description of the allow-list: element type -> attribute -> spec."""
    return {
        cls.__name__: {
            attr: {'type': spec.py_type.__name__, 'unit': spec.unit, 'min': spec.min_value, 'max': spec.max_value}
            for attr, spec in attrs.items()
        }
        for cls, attrs in ALLOWED.items()
    }
