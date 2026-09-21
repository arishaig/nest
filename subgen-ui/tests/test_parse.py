from subgen_ui.parse import Event, summarize

NOW = 10_000 * 10**9
POD = "subgen-f5846bff9-rldkx"
OLD = "subgen-c4f794974-jfh25"


def ev(sec, line, pod=POD):
    return Event(sec * 10**9, pod, line)


START = "2026-09-21 22:02:51 INFO: WORKER START :[TRANSCRIBE] {n} | Jobs: 1 processing, {q} queued"
FINISH = "2026-09-21 22:02:51 INFO: WORKER FINISH: [TRANSCRIBE] {n} in 4m 58s | Remaining: {q} queued"
ERROR = (
    "2026-09-21 18:52:41 INFO: Error processing or transcribing /data/media/tv/Star Trek/{n} "
    "in English: Frame does not match AudioFifo parameters."
)


def test_queue_and_current_job_from_latest_lines():
    s = summarize(
        [
            ev(9000, FINISH.format(n="a.mkv", q=64)),
            ev(9000, START.format(n="b.mkv", q=63)),
        ],
        NOW,
        POD,
    )
    assert s["queued"] == 63
    assert s["current"]["name"] == "b.mkv"
    assert s["current"]["elapsed_s"] == 1000


def test_finished_job_clears_current():
    s = summarize([ev(9000, START.format(n="a.mkv", q=5)), ev(9100, FINISH.format(n="a.mkv", q=5))], NOW, POD)
    assert s["current"] is None
    assert s["queued"] == 5


def test_error_before_finish_marks_that_job_failed():
    s = summarize(
        [
            ev(9000, START.format(n="bad.mkv", q=3)),
            ev(9010, ERROR.format(n="bad.mkv")),
            ev(9011, FINISH.format(n="bad.mkv", q=3)),
            ev(9012, START.format(n="good.mkv", q=2)),
            ev(9100, FINISH.format(n="good.mkv", q=2)),
        ],
        NOW,
        POD,
    )
    assert s["last_hour"]["finished"] == 2
    assert s["last_hour"]["failed"] == 1
    assert s["reasons"] == [("Frame does not match AudioFifo parameters.", 1)]
    assert s["recent_errors"][0]["name"] == "bad.mkv"


def test_stale_pod_events_do_not_supply_queue_or_current_job():
    # New pod is running but hasn't logged a job yet; the old pod's last
    # START describes a queue that no longer exists.
    s = summarize([ev(9000, START.format(n="old.mkv", q=88), pod=OLD)], NOW, POD)
    assert s["queued"] is None
    assert s["current"] is None


def test_rate_and_eta_from_finish_spacing():
    finishes = [ev(9000 + i * 300, FINISH.format(n=f"{i}.mkv", q=100 - i)) for i in range(5)]
    s = summarize(finishes, NOW, POD)
    # 4 intervals of 300s => 12 jobs/hour; 96 left => 8h
    assert round(s["last_hour"]["per_hour"]) == 12
    assert round(s["last_hour"]["eta_hours"]) == 8


def test_no_events():
    s = summarize([], NOW, POD)
    assert s["queued"] is None and s["current"] is None
    assert s["last_hour"]["per_hour"] is None and s["recent_errors"] == []


def test_failures_from_an_older_pod_are_not_counted_against_the_current_one():
    s = summarize(
        [
            ev(8000, START.format(n="x.mkv", q=9), pod=OLD),
            ev(8010, ERROR.format(n="x.mkv"), pod=OLD),
            ev(8011, FINISH.format(n="x.mkv", q=9), pod=OLD),
            ev(9000, START.format(n="y.mkv", q=8)),
            ev(9300, FINISH.format(n="y.mkv", q=8)),
        ],
        NOW,
        POD,
    )
    assert s["reasons"] == [] and s["recent_errors"] == []
    assert s["last_hour"]["failed"] == 0


def test_detect_language_bursts_do_not_inflate_rate():
    events = [
        ev(9000, FINISH.format(n="a.mkv", q=100)),
        ev(9300, FINISH.format(n="b.mkv", q=99)),
    ] + [
        ev(9301 + i, "x WORKER FINISH: [DETECT_LANGUAGE] d%d.mkv in 0m 1s | Remaining: %d queued" % (i, 98 - i))
        for i in range(20)
    ]
    s = summarize(events, NOW, POD)
    assert round(s["last_hour"]["per_hour"]) == 12
