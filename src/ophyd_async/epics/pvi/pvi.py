import re
from dataclasses import dataclass
from inspect import isclass
from typing import (
    Callable,
    Dict,
    FrozenSet,
    Literal,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from ophyd_async.core import Device, DeviceVector, SimSignalBackend
from ophyd_async.core.signal import Signal
from ophyd_async.core.utils import DEFAULT_TIMEOUT
from ophyd_async.epics._backend._p4p import PvaSignalBackend
from ophyd_async.epics.signal.signal import (
    epics_signal_r,
    epics_signal_rw,
    epics_signal_w,
    epics_signal_x,
)

T = TypeVar("T")
Access = FrozenSet[
    Union[Literal["r"], Literal["w"], Literal["rw"], Literal["x"], Literal["d"]]
]


def _strip_number_from_string(string: str) -> Tuple[str, Optional[int]]:
    match = re.match(r"(.*?)(\d*)$", string)
    assert match

    name = match.group(1)
    number = match.group(2) or None
    if number:
        number = int(number)
    return name, number


def _strip_union(field: Union[Union[T], T]) -> T:
    if get_origin(field) is Union:
        args = get_args(field)
        for arg in args:
            if arg is not type(None):
                return arg
    return field


def _strip_device_vector(field: Union[Type[Device]]) -> Tuple[bool, Type[Device]]:
    if get_origin(field) is DeviceVector:
        return True, get_args(field)[0]
    return False, field


@dataclass
class PVIEntry:
    """
    A dataclass to represent a single entry in the PVI table.
    This could either be a signal or a sub-table.
    """

    sub_entries: Dict[str, Union[Dict[int, "PVIEntry"], "PVIEntry"]]
    pvi_pv: Optional[str] = None
    device: Optional[Device] = None
    common_device_type: Optional[Type[Device]] = None


def _verify_common_blocks(entry: PVIEntry, common_device: Type[Device]):
    if not entry.sub_entries:
        return
    common_sub_devices = get_type_hints(common_device)
    for sub_name, sub_device in common_sub_devices.items():
        if sub_name in ("_name", "parent"):
            continue
        assert entry.sub_entries
        if sub_name not in entry.sub_entries and get_origin(sub_device) is not Optional:
            raise RuntimeError(
                f"sub device `{sub_name}:{type(sub_device)}` was not provided by pvi"
            )
        if isinstance(entry.sub_entries[sub_name], dict):
            for sub_sub_entry in entry.sub_entries[sub_name].values():  # type: ignore
                _verify_common_blocks(sub_sub_entry, sub_device)  # type: ignore
        else:
            _verify_common_blocks(
                entry.sub_entries[sub_name], sub_device  # type: ignore
            )


_pvi_mapping: Dict[FrozenSet[str], Callable[..., Signal]] = {
    frozenset({"r", "w"}): lambda dtype, read_pv, write_pv: epics_signal_rw(
        dtype, "pva://" + read_pv, "pva://" + write_pv
    ),
    frozenset({"rw"}): lambda dtype, read_write_pv: epics_signal_rw(
        dtype, "pva://" + read_write_pv, write_pv="pva://" + read_write_pv
    ),
    frozenset({"r"}): lambda dtype, read_pv: epics_signal_r(dtype, "pva://" + read_pv),
    frozenset({"w"}): lambda dtype, write_pv: epics_signal_w(
        dtype, "pva://" + write_pv
    ),
    frozenset({"x"}): lambda _, write_pv: epics_signal_x("pva://" + write_pv),
}


def _parse_type(
    is_pvi_table: bool,
    number_suffix: Optional[int],
    common_device_type: Optional[Type[Device]],
):
    if common_device_type:
        # pre-defined type
        device_type = _strip_union(common_device_type)
        is_device_vector, device_type = _strip_device_vector(device_type)

        if ((origin := get_origin(device_type)) and issubclass(origin, Signal)) or (
            isclass(device_type) and issubclass(device_type, Signal)
        ):
            # if device_type is of the form `Signal` or `Signal[type]`
            is_signal = True
            signal_dtype = get_args(device_type)[0]
        else:
            is_signal = False
            signal_dtype = None

    elif is_pvi_table:
        # is a block, we can make it a DeviceVector if it ends in a number
        is_device_vector = number_suffix is not None
        is_signal = False
        signal_dtype = None
        device_type = Device
    else:
        # is a signal, signals aren't stored in DeviceVectors unless
        # they're defined as such in the common_device_type
        is_device_vector = False
        is_signal = True
        signal_dtype = None
        device_type = Signal

    return is_device_vector, is_signal, signal_dtype, device_type


def _sim_common_blocks(device: Device, stripped_type: Optional[Type] = None):
    device_t = stripped_type or type(device)
    for sub_name, sub_device_t in get_type_hints(device_t).items():
        if sub_name in ("_name", "parent"):
            continue

        # we'll take the first type in the union which isn't NoneType
        sub_device_t = _strip_union(sub_device_t)
        is_device_vector, sub_device_t = _strip_device_vector(sub_device_t)
        is_signal = (
            (origin := get_origin(sub_device_t)) and issubclass(origin, Signal)
        ) or (issubclass(sub_device_t, Signal))

        # TODO: worth coming back to all this code once 3.9 is gone and we can use
        # match statments: https://github.com/bluesky/ophyd-async/issues/180
        if is_device_vector:
            if is_signal:
                signal_type = args[0] if (args := get_args(sub_device_t)) else None
                sub_device_1 = sub_device_t(SimSignalBackend(signal_type, sub_name))
                sub_device_2 = sub_device_t(SimSignalBackend(signal_type, sub_name))
                sub_device = DeviceVector(
                    {
                        1: sub_device_1,
                        2: sub_device_2,
                    }
                )
            else:
                sub_device = DeviceVector(
                    {
                        1: sub_device_t(),
                        2: sub_device_t(),
                    }
                )
            for value in sub_device.values():
                value.parent = sub_device

        elif is_signal:
            signal_type = args[0] if (args := get_args(sub_device_t)) else None
            sub_device = sub_device_t(SimSignalBackend(signal_type, sub_name))
        else:
            sub_device = sub_device_t()

        if not is_signal:
            if is_device_vector:
                for sub_device_in_vector in sub_device.values():
                    _sim_common_blocks(sub_device_in_vector, stripped_type=sub_device_t)
            else:
                _sim_common_blocks(sub_device, stripped_type=sub_device_t)

        setattr(device, sub_name, sub_device)
        sub_device.parent = device


async def _get_pvi_entries(entry: PVIEntry, timeout=DEFAULT_TIMEOUT):
    if not entry.pvi_pv or not entry.pvi_pv.endswith(":PVI"):
        raise RuntimeError("Top level entry must be a pvi table")

    pvi_table_signal_backend: PvaSignalBackend = PvaSignalBackend(
        None, entry.pvi_pv, entry.pvi_pv
    )
    await pvi_table_signal_backend.connect(
        timeout=timeout
    )  # create table signal backend

    pva_table = (await pvi_table_signal_backend.get_value())["pvi"]
    common_device_type_hints = (
        get_type_hints(entry.common_device_type) if entry.common_device_type else {}
    )

    for sub_name, pva_entries in pva_table.items():
        pvs = list(pva_entries.values())
        is_pvi_table = len(pvs) == 1 and pvs[0].endswith(":PVI")
        sub_name_split, sub_number_split = _strip_number_from_string(sub_name)
        is_device_vector, is_signal, signal_dtype, device_type = _parse_type(
            is_pvi_table,
            sub_number_split,
            common_device_type_hints.get(sub_name_split),
        )
        if is_signal:
            device = _pvi_mapping[frozenset(pva_entries.keys())](signal_dtype, *pvs)
        else:
            device = device_type()

        sub_entry = PVIEntry(
            device=device, common_device_type=device_type, sub_entries={}
        )

        if is_device_vector:
            # If device vector then we store sub_name -> {sub_number -> sub_entry}
            # and aggregate into `DeviceVector` in `_set_device_attributes`
            sub_number_split = 1 if sub_number_split is None else sub_number_split
            if sub_name_split not in entry.sub_entries:
                entry.sub_entries[sub_name_split] = {}
            entry.sub_entries[sub_name_split][
                sub_number_split
            ] = sub_entry  # type: ignore
        else:
            entry.sub_entries[sub_name] = sub_entry

        if is_pvi_table:
            sub_entry.pvi_pv = pvs[0]
            await _get_pvi_entries(sub_entry)

    if entry.common_device_type:
        _verify_common_blocks(entry, entry.common_device_type)


def _set_device_attributes(entry: PVIEntry):
    for sub_name, sub_entry in entry.sub_entries.items():
        if isinstance(sub_entry, dict):
            sub_device = DeviceVector()  # type: ignore
            for key, device_vector_sub_entry in sub_entry.items():
                sub_device[key] = device_vector_sub_entry.device
                if device_vector_sub_entry.pvi_pv:
                    _set_device_attributes(device_vector_sub_entry)
                # Set the device vector entry to have the device vector as a parent
                device_vector_sub_entry.device.parent = sub_device  # type: ignore
        else:
            sub_device = sub_entry.device  # type: ignore
            if sub_entry.pvi_pv:
                _set_device_attributes(sub_entry)

        sub_device.parent = entry.device
        setattr(entry.device, sub_name, sub_device)


async def fill_pvi_entries(
    device: Device, root_pv: str, timeout=DEFAULT_TIMEOUT, sim=False
):
    """
    Fills a ``device`` with signals from a the ``root_pvi:PVI`` table.

    If the device names match with parent devices of ``device`` then types are used.
    """
    if sim:
        # set up sim signals for the common annotations
        _sim_common_blocks(device)
    else:
        # check the pvi table for devices and fill the device with them
        root_entry = PVIEntry(
            pvi_pv=root_pv,
            device=device,
            common_device_type=type(device),
            sub_entries={},
        )
        await _get_pvi_entries(root_entry, timeout=timeout)
        _set_device_attributes(root_entry)

    # We call set name now the parent field has been set in all of the
    # introspect-initialized devices. This will recursively set the names.
    device.set_name(device.name)
