"""On the rgw backend a bucket vanishes from the Admin Ops lists when the user deletes it with
S3 DeleteBucket. The mock's croit state does not watch moto, so the test S3 client forgets it
there (same as RGW would): wrap the client factory with `forgetting(factory, croit_state)`."""


class ForgettingClient:
    def __init__(self, real, state):
        self._real, self._state = real, state

    def __getattr__(self, name):
        return getattr(self._real, name)

    def delete_bucket(self, **kw):
        out = self._real.delete_bucket(**kw)
        short = kw["Bucket"]
        for full in [n for n in self._state.buckets if n.partition(":")[2] == short or n == short]:
            del self._state.buckets[full]
        return out


def forgetting(factory, state):
    return lambda *a, **kw: ForgettingClient(factory(*a, **kw), state)
