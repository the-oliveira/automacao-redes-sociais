"""Cliente Metricool (publisher central). Contrato baseado em https://app.metricool.com/api/swagger.json.

Auth: header X-Mc-Auth (userToken) + query userId/blogId.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from nikki.settings import Settings, get_settings


class PublishError(RuntimeError):
    def __init__(self, message: str, retryable: bool, response: dict[str, Any] | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.response = response


def classify_http(status: int) -> bool:
    """True = retryable."""
    return status in (408, 409, 425, 429) or status >= 500


@dataclass
class ScheduledResult:
    external_id: str
    uuid: str | None
    status: str | None
    public_url: str | None
    raw: dict[str, Any]


# Formatos internos -> tipos do Metricool por rede
IG_TYPES = {"feed": "POST", "story": "STORY", "reel": "REEL"}
FB_TYPES = {"post": "POST", "story": "STORY", "reel": "REEL"}


class MetricoolClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = settings or get_settings()
        self._http = httpx.Client(
            base_url=self.s.metricool_base_url,
            headers={"X-Mc-Auth": self.s.metricool_user_token, "Accept": "application/json"},
            timeout=60.0,
            transport=transport,
        )

    # ------------------------------------------------------------- básico
    def _params(self, **extra: Any) -> dict[str, Any]:
        return {"userId": self.s.metricool_user_id, "blogId": self.s.metricool_blog_id, **extra}

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        params = self._params(**kw.pop("params", {}))
        try:
            r = self._http.request(method, path, params=params, **kw)
        except httpx.TimeoutException as e:
            raise PublishError(f"timeout Metricool {path}", retryable=True) from e
        except httpx.HTTPError as e:
            raise PublishError(f"erro de rede Metricool {path}: {e}", retryable=True) from e
        if r.status_code >= 400:
            body = r.text[:1000]
            raise PublishError(f"Metricool {method} {path} -> HTTP {r.status_code}: {body}",
                               retryable=classify_http(r.status_code), response={"status": r.status_code, "body": body})
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def brands(self) -> list[dict[str, Any]]:
        return self._req("GET", "/admin/simpleProfiles")

    # -------------------------------------------------------------- mídia
    def upload_file(self, path: Path) -> str:
        """Sobe um arquivo local para o S3 do Metricool e devolve a URL utilizável em `media`."""
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if len(data) >= 5 * 1024 * 1024:
            return self._upload_multipart(path, data, ctype)
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        tx = self._req("PUT", "/v2/media/s3/upload-transactions", json={
            "resourceType": "planner", "contentType": ctype, "fileExtension": path.suffix.lstrip("."),
            "parts": [{"size": len(data), "startByte": 0, "endByte": len(data), "hash": digest}],
        })["data"]
        put = httpx.put(tx["presignedUrl"], content=data, headers={"Content-Type": ctype,
                        "x-amz-checksum-sha256": digest}, timeout=120)
        if put.status_code >= 300:
            # algumas assinaturas não incluem o checksum no header: tenta sem
            put = httpx.put(tx["presignedUrl"], content=data, headers={"Content-Type": ctype}, timeout=120)
        if put.status_code >= 300:
            raise PublishError(f"upload S3 falhou HTTP {put.status_code}: {put.text[:300]}", retryable=classify_http(put.status_code))
        file_url = tx.get("fileUrl") or tx["presignedUrl"].split("?", 1)[0]
        done = self._req("PATCH", "/v2/media/s3/upload-transactions", json={"simple": {"fileUrl": file_url}})
        return ((done or {}).get("data") or {}).get("fileUrl") or file_url

    def _upload_multipart(self, path: Path, data: bytes, ctype: str) -> str:
        part_size = 5 * 1024 * 1024
        parts = []
        for i, start in enumerate(range(0, len(data), part_size)):
            chunk = data[start:start + part_size]
            parts.append({"size": len(chunk), "startByte": start, "endByte": start + len(chunk),
                          "hash": base64.b64encode(hashlib.sha256(chunk).digest()).decode()})
        tx = self._req("PUT", "/v2/media/s3/upload-transactions", json={
            "resourceType": "planner", "contentType": ctype, "fileExtension": path.suffix.lstrip("."), "parts": parts,
        })["data"]
        done_parts = []
        for p, meta in zip(tx["parts"], parts):
            chunk = data[p["startByte"]:p["endByte"]]
            r = httpx.put(p["presignedUrl"], content=chunk, headers={"x-amz-checksum-sha256": meta["hash"]}, timeout=300)
            if r.status_code >= 300:
                raise PublishError(f"upload parte {p['partNumber']} falhou HTTP {r.status_code}", retryable=classify_http(r.status_code))
            done_parts.append({"partNumber": p["partNumber"], "etag": r.headers.get("ETag", "").strip('"')})
        done = self._req("PATCH", "/v2/media/s3/upload-transactions", json={
            "multipart": {"uploadId": tx["uploadId"], "key": tx["key"], "parts": done_parts}})
        return ((done or {}).get("data") or {}).get("fileUrl") or tx.get("fileUrl")

    def normalize_image_url(self, url: str) -> str:
        res = self._req("GET", "/actions/normalize/image/url", params={"url": url})
        if isinstance(res, str):
            return res.strip().strip('"')
        return (res or {}).get("data") or (res or {}).get("url") or url

    # ------------------------------------------------------------ posts
    def build_post(self, *, network: str, fmt: str, text: str, media: list[str], when: datetime,
                   title: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "publicationDate": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": self.s.timezone},
            "text": text,
            "providers": [{"network": network}],
            "autoPublish": True,
            "draft": False,
            "shortener": False,
            "media": media,
            "saveExternalMediaFiles": True,
        }
        if network == "instagram":
            body["instagramData"] = {"type": IG_TYPES[fmt], "autoPublish": True, "showReelOnFeed": True}
        elif network == "facebook":
            body["facebookData"] = {"type": FB_TYPES.get(fmt, "POST")}
        elif network == "tiktok":
            body["tiktokData"] = {
                "privacyOption": "PUBLIC_TO_EVERYONE", "disableComment": False, "disableDuet": False,
                "disableStitch": False, "commercialContentThirdParty": False, "commercialContentOwnBrand": True,
                "title": (title or "")[:90],
            }
        elif network == "youtube":
            body["youtubeData"] = {"title": (title or text.splitlines()[0])[:100], "type": "short", "privacy": "public",
                                   "madeForKids": False, "tags": tags or [], "category": "22", "notifySubscribers": True}
        return body

    def schedule(self, body: dict[str, Any]) -> ScheduledResult:
        res = self._req("POST", "/v2/scheduler/posts", json=body)
        data = (res or {}).get("data") or {}
        if not data.get("id"):
            raise PublishError(f"Metricool não devolveu id do post: {str(res)[:300]}", retryable=False, response=res)
        prov = (data.get("providers") or [{}])[0]
        return ScheduledResult(str(data["id"]), data.get("uuid"), prov.get("status"), prov.get("publicUrl"), data)

    def get_post(self, post_id: str) -> dict[str, Any]:
        return (self._req("GET", f"/v2/scheduler/posts/{post_id}") or {}).get("data") or {}

    def list_posts(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        res = self._req("GET", "/v2/scheduler/posts", params={
            "start": start.strftime("%Y-%m-%dT%H:%M:%S"), "end": end.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": self.s.timezone})
        return (res or {}).get("data") or []

    def delete_post(self, post_id: str) -> None:
        self._req("DELETE", f"/v2/scheduler/posts/{post_id}")

    # ---------------------------------------------------------- analytics
    ANALYTICS_PATHS = {
        "instagram": ["/v2/analytics/posts/instagram", "/v2/analytics/reels/instagram", "/v2/analytics/stories/instagram"],
        "facebook": ["/v2/analytics/posts/facebook", "/v2/analytics/reels/facebook"],
        "tiktok": ["/v2/analytics/posts/tiktok"],
    }

    def analytics(self, network: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in self.ANALYTICS_PATHS.get(network, []):
            res = self._req("GET", path, params={"from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                                                 "to": end.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": self.s.timezone})
            for item in (res or {}).get("data") or []:
                item["_source"] = path.rsplit("/", 2)[-2]
                out.append(item)
        return out

    def youtube_analytics(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        # Swagger não tem /v2 para YouTube; o endpoint v1 (marcado deprecated) usa datas yyyyMMdd.
        res = self._req("GET", "/stats/youtube/videos", params={"start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d")})
        if isinstance(res, list):
            return res
        return (res or {}).get("data") or []
