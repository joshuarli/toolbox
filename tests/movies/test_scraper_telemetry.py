from tool.movies.scraper import _summarize_processes


def test_summarize_processes_reports_cpu_deltas_by_process_type() -> None:
    first, previous = _summarize_processes(
        [
            {"id": 10, "type": "browser", "cpuTime": 2.0},
            {"id": 11, "type": "renderer", "cpuTime": 1.5},
            {"id": 12, "type": "renderer", "cpuTime": 0.5},
        ],
        {},
        5.0,
    )
    assert first["renderer"] == {"count": 2, "cpu_s": 2.0, "cpu_delta_s": 0.0, "cpu_pct": None}

    second, _ = _summarize_processes(
        [
            {"id": 10, "type": "browser", "cpuTime": 2.5},
            {"id": 11, "type": "renderer", "cpuTime": 3.0},
            {"id": 12, "type": "renderer", "cpuTime": 0.75},
        ],
        previous,
        5.0,
    )
    assert second["browser"]["cpu_delta_s"] == 0.5
    assert second["browser"]["cpu_pct"] == 10.0
    assert second["renderer"]["cpu_delta_s"] == 1.75
    assert second["renderer"]["cpu_pct"] == 35.0
