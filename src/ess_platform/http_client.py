"""서버 간 POST 요청과 응답 크기·타임아웃을 공통으로 처리한다."""

import json
from urllib.request import Request, urlopen


def request_json(url: str, payload: dict) -> dict:
    """입력: 서버 URL·POST할 dict. 반환: JSON object를 해석한 dict.

    5초 timeout과 1 MB 응답 제한을 적용하며 HTTP·통신·형식 오류는 호출자에게
    전달한다.
    """
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("internal response exceeded 1 MB")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("internal response must be an object")
    return result
