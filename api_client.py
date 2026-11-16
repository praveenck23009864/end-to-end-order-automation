"""Local API client with a DPAPI-backed credential and safe failure messages."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from sensitive_data import get_api_token

API_URL = "http://127.0.0.1:8000"


class ApiError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Prevent redirects so the bearer credential
        # stays with the intended local service.
        return None


def request(method, path, data=None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = Request(
        API_URL + path,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + get_api_token(),
        },
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise ApiError(
                "API authentication failed. Use the same Windows account for API and runner."
            ) from None
        if error.code == 409:
            raise ApiError(
                "API conflict. Check the existing run before retrying."
            ) from None
        raise ApiError(f"API returned HTTP {error.code}.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ApiError("Cannot reach or read the local API on port 8000.") from None


def list_order_ids():
    result = []
    offset = 0
    while True:
        batch = request("GET", f"/orders?limit=100&offset={offset}")
        result.extend(item["id"] for item in batch)
        if len(batch) < 100:
            return result
        offset += 100


def load_order(key):
    return request("GET", "/orders/" + quote(key, safe=""))["data"]


def begin_run(key, run_id):
    return request(
        "POST", "/orders/" + quote(key, safe="") + "/runs", {"run_id": run_id}
    )


def progress(run_id, status, stage, error_code=None):
    return request(
        "PATCH", "/runs/" + quote(run_id, safe=""),
        {"status": status, "stage": stage, "error_code": error_code},
    )
