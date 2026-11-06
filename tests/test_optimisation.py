"""Offline tests for quota accounting, caching, batching and resumable cloning.

Run with: python tests/test_optimisation.py
These use fake API services, so no network / credentials are required.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ytmgr.client as client_mod
from ytmgr.client import YouTubeClient
from ytmgr.quota import QuotaExceeded, QuotaTracker


# --------------------------------------------------------------------------
# Fake googleapiclient plumbing
# --------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, fn):
        self.fn = fn
        self.http = object()

    def execute(self):
        res = self.fn()
        if isinstance(res, Exception):
            raise res
        return res


class State:
    def __init__(self, videos):
        self.videos = list(videos)
        self.insert_calls = []
        self.delete_calls = []
        self.list_calls = []
        self.created = []
        self.batch_executions = 0

    def items_page(self, playlist_id, page_token, max_results):
        start = int(page_token or 0)
        page = self.videos[start:start + max_results]
        items = [
            {
                "id": f"ITEM_{start + i}",
                "snippet": {
                    "position": start + i,
                    "title": f"V{vid}",
                    "resourceId": {"kind": "youtube#video", "videoId": vid},
                },
            }
            for i, vid in enumerate(page)
        ]
        resp = {"items": items}
        nxt = start + max_results
        if nxt < len(self.videos):
            resp["nextPageToken"] = str(nxt)
        self.list_calls.append((playlist_id, start, max_results))
        return resp


class ItemsApi:
    def __init__(self, state):
        self.state = state

    def list(self, part=None, playlistId=None, maxResults=None, pageToken=None):
        return FakeRequest(
            lambda: self.state.items_page(playlistId, pageToken, maxResults)
        )

    def insert(self, part=None, body=None):
        vid = body["snippet"]["resourceId"]["videoId"]

        def fn():
            self.state.insert_calls.append(vid)
            return {"id": f"NEW_{vid}", "snippet": body["snippet"]}

        return FakeRequest(fn)

    def delete(self, id=None):
        def fn():
            self.state.delete_calls.append(id)
            return {}

        return FakeRequest(fn)


class PlaylistsApi:
    def __init__(self, state):
        self.state = state

    def list(self, part=None, mine=None, maxResults=None, pageToken=None, id=None):
        if id:
            return FakeRequest(
                lambda: {"items": [{"id": id, "snippet": {"title": "T"}}]}
            )
        start = int(pageToken or 0)
        total = 120
        page = [
            {
                "id": f"PL{i}",
                "snippet": {"title": f"P{i}"},
                "contentDetails": {"itemCount": 0},
            }
            for i in range(start, min(start + maxResults, total))
        ]
        resp = {"items": page}
        nxt = start + maxResults
        if nxt < total:
            resp["nextPageToken"] = str(nxt)
        return FakeRequest(lambda: resp)

    def insert(self, part=None, body=None):
        self.state.created.append(body)
        return FakeRequest(lambda: {"id": "TARGET1", "snippet": body["snippet"]})

    def delete(self, id=None):
        return FakeRequest(lambda: {})


class FakeBatch:
    def __init__(self, state, callback, fail=False):
        self.state = state
        self.callback = callback
        self.reqs = []
        self.fail = fail

    def add(self, request, request_id=None):
        self.reqs.append((request_id, request))

    def execute(self):
        self.state.batch_executions += 1
        if self.fail:
            raise RuntimeError("batch endpoint unavailable")
        for rid, req in self.reqs:
            try:
                resp = req.execute()
                self.callback(rid, resp, None)
            except Exception as exc:  # noqa: BLE001
                self.callback(rid, None, exc)


class Service:
    def __init__(self, state, fail_batch=False):
        self.state = state
        self._items = ItemsApi(state)
        self._pl = PlaylistsApi(state)
        self.fail_batch = fail_batch

    def playlists(self):
        return self._pl

    def playlistItems(self):
        return self._items

    def new_batch_http_request(self, callback=None):
        return FakeBatch(self.state, callback, fail=self.fail_batch)


class ServiceNoBatch:
    def __init__(self, state):
        self.state = state
        self._items = ItemsApi(state)
        self._pl = PlaylistsApi(state)

    def playlists(self):
        return self._pl

    def playlistItems(self):
        return self._items


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    client_mod.CLONE_DIR = tmp / "clones"

    def tracker(limit=10000):
        return QuotaTracker(
            limit=limit, path=tmp / f"quota_{limit}.json", enforce=True
        )

    print("== 1. Pagination + caching ==")
    st = State([f"v{i:03d}00000000"[:11] for i in range(120)])
    c = YouTubeClient(Service(st), min_interval=0, quota=tracker())
    a = c.list_all_items("PL1")
    b = c.list_all_items("PL1")
    assert len(a) == 120 and len(b) == 120
    assert len(st.list_calls) == 3, st.list_calls
    assert c.quota.used == 3
    print(f"   list requests={len(st.list_calls)} (expect 3, cached), quota={c.quota.used}")

    print("== 2. Dedup reuses groups (no extra listing) ==")
    st2 = State(
        ["vidAAAAAAA1", "vidBBBBBBB2", "vidAAAAAAA1", "vidCCCCCCC3", "vidAAAAAAA1"]
    )
    c2 = YouTubeClient(Service(st2), min_interval=0, quota=tracker())
    groups = c2.find_duplicate_items("PLX")
    list_before = len(st2.list_calls)
    deleted = c2.remove_duplicates("PLX", groups=groups)
    assert len(st2.list_calls) == list_before, "should not re-list"
    assert len(deleted) == 2, deleted
    print(f"   removed={len(deleted)}, extra listings={len(st2.list_calls) - list_before}")

    print("== 3. Batched insert ==")
    st3 = State([])
    c3 = YouTubeClient(Service(st3), min_interval=0, quota=tracker(), batch_size=50)
    res = c3.add_items("PL1", [f"vid{i:08d}"[:11] for i in range(5)])
    assert all(ok for _, ok, _ in res) and len(st3.insert_calls) == 5
    assert st3.batch_executions == 1, st3.batch_executions
    print(f"   inserts={len(st3.insert_calls)}, batch HTTP={st3.batch_executions}")

    print("== 4. Batch unsupported -> sequential ==")
    st4 = State([])
    c4 = YouTubeClient(ServiceNoBatch(st4), min_interval=0, quota=tracker(), use_batch=True)
    res = c4.add_items("PL1", ["vidAAAAAAA1", "vidBBBBBBB2"])
    assert all(ok for _, ok, _ in res) and len(st4.insert_calls) == 2
    print(f"   sequential inserts={len(st4.insert_calls)}")

    print("== 5. Batch failure -> refund + sequential ==")
    st5 = State([])
    c5 = YouTubeClient(
        Service(st5, fail_batch=True), min_interval=0, quota=tracker(), batch_size=50
    )
    used_before = c5.quota.used
    res = c5.add_items("PL1", ["vidAAAAAAA1", "vidBBBBBBB2"])
    assert all(ok for _, ok, _ in res) and len(st5.insert_calls) == 2
    assert c5.quota.used - used_before == 100, c5.quota.used - used_before
    print(f"   fallback inserts={len(st5.insert_calls)}, quota delta={c5.quota.used - used_before}")

    print("== 6. Quota enforcement ==")
    st6 = State([])
    c6 = YouTubeClient(Service(st6), min_interval=0, quota=tracker(limit=120), batch_size=1)
    try:
        c6.add_items("PL1", ["vidAAAAAAA1", "vidBBBBBBB2", "vidCCCCCCC3"])
        raise AssertionError("expected QuotaExceeded")
    except QuotaExceeded as exc:
        print("   stopped before exceeding:", exc.friendly()[:70], "...")

    print("== 7. Resumable clone across days ==")
    source = [f"vid{i:08d}"[:11] for i in range(10)]
    st7 = State(source)
    c7 = YouTubeClient(Service(st7), min_interval=0, quota=tracker(limit=251), batch_size=2)
    r1 = c7.clone_playlist("SRC", "My Clone", skip_duplicates=True)
    s1 = r1["_clone_stats"]
    print(
        f"   run1: added={s1['added']} complete={s1['complete']} "
        f"quota_exhausted={s1['quota_exhausted']}"
    )
    assert not s1["complete"] and s1["quota_exhausted"]
    assert Path(s1["checkpoint"]).exists()
    assert len(st7.insert_calls) == 4, st7.insert_calls

    c8 = YouTubeClient(Service(st7), min_interval=0, quota=tracker(limit=10000), batch_size=2)
    r2 = c8.clone_playlist("SRC", "My Clone", skip_duplicates=True)
    s2 = r2["_clone_stats"]
    print(
        f"   run2: added={s2['added']} already={s2['already_added']} "
        f"complete={s2['complete']}"
    )
    assert s2["complete"] and s2["already_added"] == 4
    assert len(st7.insert_calls) == 10
    assert not s2["checkpoint"]
    print(f"   total inserts={len(st7.insert_calls)} (expect 10)")

    print("== 8. Clone skips duplicate source videos ==")
    st8 = State(["vidAAAAAAA1", "vidBBBBBBB2", "vidAAAAAAA1", "vidCCCCCCC3"])
    c9 = YouTubeClient(Service(st8), min_interval=0, quota=tracker(), batch_size=50)
    c9.clone_playlist("SRC", "Dedup Clone", skip_duplicates=True)
    assert len(st8.insert_calls) == 3, st8.insert_calls
    print(f"   inserted {len(st8.insert_calls)} unique (expect 3)")

    print("== 9. Playlist listing pagination ==")
    st9 = State([])
    c10 = YouTubeClient(Service(st9), min_interval=0, quota=tracker())
    pls = c10.list_all_playlists()
    assert len(pls) == 120, len(pls)
    print(f"   playlists paged={len(pls)} (expect 120)")

    print("\nALL OPTIMISATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
