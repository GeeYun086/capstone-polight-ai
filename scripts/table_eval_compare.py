"""
PyMuPDF(기존 파이프라인) vs table-transformer 표 추출 정확도/속도 비교 스크립트.

- 기존 extract_pdf_text.py는 수정하지 않고 그대로 import해서 사용한다.
- 정답(ground_truth.json)에 있는 각 셀 텍스트가 각 방법의 출력에 얼마나
  살아남아 있는지를 '내용 재현율(content recall)'로 측정한다.
  (공백을 모두 제거한 뒤 최장 공통 부분열 비율로 계산 - 셀 경계가
  다르게 나오더라도 텍스트 내용 보존 여부를 공정하게 비교하기 위함)
- table-transformer는 페이지에 표 후보가 여러 개 감지된 경우, 정답과
  가장 잘 맞는 후보를 선택한다(표 선택 모호성과 구조 인식 정확도를 분리해서 보기 위함).
"""

import difflib
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from extract_pdf_text import extract_pdf_pages  # noqa: E402  (기존 스크립트 재사용, 수정하지 않음)

RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"
EVAL_DIR = PROJECT_ROOT / "data" / "table_eval"

RECALL_THRESHOLD = 0.7


# 비교 전 모든 공백 제거 (줄바꿈/띄어쓰기 차이로 다르다고 오판되는 것 방지)
def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


# gt_text가 candidate_text 안에 얼마나 포함돼 있는지 비율로 계산 (내용 재현율 지표)
def containment_ratio(gt_text: str, candidate_text: str) -> float:
    """
    gt_text가 candidate_text 안에 (공백 무시하고) 얼마나 온전히 들어있는지 비율.

    단일 최장 연속 매칭(find_longest_match)만 쓰면, 중간에 아주 짧은 삽입/누락 하나
    때문에 매칭이 두 조각으로 쪼개져 실제로는 내용이 거의 다 살아있는데도 낮은 점수가
    나오는 문제가 있어(예: 실제 100% 포함인데 70% 미만으로 나옴), 모든 매칭 블록의
    글자 수 합을 사용한다(전체 문자 단위 재현율).
    """
    gt = normalize(gt_text)
    cand = normalize(candidate_text)
    if not gt:
        return 1.0
    if not cand:
        return 0.0
    # 모든 매칭 블록의 글자 수를 합산 (단일 최장 매칭만 쓰면 점수가 부당하게 낮아짐)
    matcher = difflib.SequenceMatcher(None, gt, cand, autojunk=False)
    matched_chars = sum(block.size for block in matcher.get_matching_blocks())
    return matched_chars / len(gt)


# table-transformer 결과(2차원 rows)를 셀 순서 무시하고 하나의 문자열로 펼침
def flatten_tt_candidate(candidate: dict) -> str:
    cells = [cell for row in candidate["rows"] for cell in row]
    return "\n".join(cells)


# 정답 셀마다 재현율을 계산하고, 임계값(0.7) 이상 통과한 셀의 비율을 최종 recall로 산출
def score_against_blob(gt_cells: list[str], blob: str) -> tuple[float, list[float]]:
    ratios = [containment_ratio(cell, blob) for cell in gt_cells]
    recall = sum(1 for r in ratios if r >= RECALL_THRESHOLD) / len(ratios)
    return recall, ratios


# PDF별로 extract_pdf_pages(기존 파이프라인)를 1회 호출하고, 정답 페이지 텍스트를 채점
def evaluate_pymupdf(ground_truth: list[dict]) -> dict:
    """PDF별로 1회씩 extract_pdf_pages를 호출(기존 파이프라인 그대로)하고, 페이지별 텍스트를 채점."""
    results = {}
    # 같은 PDF는 한 번만 처리하도록 그룹핑
    by_pdf: dict[str, list[dict]] = {}
    for entry in ground_truth:
        by_pdf.setdefault(entry["pdf"], []).append(entry)

    for pdf_name, entries in by_pdf.items():
        pdf_path = RAW_PDF_DIR / pdf_name
        start = time.time()
        pages = extract_pdf_pages(pdf_path)
        elapsed_total = time.time() - start
        # PyMuPDF는 페이지 단위 호출 API가 없어 전체 처리 시간을 페이지 수로 나눠 추정
        per_page_elapsed = elapsed_total / len(pages) if pages else 0.0

        for entry in entries:
            page_text = pages[entry["page"] - 1]["text"]
            gt_cells = [cell for row in entry["rows"] for cell in row]
            recall, ratios = score_against_blob(gt_cells, page_text)
            results[entry["table_id"]] = {
                "recall": recall,
                "ratios": ratios,
                "elapsed_sec_per_page_est": per_page_elapsed,
                "elapsed_sec_full_doc": elapsed_total,
                "doc_pages": len(pages),
            }
    return results


