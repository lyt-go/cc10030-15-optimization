"""路径还原多批次交接的失败恢复测试。

覆盖：跨日期多批次巡检、逐段确认、交付号对账、重试后新图发现、单图拼接约束、
补算差额与已交付值冻结、重跑后的图/分段/里程核对。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.services import route_restore as rr
from app.services.route_restore import RouteRestoreService


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    """每个用例独立数据目录，交接场景从初始状态重新装载。"""
    import app.services.base as base_mod

    tmpdir = Path(tempfile.mkdtemp(prefix="geo_handover_"))
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["route_restore", "checkpoints", "track_segments"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


def service() -> RouteRestoreService:
    return RouteRestoreService()


ALL_BATCHES = [
    rr.BATCH_DELIVERED_FULL,
    rr.BATCH_DELIVERED_PARTIAL,
    rr.BATCH_OLD_PARTIAL_CONFIRMED,
    rr.BATCH_RETRY_NEW_GRAPH,
]


class TestHandoverPrepare:
    def test_both_graphs_valid_on_historical_date(self):
        svc = service()
        ids = {g["graph_id"] for g in svc.list_graphs(valid_on="2026-09-01")}
        assert ids == {rr.GRAPH_OLD, rr.GRAPH_NEW}

    def test_prepare_reads_segments_and_splits_batches(self):
        svc = service()
        result = svc.prepare_handover(ALL_BATCHES)
        assert result["ok"] is True
        assert result["batch_count"] == 4
        # 跨日期
        assert result["dates"] == ["2026-08-20", "2026-08-25", "2026-09-01", "2026-09-08"]
        # 完成 / 异常 / 待处理分开
        assert result["completed_batches"] == [rr.BATCH_DELIVERED_FULL]
        assert result["abnormal_batches"] == [rr.BATCH_RETRY_NEW_GRAPH]
        assert set(result["pending_batches"]) == {
            rr.BATCH_DELIVERED_PARTIAL, rr.BATCH_OLD_PARTIAL_CONFIRMED,
        }
        for batch in result["batches"]:
            assert batch["candidate_route_count"] > 0
            assert batch["original_trajectory_points"] > 0
            for segment in batch["segments"]:
                assert segment["original_trajectory"]
                assert segment["candidate_routes"]

    def test_prepare_keeps_missing_batches_visible(self):
        svc = service()
        result = svc.prepare_handover([rr.BATCH_DELIVERED_FULL, "BATCH-UNKNOWN"])
        assert result["missing_batches"] == ["BATCH-UNKNOWN"]


class TestSegmentConfirmation:
    def test_partial_batch_confirms_only_first_segment(self):
        svc = service()
        batches = {b["batch_key"]: b for b in svc.list_handover_batches()}
        batch = batches[rr.BATCH_DELIVERED_PARTIAL]
        assert batch["delivered_segments"] == [1]
        assert batch["pending_segments"] == [2]
        assert batch["delivered_mileage_m"] == 700.0
        assert batch["delivery_no"] == "DLV-2026-08-25-001"

    def test_confirming_remaining_segment_freezes_its_own_delivery(self):
        svc = service()
        first = svc.verify_delivery("DLV-2026-08-25-001")
        result = svc.confirm_segment(rr.BATCH_DELIVERED_PARTIAL, 2)
        assert result["ok"] is True
        assert result["delivery_no"] != first["delivery_no"]
        # 首段交付值不被后一次确认改写
        assert svc.verify_delivery("DLV-2026-08-25-001")["actual_mileage_m"] == 700.0
        again = svc.confirm_segment(rr.BATCH_DELIVERED_PARTIAL, 2)
        assert again["already_confirmed"] is True
        assert again["delivery_no"] == result["delivery_no"]

    def test_confirm_failed_segment_is_rejected_without_closing_batch(self):
        svc = service()
        result = svc.confirm_segment(rr.BATCH_RETRY_NEW_GRAPH, 2)
        assert result["ok"] is False
        batches = {b["batch_key"]: b for b in svc.list_handover_batches()}
        assert batches[rr.BATCH_RETRY_NEW_GRAPH]["status"] == "还原异常"


class TestDeliveryReconciliation:
    def test_verify_delivery_by_number(self):
        svc = service()
        record = svc.verify_delivery("DLV-2026-08-20-001")
        assert record["ok"] is True
        assert record["batch_key"] == rr.BATCH_DELIVERED_FULL
        assert record["actual_mileage_m"] == 1200.0
        assert [s["segment_id"] for s in record["segments"]] == [1, 2]
        assert record["graph_id"] == rr.GRAPH_OLD

    def test_unknown_delivery_number(self):
        svc = service()
        assert svc.verify_delivery("DLV-NOT-EXIST")["ok"] is False


class TestResumeWithNewGraph:
    def test_resume_discovers_graph_registered_after_retry(self):
        svc = service()
        result = svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        assert result["ok"] is True
        adopted = result["adopted_graph"]["graph_id"]
        assert adopted == rr.GRAPH_NEW
        # 新图确实是在重试申请之后才登记的
        assert result["adopted_graph"]["registered_at"] > result["retry_request"]["requested_at"]
        assert [g["graph_id"] for g in result["discovered_graphs"]] == [rr.GRAPH_NEW]

    def test_resume_reports_900_against_1200_plan(self):
        svc = service()
        result = svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        assert result["original_plan_mileage_m"] == 1200.0
        assert result["final_mileage_m"] == 900.0
        assert result["mileage_delta_m"] == -300.0
        assert result["final_route"]["graph_id"] == rr.GRAPH_NEW
        assert result["final_route"]["segments"][0]["edge_ids"] == ["NEW-AD", "NEW-DB"]

    def test_resume_includes_another_confirmed_batch_original_values(self):
        svc = service()
        result = svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        other = result["other_confirmed_batch"]
        assert other["batch_key"] == rr.BATCH_DELIVERED_FULL
        assert other["original_mileage_m"] == 1200.0
        assert other["delivery_no"] == "DLV-2026-08-20-001"
        # 原批次按原图原值保留
        assert svc.verify_delivery("DLV-2026-08-20-001")["actual_mileage_m"] == 1200.0

    def test_resume_twice_does_not_rewrite_delivered_value(self):
        svc = service()
        first = svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        second = svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        assert second["already_delivered"] is True
        assert second["delivery_no"] == first["delivery_no"]
        assert svc.verify_delivery(first["delivery_no"])["actual_mileage_m"] == 900.0

    def test_resume_records_adopted_graph_on_batch(self):
        svc = service()
        svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        batches = {b["batch_key"]: b for b in svc.list_handover_batches()}
        usage = batches[rr.BATCH_OLD_PARTIAL_CONFIRMED]["graph_usages"]
        assert usage and usage[-1]["graph_id"] == rr.GRAPH_NEW
        assert usage[-1]["mileage_delta_m"] == -300.0


class TestRetryAndClosure:
    def test_retry_adds_temporary_closure_on_same_graph(self):
        svc = service()
        result = svc.request_retry(rr.BATCH_RETRY_NEW_GRAPH)
        assert result["ok"] is True
        assert result["added_closure"]["edge_id"] == "NEW-EB"
        assert result["added_closure"]["temporary"] is True
        graphs = {g["graph_id"]: g for g in svc.list_graphs()}
        assert "NEW-EB" in graphs[rr.GRAPH_NEW]["closed_edges"]

    def test_resume_leaves_closed_segment_failed(self):
        svc = service()
        result = svc.resume_batch(rr.BATCH_RETRY_NEW_GRAPH)
        assert result["ok"] is True
        statuses = {s["segment_id"]: s["status"] for s in result["final_route"]["segments"]}
        assert statuses[1] == "已交付"
        assert statuses[2] == "还原失败"
        assert result["final_mileage_m"] == 900.0

    def test_recalculate_after_release_reports_delta_and_segments(self):
        svc = service()
        svc.resume_batch(rr.BATCH_RETRY_NEW_GRAPH)
        delivered_before = svc.verify_delivery("DLV-2026-09-08-001")
        svc.release_closure(rr.GRAPH_NEW, "NEW-EB")
        result = svc.recalculate_batch(rr.BATCH_RETRY_NEW_GRAPH)
        assert result["ok"] is True
        assert result["delivered_mileage_m"] == delivered_before["actual_mileage_m"]
        assert result["added_mileage_m"] == 200.0
        delta = {d["segment_id"]: d for d in result["delta_segments"]}
        assert delta[2]["new_route"] is True
        assert delta[2]["mileage_delta_m"] == 200.0
        assert delta[2]["route"]["edge_ids"] == ["NEW-EB"]
        # 已交付分段原值保持
        assert svc.verify_delivery("DLV-2026-09-08-001")["actual_mileage_m"] == 900.0

    def test_recalculate_does_not_change_delivered_segments(self):
        svc = service()
        result = svc.recalculate_batch(rr.BATCH_DELIVERED_PARTIAL)
        assert result["delivered_segments"][0]["mileage_m"] == 700.0
        assert result["delivered_segments"][0]["frozen"] is True
        assert result["delivered_mileage_m"] == 700.0
        assert result["added_mileage_m"] == 500.0


class TestSingleGraphConstraint:
    def test_cannot_splice_new_edges_into_old_graph(self):
        svc = service()
        old = svc._state()["graphs"][rr.GRAPH_OLD]
        with pytest.raises(ValueError):
            svc._validate_single_graph(old, ["OLD-AC", "NEW-DB"])

    def test_each_segment_uses_edges_from_one_graph(self):
        svc = service()
        svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        state = svc._state()
        for batch in state["batches"].values():
            for segment in batch["segments"]:
                if not segment["route"]:
                    continue
                graph = state["graphs"][segment["graph_id"]]
                svc._validate_single_graph(graph, segment["route"]["edge_ids"])

    def test_delivery_never_mixes_graphs(self):
        svc = service()
        svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        record = svc.verify_delivery("DLV-2026-09-01-001")
        assert record["graph_id"] == rr.GRAPH_NEW
        assert record["mixed_graph_used"] is False


class TestRecheckAfterRerun:
    def test_check_reports_graphs_continuable_and_remaining(self):
        svc = service()
        svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        result = svc.check_batches(ALL_BATCHES)
        by_key = {b["batch_key"]: b for b in result["batches"]}
        # 续做后的批次实际采用新图
        assert by_key[rr.BATCH_OLD_PARTIAL_CONFIRMED]["actual_graph_ids"] == [rr.GRAPH_NEW]
        assert by_key[rr.BATCH_OLD_PARTIAL_CONFIRMED]["status"] == "已交付"
        assert by_key[rr.BATCH_OLD_PARTIAL_CONFIRMED]["mixed_graph_used"] is False
        # 只确认首段的批次仍有一段可继续交付
        partial = by_key[rr.BATCH_DELIVERED_PARTIAL]
        assert [s["segment_id"] for s in partial["continuable_segments"]] == [2]
        assert partial["delivered_mileage_m"] == 700.0
        assert partial["remaining_mileage_m"] == 500.0
        # 异常批次仍有可继续分段（同日期新图有可走路线）
        retry_batch = by_key[rr.BATCH_RETRY_NEW_GRAPH]
        assert retry_batch["status"] == "还原异常"
        assert retry_batch["actual_graph_ids"] == [rr.GRAPH_NEW]

    def test_check_after_full_rerun_has_no_remaining(self):
        svc = service()
        svc.release_closure(rr.GRAPH_NEW, "NEW-EB")
        for key in ALL_BATCHES:
            svc.start_or_resume_batch(key)
        svc.confirm_segment(rr.BATCH_DELIVERED_PARTIAL, 2)
        result = svc.check_batches(ALL_BATCHES)
        assert result["summary"]["remaining_mileage_m"] == 0.0
        assert set(result["completed_batches"]) == set(ALL_BATCHES)

    def test_start_or_resume_is_idempotent_for_delivered_batch(self):
        svc = service()
        first = svc.start_or_resume_batch(rr.BATCH_DELIVERED_FULL)
        assert first["already_delivered"] is True
        assert first["delivered_mileage_m"] == 1200.0
        second = svc.start_or_resume_batch(rr.BATCH_DELIVERED_FULL)
        assert second["delivery_no"] == first["delivery_no"]


class TestLegacyRestoreStillWorks:
    def test_restore_from_segments_unchanged(self, setup_data_dir):
        from app.services.track_segment import TrackSegmentService

        batch_key = "ROUTE-LEGACY"
        seg_file = Path(setup_data_dir) / "track_segments" / f"{batch_key}_segments.jsonl"
        import json
        seg_file.write_text(json.dumps({
            "segment_id": 1,
            "batch_key": batch_key,
            "start_lat": 39.905,
            "start_lon": 116.39,
            "end_lat": 39.91,
            "end_lon": 116.42,
            "point_count": 12,
            "mileage": 800.0,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        svc = service()
        result = svc.restore_from_segments(batch_key)
        assert result["restored_routes"] == 1
        assert result["segments_processed"] == 1
        routes, _ = svc.get_restored_routes(batch_key)
        assert routes[0]["road_id"] == "RN001"


class TestGraphUsageAudit:
    def test_seed_deliveries_record_actual_graph(self):
        svc = service()
        batches = {b["batch_key"]: b for b in svc.list_handover_batches()}
        full = batches[rr.BATCH_DELIVERED_FULL]
        assert full["graph_usages"]
        assert full["graph_usages"][0]["graph_id"] == rr.GRAPH_OLD
        assert full["graph_usages"][0]["mileage_m"] == 1200.0
        partial = batches[rr.BATCH_DELIVERED_PARTIAL]
        assert partial["graph_usages"][0]["segment_ids"] == [1]

    def test_recalculate_usage_is_appended_not_replaced(self):
        svc = service()
        svc.recalculate_batch(rr.BATCH_DELIVERED_PARTIAL)
        svc.confirm_segment(rr.BATCH_DELIVERED_PARTIAL, 2)
        batches = {b["batch_key"]: b for b in svc.list_handover_batches()}
        events = [u["event"] for u in batches[rr.BATCH_DELIVERED_PARTIAL]["graph_usages"]]
        assert events == ["首次交付", "补算", "逐段确认"]

    def test_every_usage_has_single_graph(self):
        svc = service()
        svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        for batch in svc.list_handover_batches():
            for usage in batch["graph_usages"]:
                assert usage["graph_id"] in svc._state()["graphs"]
                assert "+" not in usage["graph_id"]


class TestRecheckDetails:
    def test_check_lists_actual_graphs_and_usages(self):
        svc = service()
        svc.resume_batch(rr.BATCH_OLD_PARTIAL_CONFIRMED)
        result = svc.check_batches([rr.BATCH_OLD_PARTIAL_CONFIRMED])
        row = result["batches"][0]
        assert row["actual_graphs"][0]["graph_id"] == rr.GRAPH_NEW
        assert row["graph_usages"][-1]["event"] == "续做"
        assert row["original_plan"]["mileage_m"] == 1200.0
        assert row["remaining_mileage_m"] == 0.0

    def test_retry_batch_remains_abnormal_until_closed_edge_released(self):
        svc = service()
        svc.resume_batch(rr.BATCH_RETRY_NEW_GRAPH)
        after_resume = svc.check_batches([rr.BATCH_RETRY_NEW_GRAPH])
        assert after_resume["abnormal_batches"] == [rr.BATCH_RETRY_NEW_GRAPH]
        svc.release_closure(rr.GRAPH_NEW, "NEW-EB")
        svc.recalculate_batch(rr.BATCH_RETRY_NEW_GRAPH)
        svc.confirm_segment(rr.BATCH_RETRY_NEW_GRAPH, 2)
        after_recalc = svc.check_batches([rr.BATCH_RETRY_NEW_GRAPH])
        assert after_recalc["completed_batches"] == [rr.BATCH_RETRY_NEW_GRAPH]
        assert after_recalc["summary"]["remaining_mileage_m"] == 0.0


class TestTrackSegmentRead:
    def test_prepare_prefers_track_segment_dataset(self, setup_data_dir):
        import json

        for batch_key, mileage in [
            (rr.BATCH_DELIVERED_PARTIAL, 1234.5),
            (rr.BATCH_OLD_PARTIAL_CONFIRMED, 4321.0),
        ]:
            path = Path(setup_data_dir) / "track_segments" / f"{batch_key}_segments.jsonl"
            records = [
                {
                    "segment_id": 1,
                    "batch_key": batch_key,
                    "start_lat": 39.9001,
                    "start_lon": 116.4001,
                    "end_lat": 39.9101,
                    "end_lon": 116.4201,
                    "point_count": 42,
                    "mileage": mileage,
                }
            ]
            path.write_text(
                "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

        svc = service()
        result = svc.prepare_handover([rr.BATCH_DELIVERED_PARTIAL,
                                       rr.BATCH_OLD_PARTIAL_CONFIRMED])
        by_key = {item["batch_key"]: item for item in result["batches"]}
        partial = by_key[rr.BATCH_DELIVERED_PARTIAL]
        assert partial["track_segment_source"] == "track_segments"
        assert partial["track_segments_read"] == 1
        assert partial["track_segments_matched"] == 1
        first = partial["segments"][0]
        assert first["start_lat"] == 39.9001
        assert first["point_count"] == 42
        assert first["source_mileage_m"] == 1234.5
        assert partial["delivered_mileage_m"] == 700.0

    def test_prepare_falls_back_without_track_file(self):
        svc = service()
        result = svc.prepare_handover([rr.BATCH_RETRY_NEW_GRAPH])
        row = result["batches"][0]
        assert row["track_segment_source"] == "handover_registry"
        assert row["track_segments_read"] == 0
        assert row["original_trajectory_points"] > 0
