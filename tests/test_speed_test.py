"""
Tests for speed_test.py.

Covers:
  * Pure formatting helpers (humanize_bytes, humanize_speed, sparkline,
    gauge_bar, rate_speed, speed_dots, _clean_url_error).
  * RequestResult dataclass invariants (frozen, speed_bps).
  * HTTP layer against a real loopback http.server fixture:
      - download_once returns (elapsed, n_bytes, 200) for a known file.
      - perform_measurement wraps errors into RequestResult(ok=False, ...).
  * CLI parsing + URL validation via resolve_url with monkey-patched input.
  * Aggregate reporting reads real fields from RequestResult.

The tests intentionally avoid network access outside 127.0.0.1 so they run
anywhere without connectivity or DNS.
"""

from __future__ import annotations

import http.server
import socket
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Iterator

import pytest

import speed_test as st


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
class TestHumanizeBytes:
    @pytest.mark.parametrize("n, expected", [
        (0, "0.00 B"),
        (512, "512.00 B"),
        (1024, "1.00 KB"),
        (1024 * 1024, "1.00 MB"),
        (1024 * 1024 * 1024, "1.00 GB"),
        (1536, "1.50 KB"),
        (1.5 * 1024 * 1024, "1.50 MB"),
    ])
    def test_boundary_and_typical(self, n: float, expected: str) -> None:
        assert st.humanize_bytes(n) == expected

    def test_very_large_stays_in_gb(self) -> None:
        # Deliberately above 1024 GB — humanize_bytes never rolls over to TB.
        assert st.humanize_bytes(5 * 1024**4).endswith(" GB")


class TestHumanizeSpeed:
    def test_mb_when_at_least_hundredth_mb(self) -> None:
        assert st.humanize_speed(1024 * 1024) == "1.00 MB/s"
        assert st.humanize_speed(1024 * 1024 * 12.34) == "12.34 MB/s"

    def test_falls_back_to_kb_when_slower(self) -> None:
        assert st.humanize_speed(1024).endswith(" KB/s")
        assert st.humanize_speed(0.0) == "0.00 KB/s"

    def test_threshold_at_point_zero_one_mb(self) -> None:
        # 0.01 MB/s exactly should render as MB/s
        assert st.humanize_speed(0.01 * 1024 * 1024) == "0.01 MB/s"


class TestSparkline:
    def test_empty_returns_empty_string(self) -> None:
        assert st.sparkline([]) == ""

    def test_all_equal_yields_lowest_bucket(self) -> None:
        # When all values equal, span == 1.0 (fallback); (v-lo)/span == 0.
        out = st.sparkline([5.0, 5.0, 5.0])
        assert len(out) == 3
        # First char of SPARK_CHARS is " " (index 0).
        assert set(out) == {st.SPARK_CHARS[0]}

    def test_uses_only_alphabet_and_respects_width(self) -> None:
        values = list(range(50))
        out = st.sparkline(values, width=10)
        assert len(out) == 10
        assert set(out).issubset(set(st.SPARK_CHARS))

    def test_extremes_map_to_bounds(self) -> None:
        out = st.sparkline([0.0, 100.0])
        assert out[0] == st.SPARK_CHARS[0]
        assert out[-1] == st.SPARK_CHARS[-1]


class TestGaugeBar:
    def test_zero_best_returns_empty_text(self) -> None:
        text = st.gauge_bar(value=1.0, best=0.0, width=10)
        assert text.plain == ""

    def test_full_when_value_equals_best(self) -> None:
        text = st.gauge_bar(value=5.0, best=5.0, width=10)
        assert text.plain == "█" * 10

    def test_half(self) -> None:
        text = st.gauge_bar(value=5.0, best=10.0, width=10)
        assert text.plain == "█" * 5 + "░" * 5

    def test_value_over_best_clamps_to_full(self) -> None:
        text = st.gauge_bar(value=99.0, best=1.0, width=8)
        assert text.plain == "█" * 8


class TestRateSpeed:
    @pytest.mark.parametrize("mb, expected_label, expected_dots", [
        (0.1, "Very Slow", 1),
        (0.49, "Very Slow", 1),
        (0.5, "Slow", 2),
        (1.9, "Slow", 2),
        (2.0, "Decent", 3),
        (9.9, "Decent", 3),
        (10.0, "Fast", 4),
        (49.9, "Fast", 4),
        (50.0, "Blazing Fast", 5),
        (500.0, "Blazing Fast", 5),
    ])
    def test_thresholds(self, mb: float, expected_label: str,
                        expected_dots: int) -> None:
        label, _style, dots = st.rate_speed(mb * 1024 * 1024)
        assert label == expected_label
        assert dots == expected_dots


class TestSpeedDots:
    def test_full_and_empty_bounds(self) -> None:
        assert st.speed_dots(0).plain == "○○○○○"
        assert st.speed_dots(5).plain == "●●●●●"
        assert st.speed_dots(3).plain == "●●●○○"

    def test_out_of_range_is_clamped(self) -> None:
        # A defensive rate_speed shouldn't return >5 but we still guard.
        assert st.speed_dots(-1).plain == "○○○○○"
        assert st.speed_dots(99).plain == "●●●●●"


