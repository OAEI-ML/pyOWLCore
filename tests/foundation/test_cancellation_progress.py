from __future__ import annotations

import unittest

from pyowl_core import (
    CancellationSource,
    CancellationToken,
    OperationCancelledError,
    ProgressBuffer,
    ProgressEvent,
    report_progress,
)


class CancellationTests(unittest.TestCase):
    def test_source_cancels_read_only_token_exactly_once(self) -> None:
        source = CancellationSource()
        token = source.token
        self.assertFalse(token.cancelled)
        self.assertTrue(source.cancel("requested by caller"))
        self.assertFalse(source.cancel("second reason"))
        self.assertEqual(token.reason, "requested by caller")
        with self.assertRaises(OperationCancelledError) as caught:
            token.check()
        self.assertEqual(caught.exception.reason, "requested by caller")

    def test_deadline_uses_monotonic_time(self) -> None:
        token = CancellationToken(deadline_seconds=1e-12)
        with self.assertRaises(OperationCancelledError):
            token.check()
        self.assertEqual(token.reason, "deadline exceeded")


class ProgressTests(unittest.TestCase):
    def test_progress_events_are_validated_and_buffer_is_bounded(self) -> None:
        buffer = ProgressBuffer(limit=2)
        for completed in range(3):
            report_progress(
                buffer,
                ProgressEvent(stage="parse", completed=completed, total=3, details={"shard": 1}),
            )
        self.assertEqual([event.completed for event in buffer.snapshot()], [1, 2])
        with self.assertRaises(ValueError):
            ProgressEvent(stage="parse", completed=2, total=1)
        with self.assertRaises(TypeError):
            ProgressEvent(
                stage="parse",
                completed=0,
                details={"bad": object()},  # type: ignore[dict-item]
            )

    def test_none_reporter_is_a_noop(self) -> None:
        report_progress(None, ProgressEvent(stage="parse", completed=0))


if __name__ == "__main__":
    unittest.main()
