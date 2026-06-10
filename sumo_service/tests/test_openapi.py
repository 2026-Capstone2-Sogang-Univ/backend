from fastapi.testclient import TestClient

import app.main as main

# TestClient(app) 인스턴스를 with 없이 쓰면 lifespan(CLI 스레드/DB 풀)이 실행되지 않으므로
# SUMO/DB 없이 라우팅과 OpenAPI 스키마 서빙만 검증할 수 있다.


def test_live_server_serves_openapi_json():
    client = TestClient(main.app)
    resp = client.get("/openapi.json")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    spec = resp.json()
    assert spec["info"]["title"] == "SUMO Service"
    # 기존/신규 엔드포인트가 스키마에 포함되는지 확인.
    assert "/simulation/surge" in spec["paths"]
    assert "/simulation/demand-forecast" in spec["paths"]


def test_live_server_serves_swagger_and_redoc_docs():
    client = TestClient(main.app)

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def _ok_response_schema(spec: dict, path: str, method: str) -> dict:
    responses = spec["paths"][path][method]["responses"]
    ok = responses.get("200") or responses.get("201")
    return ok["content"]["application/json"]["schema"]


def test_main_display_endpoints_have_non_empty_response_schema():
    # response_model이 빠지면 200 응답 스키마가 빈 객체({})로 나간다.
    # 주요 표시 엔드포인트는 컴포넌트 스키마($ref)를 참조해야 한다.
    client = TestClient(main.app)
    spec = client.get("/openapi.json").json()

    cases = [
        ("/simulation/status", "get"),
        ("/simulation/kpi", "get"),
        ("/simulation/surge", "get"),
        ("/simulation/passengers", "get"),
        ("/simulation/demand-forecast", "get"),
        ("/simulation/start", "post"),
    ]
    for path, method in cases:
        schema = _ok_response_schema(spec, path, method)
        assert schema and schema != {}, f"{method.upper()} {path} has empty response schema"
        assert "$ref" in schema, f"{method.upper()} {path} response schema is not a model ref"
