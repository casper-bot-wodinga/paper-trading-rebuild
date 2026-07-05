"""Tests for knowledge sharing module — SPEC-v3 §7."""

import pytest
from datetime import datetime, timedelta

from src.knowledge import (
    TraderSkillProfile,
    ToolRequest,
    ToolUsage,
    Signal,
    SignalBoard,
    CrossTraderInsight,
    detect_herding,
    detect_divergence,
    check_correlation_risk,
    DEFAULT_TOOLKITS,
    ALL_TOOLS,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Skill Profile tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTraderSkillProfile:
    def test_default_toolkit(self):
        profile = TraderSkillProfile("kairos")
        assert "momentum_tools" in profile.tools
        assert "rsi" in profile.tools
        assert profile.has_tool("macd")
        assert not profile.has_tool("sentiment_tools")

    def test_empty_trader_gets_empty(self):
        profile = TraderSkillProfile("unknown_trader")
        assert len(profile.tools) == 0

    def test_custom_initial_tools(self):
        profile = TraderSkillProfile("test", initial_tools={"rsi", "macd"})
        assert profile.has_tool("rsi")
        assert profile.has_tool("macd")
        assert not profile.has_tool("bollinger_bands")

    def test_request_tool_valid(self):
        profile = TraderSkillProfile("kairos")
        req = profile.request_tool("bollinger_bands", "Need bands to detect oversold conditions in mean-reverting regime")
        assert req.status == "pending"
        assert req.tool == "bollinger_bands"

    def test_request_unknown_tool_fails(self):
        profile = TraderSkillProfile("kairos")
        with pytest.raises(ValueError, match="Unknown tool"):
            profile.request_tool("magic_crystal_ball", "I want to see the future")

    def test_request_already_have_tool_fails(self):
        profile = TraderSkillProfile("kairos")
        with pytest.raises(ValueError, match="Already have"):
            profile.request_tool("rsi", "need it")

    def test_request_too_short_reason_fails(self):
        profile = TraderSkillProfile("kairos")
        with pytest.raises(ValueError, match="too short"):
            profile.request_tool("bollinger_bands", "need")

    def test_grant_tool(self):
        profile = TraderSkillProfile("kairos")
        assert not profile.has_tool("bollinger_bands")
        profile.grant_tool("bollinger_bands")
        assert profile.has_tool("bollinger_bands")

    def test_record_usage(self):
        profile = TraderSkillProfile("kairos")
        profile.record_usage("rsi", was_profitable=True)
        profile.record_usage("rsi", was_profitable=True)
        profile.record_usage("rsi", was_profitable=False)
        report = profile.tool_report()
        rsi_usage = report["usage"]["rsi"]
        assert rsi_usage["times_used"] == 3
        assert rsi_usage["win_rate"] == pytest.approx(2 / 3, abs=0.01)

    def test_revoke_stale_tools(self):
        profile = TraderSkillProfile("kairos", unused_days_before_revoke=0)
        profile.grant_tool("bollinger_bands")
        profile.record_usage("bollinger_bands")
        # Set last_used to 10 days ago
        profile._usage["bollinger_bands"].last_used = datetime.now() - timedelta(days=10)
        revoked = profile.revoke_stale_tools()
        assert "bollinger_bands" in revoked
        assert not profile.has_tool("bollinger_bands")

    def test_default_tools_not_revoked(self):
        profile = TraderSkillProfile("kairos", unused_days_before_revoke=0)
        # Default tools should never be revoked
        revoked = profile.revoke_stale_tools()
        assert "rsi" not in revoked
        assert profile.has_tool("rsi")

    def test_tool_report(self):
        profile = TraderSkillProfile("kairos")
        report = profile.tool_report()
        assert report["trader"] == "kairos"
        assert report["tool_count"] >= 4
        assert "pending_requests" in report


class TestToolRequest:
    def test_approve(self):
        req = ToolRequest(trader_id="kairos", tool="bollinger_bands", reason="Need it")
        req.approve()
        assert req.status == "approved"
        assert req.reviewer == "casper"

    def test_deny(self):
        req = ToolRequest(trader_id="kairos", tool="vwap", reason="VWAP for execution")
        req.deny(note="Overlaps with volume_profile")
        assert req.status == "denied"
        assert req.reviewer == "casper"
        assert "volume_profile" in req.review_note


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Board tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignalBoard:
    def test_publish_and_read(self):
        board = SignalBoard()
        board.publish_observation("kairos", "AAPL", "Momentum breakout above 0.8", regime="TRENDING_UP", confidence=0.72)
        assert len(board) == 1

    def test_recent_filtered(self):
        board = SignalBoard()
        board.publish_observation("kairos", "AAPL", "signal 1")
        board.publish_observation("stonks", "TSLA", "signal 2")
        board.publish_observation("kairos", "GOOG", "signal 3")

        kairos_signals = board.recent(trader="kairos")
        assert len(kairos_signals) == 2
        assert all(s.trader == "kairos" for s in kairos_signals)

    def test_recent_by_ticker(self):
        board = SignalBoard()
        board.publish_observation("kairos", "AAPL", "buy signal")
        board.publish_observation("stonks", "TSLA", "sentiment spike")

        aapl = board.recent_for_ticker("AAPL")
        assert len(aapl) == 1
        assert aapl[0].ticker == "AAPL"

    def test_filter_by_type(self):
        board = SignalBoard()
        board.publish_observation("kairos", "AAPL", "obs")
        board.publish_lesson("stonks", "TSLA", "lesson learned")
        board.publish_alert("kairos", "market crash incoming!")

        assert len(board.alerts()) == 1
        assert len(board.lessons()) == 1

        obs = board.recent(signal_type="observation")
        assert len(obs) == 1
        assert obs[0].signal_type == "observation"

    def test_prune_by_age(self):
        board = SignalBoard(max_age_hours=0)  # prune immediately
        board.publish_observation("kairos", "AAPL", "old signal")
        assert len(board) == 0  # pruned immediately since age_hours=0

    def test_prune_by_count(self):
        board = SignalBoard(max_signals=3)
        for i in range(5):
            board.publish_observation("kairos", f"T{i}", f"signal {i}")
        assert len(board) == 3  # only 3 most recent kept

    def test_publish_lesson(self):
        board = SignalBoard()
        sig = board.publish_lesson("kairos", "AAPL", "Wait for RSI confirmation before entering")
        assert sig.signal_type == "lesson"
        assert sig.confidence == 1.0

    def test_publish_alert(self):
        board = SignalBoard()
        sig = board.publish_alert("stonks", "VIX spiking above 30")
        assert sig.signal_type == "alert"
        assert sig.ticker == "*"

    def test_summary(self):
        board = SignalBoard()
        board.publish_observation("kairos", "AAPL", "obs1")
        board.publish_observation("stonks", "AAPL", "obs2")
        board.publish_alert("kairos", "alert!")

        s = board.summary()
        assert s["total_signals"] == 3
        assert len(s["active_traders"]) == 2
        assert s["recent_alerts"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Trader Analysis tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectHerding:
    def test_no_herding_when_diverse(self):
        positions = {
            "kairos": {"AAPL": 1000, "GOOG": 500},
            "stonks": {"TSLA": 800, "META": 300},
        }
        results = detect_herding(positions)
        assert len(results) == 0

    def test_herding_detected(self):
        positions = {
            "kairos": {"AAPL": 1000},
            "stonks": {"AAPL": 800},
            "aldridge": {"AAPL": 500},
        }
        results = detect_herding(positions, threshold=2)
        assert len(results) >= 1
        assert results[0].ticker == "AAPL"
        assert results[0].severity == "warning"

    def test_herding_below_threshold(self):
        positions = {
            "kairos": {"AAPL": 1000},
            "stonks": {"AAPL": 800},
        }
        results = detect_herding(positions, threshold=3)
        assert len(results) == 0  # threshold 3, only 2 traders


class TestDetectDivergence:
    def test_all_negative_alphas(self):
        alphas = {"kairos": -0.02, "stonks": -0.01, "aldridge": -0.03}
        result = detect_divergence(alphas, threshold=3)
        assert result is not None
        assert result.type == "divergence"
        assert result.severity == "critical"

    def test_mixed_alphas_no_alert(self):
        alphas = {"kairos": 0.02, "stonks": -0.01, "aldridge": 0.01}
        result = detect_divergence(alphas, threshold=3)
        assert result is None

    def test_below_threshold_no_alert(self):
        alphas = {"kairos": -0.02, "stonks": -0.01}
        result = detect_divergence(alphas, threshold=3)
        assert result is None


class TestCheckCorrelationRisk:
    def test_no_overlap(self):
        positions = {
            "kairos": {"AAPL": 1000},
            "stonks": {"TSLA": 800},
        }
        results = check_correlation_risk(positions)
        assert len(results) == 0

    def test_high_overlap(self):
        positions = {
            "kairos": {"AAPL": 1000, "GOOG": 500, "MSFT": 300},
            "stonks": {"AAPL": 800, "GOOG": 400},
        }
        results = check_correlation_risk(positions, max_overlap_pct=0.50)
        # kairos has 3 tickers, stonks has 2, overlap = 2/3 = 67% > 50%
        assert len(results) >= 1
        assert "AAPL" in results[0].description
        assert "GOOG" in results[0].description

    def test_low_overlap_not_flagged(self):
        positions = {
            "kairos": {"AAPL": 1000, "GOOG": 500, "MSFT": 300, "AMZN": 200},
            "stonks": {"AAPL": 800},
        }
        results = check_correlation_risk(positions, max_overlap_pct=0.50)
        # 1/4 = 25% overlap, below 50%
        assert len(results) == 0

    def test_empty_positions_handled(self):
        positions = {"kairos": {}, "stonks": {"AAPL": 800}}
        results = check_correlation_risk(positions)
        assert len(results) == 0
