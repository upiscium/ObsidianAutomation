"""Explicit optional read sources, not a runtime registry or execution port.

B3FakeAdapter and B5 ReadPort only inspect test-only mock state. Accepting their
exact types does not create production readers or authenticate effect evidence.
The existing v1 composer remains the final DTO/redaction boundary.
"""
from __future__ import annotations

from .webui_b1_adapter import B1ReadAdapter
from .webui_b3_adapter import B3FakeAdapter
from .webui_b5_ports import ReadPort


def validate_optional_read_ports(ports: object) -> dict[str, object]:
    """Copy a built-in map without observing or looking up supplied methods.

    Exact types deliberately exclude duck typing, subclasses and the B1/B5
    manual simulation ports. No B2/B4 ownership can enter through this map.
    Validate the entire map before the first possible observe lookup.
    """
    if (type(ports) is not dict or
            not all(type(key) is str and key in ("b1", "b3", "b5") for key in ports)):
        raise TypeError("invalid_workflow_control_read_ports")
    for key, approved in (("b1", B1ReadAdapter), ("b3", B3FakeAdapter), ("b5", ReadPort)):
        if key in ports and type(ports[key]) is not approved:
            raise TypeError("invalid_workflow_control_read_ports")
    return dict(ports)


def materialize_optional_read_ports(ports: dict[str, object]) -> dict[str, object]:
    """Observe each attached source once, in B1/B3/B5 order, for one GET.

    Omission stays omission (pending). An attempted but failed/invalid read is
    False, the existing composer's unavailable sentinel, never missing/healthy.
    No errors or invalid non-dict objects escape to the response. Nested DTO
    validation belongs to read_control_catalog(optional_snapshots=...), not here.
    Its outer clock must be sampled after these observations, not before them.
    """
    attached = validate_optional_read_ports(ports)
    snapshots: dict[str, object] = {}
    for key in ("b1", "b3", "b5"):
        if key not in attached:
            continue
        try:
            value = attached[key].observe()
            snapshots[key] = value if type(value) is dict else False
        except Exception:
            snapshots[key] = False
    return snapshots
