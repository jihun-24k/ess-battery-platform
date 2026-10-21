"""과거 MAT 재생 코드. 선택한 셀의 사이클·샘플을 읽고 품질 검증 후 윈도를 생성한다.

원천 파일을 변경하지 않으며 현재는 누락된 battery_etl에 의존한다.
"""

from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from itertools import islice
from math import isfinite, ulp
from pathlib import Path

from battery_etl.errors import ConfigurationError, StructureError
from battery_etl.extract import iter_cells, iter_cycles


@dataclass(frozen=True)
class Measurement:
    cycle_index: int
    sample_index: int
    source_time: float
    temperature: float
    voltage: float
    current: float
    simulated: bool = False


@dataclass(frozen=True)
class ReplayIssue:
    code: str
    cycle_index: int
    sample_index: int
    detail: str


@dataclass(frozen=True)
class Window:
    measurements: tuple[Measurement, ...]


def available_cycles(path: Path, cell_index: int) -> int:
    """입력: MAT 경로·셀 위치. 반환: 선택한 셀에 기록된 사이클 개수. 잘못된 위치는 예외."""
    if cell_index < 0:
        raise ConfigurationError("cell index must be nonnegative")
    with closing(iter_cells(path)) as cells:
        cell = next(islice(cells, cell_index, cell_index + 1), None)
    if cell is None:
        raise ConfigurationError("cell index is outside the source batch")
    return cell.cycle_count


def iter_measurements(
    path: Path, cell_index: int, start_cycle: int, cycle_count: int
) -> Iterator[Measurement]:
    """입력: MAT 경로·셀 위치·시작 사이클·개수. 반환: 원천 순서의 Measurement iterator.

    T/V/I/t 길이가 다르면 예외를 발생시키며 원천 단위와 파일은 변경하지 않는다.
    """
    if cell_index < 0 or start_cycle < 0 or cycle_count < 1:
        raise ConfigurationError("invalid cell/cycle selection")
    with closing(iter_cycles(path, cell_index)) as cycles:
        for cycle in islice(cycles, start_cycle, start_cycle + cycle_count):
            keys = ("T", "V", "I", "t")
            if len({cycle.waves[k].size for k in keys}) != 1:
                raise StructureError(
                    f"cell {cell_index}, cycle {cycle.index}: T/V/I/t length mismatch"
                )
            for index in range(cycle.waves["t"].size):
                yield Measurement(
                    cycle.index,
                    index,
                    float(cycle.waves["t"][index]),
                    float(cycle.waves["T"][index]),
                    float(cycle.waves["V"][index]),
                    float(cycle.waves["I"][index]),
                )


def inject_temperature_offset(
    measurements: Iterable[Measurement], offset: float, after_samples: int
) -> Iterator[Measurement]:
    """입력: 측정 iterator·온도 가산값·시작 샘플. 반환: 합성 표시를 포함한 복사본
    iterator.

    평가 데이터만 변경하며 원본 객체와 파일을 수정하지 않는다.
    """
    if not isfinite(offset) or offset <= 0 or after_samples < 0:
        raise ConfigurationError("demo offset must be finite/positive and start nonnegative")
    for item in measurements:
        if item.sample_index >= after_samples:
            yield replace(item, temperature=item.temperature + offset, simulated=True)
        else:
            yield item


def iter_windows(
    measurements: Iterable[Measurement], size: int, stride: int
) -> Iterator[Window | ReplayIssue]:
    """입력: 측정 iterator·윈도 크기·이동 간격. 반환: Window 또는 ReplayIssue iterator.

    사이클 변경·결측·시간 역전·샘플 누락에서 이력을 비워 잘못된 구간을 연결하지
    않는다.
    """
    if size < 2 or not 1 <= stride <= size:
        raise ConfigurationError("window >= 2 and 1 <= stride <= window required")
    history: deque[Measurement] = deque(maxlen=size)
    previous: Measurement | None = None
    count = 0
    for item in measurements:
        if previous is not None and item.cycle_index != previous.cycle_index:
            history.clear()
            count = 0
            previous = None
        values = (item.source_time, item.temperature, item.voltage, item.current)
        if not all(
            isinstance(v, (float, int)) and not isinstance(v, bool) and isfinite(v) for v in values
        ):
            history.clear()
            count = 0
            previous = None
            yield ReplayIssue(
                "INVALID_MEASUREMENT",
                item.cycle_index,
                item.sample_index,
                "T/V/I/t must be finite numbers; window reset",
            )
            continue
        if previous is not None:
            tolerance = 8 * ulp(max(abs(item.source_time), abs(previous.source_time), 1.0))
            code = None
            if item.sample_index != previous.sample_index + 1:
                code = "SAMPLE_GAP"
            elif item.source_time < previous.source_time - tolerance:
                code = "TIME_REVERSED"
            if code:
                history.clear()
                count = 0
                yield ReplayIssue(code, item.cycle_index, item.sample_index, "window reset")
                if code == "TIME_REVERSED":
                    previous = None
                    continue
        history.append(item)
        previous = item
        count += 1
        if len(history) == size and (count - size) % stride == 0:
            yield Window(tuple(history))
