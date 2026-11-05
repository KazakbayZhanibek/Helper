"""Small, allowlisted timing snapshots; never source text, paths or model output.

Durations use a monotonic clock and describe this worker attempt only. In
particular ``model_request`` includes budget checks, transport, response parsing
and receipt processing; it is not a measurement of model inference alone.
Snapshots are written with existing progress checkpoints/finalization, not per
frame. A killed process can leave a partial last checkpoint.
"""
from contextlib import contextmanager
import time


STAGES = frozenset({
    'frame_extraction', 'frame_decode', 'frame_persistence', 'frame_reading', 'model_request',
    'validation', 'aggregation', 'visual_review_request',
})
COUNTERS = frozenset({'prepared_frames', 'reused_frame_batches', 'completed_windows', 'completed_visual_reviews'})


class AttemptMetrics:
    def __init__(self, *, attempt, queue_wait_ms=None, clock=None):
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._ended = None
        self._outcome = 'running'
        self.attempt = max(1, int(attempt))
        self.queue_wait_ms = None if queue_wait_ms is None else max(0, round(queue_wait_ms, 3))
        self._stages = {}
        self._counters = {}

    @classmethod
    def for_job(cls, job):
        # started_at is replaced on each retry, but there is no requeued_at.
        # Reporting started_at-created_at as retry queue delay would be false.
        wait = ((job.started_at - job.created_at).total_seconds() * 1000
                if job.attempts == 1 and job.started_at and job.created_at else None)
        return cls(attempt=job.attempts, queue_wait_ms=wait)

    @contextmanager
    def measure(self, stage):
        if stage not in STAGES:
            raise ValueError('Unknown metric stage')
        started, failed = self._clock(), False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            item = self._stages.setdefault(stage, {'total_ms': 0.0, 'invocations': 0, 'errors': 0})
            item['total_ms'] += max(0.0, (self._clock() - started) * 1000)
            item['invocations'] += 1
            item['errors'] += int(failed)

    def count(self, name, value=1):
        if name not in COUNTERS or type(value) is not int or value < 0:
            raise ValueError('Unknown or invalid metric counter')
        self._counters[name] = self._counters.get(name, 0) + value

    def finish(self, outcome):
        if outcome not in {'done', 'failed', 'cancelled'}:
            raise ValueError('Unknown metric outcome')
        if self._ended is None:
            self._ended = self._clock()
            self._outcome = outcome

    def snapshot(self):
        ended = self._ended if self._ended is not None else self._clock()
        return {
            'version': 1, 'scope': 'current_attempt', 'attempt': self.attempt,
            'outcome': self._outcome, 'queue_wait_ms': self.queue_wait_ms,
            'total_ms': round(max(0.0, (ended - self._started) * 1000), 3),
            'stages': {name: {**value, 'total_ms': round(value['total_ms'], 3)}
                       for name, value in self._stages.items()},
            'counters': dict(self._counters),
        }


@contextmanager
def measured(context, stage):
    metrics = getattr(context, 'metrics', None)
    if metrics is None:
        yield
    else:
        with metrics.measure(stage):
            yield


def count(context, name, value=1):
    metrics = getattr(context, 'metrics', None)
    if metrics is not None:
        metrics.count(name, value)


def with_metrics(result, context, *, outcome=None):
    """Copy a result without dropping its proposal, progress or budget fields."""
    result = dict(result or {})
    metrics = getattr(context, 'metrics', None)
    if metrics is not None:
        if outcome is not None:
            metrics.finish(outcome)
        result['metrics'] = metrics.snapshot()
    return result
