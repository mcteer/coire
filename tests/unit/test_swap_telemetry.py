"""Native guards distinguish compressor swap-outs from filesystem page-outs."""

import ctypes
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coire_core.errors import TrainingUnavailable


@pytest.mark.parametrize("swapouts", [0, 7])
def test_darwin_swap_uses_compressor_counter_and_kernel_page_size(
    monkeypatch: pytest.MonkeyPatch, swapouts: int
) -> None:
    from coire_node import swap

    api = Mock()
    api.mach_host_self.return_value = 19
    api.mach_port_deallocate.return_value = 0

    def statistics(host: int, flavor: int, buffer: object, count: object) -> int:
        assert (host, flavor) == (19, 4)
        native = swap._VMStatistics64Prefix.from_buffer(buffer)
        native.pageouts = 99999
        native.swapouts = swapouts
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = 38
        return 0

    def page_size(host: int, result: object) -> int:
        assert host == 19
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32))[0] = 16384
        return 0

    api.host_statistics64.side_effect = statistics
    api.host_page_size.side_effect = page_size
    monkeypatch.setattr(swap, "_IS_DARWIN", True)
    monkeypatch.setattr(swap, "_darwin_api", lambda: (api, 11))
    monkeypatch.setattr(swap.psutil, "swap_memory", lambda: SimpleNamespace(used=123, sout=99999))
    observed = swap.read_swap_sample()
    assert observed == swap.SwapSample(used=123, out=swapouts * 16384)
    api.mach_port_deallocate.assert_called_once_with(11, 19)


@pytest.mark.parametrize("fault", ["syscall", "short", "oversize", "page_size", "release"])
def test_unknown_native_swap_refuses_and_releases_its_host_port(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from coire_node import swap

    api = Mock()
    api.mach_host_self.return_value = 19
    api.mach_port_deallocate.return_value = 1 if fault == "release" else 0

    def statistics(host: int, flavor: int, buffer: object, count: object) -> int:
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = (
            1 if fault == "short" else 2048 if fault == "oversize" else 38
        )
        return 1 if fault == "syscall" else 0

    def page_size(host: int, result: object) -> int:
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32))[0] = (
            0 if fault == "page_size" else 16384
        )
        return 0

    api.host_statistics64.side_effect = statistics
    api.host_page_size.side_effect = page_size
    monkeypatch.setattr(swap, "_IS_DARWIN", True)
    monkeypatch.setattr(swap, "_darwin_api", lambda: (api, 11))
    monkeypatch.setattr(swap.psutil, "swap_memory", lambda: SimpleNamespace(used=0, sout=0))
    with pytest.raises(TrainingUnavailable):
        swap.read_swap_sample()
    api.mach_port_deallocate.assert_called_once_with(11, 19)


def test_linux_retains_actual_swap_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from coire_node import swap

    monkeypatch.setattr(swap, "_IS_DARWIN", False)
    monkeypatch.setattr(swap.psutil, "swap_memory", lambda: SimpleNamespace(used=12, sout=34))
    assert swap.read_swap_sample() == swap.SwapSample(used=12, out=34)


@pytest.mark.skipif(sys.platform != "darwin", reason="actual Darwin kernel counterpart")
def test_actual_kernel_swap_sample_is_available() -> None:
    from coire_node.swap import read_swap_sample

    def kernel_swap_bytes() -> int:
        result = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, text=True, check=True, timeout=5
        )
        page = re.search(r"page size of (\d+) bytes", result.stdout)
        swapped = re.search(r"^Swapouts:\s+(\d+)\.", result.stdout, re.MULTILINE)
        assert page is not None and swapped is not None
        return int(page[1]) * int(swapped[1])

    before = kernel_swap_bytes()
    sample = read_swap_sample()
    after = kernel_swap_bytes()
    assert sample.used >= 0 and before <= sample.out <= after


@pytest.mark.skipif(sys.platform != "darwin", reason="independent Darwin SDK ABI counterpart")
def test_native_counter_offsets_match_the_installed_sdk(tmp_path: Path) -> None:
    from coire_node.swap import _VMStatistics64Prefix

    source = tmp_path / "swap-abi.c"
    executable = tmp_path / "swap-abi"
    source.write_text(
        "#include <stddef.h>\n#include <stdio.h>\n#include <mach/vm_statistics.h>\n"
        'int main(void) { printf("%zu %zu\\n", '
        "offsetof(vm_statistics64_data_t, pageouts), "
        "offsetof(vm_statistics64_data_t, swapouts)); return 0; }\n"
    )
    subprocess.run(
        ["/usr/bin/clang", "-Wall", "-Werror", str(source), "-o", str(executable)],
        capture_output=True,
        check=True,
        timeout=30,
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, check=True, timeout=5
    )
    assert tuple(map(int, result.stdout.split())) == (
        _VMStatistics64Prefix.pageouts.offset,
        _VMStatistics64Prefix.swapouts.offset,
    )
