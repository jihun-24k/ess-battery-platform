"""과거 MVP의 학습 문서 조회. 고정된 사건 종류를 로컬 문서와 연결하고 내용·해시를
반환한다.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MANUAL_DIR = Path(__file__).parent / "manuals"
MANUALS = {"measurement_anomaly": "measurement_anomaly.md"}


@dataclass(frozen=True)
class Manual:
    path: str
    sha256: str
    content: str
    action: str = "mock_cooling_boost"
    learning_example: bool = True


def get_manual(kind: str, directory: Path = DEFAULT_MANUAL_DIR) -> Manual | None:
    """입력: 사건 종류·학습 문서 디렉터리. 반환: 내용·경로·hash를 담은 Manual 또는 None."""
    filename = MANUALS.get(kind)
    if filename is None:
        return None
    path = directory / filename
    try:
        raw = path.read_bytes()
        content = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    if not content.strip():
        return None
    return Manual(str(path.resolve()), hashlib.sha256(raw).hexdigest(), content)