# table_transformer_extract.py가 만든 결과를 ground truth와 대조해 채점
def evaluate_table_transformer(ground_truth: list[dict], tt_results: list[dict]) -> dict:
    tt_by_id = {r["table_id"]: r for r in tt_results}
    results = {}

    for entry in ground_truth:
        table_id = entry["table_id"]
        gt_cells = [cell for row in entry["rows"] for cell in row]
        tt_entry = tt_by_id.get(table_id)

        # 표를 아예 못 찾은 경우 -> recall 0
        if not tt_entry or not tt_entry.get("candidates"):
            results[table_id] = {
                "recall": 0.0,
                "ratios": [0.0] * len(gt_cells),
                "elapsed_sec": tt_entry["elapsed_sec"] if tt_entry else None,
                "chosen_candidate_idx": None,
                "num_candidates": 0,
            }
            continue

        # 후보가 여러 개면 각각 채점해서 recall이 가장 높은 후보를 선택
        best_idx, best_recall, best_ratios = None, -1.0, None
        for idx, cand in enumerate(tt_entry["candidates"]):
            blob = flatten_tt_candidate(cand)
            recall, ratios = score_against_blob(gt_cells, blob)
            if recall > best_recall:
                best_idx, best_recall, best_ratios = idx, recall, ratios

        chosen = tt_entry["candidates"][best_idx]
        results[table_id] = {
            "recall": best_recall,
            "ratios": best_ratios,
            "elapsed_sec": tt_entry["elapsed_sec"],
            "chosen_candidate_idx": best_idx,
            "num_candidates": len(tt_entry["candidates"]),
            "detected_rows": chosen["detected_rows"],
            "detected_cols": chosen["detected_cols"],
            "gt_rows": len(entry["rows"]),
            "gt_cols": len(entry["columns"]),
        }
    return results


# CLI 진입점: 두 방식을 각각 채점해 table_id별로 나란히 정리, comparison_report.json 저장 + 콘솔 출력
def main() -> None:
    ground_truth = json.loads((EVAL_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    tt_results = json.loads((EVAL_DIR / "table_transformer_results.json").read_text(encoding="utf-8"))

    print("PyMuPDF(기존 파이프라인) 실행 중...")
    pymupdf_scores = evaluate_pymupdf(ground_truth)

    print("table-transformer 채점 중...")
    tt_scores = evaluate_table_transformer(ground_truth, tt_results)

    # table_id 기준으로 두 방식 점수를 한 행씩 합침
    report_rows = []
    for entry in ground_truth:
        table_id = entry["table_id"]
        pm = pymupdf_scores[table_id]
        tt = tt_scores[table_id]
        report_rows.append(
            {
                "table_id": table_id,
                "pdf": entry["pdf"],
                "page": entry["page"],
                "gt_cells": len(pm["ratios"]),
                "pymupdf_recall": pm["recall"],
                "pymupdf_sec_per_page_est": pm["elapsed_sec_per_page_est"],
                "tt_recall": tt["recall"],
                "tt_sec": tt["elapsed_sec"],
                "tt_num_candidates": tt["num_candidates"],
                "tt_detected_rows": tt.get("detected_rows"),
                "tt_detected_cols": tt.get("detected_cols"),
                "gt_rows": tt.get("gt_rows"),
                "gt_cols": tt.get("gt_cols"),
            }
        )

    out_path = EVAL_DIR / "comparison_report.json"
    out_path.write_text(json.dumps(report_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 100)
    print(f"{'table_id':<18}{'PyMuPDF recall':>16}{'TT recall':>12}{'PyMuPDF sec/p':>15}{'TT sec':>10}{'TT cols(gt)':>14}")
    print("-" * 100)
    for row in report_rows:
        cols_str = f"{row['tt_detected_cols']}({row['gt_cols']})"
        print(
            f"{row['table_id']:<18}"
            f"{row['pymupdf_recall']*100:>15.1f}%"
            f"{row['tt_recall']*100:>11.1f}%"
            f"{row['pymupdf_sec_per_page_est']:>15.3f}"
            f"{row['tt_sec']:>10.2f}"
            f"{cols_str:>14}"
        )

    # 전체 평균 계산 후 출력
    avg_pm_recall = sum(r["pymupdf_recall"] for r in report_rows) / len(report_rows)
    avg_tt_recall = sum(r["tt_recall"] for r in report_rows) / len(report_rows)
    avg_pm_time = sum(r["pymupdf_sec_per_page_est"] for r in report_rows) / len(report_rows)
    avg_tt_time = sum(r["tt_sec"] for r in report_rows) / len(report_rows)

    print("-" * 100)
    print(f"{'AVERAGE':<18}{avg_pm_recall*100:>15.1f}%{avg_tt_recall*100:>11.1f}%{avg_pm_time:>15.3f}{avg_tt_time:>10.2f}")
    print("=" * 100)
    print(f"\n저장 완료: {out_path}")


if __name__ == "__main__":
    main()
