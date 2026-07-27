"""
table-transformer(Microsoft) 기반 보험약관 PDF 표 추출 실험 스크립트.

기존 파이프라인(extract_pdf_text.py 등)은 수정하지 않고, 별도의 비교 실험용으로 추가된 스크립트입니다.

동작 방식:
1. PyMuPDF로 대상 페이지를 이미지로 렌더링 (poppler 불필요)
2. microsoft/table-transformer-detection 으로 표 영역(bbox) 탐지
3. microsoft/table-transformer-structure-recognition 으로 표 내부의 행(row)/열(column) 탐지
4. 행 x 열 교차 영역을 셀 bbox로 계산 후, 이미지 좌표 -> PDF 좌표로 역변환
5. PyMuPDF의 실제 텍스트 레이어(page.get_text with clip)에서 셀 영역의 텍스트를 추출 (OCR 불필요, 원본 PDF가 디지털 텍스트이므로)

주의: transformers/torch는 프로젝트 .venv가 아니라 시스템 전역 python에 설치되어 있음
(requirements.txt/venv는 건드리지 않기 위해 별도 환경에서 실행).
"""

import argparse
import json
import time
from pathlib import Path

import fitz
import torch
from PIL import Image
from transformers import DetrImageProcessor, TableTransformerForObjectDetection

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"
OUTPUT_DIR = PROJECT_ROOT / "data" / "table_eval"

RENDER_DPI = 200
PDF_POINTS_PER_INCH = 72
SCALE = RENDER_DPI / PDF_POINTS_PER_INCH

DETECTION_MODEL_ID = "microsoft/table-transformer-detection"
STRUCTURE_MODEL_ID = "microsoft/table-transformer-structure-recognition"

DETECTION_THRESHOLD = 0.7
STRUCTURE_THRESHOLD = 0.5
CROP_PADDING_PX = 10


# 표 탐지 모델 + 구조 인식 모델을 감싼 래퍼. 무거운 모델 로딩을 한 번만 하기 위해 클래스로 구성
class TableTransformerPipeline:
    # 두 모델을 로드하고 eval() 모드로 고정 (추론 전용)
    def __init__(self) -> None:
        self.processor = DetrImageProcessor()
        self.detection_model = TableTransformerForObjectDetection.from_pretrained(DETECTION_MODEL_ID)
        self.structure_model = TableTransformerForObjectDetection.from_pretrained(STRUCTURE_MODEL_ID)
        self.detection_model.eval()
        self.structure_model.eval()

    # 이미지 1장을 모델에 넣어 threshold 이상인 객체만 [{label, score, box}]로 반환
    def _run_model(self, image: Image.Image, model, threshold: float) -> list[dict]:
        inputs = self.processor(images=image, return_tensors="pt")
        # 추론이므로 그래디언트 계산 생략(메모리/속도 절약)
        with torch.no_grad():
            outputs = model(**inputs)

        target_sizes = torch.tensor([image.size[::-1]])
        results = self.processor.post_process_object_detection(
            outputs, threshold=threshold, target_sizes=target_sizes
        )[0]

        id2label = model.config.id2label
        objects = []
        for score, label, box in zip(results["scores"], results["labels"], results["boxes"]):
            objects.append(
                {
                    "label": id2label[int(label)],
                    "score": float(score),
                    "box": [float(x) for x in box.tolist()],  # [xmin, ymin, xmax, ymax]
                }
            )
        return objects

    # 1단계: 페이지 이미지에서 표 위치(bbox)만 탐지
    def detect_tables(self, page_image: Image.Image) -> list[dict]:
        objects = self._run_model(page_image, self.detection_model, DETECTION_THRESHOLD)
        # "table rotated"도 포함: 세로로 긴 표에서 실제로는 회전되지 않았는데도
        # 이 라벨로 분류되는 경우가 있어(모델의 실제 한계), bbox 탐지 목적으로는 동일하게 취급
        return [o for o in objects if o["label"] in ("table", "table rotated")]

    # 2단계: 표 하나(크롭 이미지)에서 내부 행/열 경계 탐지
    def recognize_structure(self, table_image: Image.Image) -> list[dict]:
        return self._run_model(table_image, self.structure_model, STRUCTURE_THRESHOLD)


# PDF 페이지를 지정 DPI로 이미지 렌더링. doc/page도 함께 반환해 이후 텍스트 재조회에 사용
def render_page(pdf_path: Path, page_number: int, dpi: int = RENDER_DPI):
    doc = fitz.open(pdf_path)
    page = doc[page_number - 1]
    pix = page.get_pixmap(dpi=dpi)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return doc, page, image


