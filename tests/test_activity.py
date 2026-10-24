import array
import random
import sys

import pytest

from audio_agent.activity import ActivityGate, ActivityLimitError, SAMPLE_RATE


def pcm(seconds, amplitude=0):
    samples = array.array("h", [amplitude] * round(seconds * SAMPLE_RATE))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def duration(segment):
    return len(segment.pcm) / (SAMPLE_RATE * 2)


def test_long_silence_never_emits_or_grows_memory():
    gate = ActivityGate()
    # One oversized input verifies internal buffering does not mirror input size.
    assert gate.feed(pcm(120)) == []
    assert gate.activity_state == "listening"
    assert gate.buffered_bytes < 20000
    assert gate.finish() == []


def test_speech_until_quiet_creates_one_dynamic_segment_with_preroll_and_tail():
    gate = ActivityGate()
    assert gate.feed(pcm(1) + pcm(0.5, 1000)) == []
    assert gate.activity_state == "recording"
    assert gate.feed(pcm(0.78)) == []
    result = gate.feed(pcm(0.02))
    assert len(result) == 1
    assert result[0].start_offset == pytest.approx(0.7)
    assert result[0].end_sample / SAMPLE_RATE == pytest.approx(1.7)
    assert duration(result[0]) == pytest.approx(1.0)
    assert gate.activity_state == "listening"
    assert gate.finish() == []


def test_two_utterances_keep_the_gap_in_original_timestamps():
    gate = ActivityGate()
    result = gate.feed(pcm(1) + pcm(0.5, 1000) + pcm(1.5) + pcm(0.8, 2000) + pcm(1))
    assert len(result) == 2
    assert result[0].start_offset == pytest.approx(0.7)
    assert result[0].end_sample / SAMPLE_RATE == pytest.approx(1.7)
    assert result[1].start_offset == pytest.approx(2.7)
    assert result[1].end_sample / SAMPLE_RATE == pytest.approx(4.0)
    assert duration(result[1]) == pytest.approx(1.3)
    assert gate.finish() == []


def test_clicks_and_short_spikes_are_not_saved():
    gate = ActivityGate()
    assert gate.feed(pcm(1) + pcm(0.02, 25000) + pcm(1) + pcm(0.18, 4000) + pcm(1)) == []
    assert gate.finish() == []


def test_pause_shorter_than_quiet_timeout_keeps_one_event():
    gate = ActivityGate()
    result = gate.feed(pcm(0.4, 1000) + pcm(0.5) + pcm(0.6, 1000) + pcm(0.8))
    assert len(result) == 1
    assert result[0].start_sample == 0
    assert duration(result[0]) == pytest.approx(1.7)


def test_45_second_continuous_sound_is_not_periodically_split():
    gate = ActivityGate()
    assert gate.feed(pcm(45, 1000)) == []
    assert gate.activity_state == "recording"
    result = gate.feed(pcm(0.8))
    assert len(result) == 1
    assert duration(result[0]) == pytest.approx(45.2)
    assert gate.finish() == []


def test_stop_flushes_active_only_and_trims_quiet_tail():
    gate = ActivityGate()
    assert gate.feed(pcm(1) + pcm(0.4, 1000) + pcm(0.5)) == []
    result = gate.finish()
    assert len(result) == 1
    assert result[0].start_offset == pytest.approx(0.7)
    assert duration(result[0]) == pytest.approx(0.9)
    assert gate.finish() == []
    idle = ActivityGate()
    assert idle.feed(pcm(2)) == []
    assert idle.finish() == []


def test_arbitrary_odd_byte_fragmentation_preserves_exact_segments():
    source = pcm(0.6) + pcm(0.52, 1000) + pcm(1) + pcm(0.33, 3000) + pcm(0.91)
    whole = ActivityGate()
    expected = whole.feed(source) + whole.finish()
    fragmented = ActivityGate()
    actual = []
    randomizer = random.Random(12)
    cursor = 0
    while cursor < len(source):
        take = randomizer.randrange(1, 1501)
        actual.extend(fragmented.feed(source[cursor:cursor + take]))
        cursor += take
    actual.extend(fragmented.finish())
    assert actual == expected
    assert len(actual) == 2


def test_partial_final_frame_includes_whole_samples_and_drops_single_byte():
    gate = ActivityGate()
    sound = pcm(0.205, 1000)
    assert gate.feed(sound + b"\xff") == []
    result = gate.finish()
    assert len(result) == 1
    assert result[0].pcm == sound


def test_hysteresis_keeps_confirmed_quieter_sound_without_new_trigger():
    gate = ActivityGate()
    # 164/32768 ~=0.005 lies between start and release thresholds.
    assert gate.feed(pcm(0.3, 1000) + pcm(1, 164)) == []
    assert gate.activity_state == "recording"
    result = gate.feed(pcm(0.8))
    assert len(result) == 1 and duration(result[0]) == pytest.approx(1.5)
    quiet_start = ActivityGate()
    assert quiet_start.feed(pcm(2, 164)) == []
    assert quiet_start.finish() == []


def test_memory_guard_stops_with_whole_active_event_never_time_splits():
    gate = ActivityGate(max_active_seconds=1)
    with pytest.raises(ActivityLimitError) as raised:
        gate.feed(pcm(2, 1000))
    assert len(raised.value.segments) == 1
    assert duration(raised.value.segments[0]) == pytest.approx(1)
    assert gate.finish() == []
    assert gate.activity_state == "listening"
    assert gate.buffered_bytes < 20000


def test_memory_guard_preserves_preceding_completed_events_in_same_feed():
    gate = ActivityGate(max_active_seconds=1.2)
    with pytest.raises(ActivityLimitError) as raised:
        gate.feed(pcm(0.3, 1000) + pcm(0.8) + pcm(2, 1000))
    assert len(raised.value.segments) == 2
    assert duration(raised.value.segments[0]) == pytest.approx(0.5)
    assert duration(raised.value.segments[1]) == pytest.approx(1.2)


@pytest.mark.parametrize("options", [
    {"sound_threshold": float("nan")}, {"release_threshold": 0.02},
    {"min_sound_seconds": 0}, {"end_silence_seconds": 0.1},
    {"max_active_seconds": 601}, {"max_segment_seconds": -1},
])
def test_invalid_settings_are_rejected(options):
    with pytest.raises(ValueError):
        ActivityGate(**options)
