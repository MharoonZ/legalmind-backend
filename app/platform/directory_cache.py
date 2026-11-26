"""Small public-only cache, invalidated after committed directory changes."""

import asyncio
import time
from collections import OrderedDict


class DirectoryCache:
    def __init__(self):
        self.values = OrderedDict()
        self.locks = {}
        self.generation = 0

    def clear(self):
        self.generation += 1
        self.values.clear()

    async def get(self, key, load, *, enabled):
        # A disconnected LISTEN connection cannot guarantee invalidations.
        if not enabled():
            return await load()
        if len(self.locks) > 64:
            self.locks = {k: v for k, v in self.locks.items() if v.locked()}
        async with self.locks.setdefault(key, asyncio.Lock()):
            cached = self.values.get(key)
            if cached and cached[0] > time.monotonic():
                self.values.move_to_end(key)
                return cached[1]
            generation = self.generation
            result = await load()
            if generation == self.generation and enabled():
                self.values[key] = (time.monotonic() + 15, result)
                self.values.move_to_end(key)
                while len(self.values) > 32:
                    self.values.popitem(last=False)
            return result