# 같은 행/열에 대해 겹치는 여러 후보 박스를 하나로 병합 (구간 병합 알고리즘)
def merge_overlapping_intervals(objects: list[dict], axis: int) -> list[dict]:
    """
    같은 컬럼/행에 대해 겹치는 여러 후보 박스를 하나로 병합한다.
    table-transformer의 구조 인식 결과는 동일 컬럼/행에 대해 겹치는
    여러 박스를 내놓는 경우가 많아(DETR 다중 쿼리 특성), 겹치는 구간을
    합쳐 실제 컬럼/행 경계를 복원한다.

    axis=0 -> x축(xmin,xmax) 기준 병합 (컬럼용)
    axis=1 -> y축(ymin,ymax) 기준 병합 (행용)
    """
    lo_idx, hi_idx = (0, 2) if axis == 0 else (1, 3)
    other_lo_idx, other_hi_idx = (1, 3) if axis == 0 else (0, 2)

    # 시작 좌표 기준 정렬 후 순서대로 겹치면 흡수, 안 겹치면 새 그룹
    ordered = sorted(objects, key=lambda o: o["box"][lo_idx])
    merged: list[dict] = []
    for obj in ordered:
        box = obj["box"]
        if merged and box[lo_idx] <= merged[-1]["box"][hi_idx]:
            prev = merged[-1]
            prev["box"][hi_idx] = max(prev["box"][hi_idx], box[hi_idx])
            prev["box"][lo_idx] = min(prev["box"][lo_idx], box[lo_idx])
            prev["box"][other_lo_idx] = min(prev["box"][other_lo_idx], box[other_lo_idx])
            prev["box"][other_hi_idx] = max(prev["box"][other_hi_idx], box[other_hi_idx])
            prev["score"] = max(prev["score"], obj["score"])
        else:
            merged.append({"label": obj["label"], "score": obj["score"], "box": list(box)})
    return merged


# 인접 행/열 사이 빈틈을 서로의 경계 중간지점까지 확장해서 메운다 (셀 텍스트 누락 방지)
def fill_gaps(objects: list[dict], axis: int, table_extent: tuple[float, float]) -> list[dict]:
    """
    인접한 행/열 사이의 빈 간격을 서로의 경계 중간지점까지 확장해서 메운다.
    (공식 table-transformer 추론 파이프라인의 후처리 방식과 동일한 방식:
    탐지된 행/열 박스 사이에 빈틈이 있으면 셀 내용이 잘리므로, 각 행/열이
    표 전체를 빈틈없이 커버하도록 경계를 확장한다.)
    """
    lo_idx, hi_idx = (0, 2) if axis == 0 else (1, 3)
    ordered = sorted(objects, key=lambda o: o["box"][lo_idx])
    if not ordered:
        return ordered

    # 표의 처음/끝은 표 전체 바깥 경계까지 확장
    table_lo, table_hi = table_extent
    ordered[0]["box"][lo_idx] = table_lo
    ordered[-1]["box"][hi_idx] = table_hi

    # 인접한 것끼리는 중간지점을 경계로 삼음
    for i in range(len(ordered) - 1):
        cur, nxt = ordered[i], ordered[i + 1]
        midpoint = (cur["box"][hi_idx] + nxt["box"][lo_idx]) / 2
        cur["box"][hi_idx] = midpoint
        nxt["box"][lo_idx] = midpoint

    return ordered


# 행 목록 x 열 목록을 교차시켜 각 셀 bbox의 격자(grid)를 만든다
def build_grid_cells(
    row_objects: list[dict], col_objects: list[dict], table_box: list[float]
) -> list[list[list[float]]]:
    """행/열 bbox 리스트로부터 (row_idx, col_idx) 그리드 셀 bbox를 계산한다."""
    # 중복 병합 -> 빈틈 메움 순으로 전처리
    row_objects = merge_overlapping_intervals(row_objects, axis=1)
    col_objects = merge_overlapping_intervals(col_objects, axis=0)

    row_objects = fill_gaps(row_objects, axis=1, table_extent=(table_box[1], table_box[3]))
    col_objects = fill_gaps(col_objects, axis=0, table_extent=(table_box[0], table_box[2]))

    rows = sorted(row_objects, key=lambda o: o["box"][1])
    cols = sorted(col_objects, key=lambda o: o["box"][0])

    # 행의 y범위 x 열의 x범위 = 셀 bbox
    grid = []
    for row in rows:
        r_ymin, r_ymax = row["box"][1], row["box"][3]
        row_cells = []
        for col in cols:
            c_xmin, c_xmax = col["box"][0], col["box"][2]
            cell_box = [c_xmin, r_ymin, c_xmax, r_ymax]
            row_cells.append(cell_box)
        grid.append(row_cells)
    return grid


# 이미지 좌표 셀 bbox를 PDF 좌표로 역변환 후, PDF의 실제 텍스트 레이어에서 텍스트 추출 (OCR 아님)
def cell_text_from_pdf(page: fitz.Page, box_in_crop: list[float], crop_offset: tuple[float, float]) -> str:
    """크롭 이미지 좌표계의 셀 bbox를 원본 페이지 픽셀 좌표 -> PDF 좌표로 변환 후 텍스트 추출."""
    # 크롭 내부 좌표 -> 전체 페이지 이미지 좌표 -> (DPI/72로 나눠) PDF 포인트 좌표
    ox, oy = crop_offset
    xmin = (box_in_crop[0] + ox) / SCALE
    ymin = (box_in_crop[1] + oy) / SCALE
    xmax = (box_in_crop[2] + ox) / SCALE
    ymax = (box_in_crop[3] + oy) / SCALE
    rect = fitz.Rect(xmin, ymin, xmax, ymax)
    text = page.get_text("text", clip=rect)
    return " ".join(text.split())


