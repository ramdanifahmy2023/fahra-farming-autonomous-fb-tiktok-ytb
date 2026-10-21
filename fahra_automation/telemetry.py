"""Best-effort timing counters; durable intent and business state never use this buffer."""
from __future__ import annotations

import asyncio
import math
import re
import weakref


_buffers = weakref.WeakKeyDictionary()


class TimingBuffer:
    def __init__(self, store):
        self.store = weakref.ref(store)
        self.samples = {}
        self.flushing_names = set()
        self.lock = asyncio.Lock()

    def add(self, name, milliseconds):
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', name)
                or type(milliseconds) not in {int, float} or not math.isfinite(milliseconds) or milliseconds < 0):
            return
        known = set(self.samples) | self.flushing_names
        if name not in known and len(known) >= 128:
            return
        old = self.samples.setdefault(name, {'samples': 0, 'total_ms': 0, 'max_ms': 0, 'last_ms': 0})
        old.update(samples=old['samples'] + 1, total_ms=old['total_ms'] + milliseconds,
                   max_ms=max(old['max_ms'], milliseconds), last_ms=milliseconds)

    def restore(self, batch):
        for name, old in batch.items():
            current = self.samples.get(name)
            self.samples[name] = ({'samples': old['samples'] + current['samples'],
                'total_ms': old['total_ms'] + current['total_ms'],
                'max_ms': max(old['max_ms'], current['max_ms']), 'last_ms': current['last_ms']}
                if current else old)

    async def flush(self):
        async with self.lock:
            store = self.store()
            if not self.samples or store is None:
                return
            batch, self.samples = self.samples, {}
            self.flushing_names = set(batch)
            task = asyncio.create_task(asyncio.to_thread(store.timings_batch, batch))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A transaction already running must settle before shutdown/another flush.
                try:
                    await task
                except Exception:
                    self.restore(batch)
                raise
            except Exception:
                self.restore(batch)
                raise
            finally:
                self.flushing_names.clear()


def buffer_for(store):
    if store not in _buffers:
        _buffers[store] = TimingBuffer(store)
    return _buffers[store]


def record_timing(store, name, milliseconds):
    if store is not None:
        buffer_for(store).add(name, milliseconds)


async def flush(store):
    if store is not None:
        await buffer_for(store).flush()


async def loop(store, interval=5):
    while True:
        await asyncio.sleep(interval)
        try:
            await flush(store)
        except Exception:
            # Counters remain buffered; no publication/reply outcome depends on telemetry.
            continue
