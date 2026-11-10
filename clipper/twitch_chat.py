"""Twitch VOD chat replay via Twitch GQL (works for finished VODs and the in-progress VOD of a live stream).

Pages are fetched by cursor; the time range is split into slices that are fetched in parallel, each
slice seeking with contentOffsetSeconds and paging until it reaches the next slice.
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
_FIELDS = "edges{cursor node{contentOffsetSeconds message{fragments{text}}}} pageInfo{hasNextPage}"
Q_OFFSET = "query($id:ID!,$off:Int){video(id:$id){comments(contentOffsetSeconds:$off){%s}}}" % _FIELDS
Q_CURSOR = "query($id:ID!,$c:Cursor){video(id:$id){comments(after:$c){%s}}}" % _FIELDS


def _gql(query: str, variables: dict, tries: int = 4) -> dict | None:
    body = json.dumps({"query": query, "variables": variables}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(GQL, data=body, headers={"Client-Id": CLIENT_ID, "Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            c = ((d.get("data") or {}).get("video") or {}).get("comments")
            if c is not None:
                return c
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def _slice(vod_id: str, a: float, b: float) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    page = _gql(Q_OFFSET, {"id": vod_id, "off": int(a)})
    while page:
        last_t = a
        for e in page.get("edges") or []:
            n = e["node"]
            t = float(n.get("contentOffsetSeconds") or 0)
            last_t = t
            if a <= t < b:
                msg = "".join((f or {}).get("text") or "" for f in (n.get("message") or {}).get("fragments") or [])
                out.append((t, msg))
        edges = page.get("edges") or []
        if last_t >= b or not edges or not (page.get("pageInfo") or {}).get("hasNextPage"):
            break
        page = _gql(Q_CURSOR, {"id": vod_id, "c": edges[-1]["cursor"]})
    return out


def fetch_chat(vod_id: str, start: float, end: float, workers: int = 6, slice_s: float = 240) -> list[tuple[float, str]]:
    """All chat messages with start <= t < end (VOD-relative seconds), sorted."""
    edges = []
    t = start
    while t < end:
        edges.append((t, min(end, t + slice_s)))
        t += slice_s
    msgs: list[tuple[float, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(lambda ab: _slice(vod_id, *ab), edges):
            msgs.extend(part)
    msgs = sorted(set(msgs))
    return msgs
