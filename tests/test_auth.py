"""Bearer auth: resolution, header rules, masking (pure functions)."""
from app import http_engine as he


def spec(**kw):
    base = dict(id=1, name="r", method="POST", url="{{base_url}}/api/x", token_template="{{bearer_token}}")
    base.update(kw)
    return he.RequestSpec(**base)


def test_nearest_token_wins():
    folders = [{"type": "inherit"}, {"type": "bearer", "token": "{{bearer_token}}"}]
    assert he.resolve_token_template({"type": "inherit"}, folders) == ("{{bearer_token}}", "folder (+1 up)")
    assert he.resolve_token_template({"type": "bearer", "token": "override"}, folders) == ("override", "request")
    assert he.resolve_token_template({}, [{"type": "bearer", "token": "near"}, {"type": "bearer", "token": "far"}])[0] == "near"
    assert he.resolve_token_template({}, [{"type": "inherit"}]) == (None, "none")


def test_prepare_sets_bearer_header_and_renders_vars():
    p = he.prepare(spec(params=[{"key": "page", "value": "1"}]),
                   {"base_url": "http://h", "bearer_token": "abc123"},
                   item={"body": {"a": 1}, "query": {"page": 2, "v": "x y"}})
    assert p.ok
    assert p.headers == {"Authorization": "Bearer abc123", "Content-Type": "application/json"}  # no Accept
    assert p.url == "http://h/api/x?page=2&v=x+y"
    assert p.body == {"a": 1}


def test_manual_authorization_header_rejected():
    assert he.check_headers([{"key": "authorization", "value": "Bearer x"}])
    assert not he.check_headers([{"key": "X-Trace", "value": "1"}])
    p = he.prepare(spec(headers=[{"key": "Authorization", "value": "Bearer manual"}]),
                   {"base_url": "http://h", "bearer_token": "real"},
                   item={"body": {}, "headers": {"AUTHORIZATION": "Bearer fromscript"}})
    assert p.headers["Authorization"] == "Bearer real"
    assert len(p.warnings) == 2


def test_empty_or_missing_token_blocks():
    p = he.prepare(spec(), {"base_url": "http://h", "bearer_token": ""}, item={"body": {}})
    assert not p.ok and p.token_problem and "empty" in p.errors[0]
    p = he.prepare(spec(), {"base_url": "http://h"}, item={"body": {}})
    assert not p.ok and p.token_problem and "undefined" in p.errors[0]
    p = he.prepare(spec(token_template=None), {"base_url": "http://h"}, item={"body": {}})
    assert not p.ok and p.token_problem


def test_get_has_no_body_and_curl_masks():
    p = he.prepare(spec(method="GET"), {"base_url": "http://h", "bearer_token": "supersecret"}, item={"body": {"x": 1}})
    assert not p.has_body and p.warnings
    p = he.prepare(spec(), {"base_url": "http://h", "bearer_token": "supersecret"}, item={"body": {"k": "supersecret"}})
    curl = he.to_curl(p, ["supersecret"])
    assert "supersecret" not in curl and "Bearer ****" in curl and "--data '" in curl
    assert "Accept" not in curl
    assert curl.startswith("curl --request POST \\\n  --url 'http://h/api/x'")
    assert "--header 'Content-Type: application/json'" in curl and "--data '{\"k\":\"****\"}'" in curl


def test_get_sends_only_authorization():
    p = he.prepare(spec(method="GET"), {"base_url": "http://h", "bearer_token": "t0ken"})
    assert p.headers == {"Authorization": "Bearer t0ken"}


def jwt(payload: dict, sig: str = "c2ln") -> str:
    import base64, json
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return enc({"alg": "RS256", "typ": "JWT"}) + "." + enc(payload) + "." + sig


def test_pasted_token_is_cleaned():
    t = jwt({"exp": 4102444800})
    for pasted in (f"Bearer {t}", f"'Authorization: Bearer {t}'", f"  {t[:40]}\n{t[40:]}  ", f"bearer bearer {t}"):
        p = he.prepare(spec(), {"base_url": "http://h", "bearer_token": pasted}, item={"body": {}})
        assert p.ok and p.headers["Authorization"] == f"Bearer {t}", pasted


def test_truncated_jwt_blocks_and_explains():
    t = jwt({"exp": 4102444800})
    cut = t[: t.rindex(".")]          # lost the signature part, like a 1024-char cut
    p = he.prepare(spec(), {"base_url": "http://h", "bearer_token": cut}, item={"body": {}})
    assert not p.ok and p.token_problem and "looks incomplete" in p.errors[0] and "Authorization" not in p.headers
    assert he.token_info(t)["jwt"] and he.token_info(t)["expired"] is False
    assert he.token_info(jwt({"exp": 1}))["expired"] is True
    assert he.token_info("opaque-token-123") == {"length": 16, "jwt": False, "segments": 1}


def test_success_criteria():
    ok = he.evaluate(he.HttpResult(200, 1, json={"code": 200}), he.DEFAULT_SETTINGS["success"])
    assert ok == (True, "")
    assert he.evaluate(he.HttpResult(200, 1, json={"code": 400, "msg": "bad"}), he.DEFAULT_SETTINGS["success"]) == (False, "code 400 != 200")
    assert he.evaluate(he.HttpResult(202, 1, json={"code": 202}), {"require_2xx": True, "code_field": "code", "code_values": [200, 202]})[0]
    assert not he.evaluate(he.HttpResult(500, 1, json={}), he.DEFAULT_SETTINGS["success"])[0]
    assert he.evaluate(he.HttpResult(200, 1, json={"code": "x"}), he.DEFAULT_SETTINGS["success"])[0]  # non-numeric ignored
    assert he.evaluate(he.HttpResult(None, 1, error="Timeout after 30s"), {})[1] == "Timeout after 30s"


def test_settings_normalize_code_values():
    assert he.normalize_settings({"success": {"code_value": 202}})["success"]["code_values"] == [202]
    assert he.normalize_settings({"success": {"code_values": ["200", 202, "x"]}})["success"]["code_values"] == [200, 202]
    assert he.evaluate(he.HttpResult(200, 1, json={"code": 500}),
                       {"require_2xx": True, "code_field": "code", "code_values": [200, 202]})[1] == "code 500 != 200/202"


def test_settings_never_retry_4xx():
    s = he.normalize_settings({"retry": {"max_retries": 3, "on_status": [503, 400, 401, 409, "502"]}})
    assert s["retry"]["on_status"] == [502, 503]