class TestCleanUrlError:
    def test_strips_errno_prefix(self) -> None:
        raw = "[Errno 8] nodename nor servname provided, or not known"
        assert st._clean_url_error(raw) == "nodename nor servname provided"

    def test_strips_negative_errno(self) -> None:
        assert st._clean_url_error("[Errno -2] Name or service not known") \
               == "Name or service not known"

    def test_returns_fallback_when_empty(self) -> None:
        assert st._clean_url_error("") == "network error"

    def test_truncates_to_max_len(self) -> None:
        long_msg = "x" * 200
        assert len(st._clean_url_error(long_msg)) == st.ERROR_MSG_MAX_LEN


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------
class TestRequestResult:
    def test_defaults_for_failure(self) -> None:
        r = st.RequestResult(ok=False, error="boom")
        assert r.n_bytes == 0
        assert r.elapsed_sec == 0.0
        assert r.status == 0
        assert r.speed_bps == 0.0

    def test_speed_bps_computes_from_bytes_over_time(self) -> None:
        r = st.RequestResult(ok=True, elapsed_sec=2.0, n_bytes=2048, status=200)
        assert r.speed_bps == 1024.0

    def test_speed_bps_zero_when_not_ok_even_with_bytes(self) -> None:
        r = st.RequestResult(ok=False, elapsed_sec=1.0, n_bytes=999)
        assert r.speed_bps == 0.0

    def test_frozen(self) -> None:
        r = st.RequestResult(ok=True, elapsed_sec=1.0, n_bytes=1, status=200)
        with pytest.raises(FrozenInstanceError):
            r.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# HTTP layer — real loopback server (no external network).
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """Ask the OS for a free port to avoid clashes."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve a small random blob from 127.0.0.1 on a free port for the test module."""
    serve_dir: Path = tmp_path_factory.mktemp("srv")
    (serve_dir / "blob.bin").write_bytes(b"x" * 1024 * 128)   # 128 KB

    handler_cls = type(
        "QuietHandler",
        (http.server.SimpleHTTPRequestHandler,),
        {
            "log_message": lambda *_args, **_kwargs: None,  # silence access log
        },
    )

    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    # Change into the served dir so SimpleHTTPRequestHandler finds blob.bin.
    import os
    old_cwd = os.getcwd()
    os.chdir(serve_dir)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/blob.bin"
    finally:
        server.shutdown()
        server.server_close()
        os.chdir(old_cwd)


class TestDownloadOnce:
    def test_returns_correct_size_and_status(self, http_server: str) -> None:
        elapsed, n_bytes, status = st.download_once(http_server, timeout=5.0)
        assert status == 200
        assert n_bytes == 1024 * 128
        assert elapsed > 0.0

    def test_404_raises_http_error(self, http_server: str) -> None:
        import urllib.error
        bad = http_server.rsplit("/", 1)[0] + "/nope.bin"
        with pytest.raises(urllib.error.HTTPError) as exc:
            st.download_once(bad, timeout=5.0)
        assert exc.value.code == 404


class TestPerformMeasurement:
    def test_success(self, http_server: str) -> None:
        r = st.perform_measurement(http_server)
        assert r.ok is True
        assert r.status == 200
        assert r.n_bytes == 1024 * 128
        assert r.speed_bps > 0.0

    def test_http_error_becomes_failure_result(self, http_server: str) -> None:
        bad = http_server.rsplit("/", 1)[0] + "/nope.bin"
        r = st.perform_measurement(bad)
        assert r.ok is False
        assert r.error == "HTTP 404"

    def test_connection_refused_becomes_failure_result(self) -> None:
        # Free port grabbed just to know nothing is listening on it now.
        port = _free_port()
        r = st.perform_measurement(f"http://127.0.0.1:{port}/whatever")
        assert r.ok is False
        assert r.error  # some non-empty error string


# ---------------------------------------------------------------------------
# CLI / URL resolution
# ---------------------------------------------------------------------------
class TestResolveUrl:
    @pytest.mark.parametrize("url", [
        "http://example.com/file.jpg",
        "https://example.com/file.jpg",
        "https://sub.domain.co/path?x=1",
    ])
    def test_valid_urls_pass_through(self, url: str) -> None:
        assert st.resolve_url(url) == url

    @pytest.mark.parametrize("url", [
        "ftp://example.com/file",     # unsupported scheme
        "not-a-url",                  # no scheme, no netloc
        "http://",                    # no netloc
    ])
    def test_invalid_urls_return_none(self, url: str) -> None:
        assert st.resolve_url(url) is None

    def test_prompts_when_no_url_and_returns_none_on_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(st.console, "input", lambda *_a, **_kw: "")
        assert st.resolve_url(None) is None

    def test_prompts_and_validates_the_input(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            st.console, "input", lambda *_a, **_kw: "https://ok.example/x",
        )
        assert st.resolve_url(None) == "https://ok.example/x"


class TestParseArgs:
    def test_defaults(self) -> None:
        ns = st.parse_args([])
        assert ns.url is None
        assert ns.count == st.DEFAULT_REQUEST_COUNT

    def test_positional_url_and_count(self) -> None:
        ns = st.parse_args(["https://x.example/f", "-n", "3"])
        assert ns.url == "https://x.example/f"
        assert ns.count == 3


# ---------------------------------------------------------------------------
# End-to-end orchestration (loopback only)
# ---------------------------------------------------------------------------
class TestRunMeasurements:
    def test_returns_expected_number_of_results(self, http_server: str) -> None:
        rows = st.run_measurements(http_server, count=3)
        assert len(rows) == 3
        assert all(r.ok for r in rows)
        assert all(r.n_bytes == 1024 * 128 for r in rows)


class TestMain:
    def test_exits_zero_on_success(self, http_server: str) -> None:
        rc = st.main([http_server, "-n", "2"])
        assert rc == 0

    def test_exits_one_on_invalid_url(self) -> None:
        rc = st.main(["not-a-url"])
        assert rc == 1