# 표 후보 1개에 대해 구조 인식 -> 그리드 계산 -> 셀별 텍스트 추출까지 전부 처리
def extract_one_table_candidate(page: fitz.Page, page_image: Image.Image, pipeline, table_obj: dict) -> dict:
    # 표 bbox 주변에 여백을 두고 크롭
    xmin, ymin, xmax, ymax = table_obj["box"]
    xmin = max(0, xmin - CROP_PADDING_PX)
    ymin = max(0, ymin - CROP_PADDING_PX)
    xmax = min(page_image.width, xmax + CROP_PADDING_PX)
    ymax = min(page_image.height, ymax + CROP_PADDING_PX)
    crop = page_image.crop((xmin, ymin, xmax, ymax))

    structure_objects = pipeline.recognize_structure(crop)
    row_objects = [o for o in structure_objects if o["label"] == "table row"]
    col_objects = [o for o in structure_objects if o["label"] == "table column"]

    crop_table_box = [0, 0, crop.width, crop.height]
    grid = build_grid_cells(row_objects, col_objects, crop_table_box)

    # crop_offset(xmin, ymin)으로 크롭 좌표 -> 원본 페이지 좌표 복원
    rows_text = []
    for row_cells in grid:
        row_values = [cell_text_from_pdf(page, cell_box, (xmin, ymin)) for cell_box in row_cells]
        rows_text.append(row_values)

    return {
        "rows": rows_text,
        "table_score": table_obj["score"],
        "table_box": table_obj["box"],
        "detected_rows": len(grid),
        "detected_cols": len(grid[0]) if grid else 0,
    }


# 페이지 전체에서 표 탐지 -> 감지된 모든 표 후보 각각을 추출해서 candidates 리스트로 반환
def extract_table_from_page(pipeline: TableTransformerPipeline, pdf_path: Path, page_number: int) -> dict:
    """
    페이지에서 감지된 표 후보 전체를 추출해서 반환한다.
    한 페이지에 표가 여러 개 감지될 경우(예: 이전 표의 잔여 영역 + 실제 대상 표),
    어떤 후보가 정답과 가장 잘 맞는지는 평가 스크립트에서 ground truth와 비교해 선택한다.
    (표 선택 자체의 모호성과, 선택된 표 내부의 구조 인식 정확도를 분리해서 보기 위함)
    """
    start = time.time()

    doc, page, page_image = render_page(pdf_path, page_number)

    tables = pipeline.detect_tables(page_image)
    if not tables:
        doc.close()
        return {"candidates": [], "elapsed_sec": time.time() - start, "detected_tables": 0}

    # 표 후보마다 개별 추출 (어떤 후보가 정답인지는 평가 스크립트에서 결정)
    candidates = [extract_one_table_candidate(page, page_image, pipeline, t) for t in tables]

    doc.close()
    elapsed = time.time() - start
    return {
        "candidates": candidates,
        "elapsed_sec": elapsed,
        "detected_tables": len(tables),
    }


# CLI 진입점: ground_truth.json의 (pdf, page) 목록을 순회하며 표 추출, 결과를 JSON으로 저장
def main() -> None:
    parser = argparse.ArgumentParser(description="table-transformer로 지정된 페이지들의 표를 추출한다.")
    parser.add_argument(
        "--ground-truth",
        type=str,
        default=str(OUTPUT_DIR / "ground_truth.json"),
        help="비교 대상 페이지 목록이 담긴 ground_truth.json 경로",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(OUTPUT_DIR / "table_transformer_results.json"),
    )
    args = parser.parse_args()

    ground_truth = json.loads(Path(args.ground_truth).read_text(encoding="utf-8"))

    # 모델 로딩은 최초 1회만
    print("모델 로딩 중 (최초 1회, 캐시 다운로드 포함 시간 소요될 수 있음)...")
    pipeline = TableTransformerPipeline()
    print("모델 로딩 완료.")

    results = []
    for entry in ground_truth:
        pdf_path = RAW_PDF_DIR / entry["pdf"]
        page_number = entry["page"]
        print(f"처리 중: {entry['pdf']} p.{page_number}")

        result = extract_table_from_page(pipeline, pdf_path, page_number)
        result["pdf"] = entry["pdf"]
        result["page"] = page_number
        result["table_id"] = entry["table_id"]
        results.append(result)

        cand_summary = ", ".join(
            f"[score={c['table_score']:.2f} rows={c['detected_rows']} cols={c['detected_cols']}]"
            for c in result["candidates"]
        )
        print(
            f"  -> tables={result['detected_tables']}, candidates: {cand_summary}, "
            f"elapsed={result['elapsed_sec']:.2f}s"
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n저장 완료: {args.output}")


if __name__ == "__main__":
    main()
