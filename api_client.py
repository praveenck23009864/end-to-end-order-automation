"""Local API client with a DPAPI-backed credential and safe failure messages."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.parse import urlparse
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
    parsed_api = urlparse(API_URL)
    if parsed_api.scheme != "https" and parsed_api.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ApiError("Sensitive API communication requires HTTPS outside this computer.")
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
        if error.code == 404:
            raise ApiError("API record not found. Verify the exact order or run ID.") from None
        if error.code == 409:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("detail")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = None
            if isinstance(detail, str) and detail.strip():
                raise ApiError(f"API conflict: {detail.strip()}") from None
            raise ApiError("API conflict. Check the existing run before retrying.") from None
        if error.code == 422:
            raise ApiError("API rejected the request. Check its fields and workflow stage.") from None
        if error.code >= 500:
            raise ApiError(f"API server failure (HTTP {error.code}).") from None
        raise ApiError(f"API request failed (HTTP {error.code}).") from None
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


def list_runs(*, order_id=None, status=None):
    """List existing runs using the API's paginated GET /runs route."""
    results = []
    offset = 0
    while True:
        batch = request("GET", f"/runs?limit=100&offset={offset}")
        results.extend(
            run for run in batch
            if (order_id is None or str(run.get("order_id")) == str(order_id))
            and (status is None or run.get("status") == status)
        )
        if len(batch) < 100:
            return results
        offset += 100


def get_sensitive_public_key():
    return request("GET", "/crypto/public-key")


def submit_sensitive_payload(run_id, compact_jwe):
    # The request body contains only the compact JWE ciphertext.
    return request(
        "POST", "/runs/" + quote(run_id, safe="") + "/sensitive",
        {"jwe": compact_jwe},
    )


def progress(run_id, status, stage, error_code=None):
    return request(
        "PATCH", "/runs/" + quote(run_id, safe=""),
        {"status": status, "stage": stage, "error_code": error_code},
    )


def recover_run(run_id, *, outcome, worker_stopped):
    """Resolve one stale run after an explicit worker and simulator check."""
    return request(
        "POST",
        "/runs/" + quote(run_id, safe="") + "/recovery",
        {"outcome": outcome, "worker_stopped": worker_stopped},
    )
